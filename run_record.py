"""Versioned local S4 run-record manifest, bounded coding fixture, and offline validator."""
import datetime
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import agent_night_shift as ns
from context_capture import (
    ContextCapture,
    SECRET_PATTERNS,
    SENSITIVE_KEYS,
    capture_call,
    digest,
    redact_card,
    redact_data,
    redact_text,
    summarize_trace,
)

SCHEMA_VERSION = "s4-run-record.v1"
EVIDENCE_CLASS_SCRIPTED = "scripted_provider_fixture"
EVIDENCE_CLASS_REAL_MODEL = "real_model"
EVIDENCE_CLASS_UNAVAILABLE = "unavailable"


def scan_for_secrets(text: str) -> List[str]:
    """Return list of detected secret pattern matches."""
    if not isinstance(text, str):
        return []
    findings = []
    for pat in SECRET_PATTERNS:
        matches = pat.findall(text)
        for m in matches:
            val = m if isinstance(m, str) else m[1] if isinstance(m, tuple) and len(m) > 1 else str(m)
            if "[REDACTED_SECRET]" not in val:
                findings.append(val[:12] + "...")
    # Check for raw lease capabilities
    for cap_match in re.finditer(r'(?i)"(lease|token|auth_token)"\s*:\s*"([^"]+)"', text):
        val = cap_match.group(2)
        if "[REDACTED" not in val and val not in ("none", "unknown", "synthetic-unused-token"):
            findings.append(f"{cap_match.group(1)}={val[:8]}...")
    return findings


