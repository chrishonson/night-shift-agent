"""The bot PAT used to be embedded in every remote URL, which is how `git push`
authenticated. Removing it from .git/config takes that away, so the agent has to
supply the credential itself.

It cannot fall through to the machine's own credential helper: that helper is
`gh auth git-credential`, which answers as the human account. A push authorised
that way is the wrong identity holding the write, and the bot account stops
being the boundary it exists to be. So the helper is disabled for the agent's
own git commands and GIT_ASKPASS answers in its place.
"""
import os
import stat

import pytest
import agent_night_shift as ns


class subprocess_result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def bot_token(monkeypatch):
    """Force the .env path. This stubs access_secret rather than subprocess.run,
    because ns.subprocess is the shared module object and patching it would also
    replace the subprocess this file uses to execute the askpass script."""
    ns.reset_gh_token_cache()
    monkeypatch.setenv("GH_BOT_TOKEN", "bot-pat")
    monkeypatch.setattr(ns, "access_secret", lambda name: None)
    yield
    ns.reset_gh_token_cache()


@pytest.fixture
def ran(monkeypatch):
    """Capture the env a command would have been given, without running it."""
    seen = {}
    toolbox = ns.Toolbox(ns.BuildState())

    def fake_exec(command, env=None, **kwargs):
        seen["command"] = command
        seen["env"] = env
        return subprocess_result(0, "ok")

    monkeypatch.setattr(toolbox, "exec_command", fake_exec)
    # A bare `git push` is judged by the checkout's branch, and the repo under test is
    # this one, which is on main. Pin a card branch so the tests do not depend on it.
    monkeypatch.setattr(toolbox, "_current_branch", lambda: "nightshift/abc123")
    seen["toolbox"] = toolbox
    return seen


# --- a git command gets the bot credential ----------------------------------

def test_a_git_push_is_given_an_askpass(ran):
    ran["toolbox"].run_shell("git push origin nightshift/abc123")

    assert ran["env"]["GIT_ASKPASS"]
    assert os.path.exists(ran["env"]["GIT_ASKPASS"])


def test_the_machines_own_helper_is_disabled_for_git(ran):
    """Left enabled, `gh auth git-credential` answers as the human account and
    the bot's push is authorised by the wrong identity."""
    ran["toolbox"].run_shell("git push origin nightshift/abc123")

    assert ran["env"]["GIT_CONFIG_COUNT"] == "1"
    assert ran["env"]["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert ran["env"]["GIT_CONFIG_VALUE_0"] == ""


def test_git_is_never_left_waiting_on_a_terminal_prompt(ran):
    """The agent is unattended. A prompt would hang the run until the lease dies."""
    ran["toolbox"].run_shell("git fetch origin")

    assert ran["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_the_askpass_answers_with_the_bot_identity(ran):
    ran["toolbox"].run_shell("git push")

    assert ran["env"]["GIT_BOT_USERNAME"] == "agentnightshift"
    assert ran["env"]["GIT_BOT_TOKEN"] == "bot-pat"


def test_the_askpass_script_is_not_readable_by_other_users(ran):
    ran["toolbox"].run_shell("git push")

    mode = os.stat(ran["env"]["GIT_ASKPASS"]).st_mode
    assert not mode & stat.S_IRWXG
    assert not mode & stat.S_IRWXO


def test_the_secret_is_not_written_into_the_script(ran):
    """The token travels in the environment. A script file holding it would be a
    new copy of the credential on disk, which is the thing being removed."""
    ran["toolbox"].run_shell("git push")

    assert "bot-pat" not in open(ran["env"]["GIT_ASKPASS"]).read()


def test_the_script_answers_username_then_password():
    import subprocess as sp
    path = ns.git_askpass_script()
    env = {**os.environ, "GIT_BOT_USERNAME": "agentnightshift", "GIT_BOT_TOKEN": "bot-pat"}

    user = sp.run([path, "Username for 'https://github.com': "],
                  capture_output=True, text=True, env=env).stdout.strip()
    password = sp.run([path, "Password for 'https://agentnightshift@github.com': "],
                      capture_output=True, text=True, env=env).stdout.strip()

    assert user == "agentnightshift"
    assert password == "bot-pat"


# --- everything else is left alone ------------------------------------------

def test_a_non_git_command_gets_no_askpass(ran):
    ran["toolbox"].run_shell("ls -la")

    assert "GIT_ASKPASS" not in ran["env"]
    assert "GIT_BOT_TOKEN" not in ran["env"]


def test_a_command_merely_mentioning_git_is_not_treated_as_git(ran):
    ran["toolbox"].run_shell("echo git push")

    assert "GIT_ASKPASS" not in ran["env"]


def test_no_token_means_no_askpass(ran, monkeypatch):
    """Without a credential there is nothing to answer with, and a half-set
    askpass would fail the push with a confusing empty-password error."""
    monkeypatch.delenv("GH_BOT_TOKEN", raising=False)
    ns.reset_gh_token_cache()

    ran["toolbox"].run_shell("git push")

    assert "GIT_ASKPASS" not in ran["env"]


def test_gh_still_gets_its_own_token(ran):
    """The gh path is unchanged: gh reads GITHUB_TOKEN, not askpass."""
    ran["toolbox"].run_shell("gh api user")

    assert ran["env"]["GITHUB_TOKEN"] == "bot-pat"
