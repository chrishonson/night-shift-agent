"""Observable context delivery, capture gaps, and real-loop synthetic replay."""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_night_shift as ns
from context_capture import ContextCapture, capture_call, digest, summarize_trace


def events(capture):
    return [json.loads(line) for line in capture.path.read_text().splitlines()]


class Provider(ns.LLMProvider):
    name = 'scripted'

    def __init__(self, error=False):
        self.error = error

    def ask(self, messages):
        if self.error:
            raise ns.QuotaExceededError('secret error text')
        return 'secret response text'


def test_failover_records_each_boundary_without_payloads(tmp_path):
    capture = ContextCapture(tmp_path, {'id': 'card', 'goal': 'secret goal', 'lease': 'lease-secret'})
    manager = ns.ProviderManager()
    manager.providers = [Provider(error=True), Provider()]
    manager.context_capture = capture
    assert manager.ask([{'role': 'user', 'content': 'secret prompt'}]) == 'secret response text'
    capture.finish('failed', 'unknown')
    rows = events(capture)
    assert [e['data']['status'] for e in rows if e['kind'] == 'provider_call_finished'] == ['quota_error', 'returned']
    calls = [e['data'] for e in rows if e['kind'] == 'context_assembled']
    assert len(calls) == 2
    assert calls[0]['ordered_sources'] == calls[1]['ordered_sources']
    assert calls[0]['decision_id'] != calls[1]['decision_id']
    assert 'secret' not in capture.path.read_text()
    assert capture.path.stat().st_mode & 0o777 == 0o600


def test_capture_io_failure_does_not_change_model_result(tmp_path):
    blocked = tmp_path / 'file'
    blocked.write_text('not a directory')
    capture = ContextCapture(blocked, {})
    manager = ns.ProviderManager()
    manager.providers = [Provider()]
    manager.context_capture = capture
    assert manager.ask('prompt') == 'secret response text'
    capture.finish('failed', 'unknown')
    assert capture.failed


@pytest.mark.parametrize('limits', [{'max_events': 2}, {'max_bytes': 4096}])
def test_overflow_stays_bounded_and_is_never_complete(tmp_path, limits):
    capture = ContextCapture(tmp_path, {}, **limits)
    for _ in range(100):
        capture.assembled([{'role': 'user', 'content': 'x'}], Provider(), 1)
    capture.finish('succeeded', 'acknowledged')
    capture.finish('succeeded', 'acknowledged')
    assert len(events(capture)) <= capture.max_events
    assert capture.path.stat().st_size <= capture.max_bytes
    assert summarize_trace(capture.path)['complete'] is False
    assert events(capture)[-1]['data']['dropped_events'] > 0


def test_crash_or_missing_terminal_is_not_a_pass(tmp_path):
    capture = ContextCapture(tmp_path, {})
    capture.stream.close()
    report = summarize_trace(capture.path)
    assert report['complete'] is False
    assert report['release_status'] == 'unknown'
    assert report['agent_quality'] == 'not_evaluated'


def test_capture_bug_does_not_trigger_provider_failover(tmp_path):
    capture = ContextCapture(tmp_path, {})
    capture.assembled = lambda *args: (_ for _ in ()).throw(ValueError('bug'))
    manager = ns.ProviderManager()
    manager.providers = [Provider()]
    manager.context_capture = capture
    assert manager.ask('x') == 'secret response text'
    assert capture.failed
    capture.finish('failed', 'unknown')
    assert not summarize_trace(capture.path)['complete']


def test_real_worker_replay_exposes_pruning_loss(tmp_path):
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'context-eval.py'
    spec = importlib.util.spec_from_file_location('context_eval', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.replay(tmp_path)
    retained, pruned = result['results']
    assert retained['required_source_at_final_boundary'] is True
    assert pruned['required_source_at_final_boundary'] is False
    assert retained['summary']['candidates'] == []
    assert result['required_source_id'] in pruned['summary']['candidates'][0]['omitted_source_ids']
    assert all(r['summary']['provider_calls'] == 3 and r['summary']['complete'] for r in result['results'])
    assert result['real_model_run'] is False
    assert result['agent_quality'] == 'not_evaluated'


def test_report_rejects_mixed_or_reordered_events(tmp_path):
    capture = ContextCapture(tmp_path, {})
    capture.finish('failed', 'unknown')
    lines = capture.path.read_text().splitlines()
    capture.path.write_text('\n'.join(reversed(lines)))
    with pytest.raises(ValueError):
        summarize_trace(capture.path)


@pytest.mark.parametrize('release_fails', [False, True])
def test_control_plane_capture_finalizes_without_exporting_lease(tmp_path, monkeypatch, release_fails):
    monkeypatch.setenv('S4_CONTEXT_CAPTURE_DIR', str(tmp_path / 'capture'))
    original = Path.cwd()
    try:
        agent = ns.NightShiftAgent(str(tmp_path), token='secret-token')
        agent.detect_resources = lambda: []
        agent.control_plane.claim = lambda **kw: {'card': {'id': 'c', 'goal': 'secret goal'}, 'run_id': 'lease-secret'}
        agent.execute_card = lambda *args: ('failed', [], None, 'secret error')

        def release(**kwargs):
            if release_fails:
                raise RuntimeError('secret release error')
            return {}

        agent.control_plane.release = release
        agent.run_control_plane(max_runs=1)
        trace = next((tmp_path / 'capture').glob('*.jsonl'))
        assert 'secret' not in trace.read_text()
        report = summarize_trace(trace)
        assert report['complete'] is True
        assert report['release_status'] == ('unknown' if release_fails else 'acknowledged')
        assert agent.llm.context_capture is None
    finally:
        os.chdir(original)


def test_capture_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv('S4_CONTEXT_CAPTURE_DIR', raising=False)
    original = Path.cwd()
    try:
        agent = ns.NightShiftAgent(str(tmp_path), token='unused')
        agent.detect_resources = lambda: []
        agent.control_plane.claim = lambda **kw: {'card': {'id': 'c'}, 'run_id': 'r'}
        agent.control_plane.release = lambda **kw: {}
        agent.execute_card = lambda *args: ('failed', [], None, None)
        agent.run_control_plane(max_runs=1)
        assert not list(tmp_path.glob('*.jsonl'))
        assert agent.llm.context_capture is None
    finally:
        os.chdir(original)


def test_duplicate_message_omission_is_not_hidden(tmp_path):
    capture = ContextCapture(tmp_path, {})
    message = {'role': 'user', 'content': 'same content'}
    capture.transformed([message, message], [message], 100)
    capture.finish('failed', 'unknown')
    assert summarize_trace(capture.path)['candidates'][0]['omitted_source_ids'] == [digest(['user', 'same content'])]


def test_cli_replay_stdout_is_json(tmp_path):
    import subprocess
    script = Path(__file__).resolve().parents[1] / 'scripts' / 'context-eval.py'
    completed = subprocess.run([sys.executable, str(script), 'replay', '--output-dir', str(tmp_path)],
                               capture_output=True, text=True, check=True)
    result = json.loads(completed.stdout)
    assert result['results'][0]['required_source_at_final_boundary'] is True
    assert result['results'][1]['required_source_at_final_boundary'] is False
