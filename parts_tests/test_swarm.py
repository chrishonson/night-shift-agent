"""Tests for the swarm worker surface: control plane transport, lease
maintenance, per-card token accounting, and the usage/local-inference loop.

Only two things are faked: the urllib transport underneath the JSON-RPC
client, and the LLM providers. The control plane client, the heartbeat
worker, the provider manager and the agent are all the real objects.
"""

import io
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent_night_shift as ns


# ---------------------------------------------------------------------------
# Transport fake: the network boundary, and nothing above it
# ---------------------------------------------------------------------------

class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class McpError:
    """Marks a scripted reply that should come back as a JSON-RPC error object."""

    def __init__(self, message):
        self.message = message


def mcp_body(payload):
    """A well-formed JSON-RPC body carrying an MCP text content block."""
    return json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]}
    })


def sse_body(payload):
    """The same body in the SSE framing the deployed control plane actually emits."""
    return "event: message\ndata: " + mcp_body(payload) + "\n\n"


class PlaneStub:
    """Stands in for urllib.request.urlopen: routes tool calls to canned replies."""

    def __init__(self, **handlers):
        self.handlers = dict(handlers)
        self.calls = []       # (tool_name, arguments)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        body = json.loads(req.data.decode("utf-8"))
        tool = body["params"]["name"]
        args = body["params"]["arguments"]
        self.calls.append((tool, args))

        reply = self.handlers.get(tool, {})
        if callable(reply):
            reply = reply(args)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, McpError):
            return FakeResponse(json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "error": {"code": -32000, "message": reply.message}
            }).encode("utf-8"))
        if isinstance(reply, str):
            return FakeResponse(reply.encode("utf-8"))
        return FakeResponse(mcp_body(reply).encode("utf-8"))

    def tools_called(self):
        return [name for name, _ in self.calls]

    def count(self, tool):
        return sum(1 for name, _ in self.calls if name == tool)

    def args_for(self, tool):
        return [args for name, args in self.calls if name == tool]


@pytest.fixture
def plane(monkeypatch):
    """Installs a PlaneStub and hands back a real client wired to it."""
    def install(**handlers):
        stub = PlaneStub(**handlers)
        monkeypatch.setattr(ns.urllib.request, "urlopen", stub)
        client = ns.ControlPlaneClient(base_url="https://control.test/controlPlaneMcp", token="test-token")
        return stub, client
    return install


def usage_entry(**overrides):
    """A usage_report `self` entry in the shape usagePolicy.ts emits."""
    entry = {
        "identity_id": "night-shift-01",
        "display_name": "Night Shift 01",
        "window_start": 1788453185794,
        "window_end": 1788471185794,
        "input": 0, "output": 0, "total": 0, "runs": 0,
        "allowance": None, "remaining": None, "exhausted": False,
    }
    entry.update(overrides)
    return entry


def usage_report_for(entry):
    return {"generated_at": "2026-09-03T16:33:05.794Z", "window_seconds": 18000,
            "identities": [entry], "self": entry}


def claim_reply(card_id, run_id, **card_fields):
    card = {"id": card_id, "title": f"Card {card_id}", "goal": "do the thing", "kind": "task"}
    card.update(card_fields)
    return {"card": card, "run_id": run_id}


# ---------------------------------------------------------------------------
# ControlPlaneClient: transport and payload shapes
# ---------------------------------------------------------------------------

def test_call_tool_posts_jsonrpc_to_the_mcp_path_with_a_bearer(plane):
    stub, client = plane(board_snapshot={"ok": True})

    assert client.snapshot() == {"ok": True}

    req = stub.requests[0]
    # The function root 404s; app.ts routes only POST /mcp and GET /healthz.
    assert req.full_url == "https://control.test/controlPlaneMcp/mcp"
    assert req.get_header("Authorization") == "Bearer test-token"
    body = json.loads(req.data.decode("utf-8"))
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert body["params"] == {"name": "board_snapshot", "arguments": {}}


def test_a_trailing_slash_on_the_base_url_does_not_double_up(monkeypatch):
    stub = PlaneStub(board_snapshot={})
    monkeypatch.setattr(ns.urllib.request, "urlopen", stub)
    ns.ControlPlaneClient(base_url="https://control.test/fn/", token="t").snapshot()
    assert stub.requests[0].full_url == "https://control.test/fn/mcp"


