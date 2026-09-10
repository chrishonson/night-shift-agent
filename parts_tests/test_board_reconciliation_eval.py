import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from board_reconciliation_eval import (
    load_fixtures,
    build_case_input,
    judge_reconciliation,
    compute_scorecard,
    run_reconciliation_eval,
    ScriptedReconciliationProvider,
    write_json,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_PATH = ROOT / "eval/s4/reconciliation-fixtures.json"


def test_load_fixtures_valid():
    fixtures = load_fixtures(FIXTURES_PATH)
    assert fixtures["schema_version"] == "board-reconciliation-benchmark.v1"
    assert fixtures["benchmark_id"] == "s4-board-reconciliation-v1"
    assert len(fixtures["cases"]) == 10

    # Ensure exactly 5 baseline and 5 held_out cases
    baseline = [c for c in fixtures["cases"] if c["split"] == "baseline"]
    held_out = [c for c in fixtures["cases"] if c["split"] == "held_out"]
    assert len(baseline) == 5
    assert len(held_out) == 5

    # Check that all 5 archetypes are covered in both baseline and held_out
    baseline_archetypes = {c["archetype"] for c in baseline}
    held_out_archetypes = {c["archetype"] for c in held_out}
    assert len(baseline_archetypes) == 5
    assert len(held_out_archetypes) == 5


def test_load_fixtures_validation_errors(tmp_path):
    fixtures = load_fixtures(FIXTURES_PATH)

    # 1. Invalid schema_version
    bad = copy.deepcopy(fixtures)
    bad["schema_version"] = "invalid.v1"
    p = tmp_path / "f1.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="Unsupported schema_version"):
        load_fixtures(p)

    # 2. Missing system_prompt
    bad = copy.deepcopy(fixtures)
    del bad["system_prompt"]
    p = tmp_path / "f2.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="benchmark_id or system_prompt"):
        load_fixtures(p)

    # 3. Duplicate case_id
    bad = copy.deepcopy(fixtures)
    bad["cases"][1]["case_id"] = bad["cases"][0]["case_id"]
    p = tmp_path / "f3.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="Duplicate or empty case_id"):
        load_fixtures(p)

    # 4. Invalid split
    bad = copy.deepcopy(fixtures)
    bad["cases"][0]["split"] = "invalid_split"
    p = tmp_path / "f4.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="Invalid split"):
        load_fixtures(p)

    # 5. Incident crosses splits
    bad = copy.deepcopy(fixtures)
    bad["cases"][1]["incident_id"] = bad["cases"][0]["incident_id"]  # case 0 is baseline, case 1 is held_out
    p = tmp_path / "f5.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="crosses splits"):
        load_fixtures(p)

    # 6. Invalid archetype
    bad = copy.deepcopy(fixtures)
    bad["cases"][0]["archetype"] = "unknown_archetype"
    p = tmp_path / "f6.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="Invalid archetype"):
        load_fixtures(p)

    # 7. Invalid expected action
    bad = copy.deepcopy(fixtures)
    bad["cases"][0]["expected"]["action"] = "illegal_action"
    p = tmp_path / "f7.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="Invalid expected action"):
        load_fixtures(p)

    # 8. Empty cases
    bad = copy.deepcopy(fixtures)
    bad["cases"] = []
    p = tmp_path / "f8.json"
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="contain no cases"):
        load_fixtures(p)


def test_build_case_input():
    fixtures = load_fixtures(FIXTURES_PATH)
    case = fixtures["cases"][0]
    messages = build_case_input(fixtures, case)

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == fixtures["system_prompt"]
    assert messages[1]["role"] == "user"

    payload = json.loads(messages[1]["content"])
    assert payload["case_id"] == case["case_id"]
    assert payload["scenario"] == case["scenario"]
    assert "expected" not in payload


def test_judge_reconciliation_pass():
    fixtures = load_fixtures(FIXTURES_PATH)
    for case in fixtures["cases"]:
        expected = case["expected"]
        result = judge_reconciliation(
            json.dumps(expected),
            expected,
            case_archetype=case["archetype"]
        )
        assert result["status"] == "passed"
        assert result["failure_category"] is None
        assert result["parsed"] == expected