def evaluate_criteria(
    criteria: List[Any],
    workspace_path: Optional[Path],
    patch_text: str,
    gate_results: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Evaluate independent acceptance criteria against the workspace, diff, and gates."""
    gate_results = gate_results or []
    gate_status_map = {g.get("gate_id"): g.get("status") for g in gate_results}
    evaluated_criteria = []
    overall_passed = True

    for i, c in enumerate(criteria):
        c_id = f"criterion_{i+1}"
        desc = str(c)
        c_type = "custom"
        passed = False
        detail = ""

        if isinstance(c, dict):
            c_id = c.get("id", c_id)
            desc = c.get("description", desc)
            c_type = c.get("type", "custom")

            if c_type == "file_contains":
                target = c.get("target")
                pattern = c.get("pattern", "")
                if workspace_path and (workspace_path / target).exists():
                    content = (workspace_path / target).read_text()
                    if pattern in content:
                        passed = True
                        detail = f"Pattern '{pattern}' found in {target}"
                    else:
                        passed = False
                        detail = f"Pattern '{pattern}' NOT found in {target}"
                else:
                    passed = False
                    detail = f"File {target} does not exist in workspace"

            elif c_type == "diff_contains":
                pattern = c.get("pattern", "")
                if pattern in patch_text:
                    passed = True
                    detail = f"Pattern '{pattern}' found in patch diff"
                else:
                    passed = False
                    detail = f"Pattern '{pattern}' NOT found in patch diff"

            elif c_type == "diff_not_contains":
                pattern = c.get("pattern", "")
                if pattern not in patch_text:
                    passed = True
                    detail = f"Forbidden pattern '{pattern}' absent from patch diff"
                else:
                    passed = False
                    detail = f"Forbidden pattern '{pattern}' WAS found in patch diff"

            elif c_type == "gate_status":
                gate_id = c.get("gate_id")
                expected = c.get("expected_status", "passed")
                actual = gate_status_map.get(gate_id)
                if actual == expected:
                    passed = True
                    detail = f"Gate '{gate_id}' status is '{actual}'"
                else:
                    passed = False
                    detail = f"Gate '{gate_id}' status '{actual}' != expected '{expected}'"

            elif c_type == "python_eval":
                code = c.get("code", "")
                if workspace_path:
                    cmd = [sys.executable, "-c", code]
                    res = subprocess.run(cmd, cwd=workspace_path, capture_output=True, text=True, timeout=10)
                    if res.returncode == 0:
                        passed = True
                        detail = f"Python assertion succeeded"
                    else:
                        passed = False
                        detail = f"Python assertion failed: {res.stderr.strip() or res.stdout.strip()}"
                else:
                    passed = False
                    detail = "No workspace available for python_eval"

            elif c_type == "callable":
                fn = c.get("fn")
                try:
                    passed, detail = fn(workspace_path, patch_text, gate_results)
                except Exception as e:
                    passed = False
                    detail = f"Evaluator exception: {e}"

            else:
                passed = False
                detail = f"Unknown criterion type '{c_type}'"

        elif isinstance(c, str):
            if c.startswith("file_contains:"):
                _, target, pattern = c.split(":", 2)
                if workspace_path and (workspace_path / target).exists() and pattern in (workspace_path / target).read_text():
                    passed = True
                    detail = f"'{pattern}' in {target}"
                else:
                    passed = False
                    detail = f"'{pattern}' not in {target}"
            elif c.startswith("diff_contains:"):
                pattern = c.split(":", 1)[1]
                if pattern in patch_text:
                    passed = True
                    detail = f"'{pattern}' in diff"
                else:
                    passed = False
                    detail = f"'{pattern}' not in diff"
            elif c.startswith("gate:"):
                gate_id = c.split(":", 1)[1]
                if gate_status_map.get(gate_id) == "passed":
                    passed = True
                    detail = f"Gate {gate_id} passed"
                else:
                    passed = False
                    detail = f"Gate {gate_id} did not pass"
            else:
                # Substring in diff or workspace
                if c in patch_text:
                    passed = True
                    detail = f"String '{c}' matched in diff"
                else:
                    passed = False
                    detail = f"String '{c}' not matched in diff"

        if not passed:
            overall_passed = False

        evaluated_criteria.append({
            "id": c_id,
            "description": desc,
            "type": c_type,
            "passed": passed,
            "detail": detail,
        })

    return {
        "evaluated": True,
        "passed": overall_passed if criteria else True,
        "criteria": evaluated_criteria,
    }


class RunRecord:
    """Manages creation and finalization of an evaluable run record directory."""

    def __init__(
        self,
        output_dir: Path,
        card: Dict[str, Any],
        evidence_class: str = EVIDENCE_CLASS_SCRIPTED,
        mode: str = "payload",
        max_bytes: int = 1_048_576,
        max_events: int = 4096,
        workspace_dir: Optional[Path] = None,
    ):
        self.record_id = str(uuid.uuid4())
        self.output_dir = Path(output_dir).resolve()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.card = card
        self.evidence_class = evidence_class
        self.mode = mode
        self.created_at_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.workspace_dir = Path(workspace_dir).resolve() if workspace_dir else None

        # Capture git starting state if workspace is available
        self.starting_commit, self.starting_dirty, self.untracked, self.branch = self._inspect_git(self.workspace_dir)

        # Initialize ContextCapture in payload mode targeting output_dir
        self.capture = ContextCapture(
            directory=self.output_dir,
            card=card,
            max_bytes=max_bytes,
            max_events=max_events,
            mode=mode,
            evidence_class=evidence_class,
            run_record_dir=self.output_dir,
        )

    def _inspect_git(self, repo_dir: Optional[Path]) -> Tuple[Any, Any, List[str], Any]:
        if not repo_dir or not (repo_dir / ".git").exists():
            return (
                {"status": "unavailable", "reason": "not_a_git_repository"},
                {"status": "unavailable", "reason": "not_a_git_repository"},
                [],
                {"status": "unavailable", "reason": "not_a_git_repository"},
            )
        try:
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=repo_dir, capture_output=True, text=True, timeout=5
            ).stdout.strip()
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"], cwd=repo_dir, capture_output=True, text=True, timeout=5
            )
            dirty = bool(status_proc.stdout.strip())
            untracked = [
                line[3:].strip() for line in status_proc.stdout.splitlines() if line.startswith("??")
            ]
            branch_proc = subprocess.run(
                ["git", "branch", "--show-current"], cwd=repo_dir, capture_output=True, text=True, timeout=5
            )
            branch = branch_proc.stdout.strip() or "HEAD"
            return head, dirty, untracked, branch
        except Exception:
            return (
                {"status": "unavailable", "reason": "git_inspection_failed"},
                {"status": "unavailable", "reason": "git_inspection_failed"},
                [],
                {"status": "unavailable", "reason": "git_inspection_failed"},
            )

    def finalize(
        self,
        outcome: str,
        release_status: str = "unknown",
        workspace_path: Optional[Path] = None,
        gate_results: Optional[List[Dict[str, Any]]] = None,
        patch_text: Optional[str] = None,
        providers: Optional[List[Any]] = None,
        no_commit_explanation: Optional[str] = None,
        pre_attempt_decision: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Finalize events, generate unified diff, evaluate acceptance, and write manifest."""
        ws = workspace_path or self.workspace_dir
        gate_results = gate_results or []

        # Generate patch if not explicitly provided
        patch = patch_text
        patch_stat = "none"
        if patch is None and ws and (ws / ".git").exists():
            try:
                # Stage untracked with intent-to-add so diff covers new files
                subprocess.run(["git", "add", "-N", "."], cwd=ws, capture_output=True, timeout=5)
                diff_proc = subprocess.run(
                    ["git", "diff", "HEAD"], cwd=ws, capture_output=True, text=True, timeout=5
                )
                patch = diff_proc.stdout
                stat_proc = subprocess.run(
                    ["git", "diff", "--stat", "HEAD"], cwd=ws, capture_output=True, text=True, timeout=5
                )
                patch_stat = stat_proc.stdout.strip() or "0 files changed"
            except Exception as e:
                patch = f"[DIFF_UNAVAILABLE: {e}]"
        elif patch is None:
            patch = ""

        # Write patch artifact
        patch_file = self.output_dir / "patch.diff"
        patch_file.write_text(patch)

        # Evaluate independent acceptance criteria
        acceptance_criteria = self.card.get("acceptance_criteria") or []
        acceptance_eval = evaluate_criteria(acceptance_criteria, ws, patch, gate_results)

        # Finish ContextCapture stream
        self.capture.finish(outcome, release_status, patch=patch, acceptance=acceptance_eval, no_commit_explanation=no_commit_explanation)

        # Parse captured events for execution summary
        events = []
        if self.capture.path.exists():
            events = [json.loads(line) for line in self.capture.path.read_text().splitlines()]

        provider_attempts = []
        failover_attribution = []
        tool_calls = []
        verification_events = []
        last_provider_name = None

        for e in events:
            kind = e.get("kind")
            data = e.get("data", {})
            if kind == "provider_call_finished":
                decision_id = data.get("decision_id")
                # find corresponding context_assembled
                assembled = next(
                    (ev["data"] for ev in events if ev.get("kind") == "context_assembled" and ev.get("data", {}).get("decision_id") == decision_id),
                    {}
                )
                p_name = assembled.get("provider_name") or assembled.get("provider_adapter", "unknown")
                attempt = {
                    "decision_id": decision_id,
                    "provider": p_name,
                    "status": data.get("status"),
                    "duration_ms": data.get("duration_ms"),
                }
                provider_attempts.append(attempt)

                if last_provider_name and last_provider_name != p_name and provider_attempts:
                    prev_status = provider_attempts[-2]["status"] if len(provider_attempts) >= 2 else "unknown"
                    failover_attribution.append({
                        "from_provider": last_provider_name,
                        "to_provider": p_name,
                        "reason": prev_status,
                        "decision_id": decision_id,
                    })
                last_provider_name = p_name

            elif kind == "tool_call_finished":
                tool_calls.append({
                    "tool": data.get("tool"),
                    "args": data.get("args"),
                    "duration_ms": data.get("duration_ms"),
                    "status": "error" if data.get("error") else "ok",
                    "output_chars": len(data.get("output", "") or ""),
                })

            elif kind == "verification_finished":
                verification_events.append(data)

        # Derive completeness
        terminal_event = events[-1] if events and events[-1].get("kind") == "trace_finalized" else None
        complete = bool(
            terminal_event
            and not terminal_event.get("data", {}).get("dropped_events")
            and not terminal_event.get("data", {}).get("capture_io_failed")
        )

        harness_hash = digest({
            name: Path(__file__).with_name(name).read_text()
            for name in ("context_capture.py", "agent_night_shift.py", "run_record.py")
            if Path(__file__).with_name(name).exists()
        })

        provider_configs = []
        if providers:
            for p in providers:
                provider_configs.append({
                    "name": getattr(p, "name", type(p).__name__),
                    "model": getattr(p, "model", None),
                    "kind": getattr(p, "kind", "scripted"),
                })
        else:
            provider_configs.append({
                "name": "scripted_provider",
                "model": "none",
                "kind": "fixture",
            })

        all_gates_passed = all(g.get("status") == "passed" for g in gate_results) if gate_results else False

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "record_id": self.record_id,
            "trace_id": self.capture.trace_id,
            "created_at_iso": self.created_at_iso,
            "evidence_class": self.evidence_class,
            "real_model_evidence": {
                "status": "unavailable" if self.evidence_class != EVIDENCE_CLASS_REAL_MODEL else "available",
                "reason": (
                    "no_real_model_run_for_card_16"
                    if "16" in str(self.card.get("id", "")) or "CARD-16" in str(self.output_dir) or "unchanged" in str(self.output_dir)
                    else "no_real_model_run_for_card_50"
                ) if self.evidence_class != EVIDENCE_CLASS_REAL_MODEL else None,
            },
            "mode": self.mode,
            "task": redact_card(self.card),
            "environment": {
                "starting_commit": self.starting_commit,
                "starting_dirty": self.starting_dirty,
                "untracked_files": self.untracked,
                "branch": self.branch,
                "harness_source_hash": harness_hash,
                "python_version": sys.version.split()[0],
                "platform": sys.platform,
            },
            "harness_configuration": {
                "max_iterations": getattr(ns, "MAX_ITERATIONS", 10),
                "max_context_chars": getattr(ns, "MAX_CONTEXT_CHARS", 1000000),
                "providers": provider_configs,
                "quota_reader": {
                    "status": "unavailable",
                    "reason": "deferred_offline",
                },
            },
            "execution_summary": {
                "iterations": len([e for e in events if e.get("kind") == "context_assembled"]),
                "provider_attempts": provider_attempts,
                "failover_attribution": failover_attribution,
                "tool_calls": tool_calls,
                "verification": {
                    "attempted": bool(gate_results),
                    "passed": all_gates_passed,
                    "gates": gate_results,
                },
                "independent_acceptance": acceptance_eval,
                "pre_attempt_decision": pre_attempt_decision,
                "no_commit_explanation": no_commit_explanation,
                "terminal_outcome": outcome,
                "release_status": release_status,
                "complete": complete,
            },
            "artifacts": {
                "events": "events.jsonl",
                "patch": "patch.diff",
                "final_patch_stat": patch_stat,
            },
        }

        manifest_file = self.output_dir / "manifest.json"
        manifest_file.write_text(json.dumps(manifest, indent=2))
        return manifest