def test_request_ids_increment_across_calls(plane):
    stub, client = plane(board_snapshot={})
    client.snapshot()
    client.snapshot()
    ids = [json.loads(r.data.decode("utf-8"))["id"] for r in stub.requests]
    assert ids == [1, 2]


def test_sse_framed_responses_are_parsed(plane):
    entry = usage_entry(total=7)
    _, client = plane(usage_report=sse_body(usage_report_for(entry)))
    assert client.usage_report()["self"]["total"] == 7


def test_an_mcp_error_becomes_a_runtime_error(plane):
    _, client = plane(card_heartbeat=McpError("run not held by this identity"))
    with pytest.raises(RuntimeError, match="not held"):
        client.heartbeat("run-1")


def test_an_http_error_becomes_a_runtime_error_carrying_the_status(plane):
    _, client = plane(card_abandon=ns.urllib.error.HTTPError(
        "https://control.test/controlPlaneMcp/mcp", 403, "Forbidden", {}, io.BytesIO(b"admin only")
    ))
    with pytest.raises(RuntimeError, match="403"):
        client.call_tool("card_abandon", {"card_id": "c1", "reason": "x"})


def test_an_unresolvable_identity_refuses_to_call_at_all(plane):
    stub, client = plane(board_snapshot={})
    client.token = None
    with pytest.raises(RuntimeError, match="Bearer token"):
        client.snapshot()
    assert stub.calls == []


def test_claim_returns_none_when_nothing_is_claimable(plane):
    _, client = plane(card_claim={"claimed": None})
    assert client.claim(lane="local") is None


def test_claim_declares_lane_and_physical_resources(plane):
    stub, client = plane(card_claim=claim_reply("c1", "r1"))
    assert client.claim(lane="local", resources=["android-device"])["run_id"] == "r1"
    assert stub.args_for("card_claim")[0] == {"lane": "local", "resources": ["android-device"]}


def test_claim_omits_resources_when_the_host_has_none(plane):
    stub, client = plane(card_claim={"claimed": None})
    client.claim(lane="local", resources=[])
    assert stub.args_for("card_claim")[0] == {"lane": "local"}


def test_release_carries_tokens_gates_and_artifacts(plane):
    stub, client = plane(card_release={"usage": usage_entry(total=120)})
    gates = [{"gate_id": "quality", "status": "passed", "duration_ms": 900}]
    artifacts = {"branch": "nightshift/c1", "commit_sha": "abc123"}

    client.release(run_id="r1", outcome="succeeded", gates=gates,
                   artifacts=artifacts, tokens={"input": 100, "output": 20})

    assert stub.args_for("card_release")[0] == {
        "run_id": "r1",
        "outcome": "succeeded",
        "gates": gates,
        "artifacts": artifacts,
        "tokens": {"input": 100, "output": 20},
    }


def test_release_omits_absent_optional_fields(plane):
    stub, client = plane(card_release={})
    client.release(run_id="r1", outcome="failed")
    assert stub.args_for("card_release")[0] == {"run_id": "r1", "outcome": "failed"}


def test_release_truncates_the_error_to_the_contract_bound(plane):
    stub, client = plane(card_release={})
    client.release(run_id="r1", outcome="failed", error="x" * 9000)
    # card_release bounds `error` at 4096; an over-long string is rejected outright.
    assert len(stub.args_for("card_release")[0]["error"]) == 4096


def test_release_reports_zero_tokens_rather_than_omitting_them(plane):
    stub, client = plane(card_release={})
    client.release(run_id="r1", outcome="failed", tokens={"input": 0, "output": 0})
    # A run that omits tokens is invisible to usage accounting; zero is a fact.
    assert stub.args_for("card_release")[0]["tokens"] == {"input": 0, "output": 0}


def test_decompose_and_usage_report_target_the_right_tools(plane):
    stub, client = plane(card_decompose=[], usage_report=usage_report_for(usage_entry()))
    client.decompose("parent-1", [{"title": "t", "goal": "g", "placement": ["local"]}])
    client.usage_report()
    assert stub.tools_called() == ["card_decompose", "usage_report"]
    assert stub.args_for("card_decompose")[0]["parent_id"] == "parent-1"


# ---------------------------------------------------------------------------
# LeaseHeartbeatWorker
# ---------------------------------------------------------------------------

