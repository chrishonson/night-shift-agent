"""Tests for Card #17: stall-without-write detection, mid-loop abort, and blocked release."""
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
    generate_stall_samples,
    review_run_record,
    run_stall_fixture,
    validate_run_record,
)


def test_case_stall_blocked(tmp_path):
    """Deliverable: Explored threshold iterations without writing -> blocked."""
    out_dir = tmp_path / "case_stall_blocked"
    manifest = run_stall_fixture(output_dir=out_dir, case="case_stall_blocked", threshold=25)

    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["evidence_class"] == EVIDENCE_CLASS_SCRIPTED
    assert manifest["real_model_evidence"]["status"] == "unavailable"

    # Execution summary
    summary = manifest["execution_summary"]
    assert summary["terminal_outcome"] == "blocked"
    assert summary["release_status"] == "acknowledged"
    assert summary["stall_reason"] is not None
    assert "Explored 25 iterations without writing" in summary["stall_reason"]
    assert "25 read_file" in summary["stall_reason"]

    # Tool calls must be 25 reads, 0 writes
    tool_calls = summary["tool_calls"]
    assert len(tool_calls) == 25
    assert all(t["tool"] == "read_file" for t in tool_calls)

    # Verification was baseline only, no verify_build tool call in loop
    assert not any(t["tool"] in ("verify_build", "verify") for t in tool_calls)

    # Offline validation check
    val = validate_run_record(out_dir)
    assert val["valid"] is True
    assert val["complete"] is True
    assert val["errors"] == []
    assert val["reconstructed"]["terminal_outcome"] == "blocked"
    assert "without writing" in val["reconstructed"]["stall_reason"].lower()


def test_case_justified_no_change_succeeds(tmp_path):
    """Deliverable: #16 path still succeeds (must not be blocked by stall detector)."""
    out_dir = tmp_path / "case_justified_no_change"
    manifest = run_stall_fixture(output_dir=out_dir, case="case_justified_no_change_succeeds")

    assert manifest["schema_version"] == SCHEMA_VERSION
    summary = manifest["execution_summary"]
    assert summary["terminal_outcome"] == "succeeded"
    assert summary["release_status"] == "acknowledged"
    assert summary["pre_attempt_decision"] == "justified_no_change_completion"
    assert summary["no_commit_explanation"] is not None
    assert "Goal already satisfied before coding attempt" in summary["no_commit_explanation"]

    # Coding loop was avoided entirely; stall detector never engaged
    assert len(summary["tool_calls"]) == 0

    val = validate_run_record(out_dir)
    assert val["valid"] is True
    assert val["complete"] is True
    assert val["reconstructed"]["terminal_outcome"] == "succeeded"


def test_case_healthy_read_then_write(tmp_path):
    """Deliverable: Many reads then write continues (not blocked)."""
    out_dir = tmp_path / "case_healthy_read_then_write"
    manifest = run_stall_fixture(output_dir=out_dir, case="case_healthy_read_then_write", threshold=25)

    assert manifest["schema_version"] == SCHEMA_VERSION
    summary = manifest["execution_summary"]
    assert summary["terminal_outcome"] == "succeeded"
    assert summary["release_status"] == "acknowledged"
    assert summary["stall_reason"] is None

    # Reconstructed tool calls: 20 reads, 1 write_file, 1 verify_build
    tool_calls = summary["tool_calls"]
    assert len(tool_calls) == 22
    assert sum(1 for t in tool_calls if t["tool"] == "read_file") == 20
    assert any(t["tool"] == "write_file" for t in tool_calls)
    assert any(t["tool"] == "verify_build" for t in tool_calls)

    # Acceptance passed
    assert summary["independent_acceptance"]["passed"] is True

    # Patch is non-empty
    patch_text = (out_dir / "patch.diff").read_text()
    assert "Hello, {name}!" in patch_text

    val = validate_run_record(out_dir)
    assert val["valid"] is True
    assert val["complete"] is True
    assert val["reconstructed"]["terminal_outcome"] == "succeeded"