def validate_run_record(path: Path) -> Dict[str, Any]:
    """Offline validation reconstructing requested change, actions, diff, and acceptance from the sample."""
    path = Path(path).resolve()
    if path.is_file() and path.name == "manifest.json":
        record_dir = path.parent
        manifest_file = path
    elif path.is_dir():
        record_dir = path
        manifest_file = record_dir / "manifest.json"
    else:
        raise ValueError(f"Target path does not exist or is not a manifest/directory: {path}")

    errors = []
    if not manifest_file.exists():
        return {
            "valid": False,
            "complete": False,
            "errors": [f"manifest.json missing in {record_dir}"],
        }

    try:
        manifest_text = manifest_file.read_text()
        manifest = json.loads(manifest_text)
    except Exception as e:
        return {"valid": False, "complete": False, "errors": [f"Malformed manifest.json: {e}"]}

    if manifest.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"Unsupported schema_version: {manifest.get('schema_version')} (expected {SCHEMA_VERSION})")

    events_file = record_dir / manifest.get("artifacts", {}).get("events", "events.jsonl")
    patch_file = record_dir / manifest.get("artifacts", {}).get("patch", "patch.diff")

    if not events_file.exists():
        errors.append(f"Events artifact {events_file.name} missing")
    if not patch_file.exists():
        errors.append(f"Patch artifact {patch_file.name} missing")

    events = []
    events_text = ""
    if events_file.exists():
        events_text = events_file.read_text()
        try:
            events = [json.loads(line) for line in events_text.splitlines() if line.strip()]
        except Exception as e:
            errors.append(f"Malformed events.jsonl: {e}")

    # Validate event sequence integrity
    trace_id = manifest.get("trace_id")
    if events:
        if any(e.get("trace_id") != trace_id for e in events):
            errors.append("trace_id mismatch across events")
        for idx, e in enumerate(events):
            if e.get("sequence") != idx:
                errors.append(f"Event sequence broken at index {idx} (found sequence {e.get('sequence')})")
                break
        if events[0].get("kind") != "attempt_opened":
            errors.append(f"Trace must begin with attempt_opened, found {events[0].get('kind')}")
        if events[-1].get("kind") != "trace_finalized":
            errors.append(f"Trace missing terminal trace_finalized record (found {events[-1].get('kind')})")

    # Completeness check
    terminal = events[-1].get("data", {}) if events and events[-1].get("kind") == "trace_finalized" else None
    complete = bool(terminal and not terminal.get("dropped_events") and not terminal.get("capture_io_failed"))
    if not complete:
        if not terminal:
            errors.append("Trace incomplete: missing_finalization")
        elif terminal.get("dropped_events"):
            errors.append(f"Trace incomplete: {terminal.get('dropped_events')} dropped_events reported")
        elif terminal.get("capture_io_failed"):
            errors.append("Trace incomplete: capture_io_failed")

    # Scan for secret leakage
    patch_text = patch_file.read_text() if patch_file.exists() else ""
    manifest_secrets = scan_for_secrets(manifest_text)
    events_secrets = scan_for_secrets(events_text)
    patch_secrets = scan_for_secrets(patch_text)

    if manifest_secrets or events_secrets or patch_secrets:
        all_secrets = manifest_secrets + events_secrets + patch_secrets
        errors.append(f"Unredacted secrets or credentials detected: {', '.join(all_secrets)}")

    # Reconstruct actions taken
    reconstructed_actions = []
    for e in events:
        if e.get("kind") == "tool_call_finished":
            data = e.get("data", {})
            reconstructed_actions.append({
                "sequence": e.get("sequence"),
                "tool": data.get("tool"),
                "args": data.get("args"),
                "status": "error" if data.get("error") else "ok",
                "duration_ms": data.get("duration_ms"),
                "output_chars": len(data.get("output") or "") if data.get("output") else 0,
            })

    # Reconstruct verification and acceptance
    task_card = manifest.get("task", {})
    exec_summary = manifest.get("execution_summary", {})
    verification = exec_summary.get("verification", {})
    independent_acceptance = exec_summary.get("independent_acceptance", {})
    terminal_outcome = exec_summary.get("terminal_outcome")
    pre_attempt_decision = exec_summary.get("pre_attempt_decision")
    no_commit_explanation = exec_summary.get("no_commit_explanation")
    patch_clean = not bool(patch_text.strip())

    # Offline corroboration of independent acceptance against patch/events
    acceptance_passed = independent_acceptance.get("passed", False)
    if not acceptance_passed and terminal_outcome == "succeeded":
        errors.append("Outcome declared 'succeeded' but independent acceptance criteria FAILED")

    # Card #16: No-change success rules:
    # 1. All required gates must have passed (gate errors never imply success)
    # 2. Goal-specific evidence is required (passing existing gates alone is insufficient)
    # 3. Explicit no-commit explanation required
    if terminal_outcome == "succeeded" and (patch_clean or pre_attempt_decision == "justified_no_change_completion"):
        gate_entries = verification.get("gates", [])
        all_gates_passed = bool(verification.get("passed")) and all(g.get("status") == "passed" for g in gate_entries)
        if not all_gates_passed or not gate_entries:
            errors.append("Gate errors never imply success: no-change success declared with failing or unrun gates")
        if not acceptance_passed or not independent_acceptance.get("criteria"):
            errors.append("Passing existing gates alone is insufficient: no-change success requires goal-specific evidence")
        if not no_commit_explanation or not str(no_commit_explanation).strip():
            errors.append("No-change success requires an explicit no-commit explanation in the record")

    # Card #16: In Case B, if green gates + requested functionality absent, must NOT short-circuit to success
    if pre_attempt_decision == "continue_working" and terminal_outcome == "succeeded" and patch_clean:
        errors.append("Invalid short-circuit success: baseline gates passed but goal-specific acceptance criteria failed")

    valid = len(errors) == 0

    return {
        "valid": valid,
        "complete": complete,
        "record_id": manifest.get("record_id"),
        "trace_id": trace_id,
        "evidence_class": manifest.get("evidence_class"),
        "real_model_evidence": manifest.get("real_model_evidence"),
        "reconstructed": {
            "requested_change": {
                "card_id": task_card.get("id"),
                "title": task_card.get("title"),
                "goal": task_card.get("goal"),
                "acceptance_criteria": task_card.get("acceptance_criteria"),
            },
            "starting_state": manifest.get("environment", {}),
            "actions": reconstructed_actions,
            "diff": patch_text,
            "verification": verification,
            "independent_acceptance": independent_acceptance,
            "failover_attribution": exec_summary.get("failover_attribution", []),
            "pre_attempt_decision": pre_attempt_decision,
            "no_commit_explanation": no_commit_explanation,
            "terminal_outcome": terminal_outcome,
            "release_status": exec_summary.get("release_status"),
        },
        "errors": errors,
    }