def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_the_lease_is_extended_repeatedly_while_a_card_runs(plane):
    stub, client = plane(card_heartbeat={"lease": {"expires_at": 1788471185794}})
    worker = ns.LeaseHeartbeatWorker(client, "run-1", interval=0.01)
    worker.start()
    try:
        assert wait_until(lambda: stub.count("card_heartbeat") >= 3)
    finally:
        worker.stop()

    assert worker.abandoned is False
    assert not worker.thread.is_alive()
    assert stub.args_for("card_heartbeat")[0] == {"run_id": "run-1"}


@pytest.mark.parametrize("message", [
    "the run was abandoned",
    "run not found",
    "run not held by this identity",
])
def test_administrative_abandonment_latches_and_stops_the_worker(plane, message):
    stub, client = plane(card_heartbeat=McpError(message))
    worker = ns.LeaseHeartbeatWorker(client, "run-1", interval=0.01)
    worker.start()
    worker.thread.join(timeout=3)

    # card_abandon cuts a run short and the failing heartbeat is the only signal
    # the worker gets, so it must latch and stop rather than keep beating.
    assert worker.abandoned is True
    assert not worker.thread.is_alive()


def test_a_transient_heartbeat_failure_does_not_abandon_the_card(plane):
    stub, client = plane(card_heartbeat=RuntimeError("connection reset by peer"))
    worker = ns.LeaseHeartbeatWorker(client, "run-1", interval=0.01)
    worker.start()
    try:
        assert wait_until(lambda: stub.count("card_heartbeat") >= 3)
    finally:
        worker.stop()

    assert worker.abandoned is False


def test_stopping_before_the_first_beat_is_safe(plane):
    stub, client = plane(card_heartbeat={})
    worker = ns.LeaseHeartbeatWorker(client, "run-1", interval=30)
    worker.start()
    worker.stop()
    assert stub.count("card_heartbeat") == 0
    assert not worker.thread.is_alive()


# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------

class StubProvider(ns.LLMProvider):
    """An LLM that costs a fixed, known number of tokens."""

    def __init__(self, label="stub", reply="ok", tokens=(10, 5), raises=None):
        super().__init__()
        self.label = label
        self.reply = reply
        self.tokens = tokens
        self.raises = raises
        self.calls = 0

    @property
    def name(self):
        return f"Stub ({self.label})"

    def ask(self, prompt_or_messages):
        self.calls += 1
        if self.raises:
            raise self.raises
        self.last_tokens = {"input": self.tokens[0], "output": self.tokens[1]}
        return self.reply


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.delenv("FORCE_PROVIDER", raising=False)
    return ns.ProviderManager()


def test_session_tokens_accumulate_across_requests(manager):
    manager.providers = [StubProvider(tokens=(100, 40))]
    manager.ask("one")
    manager.ask("two")
    assert manager.get_session_tokens() == {"input": 200, "output": 80}


def test_session_tokens_accumulate_across_a_failover(manager):
    manager.providers = [
        StubProvider(label="dead", raises=ns.QuotaExceededError("quota")),
        StubProvider(label="alive", tokens=(7, 3)),
    ]
    manager.current_index = 0

    assert manager.ask("hello") == "ok"
    assert manager.get_session_tokens() == {"input": 7, "output": 3}
    # Failover is sticky: the next request should not re-probe the dead provider.
    assert manager.current_index == 1


def test_the_counter_resets_between_cards(manager):
    manager.providers = [StubProvider(tokens=(11, 4))]
    manager.ask("card one work")
    assert manager.get_session_tokens() == {"input": 11, "output": 4}

    manager.reset_session_tokens()
    assert manager.get_session_tokens() == {"input": 0, "output": 0}

    manager.ask("card two work")
    assert manager.get_session_tokens() == {"input": 11, "output": 4}


def test_the_reported_counter_is_a_copy(manager):
    manager.get_session_tokens()["input"] = 999
    assert manager.get_session_tokens() == {"input": 0, "output": 0}


# ---------------------------------------------------------------------------
# Provider chain / tier switching
# ---------------------------------------------------------------------------

def test_the_default_chain_puts_subscription_first_and_local_last(manager):
    assert manager.local_only is False
    assert isinstance(manager.providers[0], ns.GeminiCLIProvider)
    assert isinstance(manager.providers[-1], ns.OllamaProvider)


def test_switching_to_local_leaves_only_ollama(manager):
    assert manager.set_local_only(True) is True
    assert [type(p) for p in manager.providers] == [ns.OllamaProvider]
    assert manager.local_only is True