def test_stall_releases_blocked_not_failed(tmp_path):
    """Hard Requirement: Release blocked, not failed.

    Failed returns the card to ready to burn another attempt on the same dead pattern.
    Blocked parks it for an operator, which is the correct outcome when the agent cannot get started.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    agent = ns.NightShiftAgent(str(repo_dir), token="test-token")
    agent.stall_threshold = 3
    agent.iteration_delay = 0.0

    class ReadOnlyProvider(ns.LLMProvider):
        name = "ReadOnlyProvider"
        def ask(self, messages):
            return '<agent_action>{"action": "read_file", "args": {"path": "README.md"}}</agent_action>'

    agent.llm.providers = [ReadOnlyProvider()]

    card = {
        "id": "card-stall-test",
        "title": "Stalling task",
        "goal": "Write code",
        "kind": "software",
        "gate_ids": [],
    }

    class MockHeartbeat:
        abandoned = False

    outcome, gates, artifacts, error_msg = agent.execute_card(card, "run-123", MockHeartbeat())

    # MUST be blocked, NOT failed!
    assert outcome == "blocked"
    assert artifacts is None
    assert error_msg is not None
    assert "Explored 3 iterations without writing" in error_msg
    assert "3 read_file" in error_msg

    # Test wire release in run_control_plane
    released_calls = []
    class MockControlPlane:
        def claim(self, lane=None, resources=None, repos=None):
            if not released_calls:
                return {"run_id": "run-123", "card": card}
            return None
        def heartbeat(self, run_id):
            return {"status": "ok"}
        def release(self, run_id, outcome, gates=None, artifacts=None, error=None):
            released_calls.append({
                "run_id": run_id,
                "outcome": outcome,
                "gates": gates,
                "artifacts": artifacts,
                "error": error,
            })
            return {"status": "ok"}

    agent.control_plane = MockControlPlane()
    agent.run_control_plane(lane="local", max_runs=1, poll_interval_base=0.01)

    assert len(released_calls) == 1
    rel = released_calls[0]
    assert rel["outcome"] == "blocked"  # Must be blocked!
    assert "Explored 3 iterations without writing" in rel["error"]


def test_software_vs_task_scoping(tmp_path):
    """Hard Requirement: Scope write-based stall detection to software implementation loops.

    Legitimate read-only planning/task cards use the task evidence contract
    (do not block them for not writing code).
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    agent = ns.NightShiftAgent(str(repo_dir), token="test-token")
    agent.stall_threshold = 2
    agent.iteration_delay = 0.0

    task_card = {
        "id": "card-task-1",
        "title": "Architecture evaluation",
        "goal": "Review architecture and summarize in local task draft",
        "kind": "task",
        "gate_ids": [],
    }

    # Task card provider returns valid task draft JSON
    class TaskDraftProvider(ns.LLMProvider):
        name = "TaskDraftProvider"
        def ask(self, messages):
            return json.dumps({
                "status": "complete",
                "result": "Architecture plan draft deliverable",
                "evidence": ["Read project context and verified goals"],
                "unknowns": []
            })

    agent.llm.providers = [TaskDraftProvider()]

    class MockHeartbeat:
        abandoned = False

    outcome, gates, artifacts, note = agent.execute_card(task_card, "run-task-1", MockHeartbeat())

    # Task card succeeds under task evidence contract without any code writes!
    assert outcome == "succeeded"
    assert "Task draft evidence" in note

    # Also verify non-software kind inside process_task does not trigger write stall
    agent.current_card = {"kind": "planning"}
    class ReadProvider(ns.LLMProvider):
        name = "ReadProvider"
        calls = 0
        def ask(self, messages):
            self.calls += 1
            if self.calls < 5:
                return '<agent_action>{"action": "read_file", "args": {"path": "README.md"}}</agent_action>'
            return None

    agent.llm.providers = [ReadProvider()]
    agent.process_task("plan", "", "")
    assert agent.last_stall_reason is None  # Not stalled because kind != "software"


