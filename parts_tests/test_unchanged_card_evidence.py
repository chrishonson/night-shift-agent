"""Tests for Card #16: unchanged-card evidence, justified no-change completion vs continue working."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_night_shift as ns
from run_record import (
    EVIDENCE_CLASS_SCRIPTED,
    SCHEMA_VERSION,
    RunRecord,
    generate_unchanged_card_samples,
    review_run_record,
    run_unchanged_card_fixture,
    validate_run_record,
)


def test_case_a_goal_satisfied_no_change(tmp_path):
    """Deliverable 1 & 2: Goal already satisfied + passing gates -> justified no-change completion."""
    out_dir = tmp_path / "case_a_sample"
    manifest = run_unchanged_card_fixture(output_dir=out_dir, case="case_a")

    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["evidence_class"] == EVIDENCE_CLASS_SCRIPTED
    assert manifest["real_model_evidence"]["status"] == "unavailable"

    # Execution summary
    summary = manifest["execution_summary"]
    assert summary["terminal_outcome"] == "succeeded"
    assert summary["release_status"] == "acknowledged"
    assert summary["pre_attempt_decision"] == "justified_no_change_completion"
    assert summary["no_commit_explanation"] is not None
    assert "Goal already satisfied before coding attempt" in summary["no_commit_explanation"]

    # Gates must have passed
    assert summary["verification"]["attempted"] is True
    assert summary["verification"]["passed"] is True
    assert any(g["gate_id"] == "unit_tests" and g["status"] == "passed" for g in summary["verification"]["gates"])

    # Independent acceptance criteria must pass (goal-specific evidence)
    assert summary["independent_acceptance"]["evaluated"] is True
    assert summary["independent_acceptance"]["passed"] is True
    assert len(summary["independent_acceptance"]["criteria"]) >= 3

    # Patch diff must be empty (clean working tree, no commit needed)
    patch_text = (out_dir / "patch.diff").read_text()
    assert not patch_text.strip()

    # Offline validation check
    val = validate_run_record(out_dir)
    assert val["valid"] is True
    assert val["complete"] is True
    assert val["errors"] == []
    assert val["reconstructed"]["pre_attempt_decision"] == "justified_no_change_completion"
    assert val["reconstructed"]["no_commit_explanation"] == summary["no_commit_explanation"]

    # Review report check
    report = review_run_record(out_dir)
    assert "Pre-Attempt Decision: justified_no_change_completion" in report
    assert "No-Commit Explanation:" in report
    assert "Avoided coding attempt" in report or "clean diff" in report


def test_case_b_green_gates_missing_goal_continue(tmp_path):
    """Deliverable 1 & 2: Passing gates + requested functionality absent -> continue working (must NOT short-circuit)."""
    out_dir = tmp_path / "case_b_sample"
    manifest = run_unchanged_card_fixture(output_dir=out_dir, case="case_b")

    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["evidence_class"] == EVIDENCE_CLASS_SCRIPTED
    assert manifest["real_model_evidence"]["status"] == "unavailable"

    summary = manifest["execution_summary"]
    # Must NOT short-circuit to success!
    assert summary["terminal_outcome"] != "succeeded"
    assert summary["terminal_outcome"] == "continued"
    assert summary["release_status"] == "working"
    assert summary["pre_attempt_decision"] == "continue_working"
    assert summary["no_commit_explanation"] is None

    # Baseline gates were green!
    assert summary["verification"]["attempted"] is True
    assert summary["verification"]["passed"] is True
    assert any(g["gate_id"] == "unit_tests" and g["status"] == "passed" for g in summary["verification"]["gates"])

    # Goal is absent -> independent acceptance criteria failed!
    assert summary["independent_acceptance"]["evaluated"] is True
    assert summary["independent_acceptance"]["passed"] is False
    failed_criteria = [c for c in summary["independent_acceptance"]["criteria"] if not c["passed"]]
    assert len(failed_criteria) >= 1
    assert any(c["id"] == "correct_greeting" for c in failed_criteria)

    # Tool calls executed as part of continuing normal execution
    assert len(summary["tool_calls"]) >= 1
    assert summary["tool_calls"][0]["tool"] == "read_file"

    # Offline validation check: must validate as a valid continue-working record
    val = validate_run_record(out_dir)
    assert val["valid"] is True
    assert val["complete"] is True
    assert val["errors"] == []
    assert val["reconstructed"]["pre_attempt_decision"] == "continue_working"
    assert val["reconstructed"]["terminal_outcome"] == "continued"

    # Review report check: clearly shows distinction without parsing console logs
    report = review_run_record(out_dir)
    assert "Pre-Attempt Decision: continue_working" in report
    assert "[FAIL] greet('NightShift') returns 'Hello, NightShift!'" in report
    assert "read_file" in report


def test_gate_error_never_implies_success(tmp_path):
    """Goal check requirement: Gate errors never imply success."""
    # 1. Test worker method check_preexisting_satisfaction when gates fail
    repo_dir = tmp_path / "gate_error_repo"
    repo_dir.mkdir()
    (repo_dir / "greeter.py").write_text('def greet(name): return f"Hello, {name}!"\n')

    # Create verification.json with a failing command
    verification_json = repo_dir / "verification.json"
    verification_json.write_text(json.dumps({
        "version": 1,
        "gates": [
            {
                "id": "broken_gate",
                "placement": ["local"],
                "commands": ["exit 1"]
            }
        ]
    }))

    scripts_dir = repo_dir / "scripts"
    scripts_dir.mkdir()
    source_run_gate = Path(__file__).resolve().parents[1] / "scripts" / "run-gate.py"
    if source_run_gate.exists():
        (scripts_dir / "run-gate.py").write_text(source_run_gate.read_text())

    card = {
        "id": "CARD-FAILING-GATES",
        "title": "Task with failing gates",
        "goal": "Pass gates and greet",
        "gate_ids": ["broken_gate"],
        "acceptance_criteria": [
            {"id": "defines_greet", "type": "file_contains", "target": "greeter.py", "pattern": "def greet("}
        ],
    }

    agent = ns.NightShiftAgent(str(repo_dir), token="synthetic-unused-token")
    agent.toolbox.target_gates = ["broken_gate"]

    is_satisfied, gate_results, acceptance, explanation = agent.check_preexisting_satisfaction(card, repo_dir)
    # Gate errors never imply success -> must be False!
    assert is_satisfied is False
    assert acceptance["passed"] is False

    # 2. Validator rejects no-change success if gates failed
    record_dir = tmp_path / "invalid_gate_error_record"
    record_dir.mkdir()
    (record_dir / "patch.diff").write_text("")
    (record_dir / "events.jsonl").write_text(
        json.dumps({"schema_version": 1, "trace_id": "test-trace", "sequence": 0, "observed_at_ns": 0, "kind": "attempt_opened", "data": {}}) + "\n" +
        json.dumps({"schema_version": 1, "trace_id": "test-trace", "sequence": 1, "observed_at_ns": 0, "kind": "trace_finalized", "data": {"worker_outcome": "succeeded", "release_status": "acknowledged", "dropped_events": 0, "capture_io_failed": False}}) + "\n"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_id": "rec-gate-fail",
        "trace_id": "test-trace",
        "evidence_class": EVIDENCE_CLASS_SCRIPTED,
        "artifacts": {"events": "events.jsonl", "patch": "patch.diff"},
        "execution_summary": {
            "terminal_outcome": "succeeded",
            "pre_attempt_decision": "justified_no_change_completion",
            "no_commit_explanation": "Some explanation",
            "verification": {
                "attempted": True,
                "passed": False,
                "gates": [{"gate_id": "broken_gate", "status": "failed"}]
            },
            "independent_acceptance": {
                "evaluated": True,
                "passed": True,
                "criteria": [{"id": "c1", "passed": True, "description": "some criterion"}]
            }
        }
    }
    (record_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    val = validate_run_record(record_dir)
    assert val["valid"] is False
    assert any("Gate errors never imply success" in err for err in val["errors"])


def test_false_short_circuit_rejected_by_validator(tmp_path):
    """Nick verification framing: Green gates + missing goal must NOT short-circuit to success."""
    record_dir = tmp_path / "false_short_circuit_record"
    record_dir.mkdir()
    (record_dir / "patch.diff").write_text("")
    (record_dir / "events.jsonl").write_text(
        json.dumps({"schema_version": 1, "trace_id": "test-trace-2", "sequence": 0, "observed_at_ns": 0, "kind": "attempt_opened", "data": {}}) + "\n" +
        json.dumps({"schema_version": 1, "trace_id": "test-trace-2", "sequence": 1, "observed_at_ns": 0, "kind": "trace_finalized", "data": {"worker_outcome": "succeeded", "release_status": "acknowledged", "dropped_events": 0, "capture_io_failed": False}}) + "\n"
    )
    # Manifest where baseline gates passed, diff is empty, but criteria failed, yet falsely claimed succeeded
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_id": "rec-false-success",
        "trace_id": "test-trace-2",
        "evidence_class": EVIDENCE_CLASS_SCRIPTED,
        "artifacts": {"events": "events.jsonl", "patch": "patch.diff"},
        "execution_summary": {
            "terminal_outcome": "succeeded",
            "pre_attempt_decision": "continue_working",
            "verification": {
                "attempted": True,
                "passed": True,
                "gates": [{"gate_id": "unit_tests", "status": "passed"}]
            },
            "independent_acceptance": {
                "evaluated": True,
                "passed": False,
                "criteria": [{"id": "correct_greeting", "passed": False, "description": "greet returns greeting"}]
            }
        }
    }
    (record_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    val = validate_run_record(record_dir)
    assert val["valid"] is False
    assert any("Outcome declared 'succeeded' but independent acceptance criteria FAILED" in err for err in val["errors"])
    assert any("Invalid short-circuit success" in err for err in val["errors"])


def test_missing_no_commit_explanation_rejected(tmp_path):
    """A no-change success requires an explicit no-commit explanation."""
    record_dir = tmp_path / "missing_explanation_record"
    record_dir.mkdir()
    (record_dir / "patch.diff").write_text("")
    (record_dir / "events.jsonl").write_text(
        json.dumps({"schema_version": 1, "trace_id": "test-trace-3", "sequence": 0, "observed_at_ns": 0, "kind": "attempt_opened", "data": {}}) + "\n" +
        json.dumps({"schema_version": 1, "trace_id": "test-trace-3", "sequence": 1, "observed_at_ns": 0, "kind": "trace_finalized", "data": {"worker_outcome": "succeeded", "release_status": "acknowledged", "dropped_events": 0, "capture_io_failed": False}}) + "\n"
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_id": "rec-missing-expl",
        "trace_id": "test-trace-3",
        "evidence_class": EVIDENCE_CLASS_SCRIPTED,
        "artifacts": {"events": "events.jsonl", "patch": "patch.diff"},
        "execution_summary": {
            "terminal_outcome": "succeeded",
            "pre_attempt_decision": "justified_no_change_completion",
            "no_commit_explanation": None,  # Missing!
            "verification": {
                "attempted": True,
                "passed": True,
                "gates": [{"gate_id": "unit_tests", "status": "passed"}]
            },
            "independent_acceptance": {
                "evaluated": True,
                "passed": True,
                "criteria": [{"id": "c1", "passed": True, "description": "c1"}]
            }
        }
    }
    (record_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))

    val = validate_run_record(record_dir)
    assert val["valid"] is False
    assert any("No-change success requires an explicit no-commit explanation" in err for err in val["errors"])


def test_committed_unchanged_samples_validate():
    """Deliverable 3: committed samples under eval/s4/unchanged-card-samples validate with #50 tooling."""
    repo_root = Path(__file__).resolve().parents[1]
    case_a_dir = repo_root / "eval" / "s4" / "unchanged-card-samples" / "case-a-goal-satisfied-no-change"
    case_b_dir = repo_root / "eval" / "s4" / "unchanged-card-samples" / "case-b-green-gates-missing-goal-continue"
    script = repo_root / "scripts" / "run-record.py"

    assert case_a_dir.exists()
    assert (case_a_dir / "manifest.json").exists()
    assert (case_a_dir / "events.jsonl").exists()
    assert (case_a_dir / "patch.diff").exists()

    assert case_b_dir.exists()
    assert (case_b_dir / "manifest.json").exists()
    assert (case_b_dir / "events.jsonl").exists()
    assert (case_b_dir / "patch.diff").exists()

    # 1. Case A validates via CLI (exit 0)
    proc_a = subprocess.run(
        [sys.executable, str(script), "validate", str(case_a_dir), "--json"],
        capture_output=True, text=True, check=True
    )
    json_a = json.loads(proc_a.stdout)
    assert json_a["valid"] is True
    assert json_a["complete"] is True
    assert json_a["reconstructed"]["terminal_outcome"] == "succeeded"
    assert json_a["reconstructed"]["pre_attempt_decision"] == "justified_no_change_completion"
    assert json_a["reconstructed"]["independent_acceptance"]["passed"] is True

    # 2. Case B validates via CLI (exit 0)
    proc_b = subprocess.run(
        [sys.executable, str(script), "validate", str(case_b_dir), "--json"],
        capture_output=True, text=True, check=True
    )
    json_b = json.loads(proc_b.stdout)
    assert json_b["valid"] is True
    assert json_b["complete"] is True
    assert json_b["reconstructed"]["terminal_outcome"] == "continued"
    assert json_b["reconstructed"]["pre_attempt_decision"] == "continue_working"
    assert json_b["reconstructed"]["independent_acceptance"]["passed"] is False

    # 3. Review reports clearly distinguish without parsing console logs
    rev_a = subprocess.run(
        [sys.executable, str(script), "review", str(case_a_dir)],
        capture_output=True, text=True, check=True
    ).stdout
    assert "Terminal Outcome: succeeded" in rev_a
    assert "Pre-Attempt Decision: justified_no_change_completion" in rev_a
    assert "No-Commit Explanation:" in rev_a

    rev_b = subprocess.run(
        [sys.executable, str(script), "review", str(case_b_dir)],
        capture_output=True, text=True, check=True
    ).stdout
    assert "Terminal Outcome: continued" in rev_b
    assert "Pre-Attempt Decision: continue_working" in rev_b
    assert "[FAIL] greet('NightShift') returns 'Hello, NightShift!'" in rev_b