def test_switching_back_restores_the_subscription_chain(manager):
    manager.set_local_only(True)
    assert manager.set_local_only(False) is True
    assert len(manager.providers) == 4
    assert isinstance(manager.providers[0], ns.GeminiCLIProvider)


def test_switching_to_the_current_tier_is_a_no_op(manager):
    before = manager.providers
    assert manager.set_local_only(False) is False
    assert manager.providers is before


def test_switching_tier_resets_the_sticky_provider_index(manager):
    manager.current_index = 3
    manager.set_local_only(True)
    assert manager.current_index == 0


def test_force_provider_pins_local_and_outranks_the_allowance(monkeypatch):
    monkeypatch.setenv("FORCE_PROVIDER", "ollama")
    pinned = ns.ProviderManager()

    assert pinned.pinned_local is True
    assert [type(p) for p in pinned.providers] == [ns.OllamaProvider]
    # An operator saying "local only" beats a control plane saying "spend more".
    assert pinned.set_local_only(False) is False
    assert [type(p) for p in pinned.providers] == [ns.OllamaProvider]


# ---------------------------------------------------------------------------
# decide_inference_mode: the three allowance cases
# ---------------------------------------------------------------------------

def test_under_a_declared_allowance_the_worker_keeps_spending():
    local_only, reason = ns.decide_inference_mode(
        usage_entry(input=300, output=100, total=400, allowance=1000, remaining=600)
    )
    assert local_only is False
    assert "600 of 1000" in reason


def test_an_exhausted_allowance_falls_back_to_local():
    local_only, reason = ns.decide_inference_mode(
        usage_entry(total=1000, allowance=1000, remaining=0, exhausted=True)
    )
    assert local_only is True
    assert "exhausted" in reason


def test_an_undeclared_allowance_is_unmetered_not_exhausted():
    local_only, reason = ns.decide_inference_mode(
        usage_entry(total=48000, allowance=None, remaining=None, exhausted=False)
    )
    # null means undeclared, which means unmetered. It is not zero, and a large
    # spend against it is not a reason to stop.
    assert local_only is False
    assert "no allowance" in reason
    assert "inert" in reason


def test_an_undeclared_allowance_wins_over_a_stale_exhausted_flag():
    # exhausted is only meaningful against a declared allowance; if a reply ever
    # contradicts itself, the allowance is the field that decides.
    assert ns.decide_inference_mode(usage_entry(allowance=None, exhausted=True))[0] is False


def test_a_lapsed_window_reads_as_empty_and_resumes_spending():
    # Windows reset lazily, so a rolled window arrives as a fresh zeroed entry
    # rather than as a reset event.
    local_only, reason = ns.decide_inference_mode(
        usage_entry(total=0, allowance=1000, remaining=1000, exhausted=False)
    )
    assert local_only is False
    assert "1000 of 1000" in reason


def test_a_missing_usage_entry_does_not_pessimise_to_local():
    assert ns.decide_inference_mode(None)[0] is False
    assert ns.decide_inference_mode({})[0] is False


# ---------------------------------------------------------------------------
# apply_usage_policy and run_swarm, on a real agent
# ---------------------------------------------------------------------------

@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.delenv("FORCE_PROVIDER", raising=False)
    cwd = os.getcwd()
    built = ns.NightShiftAgent(project_dir=str(tmp_path), token="test-token")
    yield built
    os.chdir(cwd)


@pytest.fixture
def wire(monkeypatch, agent):
    """Points the agent's real client at a scripted transport."""
    def install(**handlers):
        stub = PlaneStub(**handlers)
        monkeypatch.setattr(ns.urllib.request, "urlopen", stub)
        agent.control_plane = ns.ControlPlaneClient(
            base_url="https://control.test/controlPlaneMcp", token="test-token"
        )
        return stub
    return install


def test_the_policy_stays_on_subscription_under_an_allowance(agent, wire):
    wire(usage_report=usage_report_for(usage_entry(total=400, allowance=1000, remaining=600)))

    assert agent.apply_usage_policy() is False
    assert agent.llm.local_only is False
    assert len(agent.llm.providers) == 4


def test_the_policy_switches_to_local_when_exhausted(agent, wire):
    wire(usage_report=usage_report_for(
        usage_entry(total=1000, allowance=1000, remaining=0, exhausted=True)
    ))

    assert agent.apply_usage_policy() is True
    assert [type(p) for p in agent.llm.providers] == [ns.OllamaProvider]


