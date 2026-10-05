"""The bot PAT used to live only in .env, so rotating it meant editing a file on
every machine that runs the agent, and a stale copy on disk silently kept
working. Secret Manager is now the source of truth and .env is the offline
fallback, which means the ordering below is the whole point: a rotated secret
must win over a stale file.
"""
import pytest
import agent_night_shift as ns


class subprocess_result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def no_cached_token():
    ns.reset_gh_token_cache()
    yield
    ns.reset_gh_token_cache()


@pytest.fixture
def secret_manager(monkeypatch):
    """Stand in for gcloud. Records the command so the secret name is checked."""
    calls = []

    def install(stdout=None, returncode=0, raises=None):
        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if raises:
                raise raises
            return subprocess_result(returncode, stdout or "")
        monkeypatch.setattr(ns.subprocess, "run", fake_run)
        return calls

    return install


# --- ordering: the rotated secret beats the stale file ----------------------

def test_secret_manager_wins_over_a_stale_env_var(monkeypatch, secret_manager):
    monkeypatch.setenv("GH_BOT_TOKEN", "stale-from-dotenv")
    secret_manager(stdout="rotated-in-secret-manager\n")

    assert ns.resolve_gh_token() == "rotated-in-secret-manager"


def test_the_env_var_is_used_when_secret_manager_is_unreachable(monkeypatch, secret_manager):
    monkeypatch.setenv("GH_BOT_TOKEN", "  from-dotenv  ")
    secret_manager(raises=OSError("no gcloud on this host"))

    assert ns.resolve_gh_token() == "from-dotenv"


def test_the_env_var_is_used_when_the_secret_does_not_exist(monkeypatch, secret_manager):
    monkeypatch.setenv("GH_BOT_TOKEN", "from-dotenv")
    secret_manager(returncode=1, stdout="")

    assert ns.resolve_gh_token() == "from-dotenv"


def test_no_token_anywhere_resolves_to_none(monkeypatch, secret_manager):
    monkeypatch.delenv("GH_BOT_TOKEN", raising=False)
    secret_manager(returncode=1)

    assert ns.resolve_gh_token() is None


# --- which secret is read ---------------------------------------------------

def test_the_default_secret_is_the_agentnightshift_pat(monkeypatch, secret_manager):
    monkeypatch.delenv("GH_BOT_TOKEN_SECRET", raising=False)
    calls = secret_manager(stdout="t")

    ns.resolve_gh_token()

    assert "--secret=gh-bot-token-agentnightshift" in calls[0]
    assert "--project=my-brain-88870" in calls[0]


def test_a_second_bot_can_point_at_its_own_secret(monkeypatch, secret_manager):
    monkeypatch.setenv("GH_BOT_TOKEN_SECRET", "gh-bot-token-someoneelse")
    calls = secret_manager(stdout="t")

    ns.resolve_gh_token()

    assert "--secret=gh-bot-token-someoneelse" in calls[0]


# --- the cache keeps gh commands from paying for a subprocess each time ------

def test_the_token_is_fetched_once_and_reused(monkeypatch, secret_manager):
    monkeypatch.delenv("GH_BOT_TOKEN", raising=False)
    calls = secret_manager(stdout="t")

    ns.resolve_gh_token()
    ns.resolve_gh_token()
    ns.resolve_gh_token()

    assert len(calls) == 1


def test_an_absent_token_is_cached_too_so_a_missing_gcloud_is_not_retried(monkeypatch, secret_manager):
    monkeypatch.delenv("GH_BOT_TOKEN", raising=False)
    calls = secret_manager(returncode=1)

    assert ns.resolve_gh_token() is None
    assert ns.resolve_gh_token() is None

    assert len(calls) == 1


# --- the resolved token reaches the gh subprocess ---------------------------

def test_a_gh_command_is_given_the_resolved_token(monkeypatch, secret_manager):
    """run_shell injects GITHUB_TOKEN so `gh` authenticates as the bot."""
    monkeypatch.delenv("GH_BOT_TOKEN", raising=False)
    secret_manager(stdout="resolved-token")
    seen = {}

    toolbox = ns.Toolbox(ns.BuildState())

    def fake_exec(command, env=None, **kwargs):
        seen["env"] = env
        return subprocess_result(0, "ok")

    monkeypatch.setattr(toolbox, "exec_command", fake_exec)
    toolbox.run_shell("gh api user")

    assert seen["env"]["GITHUB_TOKEN"] == "resolved-token"


def test_a_non_gh_command_is_not_given_the_token(monkeypatch, secret_manager):
    monkeypatch.delenv("GH_BOT_TOKEN", raising=False)
    secret_manager(stdout="resolved-token")
    seen = {}

    toolbox = ns.Toolbox(ns.BuildState())

    def fake_exec(command, env=None, **kwargs):
        seen["env"] = env
        return subprocess_result(0, "ok")

    monkeypatch.setattr(toolbox, "exec_command", fake_exec)
    toolbox.run_shell("ls -la")

    assert "GITHUB_TOKEN" not in seen["env"]


# --- the secret is never written down ---------------------------------------

def test_the_token_is_not_logged(monkeypatch, secret_manager, caplog):
    monkeypatch.delenv("GH_BOT_TOKEN", raising=False)
    secret_manager(stdout="super-secret-value")

    with caplog.at_level("DEBUG"):
        ns.resolve_gh_token()

    assert "super-secret-value" not in caplog.text
