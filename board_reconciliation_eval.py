"""S4 Board Reconciliation Benchmark Harness and Evaluator.

Translates the five real system failure archetypes observed during the board audit into
repeatable, frozen benchmark probes with held-out generalization variants and scorecard metrics:
1. Branch Isolation (implementation on topic branch not merged into operational main checkout)
2. Stale Instruction Override (human instructions supersede card acceptance criteria)
3. Artifact vs. Attempt Outcome (deliverable artifact exists despite attempt failure/block)
4. Historical Revert Distinction (historical completion does not guarantee active availability)
5. Context Usability Funnel Stage Attribution (5-stage context loss diagnosis)
"""
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from context_capture import digest


VALID_ARCHETYPES = {
    "branch_not_integrated",
    "stale_instruction_override",
    "artifact_exists_after_failed_attempt",
    "historical_revert_distinction",
    "context_funnel_stage_attribution",
}

VALID_ACTIONS = {
    "reconcile_card",
    "reject_completion",
    "diagnose_funnel",
    "defer_to_human",
}

VALID_SPLITS = {"baseline", "held_out"}

VALID_FUNNEL_STAGES = {"stored", "accessible", "returned", "supplied", "acted_upon"}


def write_json(path: Path | str, value: Any) -> None:
    path_obj = Path(path)
    path_obj.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path_obj.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(value, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path_obj)


def load_fixtures(fixtures_path: Path | str) -> Dict[str, Any]:
    """Loads and strictly validates the board reconciliation benchmark fixtures."""
    raw = Path(fixtures_path).read_text(encoding="utf-8")
    data = json.loads(raw)

    if data.get("schema_version") != "board-reconciliation-benchmark.v1":
        raise ValueError(f"Unsupported schema_version: {data.get('schema_version')}")
    if not data.get("benchmark_id") or not data.get("system_prompt"):
        raise ValueError("Benchmark missing required benchmark_id or system_prompt")

    cases = data.get("cases", [])
    if not cases:
        raise ValueError("Fixtures contain no cases")

    case_ids = set()
    incident_splits: Dict[str, str] = {}

    for case in cases:
        cid = case.get("case_id")
        if not cid or cid in case_ids:
            raise ValueError(f"Duplicate or empty case_id: {cid}")
        case_ids.add(cid)

        split = case.get("split")
        if split not in VALID_SPLITS:
            raise ValueError(f"Invalid split '{split}' in case {cid}")

        inc = case.get("incident_id")
        if not inc:
            raise ValueError(f"Missing incident_id in case {cid}")
        if inc in incident_splits and incident_splits[inc] != split:
            raise ValueError(
                f"Incident '{inc}' crosses splits: was {incident_splits[inc]}, now {split} in {cid}"
            )
        incident_splits[inc] = split

        archetype = case.get("archetype")
        if archetype not in VALID_ARCHETYPES:
            raise ValueError(f"Invalid archetype '{archetype}' in case {cid}")

        expected = case.get("expected")
        if not expected or not isinstance(expected, dict):
            raise ValueError(f"Missing or invalid expected block in case {cid}")
        if expected.get("action") not in VALID_ACTIONS:
            raise ValueError(f"Invalid expected action in case {cid}: {expected.get('action')}")
        if not isinstance(expected.get("arguments"), dict):
            raise ValueError(f"Missing or invalid expected arguments in case {cid}")

    return data


def build_case_input(fixtures: Dict[str, Any], case: Dict[str, Any]) -> List[Dict[str, str]]:
    """Builds the standardized message input for an evaluator or provider probe."""
    scenario_payload = {
        "case_id": case["case_id"],
        "scenario": case["scenario"],
    }
    return [
        {"role": "system", "content": fixtures["system_prompt"]},
        {"role": "user", "content": json.dumps(scenario_payload, indent=2, sort_keys=True)},
    ]