def test_the_policy_returns_to_subscription_when_the_window_rolls(agent, wire):
    windows = [
        usage_report_for(usage_entry(total=1000, allowance=1000, remaining=0, exhausted=True)),
        usage_report_for(usage_entry(total=0, allowance=1000, remaining=1000)),
    ]
    wire(usage_report=lambda args: windows.pop(0))

    assert agent.apply_usage_policy() is True
    assert agent.apply_usage_policy() is False
    assert len(agent.llm.providers) == 4


def test_the_policy_is_wired_but_inert_when_no_allowance_is_declared(agent, wire, caplog):
    # night-shift-01 declares none today, so this is the production path.
    wire(usage_report=usage_report_for(usage_entry(total=125000)))

    with caplog.at_level("INFO", logger="NightShiftAgent"):
        assert agent.apply_usage_policy() is False

    assert agent.llm.local_only is False
    assert len(agent.llm.providers) == 4
    assert any("inert" in record.message for record in caplog.records)


def test_a_failed_usage_report_leaves_the_subscription_chain_alone(agent, wire):
    wire(usage_report=RuntimeError("timeout"))

    # Refusing to spend because the bookkeeping call failed is the opposite of maxing.
    assert agent.apply_usage_policy() is False
    assert len(agent.llm.providers) == 4


def test_a_failed_usage_report_does_not_drag_a_local_worker_back_to_paid(agent, wire):
    replies = [
        usage_report_for(usage_entry(total=1000, allowance=1000, remaining=0, exhausted=True)),
        RuntimeError("timeout"),
    ]
    wire(usage_report=lambda args: replies.pop(0))

    assert agent.apply_usage_policy() is True
    assert agent.apply_usage_policy() is True
    assert [type(p) for p in agent.llm.providers] == [ns.OllamaProvider]


def test_the_swarm_reports_each_cards_own_token_spend(agent, wire):
    claims = [claim_reply("c1", "r1"), claim_reply("c2", "r2")]
    stub = wire(
        card_claim=lambda args: claims.pop(0),
        usage_report=usage_report_for(usage_entry()),
        card_release={},
    )
    agent.llm.providers = [StubProvider(tokens=(100, 25))]

    def execute(card, run_id, heartbeat):
        agent.llm.reset_session_tokens()   # what execute_card does per card
        agent.llm.ask("work")
        agent.llm.ask("more work")
        return "succeeded", [{"gate_id": "quality", "status": "passed", "duration_ms": 10}], None, None

    agent.execute_card = execute
    agent.run_swarm(lane="local", max_runs=2)

    releases = stub.args_for("card_release")
    assert [r["run_id"] for r in releases] == ["r1", "r2"]
    for release in releases:
        # Per card, not cumulative across the process.
        assert release["tokens"] == {"input": 200, "output": 50}
        assert release["outcome"] == "succeeded"
        assert release["gates"][0]["gate_id"] == "quality"


def test_the_swarm_consults_usage_once_per_card(agent, wire):
    claims = [claim_reply("c1", "r1"), claim_reply("c2", "r2")]
    stub = wire(
        card_claim=lambda args: claims.pop(0),
        usage_report=usage_report_for(usage_entry()),
        card_release={},
    )
    agent.execute_card = lambda card, run_id, hb: ("succeeded", [], None, None)

    agent.run_swarm(lane="local", max_runs=2)

    # Once per claimed card. Not per LLM request.
    assert stub.count("usage_report") == 2


def test_the_swarm_does_not_consult_usage_when_nothing_is_claimable(agent, wire):
    stub = wire(card_claim={"claimed": None})
    agent.run_swarm(lane="local", max_runs=0)
    assert stub.count("usage_report") == 0


def test_the_swarm_releases_failed_with_the_error_when_execution_raises(agent, wire):
    stub = wire(
        card_claim=claim_reply("c1", "r1"),
        usage_report=usage_report_for(usage_entry()),
        card_release={},
    )

    def boom(card, run_id, hb):
        raise RuntimeError("gate runner missing")

    agent.execute_card = boom
    agent.run_swarm(lane="local", max_runs=1)

    released = stub.args_for("card_release")[0]
    assert released["outcome"] == "failed"
    assert "gate runner missing" in released["error"]
    assert released["tokens"] == {"input": 0, "output": 0}