def review_run_record(path: Path) -> str:
    """Produce a human-readable review report of the run record from the sample alone."""
    result = validate_run_record(path)
    recon = result.get("reconstructed", {})
    task = recon.get("requested_change", {})
    actions = recon.get("actions", [])
    diff = recon.get("diff", "")
    verification = recon.get("verification", {})
    acceptance = recon.get("independent_acceptance", {})
    failovers = recon.get("failover_attribution", [])

    lines = [
        "=" * 64,
        f"S4 E2E RUN RECORD REVIEW: {result.get('record_id', 'unknown')}",
        "=" * 64,
        f"Evidence Class : {result.get('evidence_class', 'unknown')}",
        f"Real Model     : {result.get('real_model_evidence', {}).get('status', 'unavailable')}",
        f"Trace ID       : {result.get('trace_id', 'unknown')}",
        f"Completeness   : {'COMPLETE' if result.get('complete') else 'INCOMPLETE'}",
        f"Validation     : {'PASS' if result.get('valid') else 'FAIL'}",
        f"Terminal Outcome: {recon.get('terminal_outcome', 'unknown')} (Release: {recon.get('release_status', 'unknown')})",
    ]
    if recon.get("pre_attempt_decision"):
        lines.append(f"Pre-Attempt Decision: {recon.get('pre_attempt_decision')}")
    if recon.get("no_commit_explanation"):
        lines.append(f"No-Commit Explanation: {recon.get('no_commit_explanation')}")

    lines.extend([
        "-" * 64,
        "TASK DEFINITION",
        f"  Card ID : {task.get('card_id')}",
        f"  Title   : {task.get('title')}",
        f"  Goal    : {task.get('goal')}",
        "  Acceptance Criteria:",
    ])
    for c in task.get("acceptance_criteria") or []:
        if isinstance(c, dict):
            lines.append(f"    - [{c.get('id', 'criterion')}]: {c.get('description', '')}")
        else:
            lines.append(f"    - {c}")

    lines.extend([
        "-" * 64,
        f"RECONSTRUCTED ACTIONS ({len(actions)} tool calls)",
    ])
    if not actions:
        lines.append("  (No tool calls: coding attempt avoided via justified no-change completion)")
    else:
        for a in actions:
            lines.append(
                f"  Step {a['sequence']}: [{a['tool']}] args={a.get('args')} -> {a.get('status')} ({a.get('duration_ms', 0)}ms, {a.get('output_chars', 0)} chars)"
            )

    if failovers:
        lines.extend(["-" * 64, "PROVIDER FAILOVERS"])
        for f in failovers:
            lines.append(f"  Failover: {f['from_provider']} -> {f['to_provider']} (reason: {f['reason']})")

    lines.extend([
        "-" * 64,
        "VERIFICATION RESULTS",
        f"  Attempted : {verification.get('attempted', False)}",
        f"  Passed    : {verification.get('passed', False)}",
    ])
    for g in verification.get("gates", []):
        lines.append(f"    - Gate '{g.get('gate_id')}': {g.get('status')} ({g.get('duration_ms', 0)}ms)")

    lines.extend([
        "-" * 64,
        "INDEPENDENT ACCEPTANCE CRITERIA EVALUATION",
        f"  Overall Passed: {acceptance.get('passed', False)}",
    ])
    for c in acceptance.get("criteria", []):
        status_str = "PASS" if c.get("passed") else "FAIL"
        lines.append(f"    [{status_str}] {c.get('description')}: {c.get('detail')}")

    lines.extend([
        "-" * 64,
        "FINAL PATCH DIFF",
    ])
    if diff.strip():
        lines.append(diff.strip())
    else:
        lines.append("  (No changes / clean diff)")
        if recon.get("no_commit_explanation"):
            lines.append(f"  Explanation: {recon.get('no_commit_explanation')}")

    if result.get("errors"):
        lines.extend([
            "-" * 64,
            "VALIDATION ERRORS",
        ])
        for err in result["errors"]:
            lines.append(f"  ❌ {err}")

    lines.append("=" * 64)
    return "\n".join(lines)


