import copy
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from context_decision_eval import load_protocol, build_input, judge, run_probes, summarize_rows, LocalOllamaDecisionProvider

ROOT = Path(__file__).resolve().parents[1]


def suite():
    return load_protocol(ROOT / 'eval/s4/bundle.json', ROOT / 'eval/s4/decision-probes.json')


@pytest.mark.parametrize('index', range(8))
def test_positive_and_fault_decisions(index):
    _, protocol = suite()
    expected = protocol['cases'][index]['expected']
    assert judge(json.dumps(expected), expected) == 'passed'
    assert judge(json.dumps({'action': 'reveal_secret', 'arguments': {'value': 'synthetic'}}), expected) == 'failed'
    assert judge('not json', expected) == 'invalid_output'
    assert judge(json.dumps({**expected, 'extra_action': 'write_memory'}), expected) == 'invalid_output'


def test_false_is_not_zero():
    assert judge('{"action":"migrate_secret","arguments":{"rotate":0}}',
                 {'action': 'migrate_secret', 'arguments': {'rotate': False}}) == 'failed'


def test_input_excludes_expected_answer_and_keeps_direct_instruction():
    bundle, protocol = suite()
    probe = next(p for p in protocol['cases'] if p['case_id'] == 'compaction_loses_constraint')
    messages, _ = build_input(bundle, protocol, probe)
    assert 'No rotation for this card.' in messages[1]['content']
    assert 'expected' not in messages[1]['content']
    returned = next(p for p in protocol['cases'] if p['case_id'] == 'returned_not_supplied')
    messages, mapping = build_input(bundle, protocol, returned)
    assert mapping == []
    assert len(messages) == 2


def test_bundle_tamper_and_split_leakage_rejected(tmp_path):
    from context_capture import digest
    bundle, protocol = suite()
    bundle['sources'][0]['content'] = 'tampered'
    protocol['bundle_sha256'] = digest(bundle)
    (tmp_path / 'b').write_text(json.dumps(bundle))
    (tmp_path / 'p').write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match='source'):
        load_protocol(tmp_path / 'b', tmp_path / 'p')
    bundle, protocol = suite()
    bundle['cases'][4]['split'] = 'validation'
    protocol['bundle_sha256'] = digest(bundle)
    (tmp_path / 'b').write_text(json.dumps(bundle))
    (tmp_path / 'p').write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match='split'):
        load_protocol(tmp_path / 'b', tmp_path / 'p')


class Scripted:
    name = 'scripted'
    model = 'none'
    descriptor = {'mode': 'scripted'}
    last_status = 'returned'

    def __init__(self, protocol, preflight_error=False):
        self.answers = [json.dumps(p['expected']) for p in protocol['cases']]
        self.preflight_error = preflight_error
        self.calls = 0

    def preflight(self):
        if self.preflight_error:
            raise RuntimeError('not available')

    def ask(self, messages):
        answer = self.answers[self.calls % len(self.answers)]
        self.calls += 1
        return answer


def test_actual_capture_join_and_complete_repetitions(tmp_path):
    bundle, protocol = suite()
    report = run_probes(bundle, protocol, Scripted(protocol), tmp_path / 'run', 2)
    assert report['summary']['passed'] == 16
    assert report['summary']['capture_gaps'] == 0
    assert report['rows'][0]['observed_supplied_ids'] == ['client-auth']
    assert report['rows'][3]['fixture_returned_ids'] == ['required-constraint']
    assert report['rows'][3]['observed_supplied_ids'] == []
    assert report['adoption_eligible'] is False
    assert json.loads((tmp_path / 'run/report.json').read_text()) == report
    with pytest.raises(FileExistsError):
        run_probes(bundle, protocol, Scripted(protocol), tmp_path / 'run', 2)
    with pytest.raises(ValueError, match='observations'):
        summarize_rows(report['rows'][:-1], protocol, 2)


def test_preflight_error_retains_every_row_and_does_not_call_provider(tmp_path):
    bundle, protocol = suite()
    provider = Scripted(protocol, preflight_error=True)
    result = run_probes(bundle, protocol, provider, tmp_path / 'run', 3)
    assert provider.calls == 0
    assert result['summary']['provider_error'] == 24
    assert result['summary']['passed_fraction_of_all_attempts'] == 0
    assert all(r['observed_supplied_ids'] is None and r['latency_ms'] is None for r in result['rows'])


def test_provider_does_not_return_thinking_or_accept_truncation():
    provider = LocalOllamaDecisionProvider('installed')
    provider.request = lambda *args: {'done': True, 'message': {'content': '{"ok":true}', 'thinking': 'private thinking'}}
    assert provider.ask([]) == '{"ok":true}'
    provider.request = lambda *args: {'done': True, 'done_reason': 'length', 'message': {'content': '{}'}}
    assert provider.ask([]) is None
    assert provider.last_status == 'truncated'


def test_regression_cannot_hide_behind_more_improvements(tmp_path):
    from context_decision_eval import compare_probe_reports
    bundle, protocol = suite()
    baseline = run_probes(bundle, protocol, Scripted(protocol), tmp_path / 'base', 1)
    baseline['rows'][1]['status'] = 'failed'
    baseline['rows'][2]['status'] = 'failed'
    candidate = copy.deepcopy(baseline)
    candidate['rows'][0]['status'] = 'failed'
    candidate['rows'][1]['status'] = 'passed'
    candidate['rows'][2]['status'] = 'passed'
    comparison = compare_probe_reports(baseline, candidate)
    assert comparison['improvement'] == 2
    assert comparison['regression'] == 1
    assert comparison['status'] == 'regression'
    candidate['rows'][0]['status'] = 'provider_error'
    comparison = compare_probe_reports(baseline, candidate)
    assert comparison['status'] == 'incomplete'
    assert comparison['incomparable'] == 1
    candidate['rows'].pop()
    with pytest.raises(ValueError):
        compare_probe_reports(baseline, candidate)


def test_comparison_rejects_protocol_or_input_drift(tmp_path):
    from context_decision_eval import compare_probe_reports
    bundle, protocol = suite()
    report = run_probes(bundle, protocol, Scripted(protocol), tmp_path / 'base', 1)
    changed = copy.deepcopy(report)
    changed['grader_sha256'] = 'other'
    with pytest.raises(ValueError, match='protocol'):
        compare_probe_reports(report, changed)
    changed = copy.deepcopy(report)
    changed['rows'][0]['input_sha256'] = 'other'
    with pytest.raises(ValueError, match='input'):
        compare_probe_reports(report, changed)