def judge_reconciliation(
    raw_output: Any,
    expected: Dict[str, Any],
    case_archetype: Optional[str] = None
) -> Dict[str, Any]:
    """Judges candidate output against expected reconciliation outcome.

    Returns a dictionary:
      status: 'passed' | 'failed' | 'invalid_output'
      failure_category: None | 'malformed_output' | 'false_completion' | 'unnecessary_rework' |
                        'instruction_ignored' | 'funnel_misattribution' | 'wrong_action' | 'argument_mismatch'
      detail: human-readable explanation
      parsed: parsed dictionary or None
    """
    if isinstance(raw_output, str):
        try:
            parsed = json.loads(raw_output)
        except Exception as exc:
            return {
                "status": "invalid_output",
                "failure_category": "malformed_output",
                "detail": f"JSON parse error: {exc}",
                "parsed": None,
            }
    elif isinstance(raw_output, dict):
        parsed = raw_output
    else:
        return {
            "status": "invalid_output",
            "failure_category": "malformed_output",
            "detail": f"Output is not string or dict: {type(raw_output)}",
            "parsed": None,
        }

    if not isinstance(parsed, dict) or "action" not in parsed or "arguments" not in parsed:
        return {
            "status": "invalid_output",
            "failure_category": "malformed_output",
            "detail": "Output JSON must contain exactly 'action' and 'arguments' keys",
            "parsed": parsed,
        }

    action = parsed.get("action")
    args = parsed.get("arguments")
    if not isinstance(args, dict):
        return {
            "status": "invalid_output",
            "failure_category": "malformed_output",
            "detail": "arguments must be a JSON object",
            "parsed": parsed,
        }

    exp_action = expected.get("action")
    exp_args = expected.get("arguments", {})

    # Action mismatch diagnostics
    if action != exp_action:
        # Check specific failure archetypes
        if (
            case_archetype in ("branch_not_integrated", "historical_revert_distinction")
            and action == "reconcile_card"
            and args.get("target_state") == "done"
        ):
            return {
                "status": "failed",
                "failure_category": "false_completion",
                "detail": "False completion: card marked done without operational integration/availability in HEAD",
                "parsed": parsed,
            }

        if (
            case_archetype == "artifact_exists_after_failed_attempt"
            and action in ("reject_completion", "defer_to_human")
        ):
            return {
                "status": "failed",
                "failure_category": "unnecessary_rework",
                "detail": "Unnecessary rework: ignored existing verified artifact deliverable",
                "parsed": parsed,
            }

        if (
            case_archetype == "stale_instruction_override"
            and action != "reconcile_card"
        ):
            return {
                "status": "failed",
                "failure_category": "instruction_ignored",
                "detail": "Instruction override ignored: failed to reconcile card under operator constraint",
                "parsed": parsed,
            }

        return {
            "status": "failed",
            "failure_category": "wrong_action",
            "detail": f"Expected action '{exp_action}' but received '{action}'",
            "parsed": parsed,
        }

    # Argument validation based on action type
    if action == "reconcile_card":
        if args.get("card_id") != exp_args.get("card_id"):
            return {
                "status": "failed",
                "failure_category": "argument_mismatch",
                "detail": f"Expected card_id '{exp_args.get('card_id')}' but got '{args.get('card_id')}'",
                "parsed": parsed,
            }
        if args.get("target_state") != exp_args.get("target_state"):
            return {
                "status": "failed",
                "failure_category": "argument_mismatch",
                "detail": f"Expected target_state '{exp_args.get('target_state')}' but got '{args.get('target_state')}'",
                "parsed": parsed,
            }

        # Check evidence keys if expected
        exp_evidence = exp_args.get("evidence", {})
        actual_evidence = args.get("evidence", {})
        if not isinstance(actual_evidence, dict):
            return {
                "status": "failed",
                "failure_category": "argument_mismatch",
                "detail": "evidence must be a JSON object",
                "parsed": parsed,
            }

        for key, exp_val in exp_evidence.items():
            if key not in actual_evidence:
                return {
                    "status": "failed",
                    "failure_category": "argument_mismatch",
                    "detail": f"Missing expected evidence key '{key}'",
                    "parsed": parsed,
                }
            if actual_evidence[key] != exp_val:
                return {
                    "status": "failed",
                    "failure_category": "argument_mismatch",
                    "detail": f"Evidence '{key}' mismatch: expected '{exp_val}', got '{actual_evidence[key]}'",
                    "parsed": parsed,
                }

    elif action == "reject_completion":
        if args.get("card_id") != exp_args.get("card_id"):
            return {
                "status": "failed",
                "failure_category": "argument_mismatch",
                "detail": f"Expected card_id '{exp_args.get('card_id')}' but got '{args.get('card_id')}'",
                "parsed": parsed,
            }
        if args.get("recommended_action") != exp_args.get("recommended_action"):
            return {
                "status": "failed",
                "failure_category": "argument_mismatch",
                "detail": f"Expected recommended_action '{exp_args.get('recommended_action')}' but got '{args.get('recommended_action')}'",
                "parsed": parsed,
            }

    elif action == "diagnose_funnel":
        if args.get("card_id") != exp_args.get("card_id"):
            return {
                "status": "failed",
                "failure_category": "argument_mismatch",
                "detail": f"Expected card_id '{exp_args.get('card_id')}' but got '{args.get('card_id')}'",
                "parsed": parsed,
            }
        if args.get("stage_of_loss") != exp_args.get("stage_of_loss"):
            return {
                "status": "failed",
                "failure_category": "funnel_misattribution",
                "detail": f"Expected stage_of_loss '{exp_args.get('stage_of_loss')}' but got '{args.get('stage_of_loss')}'",
                "parsed": parsed,
            }

    return {
        "status": "passed",
        "failure_category": None,
        "detail": "Decision matched expected action and critical parameters",
        "parsed": parsed,
    }