def test_agent_execute_card_no_change_completion(tmp_path, monkeypatch):
    """Worker behavior: execute_card avoids coding attempt when goal is already satisfied."""
    repo_dir = tmp_path / "agent_repo_a"
    repo_dir.mkdir()
    (repo_dir / "greeter.py").write_text('def greet(name: str) -> str:\n    return f"Hello, {name}!"\n')
    (repo_dir / "test_greeter.py").write_text(
        'from greeter import greet\n\ndef test_greet():\n    assert greet("NightShift") == "Hello, NightShift!"\n'
    )
    (repo_dir / "verification.json").write_text(json.dumps({
        "version": 1,
        "gates": [{"id": "unit_tests", "placement": ["local"], "commands": [f"{sys.executable} -m pytest -q test_greeter.py"]}]
    }))
    scripts_dir = repo_dir / "scripts"
    scripts_dir.mkdir()
    source_run_gate = Path(__file__).resolve().parents[1] / "scripts" / "run-gate.py"
    if source_run_gate.exists():
        (scripts_dir / "run-gate.py").write_text(source_run_gate.read_text())

    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)

    agent = ns.NightShiftAgent(str(repo_dir), token="synthetic-unused-token")

    # Mock process_task to ensure it is NEVER called for an already satisfied goal
    def fake_process_task(*args, **kwargs):
        raise AssertionError("process_task was called when goal was already satisfied!")
    monkeypatch.setattr(agent, "process_task", fake_process_task)

    class DummyHeartbeat:
        abandoned = False

    card = {
        "id": "CARD-EXECUTE-A",
        "title": "Implement greeting",
        "goal": "Implement greet function",
        "kind": "software",
        "repo": "agent_repo_a",
        "gate_ids": ["unit_tests"],
        "acceptance_criteria": [
            {"id": "defines_greet", "type": "file_contains", "target": "greeter.py", "pattern": "def greet("},
            {"id": "correct_greeting", "type": "python_eval", "code": "from greeter import greet; assert greet('NightShift') == 'Hello, NightShift!'"},
            {"id": "gate_passed", "type": "gate_status", "gate_id": "unit_tests", "expected_status": "passed"},
        ],
    }

    outcome, gate_results, artifacts, explanation = agent.execute_card(card, "run-123", DummyHeartbeat())
    assert outcome == "succeeded"
    assert artifacts.get("no_commit") is True
    assert "Goal already satisfied before coding attempt" in artifacts.get("no_commit_reason")
    assert all(g["status"] == "passed" for g in gate_results)