def test_judge_reconciliation_malformed():
    expected = {"action": "reconcile_card", "arguments": {"card_id": "c1"}}

    assert judge_reconciliation("not a json", expected)["status"] == "invalid_output"
    assert judge_reconciliation(12345, expected)["status"] == "invalid_output"
    assert judge_reconciliation(json.dumps({"action": "foo"}), expected)["status"] == "invalid_output"
    assert judge_reconciliation(json.dumps({"action": "foo", "arguments": "not_dict"}), expected)["status"] == "invalid_output"


def test_judge_reconciliation_failure_categories():
    # 1. False completion (marking unintegrated branch done)
    exp = {
        "action": "reject_completion",
        "arguments": {"card_id": "card-22", "recommended_action": "branch_integration"}
    }
    candidate = {
        "action": "reconcile_card",
        "arguments": {"card_id": "card-22", "target_state": "done"}
    }
    res = judge_reconciliation(json.dumps(candidate), exp, case_archetype="branch_not_integrated")
    assert res["status"] == "failed"
    assert res["failure_category"] == "false_completion"

    # 2. Unnecessary rework (ignoring existing artifact)
    exp_art = {
        "action": "reconcile_card",
        "arguments": {"card_id": "card-24", "target_state": "done"}
    }
    cand_rework = {
        "action": "reject_completion",
        "arguments": {"card_id": "card-24", "recommended_action": "reopen_card"}
    }
    res_art = judge_reconciliation(json.dumps(cand_rework), exp_art, case_archetype="artifact_exists_after_failed_attempt")
    assert res_art["status"] == "failed"
    assert res_art["failure_category"] == "unnecessary_rework"

    # 3. Instruction ignored (failing to reconcile under override)
    exp_override = {
        "action": "reconcile_card",
        "arguments": {"card_id": "card-45", "target_state": "abandoned"}
    }
    cand_push = {
        "action": "reject_completion",
        "arguments": {"card_id": "card-45", "recommended_action": "push_pr"}
    }
    res_override = judge_reconciliation(json.dumps(cand_push), exp_override, case_archetype="stale_instruction_override")
    assert res_override["status"] == "failed"
    assert res_override["failure_category"] == "instruction_ignored"

    # 4. Argument mismatch in reconcile_card
    cand_mismatch_state = {
        "action": "reconcile_card",
        "arguments": {"card_id": "card-45", "target_state": "done"}
    }
    res_mismatch = judge_reconciliation(json.dumps(cand_mismatch_state), exp_override, case_archetype="stale_instruction_override")
    assert res_mismatch["status"] == "failed"
    assert res_mismatch["failure_category"] == "argument_mismatch"

    # 5. Evidence mismatch
    exp_evidence = {
        "action": "reconcile_card",
        "arguments": {
            "card_id": "card-45",
            "target_state": "abandoned",
            "evidence": {"policy_override": "do_not_push_origin"}
        }
    }
    cand_bad_ev = {
        "action": "reconcile_card",
        "arguments": {
            "card_id": "card-45",
            "target_state": "abandoned",
            "evidence": {"policy_override": "wrong_value"}
        }
    }
    res_ev = judge_reconciliation(json.dumps(cand_bad_ev), exp_evidence)
    assert res_ev["status"] == "failed"
    assert res_ev["failure_category"] == "argument_mismatch"

    # 6. Funnel misattribution
    exp_funnel = {
        "action": "diagnose_funnel",
        "arguments": {"card_id": "card-30", "stage_of_loss": "returned"}
    }
    cand_funnel_wrong = {
        "action": "diagnose_funnel",
        "arguments": {"card_id": "card-30", "stage_of_loss": "stored"}
    }
    res_fn = judge_reconciliation(json.dumps(cand_funnel_wrong), exp_funnel)
    assert res_fn["status"] == "failed"
    assert res_fn["failure_category"] == "funnel_misattribution"