def compute_scorecard(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Computes comprehensive benchmark metrics and audit scorecard from observation rows."""
    total = len(rows)
    if total == 0:
        return {
            "total_observations": 0,
            "passed": 0,
            "failed": 0,
            "invalid_output": 0,
            "pass_rate": 0.0,
        }

    passed_count = sum(r["status"] == "passed" for r in rows)
    failed_count = sum(r["status"] == "failed" for r in rows)
    invalid_count = sum(r["status"] == "invalid_output" for r in rows)

    # Split breakdown
    baseline_rows = [r for r in rows if r["split"] == "baseline"]
    heldout_rows = [r for r in rows if r["split"] == "held_out"]

    baseline_pass_rate = (
        sum(r["status"] == "passed" for r in baseline_rows) / len(baseline_rows)
        if baseline_rows else 0.0
    )
    heldout_pass_rate = (
        sum(r["status"] == "passed" for r in heldout_rows) / len(heldout_rows)
        if heldout_rows else 0.0
    )

    # Per archetype breakdown
    archetype_stats: Dict[str, Dict[str, Any]] = {}
    for arc in VALID_ARCHETYPES:
        arc_rows = [r for r in rows if r["archetype"] == arc]
        if arc_rows:
            arc_passed = sum(r["status"] == "passed" for r in arc_rows)
            archetype_stats[arc] = {
                "total": len(arc_rows),
                "passed": arc_passed,
                "pass_rate": arc_passed / len(arc_rows),
            }

    # Core audit failure rate metrics:
    # 1. False completion rate: proportion of branch/revert cases incorrectly marked done
    branch_and_revert_rows = [
        r for r in rows
        if r["archetype"] in ("branch_not_integrated", "historical_revert_distinction")
    ]
    false_completion_count = sum(
        r.get("failure_category") == "false_completion" for r in branch_and_revert_rows
    )
    false_completion_rate = (
        false_completion_count / len(branch_and_revert_rows)
        if branch_and_revert_rows else 0.0
    )

    # 2. Unnecessary rework rate: proportion of artifact-exists cases failing to reconcile
    artifact_rows = [
        r for r in rows if r["archetype"] == "artifact_exists_after_failed_attempt"
    ]
    unnecessary_rework_count = sum(
        r.get("failure_category") == "unnecessary_rework" for r in artifact_rows
    )
    unnecessary_rework_rate = (
        unnecessary_rework_count / len(artifact_rows) if artifact_rows else 0.0
    )

    # 3. Instruction override fidelity: proportion of override cases correctly reconciled
    override_rows = [
        r for r in rows if r["archetype"] == "stale_instruction_override"
    ]
    instruction_override_fidelity = (
        sum(r["status"] == "passed" for r in override_rows) / len(override_rows)
        if override_rows else 0.0
    )

    # 4. Capability identification accuracy: identifying true operational availability in HEAD
    capability_identification_accuracy = (
        sum(r["status"] == "passed" for r in branch_and_revert_rows) / len(branch_and_revert_rows)
        if branch_and_revert_rows else 0.0
    )

    # 5. Funnel diagnosis accuracy: identifying exact stage of loss in 5-stage funnel
    funnel_rows = [
        r for r in rows if r["archetype"] == "context_funnel_stage_attribution"
    ]
    funnel_diagnosis_accuracy = (
        sum(r["status"] == "passed" for r in funnel_rows) / len(funnel_rows)
        if funnel_rows else 0.0
    )

    return {
        "total_observations": total,
        "passed": passed_count,
        "failed": failed_count,
        "invalid_output": invalid_count,
        "pass_rate": passed_count / total,
        "splits": {
            "baseline": {
                "total": len(baseline_rows),
                "passed": sum(r["status"] == "passed" for r in baseline_rows),
                "pass_rate": baseline_pass_rate,
            },
            "held_out": {
                "total": len(heldout_rows),
                "passed": sum(r["status"] == "passed" for r in heldout_rows),
                "pass_rate": heldout_pass_rate,
            },
        },
        "archetypes": archetype_stats,
        "metrics": {
            "false_completion_rate": false_completion_rate,
            "unnecessary_rework_rate": unnecessary_rework_rate,
            "instruction_override_fidelity": instruction_override_fidelity,
            "capability_identification_accuracy": capability_identification_accuracy,
            "funnel_diagnosis_accuracy": funnel_diagnosis_accuracy,
        },
    }


class ScriptedReconciliationProvider:
    """Mock provider for dry-runs and automated verification."""
    def __init__(self, fixtures: Dict[str, Any], fault_mode: Optional[str] = None):
        self.fixtures = fixtures
        self.fault_mode = fault_mode
        self.descriptor = {
            "mode": "scripted_reconciliation",
            "fault_mode": fault_mode,
        }

    def ask(self, messages: List[Dict[str, str]]) -> str:
        # Extract case_id from user prompt
        user_msg = next((m["content"] for m in messages if m["role"] == "user"), "{}")
        payload = json.loads(user_msg)
        cid = payload.get("case_id")
        case = next((c for c in self.fixtures["cases"] if c["case_id"] == cid), None)
        if not case:
            return json.dumps({"action": "defer_to_human", "arguments": {"card_id": "unknown"}})

        expected = case["expected"]

        # If fault mode simulated:
        if self.fault_mode == "false_completion":
            if case["archetype"] in ("branch_not_integrated", "historical_revert_distinction"):
                return json.dumps({
                    "action": "reconcile_card",
                    "arguments": {
                        "card_id": case["scenario"]["card_id"],
                        "target_state": "done",
                        "reason": "Blindly trusting feature branch without checking main checkout",
                    }
                })

        if self.fault_mode == "rework":
            if case["archetype"] == "artifact_exists_after_failed_attempt":
                return json.dumps({
                    "action": "reject_completion",
                    "arguments": {
                        "card_id": case["scenario"]["card_id"],
                        "recommended_action": "reopen_card",
                        "reason": "Attempt budget exhausted, starting over from scratch",
                    }
                })

        if self.fault_mode == "malformed":
            return "This is not valid JSON string."

        return json.dumps(expected)


def run_reconciliation_eval(
    fixtures: Dict[str, Any],
    provider: Any,
    output_dir: Optional[Path | str] = None,
    repetitions: int = 1
) -> Dict[str, Any]:
    """Runs the full reconciliation benchmark suite against a provider."""
    started_ns = time.time_ns()
    rows = []

    for rep in range(1, repetitions + 1):
        for case in fixtures["cases"]:
            messages = build_case_input(fixtures, case)
            t0 = time.monotonic()
            try:
                raw_output = provider.ask(messages)
                latency_ms = (time.monotonic() - t0) * 1000
            except Exception as exc:
                raw_output = None
                latency_ms = None
                status_dict = {
                    "status": "invalid_output",
                    "failure_category": "provider_exception",
                    "detail": f"Provider threw exception: {exc}",
                    "parsed": None,
                }
            else:
                status_dict = judge_reconciliation(
                    raw_output,
                    case["expected"],
                    case_archetype=case["archetype"]
                )

            row = {
                "case_id": case["case_id"],
                "incident_id": case["incident_id"],
                "split": case["split"],
                "archetype": case["archetype"],
                "repetition": rep,
                "status": status_dict["status"],
                "failure_category": status_dict.get("failure_category"),
                "detail": status_dict.get("detail"),
                "latency_ms": latency_ms,
                "raw_output": raw_output,
                "parsed": status_dict.get("parsed"),
                "input_sha256": digest(messages),
            }
            rows.append(row)

    scorecard = compute_scorecard(rows)

    report = {
        "schema_version": "board-reconciliation-run.v1",
        "benchmark_id": fixtures.get("benchmark_id"),
        "started_at_ns": started_ns,
        "finished_at_ns": time.time_ns(),
        "repetitions": repetitions,
        "provider": getattr(provider, "descriptor", {"mode": "custom"}),
        "fixtures_sha256": digest(fixtures),
        "scorecard": scorecard,
        "rows": rows,
    }

    if output_dir:
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        write_json(out_path / "report.json", report)

    return report


def main():
    parser = argparse.ArgumentParser(description="S4 Board Reconciliation Benchmark Evaluator")
    parser.add_argument(
        "--fixtures",
        default=str(Path(__file__).parent / "eval/s4/reconciliation-fixtures.json"),
        help="Path to reconciliation fixtures JSON"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run against scripted mock provider to verify harness integrity"
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=1,
        help="Number of repetitions per case"
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to save evaluation report"
    )
    args = parser.parse_args()

    fixtures = load_fixtures(args.fixtures)
    print(f"Loaded {len(fixtures['cases'])} cases from {args.fixtures}")

    if args.dry_run:
        provider = ScriptedReconciliationProvider(fixtures)
        print("Running in dry-run mode with ScriptedReconciliationProvider...")
    else:
        print("No live provider configured. Use --dry-run for self-test or provide an adapter.", file=sys.stderr)
        sys.exit(1)

    report = run_reconciliation_eval(
        fixtures,
        provider,
        output_dir=args.output_dir,
        repetitions=args.repetitions
    )

    sc = report["scorecard"]
    print(f"\n--- S4 Board Reconciliation Scorecard ---")
    print(f"Total Observations: {sc['total_observations']}")
    print(f"Pass Rate: {sc['pass_rate']:.1%} ({sc['passed']}/{sc['total_observations']})")
    print(f"Baseline Pass Rate: {sc['splits']['baseline']['pass_rate']:.1%}")
    print(f"Held-Out Pass Rate: {sc['splits']['held_out']['pass_rate']:.1%}")
    print("\nMetrics:")
    for metric_name, val in sc["metrics"].items():
        print(f"  {metric_name}: {val:.1%}")


if __name__ == "__main__":
    main()