def test_the_swarm_keeps_working_when_a_release_fails(agent, wire):
    claims = [claim_reply("c1", "r1"), claim_reply("c2", "r2")]
    attempts = {"n": 0}

    def release(args):
        attempts["n"] += 1
        return RuntimeError("release timed out") if attempts["n"] == 1 else {}

    stub = wire(
        card_claim=lambda args: claims.pop(0),
        usage_report=usage_report_for(usage_entry()),
        card_release=release,
    )
    agent.execute_card = lambda card, run_id, hb: ("succeeded", [], None, None)

    agent.run_swarm(lane="local", max_runs=2)

    # A lost release costs one run record, not the worker.
    assert attempts["n"] == 2
    assert stub.count("card_claim") == 2


def test_a_card_claimed_while_exhausted_runs_on_local_inference(agent, wire):
    wire(
        card_claim=claim_reply("c1", "r1"),
        usage_report=usage_report_for(
            usage_entry(total=1000, allowance=1000, remaining=0, exhausted=True)
        ),
        card_release={},
    )
    seen = {}

    def record(card, run_id, hb):
        seen["chain"] = [type(p) for p in agent.llm.providers]
        return "succeeded", [], None, None

    agent.execute_card = record
    agent.run_swarm(lane="local", max_runs=1)

    # The tier is chosen before the card starts, so the whole card runs on it.
    assert seen["chain"] == [ns.OllamaProvider]


# ---------------------------------------------------------------------------
# execute_card: per-card reset and abandonment
# ---------------------------------------------------------------------------

class StubLease:
    def __init__(self, abandoned=False):
        self.abandoned = abandoned


def test_execute_card_resets_the_token_counter_before_the_card_runs(agent):
    agent.llm.providers = [StubProvider(tokens=(9, 3))]
    agent.llm.ask("spend left over from a previous card")
    assert agent.llm.get_session_tokens()["input"] == 9

    seen = {}

    def process(task, context, files):
        seen["at_start"] = agent.llm.get_session_tokens()
        return False

    agent.process_task = process
    outcome, _, _, error = agent.execute_card(
        {"id": "c1", "title": "t", "goal": "g", "kind": "task"}, "r1", StubLease()
    )

    assert seen["at_start"] == {"input": 0, "output": 0}
    assert outcome == "failed"
    assert error


def test_execute_card_reports_abandoned_when_the_lease_was_revoked(agent):
    agent.process_task = lambda task, context, files: True

    outcome, _, _, error = agent.execute_card(
        {"id": "c1", "title": "t", "goal": "g", "kind": "task"}, "r1", StubLease(abandoned=True)
    )

    # An admin card_abandon invalidates the run; committing and then releasing
    # `succeeded` against a dead run would be a lie.
    assert outcome == "abandoned"
    assert "abandoned" in error.lower()


def test_execute_card_restores_the_working_directory(agent):
    agent.process_task = lambda task, context, files: False
    before = os.getcwd()

    agent.execute_card({"id": "c1", "title": "t", "goal": "g", "kind": "task"}, "r1", StubLease())

    assert os.getcwd() == before


def test_execute_card_hands_the_cards_gates_to_the_toolbox(agent):
    agent.process_task = lambda task, context, files: False

    agent.execute_card(
        {"id": "c1", "title": "t", "goal": "g", "kind": "task", "gate_ids": ["quality", "unit"]},
        "r1", StubLease()
    )

    # Contract verification runs exactly the gates the card declares.
    assert agent.toolbox.target_gates == ["quality", "unit"]
    assert agent.toolbox.current_card_id == "c1"


# ---------------------------------------------------------------------------
# Gate contract verification: what produces the gate results a release carries
# ---------------------------------------------------------------------------

GATE_RUNNER = """#!/usr/bin/env python3
import json, sys, pathlib
gate = sys.argv[1]
outcomes = json.loads(pathlib.Path(__file__).with_name("outcomes.json").read_text())
pathlib.Path(__file__).with_name("ran.txt").open("a").write(gate + "\\n")
print(f"running {gate}")
sys.exit(outcomes.get(gate, 1))
"""


@pytest.fixture
def repo(tmp_path):
    """A project laid out the way a `software` card's repo is: a contract plus a gate runner."""
    def build(gates, outcomes):
        (tmp_path / "scripts").mkdir(exist_ok=True)
        (tmp_path / "verification.json").write_text(json.dumps({"gates": gates}))
        runner = tmp_path / "scripts" / "run-gate.py"
        runner.write_text(GATE_RUNNER)
        (tmp_path / "scripts" / "outcomes.json").write_text(json.dumps(outcomes))
        toolbox = ns.Toolbox(ns.BuildState(), project_dir=tmp_path)
        return toolbox, tmp_path
    return build