def run_bounded_fixture(
    output_dir: Path,
    provider: Optional[Any] = None,
    break_independent_acceptance: bool = False,
    task_should_fail: bool = False,
    providers: Optional[List[Any]] = None,
    card_overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute a bounded coding fixture through the actual worker loop and record the run."""
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    original_cwd = Path.cwd()

    with tempfile.TemporaryDirectory() as temp_dir:
        repo_dir = Path(temp_dir) / "sample-greeter"
        repo_dir.mkdir(parents=True, exist_ok=True)

        # 1. Populate synthetic repository files
        greeter_py = repo_dir / "greeter.py"
        greeter_py.write_text('def greet(name: str) -> str:\n    # TODO: implement\n    return ""\n')

        test_greeter_py = repo_dir / "test_greeter.py"
        test_greeter_py.write_text(
            'from greeter import greet\n\n'
            'def test_greet():\n'
            '    assert greet("NightShift") == "Hello, NightShift!"\n'
        )

        verification_json = repo_dir / "verification.json"
        verification_json.write_text(json.dumps({
            "version": 1,
            "gates": [
                {
                    "id": "unit_tests",
                    "placement": ["local"],
                    "commands": [
                        f"{sys.executable} -m pytest -q test_greeter.py"
                    ]
                }
            ]
        }, indent=2))

        # Add .gitignore so diff only reflects code changes
        gitignore = repo_dir / ".gitignore"
        gitignore.write_text(".agent_logs/\n__pycache__/\n*.pyc\n.pytest_cache/\n")

        # Copy run-gate.py into repo_dir/scripts
        scripts_dir = repo_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        source_run_gate = Path(__file__).resolve().parent / "scripts" / "run-gate.py"
        if source_run_gate.exists():
            shutil.copy(source_run_gate, scripts_dir / "run-gate.py")

        # 2. Initialize git repository
        subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)

        # 3. Formulate task card
        card = {
            "id": "CARD-S4-E2E-SAMPLE",
            "title": "Implement greeting function",
            "goal": "Implement greet(name: str) in greeter.py returning 'Hello, {name}!' and pass unit_tests",
            "kind": "software",
            "repo": "sample-greeter",
            "gate_ids": ["unit_tests"],
            "acceptance_criteria": [
                {
                    "id": "defines_greet",
                    "description": "greeter.py defines function greet(name: str)",
                    "type": "file_contains",
                    "target": "greeter.py",
                    "pattern": "def greet(",
                },
                {
                    "id": "correct_greeting",
                    "description": "greet('NightShift') returns 'Hello, NightShift!'",
                    "type": "python_eval",
                    "code": "from greeter import greet; assert greet('NightShift') == 'Hello, NightShift!'",
                },
                {
                    "id": "gate_passed",
                    "description": "verification gate unit_tests passed",
                    "type": "gate_status",
                    "gate_id": "unit_tests",
                    "expected_status": "passed",
                },
            ],
        }
        if card_overrides:
            card.update(card_overrides)

        # 4. Set up scripted provider if none supplied
        if providers:
            llm_providers = providers
        elif provider:
            llm_providers = [provider]
        else:
            class DefaultScriptedProvider(ns.LLMProvider):
                name = "S4 Scripted Greeter Provider"
                calls = 0

                def ask(self, messages):
                    self.calls += 1
                    if task_should_fail:
                        return None
                    if self.calls == 1:
                        return '<agent_action>{"action": "read_file", "args": {"path": "greeter.py"}}</agent_action>'
                    elif self.calls == 2:
                        if break_independent_acceptance:
                            # Modify a comment only: passes baseline tests if we make them pass or doesn't satisfy criterion
                            content = 'def greet(name: str) -> str:\n    # returning something else\n    return "Wrong Greeting"\n'
                        else:
                            content = 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
                        return f'<agent_action>{{"action": "write_file", "args": {{"path": "greeter.py", "content": {json.dumps(content)}}}}}</agent_action>'
                    elif self.calls == 3:
                        return '<agent_action>{"action": "verify_build", "args": {}}</agent_action>'
                    return None

            llm_providers = [DefaultScriptedProvider()]

        # 5. Initialize RunRecord coordinator
        record = RunRecord(
            output_dir=output_dir,
            card=card,
            evidence_class=EVIDENCE_CLASS_SCRIPTED,
            mode="payload",
            workspace_dir=repo_dir,
        )

        # 6. Execute actual agent tool loop
        try:
            os.chdir(repo_dir)
            agent = ns.NightShiftAgent(str(repo_dir), token="synthetic-unused-token")
            agent.llm.providers = llm_providers
            agent.llm.context_capture = record.capture
            agent.toolbox.target_gates = list(card.get("gate_ids") or [])

            # Run actual process_task loop
            intro = f"CARD [{card['id']}]: {card['title']}\nGOAL: {card['goal']}"
            success = agent.process_task(intro, "", agent.toolbox.list_files())

            for handler in agent._current_file_handlers:
                ns.logger.removeHandler(handler)
                ns.prompt_logger.removeHandler(handler)
                handler.close()

            outcome = "succeeded" if success else "failed"
            release_status = "acknowledged" if success else "unknown"

            # Finalize the run record
            manifest = record.finalize(
                outcome=outcome,
                release_status=release_status,
                workspace_path=repo_dir,
                gate_results=agent.toolbox.last_gate_results,
                providers=llm_providers,
            )
            return manifest
        finally:
            os.chdir(original_cwd)


def run_unchanged_card_fixture(
    output_dir: Path,
    case: str = "case_a",
) -> Dict[str, Any]:
    """Execute scripted provider fixture for Card #16 unchanged-card evaluation.

    case_a: Goal already satisfied + passing gates -> justified no-change completion
    case_b: Passing gates + requested functionality absent -> continue working (must not short-circuit)
    """
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    original_cwd = Path.cwd()

    with tempfile.TemporaryDirectory() as temp_dir:
        repo_dir = Path(temp_dir) / "sample-greeter"
        repo_dir.mkdir(parents=True, exist_ok=True)

        # 1. Populate synthetic repository files according to case
        greeter_py = repo_dir / "greeter.py"
        test_greeter_py = repo_dir / "test_greeter.py"

        if case == "case_a":
            # Goal is already satisfied before any changes
            greeter_py.write_text(
                'def greet(name: str) -> str:\n'
                '    return f"Hello, {name}!"\n'
            )
            test_greeter_py.write_text(
                'from greeter import greet\n\n'
                'def test_greet():\n'
                '    assert greet("NightShift") == "Hello, NightShift!"\n'
            )
        elif case == "case_b":
            # Baseline existing function passes baseline tests, but card goal is NOT satisfied
            greeter_py.write_text(
                'def greet(name: str) -> str:\n'
                '    # Baseline legacy greeting; does not satisfy new card goal\n'
                '    return "legacy_greeting"\n\n'
                'def existing_helper() -> str:\n'
                '    return "ok"\n'
            )
            test_greeter_py.write_text(
                'from greeter import existing_helper\n\n'
                'def test_existing_helper():\n'
                '    # Baseline test passes, so gates are green\n'
                '    assert existing_helper() == "ok"\n'
            )
        else:
            raise ValueError(f"Unknown case: {case}")

        verification_json = repo_dir / "verification.json"
        verification_json.write_text(json.dumps({
            "version": 1,
            "gates": [
                {
                    "id": "unit_tests",
                    "placement": ["local"],
                    "commands": [
                        f"{sys.executable} -m pytest -q test_greeter.py"
                    ]
                }
            ]
        }, indent=2))

        # Add .gitignore
        gitignore = repo_dir / ".gitignore"
        gitignore.write_text(".agent_logs/\n__pycache__/\n*.pyc\n.pytest_cache/\n")

        # Copy run-gate.py into repo_dir/scripts
        scripts_dir = repo_dir / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        source_run_gate = Path(__file__).resolve().parent / "scripts" / "run-gate.py"
        if source_run_gate.exists():
            shutil.copy(source_run_gate, scripts_dir / "run-gate.py")

        # 2. Initialize git repository
        subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)

        # 3. Formulate card with goal and acceptance criteria
        card = {
            "id": f"CARD-16-{case.upper().replace('_', '-')}",
            "title": "Implement greeting function",
            "goal": "Implement greet(name: str) in greeter.py returning 'Hello, {name}!' and pass unit_tests",
            "kind": "software",
            "repo": "sample-greeter",
            "gate_ids": ["unit_tests"],
            "acceptance_criteria": [
                {
                    "id": "defines_greet",
                    "description": "greeter.py defines function greet(name: str)",
                    "type": "file_contains",
                    "target": "greeter.py",
                    "pattern": "def greet(",
                },
                {
                    "id": "correct_greeting",
                    "description": "greet('NightShift') returns 'Hello, NightShift!'",
                    "type": "python_eval",
                    "code": "from greeter import greet; assert greet('NightShift') == 'Hello, NightShift!'",
                },
                {
                    "id": "gate_passed",
                    "description": "verification gate unit_tests passed",
                    "type": "gate_status",
                    "gate_id": "unit_tests",
                    "expected_status": "passed",
                },
            ],
        }

        # 4. Initialize RunRecord
        record = RunRecord(
            output_dir=output_dir,
            card=card,
            evidence_class=EVIDENCE_CLASS_SCRIPTED,
            mode="payload",
            workspace_dir=repo_dir,
        )

        # 5. Execute worker check and loop
        try:
            os.chdir(repo_dir)
            agent = ns.NightShiftAgent(str(repo_dir), token="synthetic-unused-token")
            agent.llm.context_capture = record.capture
            agent.toolbox.target_gates = list(card.get("gate_ids") or [])

            # Pre-attempt satisfaction check (Card #16)
            is_satisfied, gate_results, acceptance, explanation = agent.check_preexisting_satisfaction(card, repo_dir)
            all_passed = all(g.get("status") == "passed" for g in gate_results) if gate_results else False
            record.capture.verification_finished(gate_results, all_passed)

            if is_satisfied:
                # Case A: Goal already satisfied + passing gates -> justified no-change completion
                outcome = "succeeded"
                release_status = "acknowledged"
                manifest = record.finalize(
                    outcome=outcome,
                    release_status=release_status,
                    workspace_path=repo_dir,
                    gate_results=gate_results,
                    patch_text="",
                    no_commit_explanation=explanation,
                    pre_attempt_decision="justified_no_change_completion",
                )
                return manifest
            else:
                # Case B: Passing gates + requested functionality absent -> continue working
                # Must NOT short-circuit to success!
                class ContinueWorkingProvider(ns.LLMProvider):
                    name = "S4 Scripted Continue-Working Provider"
                    calls = 0

                    def ask(self, messages):
                        self.calls += 1
                        if self.calls == 1:
                            return '<agent_action>{"action": "read_file", "args": {"path": "greeter.py"}}</agent_action>'
                        return None

                agent.llm.providers = [ContinueWorkingProvider()]
                intro = f"CARD [{card['id']}]: {card['title']}\nGOAL: {card['goal']}"
                agent.process_task(intro, "", agent.toolbox.list_files())

                for handler in agent._current_file_handlers:
                    ns.logger.removeHandler(handler)
                    ns.prompt_logger.removeHandler(handler)
                    handler.close()

                outcome = "continued"
                release_status = "working"
                manifest = record.finalize(
                    outcome=outcome,
                    release_status=release_status,
                    workspace_path=repo_dir,
                    gate_results=gate_results,
                    patch_text="",
                    providers=agent.llm.providers,
                    no_commit_explanation=None,
                    pre_attempt_decision="continue_working",
                )
                return manifest
        finally:
            os.chdir(original_cwd)


def generate_unchanged_card_samples(
    output_base_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Generate both Case A and Case B unchanged-card fixture samples."""
    base = Path(output_base_dir).resolve() if output_base_dir else Path(__file__).resolve().parent / "eval" / "s4" / "unchanged-card-samples"
    base.mkdir(parents=True, exist_ok=True)

    case_a_dir = base / "case-a-goal-satisfied-no-change"
    case_b_dir = base / "case-b-green-gates-missing-goal-continue"

    manifest_a = run_unchanged_card_fixture(case_a_dir, case="case_a")
    manifest_b = run_unchanged_card_fixture(case_b_dir, case="case_b")

    return manifest_a, manifest_b
