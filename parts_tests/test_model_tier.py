"""Card 18: retry a card on a higher model tier after it exhausts its iteration budget."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_night_shift as ns
import model_tier as mt

MEDIUM = "gemini-3.8-flash-medium"
FLASH_HIGH = "gemini-3.8-flash-high"
PRO_HIGH = "gemini-3.1-pro-high"
OPUS_HIGH = "claude-opus-5-5-high"
CATALOG = {MEDIUM, FLASH_HIGH, PRO_HIGH, OPUS_HIGH}
THREE_TIERS = (MEDIUM, FLASH_HIGH, PRO_HIGH)
WORKER = "night-shift-01"


def ladder(models=THREE_TIERS, allow_pool_change=False):
    return mt.Ladder(provider="antigravity", models=list(models), allow_pool_change=allow_pool_change)


def prior(termination, model=MEDIUM, outcome="failed", tier_index=None):
    return mt.PriorAttempt(
        created_at="2026-10-03T00:00:00Z",
        outcome=outcome,
        termination=termination,
        model=model,
        tier_index=tier_index,
    )


# --- tier selection -------------------------------------------------------------


def test_first_attempt_starts_at_the_default_tier():
    decision = mt.choose_tier(ladder(), None, operator_authorized=False)

    assert (decision.model, decision.tier_index, decision.decision) == (MEDIUM, 0, "default")
    assert decision.prior_termination is None


def test_iteration_cap_failure_escalates_one_tier_per_attempt():
    first = mt.choose_tier(ladder(), prior("iteration_cap", model=MEDIUM), operator_authorized=False)
    assert (first.model, first.tier_index, first.decision) == (FLASH_HIGH, 1, "escalated")
    assert first.prior_termination == "iteration_cap"

    second = mt.choose_tier(ladder(), prior("iteration_cap", model=FLASH_HIGH), operator_authorized=False)
    assert (second.model, second.tier_index, second.decision) == (PRO_HIGH, 2, "escalated")


def test_top_tier_stays_put_instead_of_looping():
    decision = mt.choose_tier(ladder(), prior("iteration_cap", model=PRO_HIGH), operator_authorized=False)

    assert (decision.model, decision.tier_index, decision.decision) == (PRO_HIGH, 2, "top_tier")


@pytest.mark.parametrize("termination", ["no_response", "error", "verification_failed"])
def test_ordinary_failures_keep_the_tier(termination):
    decision = mt.choose_tier(ladder(), prior(termination, model=FLASH_HIGH), operator_authorized=False)

    assert (decision.model, decision.tier_index, decision.decision) == (FLASH_HIGH, 1, "held")
    assert decision.prior_termination == termination


def test_stalled_card_is_not_escalated_without_operator_authorization():
    stalled = prior("stall_without_write", outcome="blocked", model=MEDIUM)

    decision = mt.choose_tier(ladder(), stalled, operator_authorized=False)

    assert (decision.model, decision.tier_index, decision.decision) == (MEDIUM, 0, "held")
    assert "authoriz" in decision.reason


def test_operator_authorized_retry_after_a_stall_escalates_one_tier():
    stalled = prior("stall_without_write", outcome="blocked", model=MEDIUM)

    decision = mt.choose_tier(ladder(), stalled, operator_authorized=True)

    assert (decision.model, decision.tier_index, decision.decision) == (FLASH_HIGH, 1, "escalated")
    assert decision.prior_termination == "stall_without_write"


def test_failure_on_a_model_outside_the_ladder_is_not_escalated():
    fallback = prior("iteration_cap", model="deepseek-r1:32b")

    decision = mt.choose_tier(ladder(), fallback, operator_authorized=False)

    assert (decision.tier_index, decision.decision) == (0, "held")


def test_a_finished_card_starts_over_at_the_default_tier():
    done = prior("completed", model=PRO_HIGH, outcome="succeeded")

    decision = mt.choose_tier(ladder(), done, operator_authorized=False)

    assert (decision.tier_index, decision.decision) == (0, "default")


def test_crossing_into_another_pool_needs_explicit_policy():
    pools = (PRO_HIGH, OPUS_HIGH)
    capped = prior("iteration_cap", model=PRO_HIGH)

    denied = mt.choose_tier(ladder(pools), capped, operator_authorized=False)
    assert (denied.model, denied.decision) == (PRO_HIGH, "pool_change_denied")

    allowed = mt.choose_tier(ladder(pools, allow_pool_change=True), capped, operator_authorized=False)
    assert (allowed.model, allowed.decision) == (OPUS_HIGH, "escalated")


# --- configuration --------------------------------------------------------------


def test_ladder_comes_from_configuration_and_is_off_without_it():
    assert mt.load_ladder({}) is None

    env = {mt.LADDER_ENV: json.dumps({"antigravity": [MEDIUM, FLASH_HIGH]}), mt.POOL_CHANGE_ENV: "1"}
    configured = mt.load_ladder(env)

    assert configured.provider == "antigravity"
    assert configured.models == [MEDIUM, FLASH_HIGH]
    assert configured.allow_pool_change is True
    assert mt.load_ladder({mt.LADDER_ENV: json.dumps({"antigravity": [MEDIUM]})}).allow_pool_change is False


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '["a"]',
        '{"antigravity": []}',
        '{"antigravity": ["a", "a"]}',
        '{"antigravity": [1, 2]}',
        '{"claude": ["x", "y"]}',
        '{"antigravity": ["a"], "gemini": ["b"]}',
    ],
)
def test_bad_ladder_configuration_is_rejected_with_a_reason(raw):
    with pytest.raises(mt.LadderConfigError):
        mt.load_ladder({mt.LADDER_ENV: raw})


def test_ladder_models_are_checked_against_the_provider_catalog():
    assert mt.ladder_problems(ladder(), lambda: CATALOG) == []

    unknown = ladder((MEDIUM, "gemini-9-imaginary"))
    assert mt.ladder_problems(unknown, lambda: CATALOG) == ["gemini-9-imaginary is not offered by antigravity"]

    unavailable = mt.ladder_problems(ladder(), lambda: None)
    assert len(unavailable) == 1 and "cannot list" in unavailable[0]


def test_agy_models_output_is_parsed_into_ids():
    output = (
        "Fetching available models...\n"
        f"{FLASH_HIGH}\tGemini 3.8 Flash (High)\n"
        f"{PRO_HIGH}\tGemini 3.1 Pro (High)\n"
        f"{OPUS_HIGH}\tClaude Opus 5.5 (High)\n"
    )

    assert mt.parse_agy_models(output) == {FLASH_HIGH, PRO_HIGH, OPUS_HIGH}
    assert mt.parse_agy_models("Fetching available models...\n") == set()


# --- prior attempts from run records ---------------------------------------------


def write_manifest(root, name, card_id, created, outcome, tier=None, **summary):
    folder = root / name
    folder.mkdir(parents=True)
    manifest = {
        "schema_version": "s4-run-record.v1",
        "created_at_iso": created,
        "task": {"id": card_id},
        "execution_summary": {"terminal_outcome": outcome, **summary},
    }
    if tier:
        manifest["tier_decision"] = tier
    (folder / "manifest.json").write_text(json.dumps(manifest))


def test_prior_attempt_is_the_newest_record_for_that_card(tmp_path):
    write_manifest(tmp_path, "a", "card-a", "2026-10-01T10:00:00Z", "failed",
                   tier={"model": MEDIUM, "tier_index": 0}, termination_reason="iteration_cap")
    write_manifest(tmp_path, "b", "card-a", "2026-10-02T10:00:00Z", "failed",
                   tier={"model": FLASH_HIGH, "tier_index": 1}, termination_reason="iteration_cap")
    write_manifest(tmp_path, "c", "card-b", "2026-10-03T10:00:00Z", "failed", termination_reason="error")

    found = mt.load_prior_attempt(tmp_path, "card-a")

    assert (found.model, found.tier_index, found.termination, found.outcome) == (FLASH_HIGH, 1, "iteration_cap", "failed")
    assert mt.load_prior_attempt(tmp_path, "card-new") is None
    assert mt.load_prior_attempt(tmp_path / "missing", "card-a") is None


def test_unreadable_records_are_skipped(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "manifest.json").write_text("{not json")
    write_manifest(tmp_path, "good", "card-a", "2026-10-01T10:00:00Z", "failed",
                   tier={"model": MEDIUM, "tier_index": 0}, termination_reason="iteration_cap")

    assert mt.load_prior_attempt(tmp_path, "card-a").termination == "iteration_cap"


def test_records_from_before_this_card_are_read_by_their_error_text(tmp_path):
    write_manifest(tmp_path, "old-cap", "card-a", "2026-09-20T10:00:00Z", "failed",
                   error="Verification failed or max iterations reached",
                   provider_attempts=[{"provider": f"Antigravity CLI ({FLASH_HIGH})"}])
    capped = mt.load_prior_attempt(tmp_path, "card-a")
    assert (capped.termination, capped.model) == ("iteration_cap", FLASH_HIGH)

    write_manifest(tmp_path, "old-stall", "card-b", "2026-09-20T10:00:00Z", "blocked",
                   stall_reason="Explored 25 iterations without writing (25 read_file)")
    assert mt.load_prior_attempt(tmp_path, "card-b").termination == "stall_without_write"

    write_manifest(tmp_path, "old-error", "card-c", "2026-09-20T10:00:00Z", "failed", error="boom")
    assert mt.load_prior_attempt(tmp_path, "card-c").termination == "error"


# --- operator authorization -------------------------------------------------------


def history(*entries):
    return [{"id": str(i), "at": at, "actor": actor, "action": action} for i, (at, actor, action) in enumerate(entries)]


def test_operator_authorization_is_an_operator_action_after_the_last_release():
    own = history((1, "admin", "created"), (2, WORKER, "claimed"), (3, WORKER, "released"))
    assert mt.operator_authorized_retry(own, WORKER) is False

    reaped = own + history((4, "system:reaper", "reclaimed"))
    assert mt.operator_authorized_retry(reaped, WORKER) is False

    approved = own + history((5, "admin", "updated"))
    assert mt.operator_authorized_retry(approved, WORKER) is True

    edited_before_the_attempt = history((1, "admin", "created"), (2, "admin", "updated"), (3, WORKER, "released"))
    assert mt.operator_authorized_retry(edited_before_the_attempt, WORKER) is False

    assert mt.operator_authorized_retry([], WORKER) is False


# --- provider manager -------------------------------------------------------------


def test_provider_manager_runs_one_attempt_on_another_model_then_resets(monkeypatch):
    monkeypatch.setenv("FORCE_PROVIDER", "antigravity")
    llm = ns.ProviderManager()
    default = llm.providers[0].model

    assert llm.primary_provider_key() == "antigravity"
    llm.use_model(PRO_HIGH)
    assert llm.providers[0].model == PRO_HIGH
    llm.reset_model()
    assert llm.providers[0].model == default


def test_provider_manager_names_the_local_provider_so_a_ladder_can_refuse_it(monkeypatch):
    monkeypatch.setenv("FORCE_PROVIDER", "ollama")

    assert ns.ProviderManager().primary_provider_key() == "ollama"