def gates_run(root):
    ran = root / "scripts" / "ran.txt"
    return ran.read_text().split() if ran.exists() else []


def test_verification_runs_every_gate_the_card_declares(repo):
    toolbox, root = repo(
        [{"id": "quality", "placement": ["local"]}, {"id": "unit", "placement": ["local"]}],
        {"quality": 0, "unit": 0},
    )
    toolbox.target_gates = ["quality", "unit"]

    report = toolbox.verify_build()

    assert "PASSED" in report
    assert gates_run(root) == ["quality", "unit"]
    assert [g["gate_id"] for g in toolbox.last_gate_results] == ["quality", "unit"]
    assert {g["status"] for g in toolbox.last_gate_results} == {"passed"}
    assert all(g["duration_ms"] >= 0 for g in toolbox.last_gate_results)
    assert toolbox.build_state.build_passed is True


def test_verification_stops_at_the_first_failing_gate(repo):
    toolbox, root = repo(
        [{"id": "quality", "placement": ["local"]}, {"id": "unit", "placement": ["local"]}],
        {"quality": 1, "unit": 0},
    )
    toolbox.target_gates = ["quality", "unit"]

    report = toolbox.verify_build()

    assert "FAILED" in report
    # A later gate cannot be trusted once an earlier one failed, and running it
    # wastes the rest of the card's budget.
    assert gates_run(root) == ["quality"]
    assert toolbox.last_gate_results == [
        {"gate_id": "quality", "status": "failed", "duration_ms": toolbox.last_gate_results[0]["duration_ms"]}
    ]
    assert toolbox.build_state.build_passed is False


def test_verification_falls_back_to_the_contracts_local_gates(repo):
    toolbox, root = repo(
        [{"id": "quality", "placement": ["local"]}, {"id": "device", "placement": ["device"]}],
        {"quality": 0, "device": 0},
    )
    toolbox.target_gates = []

    toolbox.verify_build()

    # A card with no gate_ids runs what the repo says is runnable here, and
    # nothing placed on hardware this host does not have.
    assert gates_run(root) == ["quality"]


def test_verification_defaults_to_quality_when_the_contract_places_nothing_locally(repo):
    toolbox, root = repo([{"id": "device", "placement": ["device"]}], {"quality": 0})
    toolbox.target_gates = []

    toolbox.verify_build()

    assert gates_run(root) == ["quality"]


def test_verification_checkpoints_the_files_that_passed(repo, tmp_path):
    toolbox, _ = repo([{"id": "quality", "placement": ["local"]}], {"quality": 0})
    source = tmp_path / "Main.kt"
    source.write_text("fun main() {}")
    toolbox.build_state.files_changed_since_success = [str(source)]
    toolbox.target_gates = ["quality"]

    toolbox.verify_build()

    # The revert-to-checkpoint path can only work if a pass records the content.
    assert str(source) in toolbox.build_state.last_successful_files


def test_a_malformed_contract_is_reported_not_raised(repo, tmp_path):
    toolbox, _ = repo([{"id": "quality", "placement": ["local"]}], {"quality": 0})
    (tmp_path / "verification.json").write_text("{not json")

    assert "Error reading verification contract" in toolbox.verify_build()


def test_a_repo_without_a_contract_uses_the_legacy_path(tmp_path, monkeypatch):
    toolbox = ns.Toolbox(ns.BuildState(), project_dir=tmp_path)
    called = {}

    def legacy(self):
        called["legacy"] = True
        return "legacy verification ran"

    monkeypatch.setattr(ns.Toolbox, "_verify_via_legacy", legacy)

    assert toolbox.verify_build() == "legacy verification ran"
    assert called["legacy"] is True


# ---------------------------------------------------------------------------
# Toolbox.decompose: the agent's own hook back into the board
# ---------------------------------------------------------------------------

def test_decompose_sends_children_for_the_card_being_worked(plane):
    stub, client = plane(card_decompose=[{"id": "c1a"}, {"id": "c1b"}])
    toolbox = ns.Toolbox(ns.BuildState())
    toolbox.control_plane = client
    toolbox.current_card_id = "c1"
    children = [{"title": "a", "goal": "g", "placement": ["local"]},
                {"title": "b", "goal": "g", "placement": ["local"]}]

    assert "2 children" in toolbox.decompose(children=children)
    assert stub.args_for("card_decompose")[0] == {"parent_id": "c1", "children": children}


