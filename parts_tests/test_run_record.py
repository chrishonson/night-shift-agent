"""Tests for S4 E2E run record, payload capture, failover attribution, and offline validation."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_night_shift as ns
from context_capture import ContextCapture
from run_record import (
    EVIDENCE_CLASS_SCRIPTED,
    SCHEMA_VERSION,
    RunRecord,
    evaluate_criteria,
    review_run_record,
    run_bounded_fixture,
    validate_run_record,
)


def test_success_run_record(tmp_path):
    """Deliverable 5: success through actual tool loop, diff, and independent acceptance."""
    out_dir = tmp_path / "success_record"
    manifest = run_bounded_fixture(output_dir=out_dir)

    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["evidence_class"] == EVIDENCE_CLASS_SCRIPTED
    assert manifest["real_model_evidence"]["status"] == "unavailable"
    assert manifest["execution_summary"]["terminal_outcome"] == "succeeded"
    assert manifest["execution_summary"]["independent_acceptance"]["passed"] is True
    assert manifest["execution_summary"]["complete"] is True

    # Validate offline with validator
    val = validate_run_record(out_dir)
    assert val["valid"] is True
    assert val["complete"] is True
    assert val["errors"] == []
    assert len(val["reconstructed"]["actions"]) >= 2
    assert "def greet(" in val["reconstructed"]["diff"]


def test_failure_or_interruption_run_record(tmp_path):
    """Deliverable 5: failure/interruption is accurately captured and flagged."""
    out_dir = tmp_path / "failure_record"
    manifest = run_bounded_fixture(output_dir=out_dir, task_should_fail=True)

    assert manifest["execution_summary"]["terminal_outcome"] == "failed"
    assert manifest["execution_summary"]["independent_acceptance"]["passed"] is False

    val = validate_run_record(out_dir)
    # Outcome is failed, so independent acceptance failed
    assert val["reconstructed"]["terminal_outcome"] == "failed"
    assert val["reconstructed"]["independent_acceptance"]["passed"] is False


def test_provider_failover_attribution(tmp_path):
    """Deliverable 5: provider failover attribution is explicitly recorded and identified."""
    class QuotaExceededScripted(ns.LLMProvider):
        name = "Primary Quota Exceeded Provider"
        def ask(self, messages):
            raise ns.QuotaExceededError("Rate limit reached on primary provider")

    class BackupScripted(ns.LLMProvider):
        name = "Backup Scripted Provider"
        calls = 0
        def ask(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '<agent_action>{"action": "read_file", "args": {"path": "greeter.py"}}</agent_action>'
            elif self.calls == 2:
                content = 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
                return f'<agent_action>{{"action": "write_file", "args": {{"path": "greeter.py", "content": {json.dumps(content)}}}}}</agent_action>'
            elif self.calls == 3:
                return '<agent_action>{"action": "verify_build", "args": {}}</agent_action>'
            return None

    out_dir = tmp_path / "failover_record"
    manifest = run_bounded_fixture(
        output_dir=out_dir,
        providers=[QuotaExceededScripted(), BackupScripted()],
    )

    failovers = manifest["execution_summary"]["failover_attribution"]
    assert len(failovers) >= 1
    assert failovers[0]["from_provider"] == "Primary Quota Exceeded Provider"
    assert failovers[0]["to_provider"] == "Backup Scripted Provider"
    assert failovers[0]["reason"] == "quota_error"

    val = validate_run_record(out_dir)
    assert val["valid"] is True
    recon_failovers = val["reconstructed"]["failover_attribution"]
    assert len(recon_failovers) >= 1
    assert recon_failovers[0]["from_provider"] == "Primary Quota Exceeded Provider"


def test_concurrent_record_isolation(tmp_path):
    """Deliverable 5: concurrent/independent runs are completely isolated."""
    dir_a = tmp_path / "run_a"
    dir_b = tmp_path / "run_b"

    manifest_a = run_bounded_fixture(
        output_dir=dir_a,
        card_overrides={"id": "CARD-ALPHA", "title": "Alpha Task"},
    )
    manifest_b = run_bounded_fixture(
        output_dir=dir_b,
        card_overrides={"id": "CARD-BETA", "title": "Beta Task"},
    )

    assert manifest_a["record_id"] != manifest_b["record_id"]
    assert manifest_a["trace_id"] != manifest_b["trace_id"]
    assert manifest_a["task"]["id"] == "CARD-ALPHA"
    assert manifest_b["task"]["id"] == "CARD-BETA"

    events_a = (dir_a / "events.jsonl").read_text()
    events_b = (dir_b / "events.jsonl").read_text()

    assert manifest_a["trace_id"] in events_a
    assert manifest_b["trace_id"] not in events_a
    assert manifest_b["trace_id"] in events_b
    assert manifest_a["trace_id"] not in events_b


def test_incomplete_capture_detected(tmp_path):
    """Deliverable 5: incomplete capture (dropped events or missing finalization) fails validation."""
    out_dir = tmp_path / "incomplete_record"
    run_bounded_fixture(output_dir=out_dir)

    # 1. Truncate events file so terminal trace_finalized is missing (simulating process crash)
    events_file = out_dir / "events.jsonl"
    lines = events_file.read_text().splitlines()
    truncated_lines = [l for l in lines if '"trace_finalized"' not in l]
    events_file.write_text("\n".join(truncated_lines) + "\n")

    val = validate_run_record(out_dir)
    assert val["complete"] is False
    assert any("Trace incomplete" in err or "missing terminal" in err for err in val["errors"])


def test_green_baseline_fails_independent_acceptance(tmp_path):
    """Deliverable 5: green existing tests alone != task success; independent acceptance is checked."""
    # Run fixture where existing baseline tests pass (we don't break them), but provider writes wrong return value
    out_dir = tmp_path / "green_baseline_bad_acceptance"
    manifest = run_bounded_fixture(output_dir=out_dir, break_independent_acceptance=True)

    # Unit tests gate failed because Wrong Greeting doesn't match NightShift expectation
    # And independent acceptance is False
    assert manifest["execution_summary"]["independent_acceptance"]["passed"] is False
    val = validate_run_record(out_dir)
    assert val["reconstructed"]["independent_acceptance"]["passed"] is False


def test_secret_redaction_and_capability_protection(tmp_path):
    """Deliverable 1: lease capabilities and credentials are never exported, secrets redacted."""
    out_dir = tmp_path / "secrets_test"
    secret_pat = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    bearer_token = "Bearer secret_jwt_token_123456789"
    raw_lease = "lease-secret-capability-xyz-999"

    class SecretLeakingProvider(ns.LLMProvider):
        name = "Secret Provider"
        calls = 0
        def ask(self, messages):
            self.calls += 1
            if self.calls == 1:
                return f'<agent_action>{{"action": "read_file", "args": {{"path": "greeter.py", "auth": "{bearer_token}"}}}}</agent_action>'
            elif self.calls == 2:
                content = 'def greet(name: str) -> str:\n    return f"Hello, {name}!"\n'
                return f'<agent_action>{{"action": "write_file", "args": {{"path": "greeter.py", "content": {json.dumps(content)}}}}}</agent_action>'
            elif self.calls == 3:
                return '<agent_action>{"action": "verify_build", "args": {}}</agent_action>'
            return None

    card_with_secrets = {
        "id": "CARD-SECRETS",
        "title": "Secret task",
        "goal": f"Task with secret {secret_pat}",
        "lease": raw_lease,
        "token": secret_pat,
        "kind": "software",
        "repo": "sample-greeter",
        "gate_ids": ["unit_tests"],
        "acceptance_criteria": [
            {"id": "defines_greet", "type": "file_contains", "target": "greeter.py", "pattern": "def greet("}
        ],
    }

    manifest = run_bounded_fixture(
        output_dir=out_dir,
        card_overrides=card_with_secrets,
        provider=SecretLeakingProvider(),
    )

    # Inspect all artifacts on disk
    manifest_text = (out_dir / "manifest.json").read_text()
    events_text = (out_dir / "events.jsonl").read_text()
    patch_text = (out_dir / "patch.diff").read_text()

    for secret in (secret_pat, raw_lease, "secret_jwt_token_123456789"):
        assert secret not in manifest_text
        assert secret not in events_text
        assert secret not in patch_text

    val = validate_run_record(out_dir)
    assert val["valid"] is True
    assert val["errors"] == []


def test_committed_sample_validates():
    """Deliverable 4: committed sample under eval/s4/e2e-run-record-sample validates offline."""
    sample_dir = Path(__file__).resolve().parents[1] / "eval/s4/e2e-run-record-sample"
    assert sample_dir.exists()
    assert (sample_dir / "manifest.json").exists()
    assert (sample_dir / "events.jsonl").exists()
    assert (sample_dir / "patch.diff").exists()

    result = validate_run_record(sample_dir)
    assert result["valid"] is True
    assert result["complete"] is True
    assert result["evidence_class"] == EVIDENCE_CLASS_SCRIPTED
    assert result["real_model_evidence"]["status"] == "unavailable"
    assert result["reconstructed"]["terminal_outcome"] == "succeeded"
    assert result["reconstructed"]["independent_acceptance"]["passed"] is True
    assert result["errors"] == []


def test_cli_subcommands(tmp_path):
    """Deliverable 3: CLI commands run and report status without parsing console logs."""
    sample_dir = Path(__file__).resolve().parents[1] / "eval/s4/e2e-run-record-sample"
    script = Path(__file__).resolve().parents[1] / "scripts" / "run-record.py"

    # 1. validate command
    val_proc = subprocess.run(
        [sys.executable, str(script), "validate", str(sample_dir), "--json"],
        capture_output=True, text=True, check=True
    )
    val_json = json.loads(val_proc.stdout)
    assert val_json["valid"] is True
    assert val_json["complete"] is True

    # 2. review command
    rev_proc = subprocess.run(
        [sys.executable, str(script), "review", str(sample_dir)],
        capture_output=True, text=True, check=True
    )
    assert "S4 E2E RUN RECORD REVIEW" in rev_proc.stdout
    assert "Evidence Class : scripted_provider_fixture" in rev_proc.stdout
    assert "Real Model     : unavailable" in rev_proc.stdout
