"""Card 18 wiring: termination reasons, the tier chosen for each attempt, and what the run record keeps."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_night_shift as ns
import model_tier as mt

MEDIUM = "gemini-3.8-flash-medium"
FLASH_HIGH = "gemini-3.8-flash-high"
PRO_HIGH = "gemini-3.1-pro-high"
CATALOG = {MEDIUM, FLASH_HIGH, PRO_HIGH}
LADDER = json.dumps({"antigravity": [MEDIUM, FLASH_HIGH, PRO_HIGH]})
READ = '<agent_action>{"action": "read_file", "args": {"path": "missing.txt"}}</agent_action>'
STALL = "Explored 25 iterations without writing (25 read_file)"
WORKER = "night-shift-01"


def make_agent(project_dir, monkeypatch):
    monkeypatch.setattr(ns, "resolve_gh_token", lambda: None)
    monkeypatch.chdir(project_dir)
    agent = ns.NightShiftAgent(project_dir, token="test")
    agent.iteration_delay = 0
    return agent


# --- why a run ended -----------------------------------------------------------


def test_using_the_whole_iteration_budget_is_recorded_as_iteration_cap(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(ns, "MAX_ITERATIONS", 3)
    agent.stall_threshold = 99
    agent.llm.ask = lambda messages: READ

    assert agent.process_task("task", "context", "files") is False
    assert agent.last_termination == "iteration_cap"


def test_an_empty_provider_answer_is_recorded_as_no_response(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch)
    agent.llm.ask = lambda messages: None

    assert agent.process_task("task", "context", "files") is False
    assert agent.last_termination == "no_response"


def test_exploring_without_writing_is_recorded_as_a_stall(tmp_path, monkeypatch):
    agent = make_agent(tmp_path, monkeypatch)
    monkeypatch.setattr(ns, "MAX_ITERATIONS", 10)
    agent.stall_threshold = 3
    agent.llm.ask = lambda messages: READ

    assert agent.process_task("task", "context", "files") is False
    assert agent.last_termination == "stall_without_write"
    assert agent.last_stall_reason


# --- tier choice per attempt -----------------------------------------------------


def stub_control_plane(cards):
    """Stands in for the network client: hands out the given cards and records each release."""
    waiting = list(cards)
    released = []

    def hand_out_next_card(lane=None, resources=None, repos=None):
        if not waiting:
            return None
        return {"card": waiting.pop(0), "run_id": f"run-{len(released) + 1}"}

    def record_release(run_id, outcome, gates=None, artifacts=None, error=None):
        released.append((outcome, error))

    def extend_lease(run_id):
        return {"lease": {}}

    return SimpleNamespace(identity_id=WORKER, released=released, claim=hand_out_next_card,
                           release=record_release, heartbeat=extend_lease)


def card(card_id="card-x", history=None):
    return {"id": card_id, "title": "t", "goal": "g", "kind": "software", "repo": "work",
            "gate_ids": ["g"], "history": history or []}


class Run:
    def __init__(self, agent, models_seen, records, default_model):
        self.agent = agent
        self.models_seen = models_seen
        self.records = records
        self.default_model = default_model

    def manifests(self):
        return [json.loads(p.read_text()) for p in sorted(self.records.glob("*/manifest.json"))]


def run_attempts(tmp_path, monkeypatch, cards, results, ladder=LADDER, provider="antigravity", catalog=CATALOG):
    """Claim each card in turn. `results` gives (outcome, termination) for each attempt, in order."""
    monkeypatch.setenv("FORCE_PROVIDER", provider)
    monkeypatch.setenv("ANTIGRAVITY_MODEL", MEDIUM)
    records = tmp_path / "records"
    monkeypatch.setenv("NIGHT_SHIFT_RECORD_DIR", str(records))
    if ladder is None:
        monkeypatch.delenv(mt.LADDER_ENV, raising=False)
    else:
        monkeypatch.setenv(mt.LADDER_ENV, ladder)
    monkeypatch.setattr(mt, "agy_model_catalog", lambda: catalog)
    work = tmp_path / "work"
    work.mkdir(parents=True)
    agent = make_agent(work, monkeypatch)
    agent.detect_resources = lambda: []
    default_model = agent.llm.providers[0].model
    agent.control_plane = stub_control_plane(cards)
    models_seen = []
    script = iter(results)

    def fake_execute(claimed, run_id, heartbeat):
        outcome, termination = next(script)
        models_seen.append(agent.llm.providers[0].model)
        agent.last_termination = termination
        agent.last_stall_reason = STALL if termination == "stall_without_write" else None
        error = "Verification failed or max iterations reached" if outcome == "failed" else agent.last_stall_reason
        return outcome, [], None, error

    agent.execute_card = fake_execute
    agent.run_control_plane(max_runs=len(cards))
    return Run(agent, models_seen, records, default_model)


def decisions(run):
    return [m["tier_decision"]["decision"] for m in run.manifests() if "tier_decision" in m]


def test_a_cap_failure_moves_the_next_attempt_up_a_tier_and_the_record_says_why(tmp_path, monkeypatch):
    run = run_attempts(tmp_path, monkeypatch, [card(), card()], [("failed", "iteration_cap"), ("succeeded", "completed")])

    assert run.models_seen == [MEDIUM, FLASH_HIGH]
    assert run.agent.llm.providers[0].model == run.default_model
    by_decision = {m["tier_decision"]["decision"]: m for m in run.manifests()}
    first, second = by_decision["default"], by_decision["escalated"]
    assert first["execution_summary"]["termination_reason"] == "iteration_cap"
    assert second["execution_summary"]["termination_reason"] == "completed"
    decision = second["tier_decision"]
    assert (decision["provider"], decision["model"], decision["tier_index"]) == ("antigravity", FLASH_HIGH, 1)
    assert decision["prior_termination"] == "iteration_cap"
    assert decision["reason"]


def test_the_attempt_limit_still_ends_the_climb_at_the_top_tier(tmp_path, monkeypatch):
    caps = [("failed", "iteration_cap")] * 4
    run = run_attempts(tmp_path, monkeypatch, [card() for _ in caps], caps)

    assert run.models_seen == [MEDIUM, FLASH_HIGH, PRO_HIGH, PRO_HIGH]
    assert sorted(decisions(run)) == ["default", "escalated", "escalated", "top_tier"]


def test_an_independent_card_starts_at_the_default_tier(tmp_path, monkeypatch):
    run = run_attempts(tmp_path, monkeypatch, [card("card-x"), card("card-y")],
                       [("failed", "iteration_cap"), ("succeeded", "completed")])

    assert run.models_seen == [MEDIUM, MEDIUM]


@pytest.mark.parametrize("termination", ["error", "no_response"])
def test_an_ordinary_failure_does_not_move_the_tier(tmp_path, monkeypatch, termination):
    run = run_attempts(tmp_path, monkeypatch, [card(), card()], [("failed", termination), ("succeeded", "completed")])

    assert run.models_seen == [MEDIUM, MEDIUM]
    assert sorted(decisions(run)) == ["default", "held"]


def test_a_stalled_card_returns_to_the_same_tier_unless_an_operator_acted(tmp_path, monkeypatch):
    own = [{"id": "1", "at": 1, "actor": WORKER, "action": "released"}]
    untouched = run_attempts(tmp_path / "a", monkeypatch, [card(), card(history=own)],
                             [("blocked", "stall_without_write"), ("succeeded", "completed")])
    assert untouched.models_seen == [MEDIUM, MEDIUM]

    approved = own + [{"id": "2", "at": 2, "actor": "admin", "action": "updated"}]
    authorized = run_attempts(tmp_path / "b", monkeypatch, [card(), card(history=approved)],
                              [("blocked", "stall_without_write"), ("succeeded", "completed")])
    assert authorized.models_seen == [MEDIUM, FLASH_HIGH]


def test_without_a_ladder_the_worker_behaves_exactly_as_before(tmp_path, monkeypatch):
    run = run_attempts(tmp_path, monkeypatch, [card(), card()],
                       [("failed", "iteration_cap"), ("succeeded", "completed")], ladder=None)

    assert run.models_seen == [MEDIUM, MEDIUM]
    assert all("tier_decision" not in m for m in run.manifests())
    assert [m["execution_summary"]["termination_reason"] for m in run.manifests()]


def test_a_ladder_naming_a_model_the_provider_does_not_offer_is_recorded_and_unused(tmp_path, monkeypatch):
    run = run_attempts(tmp_path, monkeypatch, [card(), card()],
                       [("failed", "iteration_cap"), ("succeeded", "completed")], catalog={MEDIUM})

    assert run.models_seen == [MEDIUM, MEDIUM]
    for manifest in run.manifests():
        assert manifest["tier_decision"]["decision"] == "disabled"
        assert FLASH_HIGH in manifest["tier_decision"]["reason"]


def test_the_ladder_must_start_at_the_configured_default_model(tmp_path, monkeypatch):
    run = run_attempts(tmp_path, monkeypatch, [card()], [("succeeded", "completed")],
                       ladder=json.dumps({"antigravity": [FLASH_HIGH, PRO_HIGH]}))

    manifest = run.manifests()[0]
    assert manifest["tier_decision"]["decision"] == "disabled"
    assert "default" in manifest["tier_decision"]["reason"]
    assert run.models_seen == [MEDIUM]


def test_a_ladder_for_another_provider_is_not_applied(tmp_path, monkeypatch):
    run = run_attempts(tmp_path, monkeypatch, [card()], [("succeeded", "completed")], provider="ollama")

    manifest = run.manifests()[0]
    assert manifest["tier_decision"]["decision"] == "disabled"
    assert "ollama" in manifest["tier_decision"]["reason"]


def test_a_failure_choosing_the_tier_never_costs_the_card_its_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(mt, "choose_tier", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    run = run_attempts(tmp_path, monkeypatch, [card()], [("succeeded", "completed")])

    assert run.agent.control_plane.released == [("succeeded", None)]
    assert run.models_seen == [MEDIUM]