def test_decompose_is_unavailable_outside_a_claimed_card():
    toolbox = ns.Toolbox(ns.BuildState())
    assert "not available" in toolbox.decompose(children=[{"title": "a"}])


def test_decompose_rejects_an_empty_child_list(plane):
    _, client = plane(card_decompose=[])
    toolbox = ns.Toolbox(ns.BuildState())
    toolbox.control_plane = client
    toolbox.current_card_id = "c1"
    assert "No child cards" in toolbox.decompose(children=[])


def test_a_rejected_decomposition_is_reported_to_the_model_not_raised(plane):
    _, client = plane(card_decompose=McpError("depth cap reached"))
    toolbox = ns.Toolbox(ns.BuildState())
    toolbox.control_plane = client
    toolbox.current_card_id = "c1"

    result = toolbox.decompose(children=[{"title": "a", "goal": "g", "placement": ["local"]}])

    assert "Decomposition failed" in result and "depth cap" in result


# ---------------------------------------------------------------------------
# Identity resolution and resource declaration
# ---------------------------------------------------------------------------

def test_the_token_comes_from_the_environment_when_it_is_set(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_BEARER_TOKEN", "  env-token  ")
    assert ns.ControlPlaneClient(base_url="https://control.test/fn").token == "env-token"


def test_the_token_falls_back_to_secret_manager(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_BEARER_TOKEN", raising=False)
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess_result(0, "secret-token\n")

    monkeypatch.setattr(ns.subprocess, "run", fake_run)
    client = ns.ControlPlaneClient(base_url="https://control.test/fn", identity_id="night-shift-01")

    assert client.token == "secret-token"
    assert "--secret=control-plane-night-shift-01" in seen["cmd"]


def test_an_unresolvable_token_leaves_the_client_unauthenticated(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_BEARER_TOKEN", raising=False)
    monkeypatch.setattr(ns.subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(OSError("no gcloud")))

    # Constructing must not explode; the refusal happens at call time with a
    # message that says which identity could not be resolved.
    assert ns.ControlPlaneClient(base_url="https://control.test/fn").token is None


class subprocess_result:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_an_attached_android_device_is_declared_as_a_resource(agent, monkeypatch):
    monkeypatch.setattr(ns.subprocess, "run", lambda cmd, **kw: subprocess_result(
        0, "List of devices attached\nemulator-5554\tdevice\n"
    ))
    # Cards requiring hardware must only reach a worker that has it.
    assert agent.detect_resources() == ["android-device"]


def test_a_host_with_no_device_declares_nothing(agent, monkeypatch):
    monkeypatch.setattr(ns.subprocess, "run", lambda cmd, **kw: subprocess_result(
        0, "List of devices attached\n"
    ))
    assert agent.detect_resources() == []


def test_a_missing_adb_is_not_fatal(agent, monkeypatch):
    monkeypatch.setattr(ns.subprocess, "run", lambda cmd, **kw: (_ for _ in ()).throw(FileNotFoundError("adb")))
    assert agent.detect_resources() == []


# ---------------------------------------------------------------------------
# Provider failover on an empty reply
# ---------------------------------------------------------------------------

def test_an_empty_reply_fails_over_to_the_next_provider(manager):
    silent = StubProvider(label="silent", reply=None)
    answering = StubProvider(label="answering", reply="done", tokens=(4, 2))
    manager.providers = [silent, answering]
    manager.current_index = 0

    assert manager.ask("hello") == "done"
    assert manager.get_session_tokens() == {"input": 4, "output": 2}


def test_a_chain_that_all_returns_empty_gives_up(manager):
    manager.providers = [StubProvider(label="a", reply=None), StubProvider(label="b", reply=None)]
    manager.current_index = 0

    assert manager.ask("hello") is None
    assert manager.get_session_tokens() == {"input": 0, "output": 0}


def test_a_result_with_no_content_block_is_returned_as_is(plane):
    _, client = plane(board_snapshot=json.dumps({
        "jsonrpc": "2.0", "id": 1, "result": {"cards": [], "generated_at": "now"}
    }))
    assert client.snapshot() == {"cards": [], "generated_at": "now"}
