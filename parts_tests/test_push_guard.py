"""The agent holds a bot token with write access and run_shell will execute
whatever the model emits. Branch protection is unavailable on a private repo
under a free GitHub account, so this guard is the only thing standing between a
hallucinated `git push origin main` and main itself.
"""
import pytest
import agent_night_shift as ns


def blocked(command, branch="nightshift/abc123"):
    return ns.blocked_push_target(command, current_branch=lambda: branch)


# --- the legitimate path stays open -----------------------------------------

def test_a_card_branch_push_is_allowed():
    assert blocked("git push -u origin nightshift/abc123") is None


def test_a_branch_merely_named_like_main_is_allowed():
    assert blocked("git push origin feature/main") is None


def test_a_bare_push_from_a_card_branch_is_allowed():
    assert blocked("git push") is None


def test_commands_that_are_not_pushes_are_untouched():
    assert blocked("./gradlew testDebugUnitTest --no-daemon") is None
    assert blocked("git commit -m 'push to main'") is None


# --- the protected branches are closed --------------------------------------

@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push -u origin master",
    "git push --force origin main",
    "git push --force-with-lease origin main",
    "git push origin HEAD:main",
    "git push origin +main",
    "git push origin main:main",
    "git push origin refs/heads/main",
    "git push --delete origin main",
    "git -C /Users/nick/git/flashy-card push origin main",
])
def test_a_push_to_a_protected_branch_is_refused(command):
    assert blocked(command) == "main" or blocked(command) == "master"


def test_a_push_hidden_in_a_compound_command_is_refused():
    assert blocked("cd /tmp/repo && git push origin main") == "main"
    assert blocked("git add -A; git push origin main") == "main"


def test_a_bare_push_while_standing_on_main_is_refused():
    assert blocked("git push", branch="main") == "main"
    assert blocked("git push origin", branch="master") == "master"


def test_pushing_every_branch_at_once_is_refused():
    assert blocked("git push --all origin") is not None
    assert blocked("git push --mirror origin") is not None


# --- and run_shell actually enforces it -------------------------------------

def test_run_shell_refuses_without_executing(monkeypatch, tmp_path):
    tb = ns.Toolbox(ns.BuildState(), project_dir=str(tmp_path))
    executed = []
    monkeypatch.setattr(tb, "exec_command", lambda *a, **k: executed.append(a))

    out = tb.run_shell("git push origin main")

    assert "protected" in out.lower()
    assert executed == []