def test_compute_scorecard_empty():
    sc = compute_scorecard([])
    assert sc["total_observations"] == 0
    assert sc["pass_rate"] == 0.0


def test_scripted_provider_and_eval_run(tmp_path):
    fixtures = load_fixtures(FIXTURES_PATH)
    provider = ScriptedReconciliationProvider(fixtures)

    report = run_reconciliation_eval(
        fixtures,
        provider,
        output_dir=tmp_path / "eval_out",
        repetitions=2
    )

    sc = report["scorecard"]
    assert sc["total_observations"] == 20
    assert sc["passed"] == 20
    assert sc["failed"] == 0
    assert sc["invalid_output"] == 0
    assert sc["pass_rate"] == 1.0
    assert sc["splits"]["baseline"]["pass_rate"] == 1.0
    assert sc["splits"]["held_out"]["pass_rate"] == 1.0

    # Verify metrics
    metrics = sc["metrics"]
    assert metrics["false_completion_rate"] == 0.0
    assert metrics["unnecessary_rework_rate"] == 0.0
    assert metrics["instruction_override_fidelity"] == 1.0
    assert metrics["capability_identification_accuracy"] == 1.0
    assert metrics["funnel_diagnosis_accuracy"] == 1.0

    # Check output report file
    report_file = tmp_path / "eval_out/report.json"
    assert report_file.exists()
    loaded = json.loads(report_file.read_text())
    assert loaded["schema_version"] == "board-reconciliation-run.v1"
    assert len(loaded["rows"]) == 20


def test_fault_modes(tmp_path):
    fixtures = load_fixtures(FIXTURES_PATH)

    # 1. False completion fault mode
    prov_fc = ScriptedReconciliationProvider(fixtures, fault_mode="false_completion")
    rep_fc = run_reconciliation_eval(fixtures, prov_fc, repetitions=1)
    sc_fc = rep_fc["scorecard"]
    assert sc_fc["metrics"]["false_completion_rate"] > 0.0
    assert sc_fc["metrics"]["capability_identification_accuracy"] < 1.0

    # 2. Rework fault mode
    prov_rw = ScriptedReconciliationProvider(fixtures, fault_mode="rework")
    rep_rw = run_reconciliation_eval(fixtures, prov_rw, repetitions=1)
    sc_rw = rep_rw["scorecard"]
    assert sc_rw["metrics"]["unnecessary_rework_rate"] > 0.0

    # 3. Malformed output fault mode
    prov_mf = ScriptedReconciliationProvider(fixtures, fault_mode="malformed")
    rep_mf = run_reconciliation_eval(fixtures, prov_mf, repetitions=1)
    sc_mf = rep_mf["scorecard"]
    assert sc_mf["invalid_output"] == 10
    assert sc_mf["pass_rate"] == 0.0


def test_provider_exception_handling():
    fixtures = load_fixtures(FIXTURES_PATH)

    class BrokenProvider:
        descriptor = {"mode": "broken"}
        def ask(self, messages):
            raise ConnectionError("Network down")

    rep = run_reconciliation_eval(fixtures, BrokenProvider(), repetitions=1)
    assert rep["scorecard"]["invalid_output"] == 10
    assert rep["rows"][0]["failure_category"] == "provider_exception"

from board_reconciliation_eval import main


def test_cli_dry_run(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "board_reconciliation_eval.py",
            "--fixtures",
            str(FIXTURES_PATH),
            "--dry-run",
            "--output-dir",
            str(tmp_path / "cli_out"),
        ],
    )
    main()
    captured = capsys.readouterr()
    assert "S4 Board Reconciliation Scorecard" in captured.out
    assert "Pass Rate: 100.0%" in captured.out
    assert (tmp_path / "cli_out/report.json").exists()


def test_cli_no_provider(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "board_reconciliation_eval.py",
            "--fixtures",
            str(FIXTURES_PATH),
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        main()
    assert excinfo.value.code == 1