def test_agent_execute_card_continues_when_goal_missing(tmp_path, monkeypatch):
    """Worker behavior: execute_card does NOT short circuit when gates pass but goal is missing."""
    repo_dir = tmp_path / "agent_repo_b"
    repo_dir.mkdir()
    (repo_dir / "greeter.py").write_text('def greet(name: str) -> str:\n    return "legacy"\n\ndef helper(): return 1\n')
    (repo_dir / "test_greeter.py").write_text(
        'from greeter import helper\n\ndef test_helper():\n    assert helper() == 1\n'
    )
    (repo_dir / "verification.json").write_text(json.dumps({
        "version": 1,
        "gates": [{"id": "unit_tests", "placement": ["local"], "commands": [f"{sys.executable} -m pytest -q test_greeter.py"]}]
    }))
    scripts_dir = repo_dir / "scripts"
    scripts_dir.mkdir()
    source_run_gate = Path(__file__).resolve().parents[1] / "scripts" / "run-gate.py"
    if source_run_gate.exists():
        (scripts_dir / "run-gate.py").write_text(source_run_gate.read_text())

    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "add", "."], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_dir, capture_output=True, check=True)

    agent = ns.NightShiftAgent(str(repo_dir), token="synthetic-unused-token")

    process_task_called = []
    def spy_process_task(*args, **kwargs):
        process_task_called.append(True)
        return False  # Simulate coding attempt didn't pass verification
    monkeypatch.setattr(agent, "process_task", spy_process_task)

    class DummyHeartbeat:
        abandoned = False

    card = {
        "id": "CARD-EXECUTE-B",
        "title": "Implement greeting",
        "goal": "Implement greet function",
        "kind": "software",
        "repo": "agent_repo_b",
        "gate_ids": ["unit_tests"],
        "acceptance_criteria": [
            {"id": "defines_greet", "type": "file_contains", "target": "greeter.py", "pattern": "def greet("},
            {"id": "correct_greeting", "type": "python_eval", "code": "from greeter import greet; assert greet('NightShift') == 'Hello, NightShift!'"},
        ],
    }

    outcome, gate_results, artifacts, explanation = agent.execute_card(card, "run-456", DummyHeartbeat())
    # Must NOT have short-circuited to success! It must have called process_task to continue working!
    assert len(process_task_called) == 1
    assert outcome == "failed"
