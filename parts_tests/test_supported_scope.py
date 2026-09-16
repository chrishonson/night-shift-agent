"""Supported worker boundaries, independent of archived research tooling."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent_night_shift as ns
from run_record import evaluate_criteria


def test_missing_contract_never_launches_a_build(tmp_path, monkeypatch):
    tool = ns.Toolbox(ns.BuildState(), project_dir=tmp_path)
    monkeypatch.setattr(tool, 'exec_command', lambda *a, **kw: pytest.fail('implicit build launched'))
    assert 'verification.json' in tool.run_tests()
    assert 'verification.json' in tool.verify_build()
    assert tool.build_state.build_passed is False


def test_empty_acceptance_is_unknown_not_passed(tmp_path):
    assert evaluate_criteria([], tmp_path, '', [])['passed'] is False


def test_nonsoftware_never_calls_a_model_or_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, 'resolve_gh_token', lambda: None)
    monkeypatch.chdir(tmp_path)
    agent = ns.NightShiftAgent(tmp_path, token='test')
    agent.llm.ask = lambda *a: pytest.fail('planning model invoked')
    agent.toolbox.dispatch = lambda *a: pytest.fail('coding tool invoked')
    outcome, gates, artifacts, error = agent.execute_card(
        {'id':'plan', 'kind':'task'}, 'unused', SimpleNamespace(abandoned=False))
    assert outcome == 'blocked'
    assert 'coordinator' in error
    assert gates == [] and artifacts is None


def test_wrong_repository_never_changes_another_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(ns, 'resolve_gh_token', lambda: None)
    monkeypatch.chdir(tmp_path)
    agent = ns.NightShiftAgent(tmp_path, token='test')
    agent.run_cmd_quiet = lambda *a: pytest.fail('git launched')
    outcome, _, _, error = agent.execute_card(
        {'id':'code','kind':'software','repo':'a-different-repo','gate_ids':['unit']},
        'unused', SimpleNamespace(abandoned=False))
    assert outcome == 'blocked' and 'project-dir' in error


def test_runtime_has_no_archived_module_dependency():
    import ast
    root = Path(ns.__file__).parent
    retired={'context_decision_eval','board_reconciliation_eval'}
    for name in ['agent_night_shift.py','context_capture.py','run_record.py']:
        tree=ast.parse((root/name).read_text())
        modules=[]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): modules.extend(n.name for n in node.names)
            elif isinstance(node, ast.ImportFrom): modules.append(node.module)
        assert not retired.intersection(modules)


def test_record_keeps_changes_after_local_commit_and_excludes_logs(tmp_path, monkeypatch):
    import subprocess
    from run_record import RunRecord
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, capture_output=True, text=True, check=True)
    git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
    (tmp_path/'code.py').write_text('value=1\n')
    git('add', '.'); git('commit', '-m', 'baseline')
    record=RunRecord(tmp_path.parent/(tmp_path.name+'-record'), {'id':'fixture'}, workspace_dir=tmp_path)
    (tmp_path/'code.py').write_text('value=2\n')
    (tmp_path/'.agent_logs').mkdir()
    (tmp_path/'.agent_logs'/'private.log').write_text('private diagnostic content')
    monkeypatch.setattr(ns, 'resolve_gh_token', lambda: None)
    monkeypatch.chdir(tmp_path)
    agent=ns.NightShiftAgent(tmp_path, token='test')
    assert agent.commit_changes('literal $(not-a-command)')
    assert '.agent_logs' not in git('ls-files').stdout
    record.finalize('succeeded', workspace_path=tmp_path)
    patch=(record.output_dir/'patch.diff').read_text()
    assert '+value=2' in patch and '-value=1' in patch
    assert 'private diagnostic content' not in patch


def test_empty_repos_flag_still_claims_only_the_selected_repository(tmp_path, monkeypatch):
    """`--repos` with no values must not widen the claim to the whole board.

    An unscoped claim resolves against the identity, which is `*`, so the worker
    would be handed task cards and other repositories' cards it cannot execute.
    """
    monkeypatch.setattr(ns, 'resolve_gh_token', lambda: None)
    monkeypatch.chdir(tmp_path)
    target = tmp_path / 'sample-target'
    target.mkdir()
    agent = ns.NightShiftAgent(target, token='test')
    seen = {}

    def record_claim(lane=ns.DEFAULT_LANE, resources=None, repos=None):
        seen['repos'] = repos
        return None

    agent.control_plane.claim = record_claim

    agent.run_control_plane(until_empty=True, repos=[])
    assert seen['repos'] == ['sample-target']

    agent.run_control_plane(until_empty=True, repos=None)
    assert seen['repos'] == ['sample-target']