def test_verify_build_attempt_clears_stall(tmp_path):
    """Hard Requirement: Only absence of any write (and no verify_build) is the stall signal.

    A verification attempt proves the agent is actively checking against gates.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    agent = ns.NightShiftAgent(str(repo_dir), token="test-token")
    agent.stall_threshold = 3
    agent.iteration_delay = 0.0

    class VerifyProvider(ns.LLMProvider):
        name = "VerifyProvider"
        calls = 0
        def ask(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '<agent_action>{"action": "read_file", "args": {"path": "README.md"}}</agent_action>'
            elif self.calls == 2:
                # Agent makes verification attempt
                return '<agent_action>{"action": "verify_build", "args": {}}</agent_action>'
            elif self.calls == 3:
                return '<agent_action>{"action": "read_file", "args": {"path": "README.md"}}</agent_action>'
            return None

    agent.llm.providers = [VerifyProvider()]
    agent.process_task("task", "", "")

    # Because verify_build was attempted, stall detector did not fire!
    assert agent.last_stall_reason is None


def test_healthy_write_before_threshold_permits_later_exploration(tmp_path):
    """Hard Requirement: Lots of reads then a write = healthy.

    Once a write occurs, the stall signal is cleared.
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    agent = ns.NightShiftAgent(str(repo_dir), token="test-token")
    agent.stall_threshold = 3
    agent.iteration_delay = 0.0

    class EarlyWriteProvider(ns.LLMProvider):
        name = "EarlyWriteProvider"
        calls = 0
        def ask(self, messages):
            self.calls += 1
            if self.calls == 1:
                return '<agent_action>{"action": "read_file", "args": {"path": "README.md"}}</agent_action>'
            elif self.calls == 2:
                # Write occurs before threshold 3
                return '<agent_action>{"action": "write_file", "args": {"path": "code.py", "content": "x = 1"}}</agent_action>'
            elif self.calls <= 5:
                # Further reading beyond threshold 3
                return '<agent_action>{"action": "read_file", "args": {"path": "README.md"}}</agent_action>'
            return None

    agent.llm.providers = [EarlyWriteProvider()]
    agent.process_task("task", "", "")

    assert agent.last_stall_reason is None


def test_mid_loop_abort_saves_budget(tmp_path):
    """Verifies that mid-loop stall abort immediately halts execution.

    It does NOT burn remaining iterations up to MAX_ITERATIONS (e.g. 80).
    """
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=repo_dir, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "agentnightshift"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "agentnightshift@gmail.com"], cwd=repo_dir, check=True)
    (repo_dir / "README.md").write_text("# Repo\n")
    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    agent = ns.NightShiftAgent(str(repo_dir), token="test-token")
    agent.stall_threshold = 5
    agent.iteration_delay = 0.0

    call_count = {"n": 0}
    class CountingProvider(ns.LLMProvider):
        name = "CountingProvider"
        def ask(self, messages):
            call_count["n"] += 1
            return '<agent_action>{"action": "read_file", "args": {"path": "README.md"}}</agent_action>'

    agent.llm.providers = [CountingProvider()]
    res = agent.process_task("task", "", "")

    assert res is False
    assert agent.last_stall_reason is not None
    # Must have stopped at exactly iteration 5, saving remaining 75 iterations!
    assert call_count["n"] == 5


def test_samples_exist_and_validate():
    """Verify that the generated fixture samples exist and pass offline validation."""
    base = Path(__file__).resolve().parents[1] / "eval" / "s4" / "stall-without-write-samples"
    stall_dir = base / "case-stall-blocked"
    no_change_dir = base / "case-justified-no-change-succeeds"
    healthy_dir = base / "case-healthy-read-then-write"

    assert stall_dir.exists(), f"Missing {stall_dir}"
    assert no_change_dir.exists(), f"Missing {no_change_dir}"
    assert healthy_dir.exists(), f"Missing {healthy_dir}"

    for sample_dir, expected_outcome in [
        (stall_dir, "blocked"),
        (no_change_dir, "succeeded"),
        (healthy_dir, "succeeded"),
    ]:
        val = validate_run_record(sample_dir)
        assert val["valid"] is True, f"Validation failed for {sample_dir}: {val['errors']}"
        assert val["complete"] is True, f"Incomplete trace for {sample_dir}: {val['errors']}"
        assert val["reconstructed"]["terminal_outcome"] == expected_outcome
