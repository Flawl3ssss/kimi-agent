"""Tool surface + RPC dispatch, driven through a stub bridge (no Kimi process).

The point of these tests is that a UI action (`coomi/tools/call`) and a model
action (`mcp__coomi__*`) execute the *same* code path, and that every tool
reports failure clearly instead of silently doing nothing.

Two failure styles coexist by design:
  * "not found: x" style text — the model can read and recover from it;
  * raised exceptions (validation of plans/workflows/scopes) — MCP marks them
    isError, which is what we want for programming errors.
"""

from __future__ import annotations

import asyncio
import json
import types

import pytest

from kimi_agent import mcp_host
from kimi_agent import rpc as rpc_module
from kimi_agent.config import Settings
from kimi_agent.events import Event


class StubSession:
    def __init__(self, sid, cwd):
        self.id = sid
        self.cwd = cwd
        self.status = "idle"
        self.mode = "default"
        self.model = "stub/model"
        self.last_text_parts: list[str] = ["ответ саб-агента"]

    def describe(self):
        return {"id": self.id, "cwd": self.cwd, "status": self.status}


class StubBridge:
    """Minimal stand-in for KimiCodeBridge."""

    def __init__(self, settings):
        from kimi_agent.client import FileService

        self.settings = settings
        self.files = FileService(settings)
        self.workspace = str(settings.workspace)
        self.active_session_hint = "sess-1"
        self.initialize_result = {"agentInfo": {"name": "stub", "version": "0"},
                                  "agentCapabilities": {}}
        self.published: list[Event] = []
        self.prompts: list[tuple[str, str]] = []
        self.spawned: list[dict] = []
        self.sessions: dict[str, StubSession] = {}
        self.subagents: dict[str, dict] = {}
        self.decisions = types.SimpleNamespace(
            pending=[], resolve=lambda *a, **k: False, cancel_all=lambda *a, **k: None)
        self.active_model_meta: dict = {}

    def publish(self, event: Event) -> None:
        self.published.append(event)

    def stderr_tail(self, lines: int = 60):
        return ["line 1", "line 2"][:lines]

    def known_sessions(self):
        return [s.describe() for s in self.sessions.values()]

    async def new_session(self, cwd=None, additional_directories=None, mode=None,
                          model=None, thinking=None, mcp_servers=None):
        self.spawned.append({"cwd": cwd or self.workspace, "model": model, "mode": mode})
        state = StubSession(f"sub-{len(self.spawned)}", cwd or self.workspace)
        self.sessions[state.id] = state
        return state

    async def prompt(self, session_id, text, images=None, resources=None):
        self.prompts.append((session_id, text))

        async def _finish():
            return {"stop_reason": "end_turn", "usage": None}

        return asyncio.ensure_future(_finish())

    async def close_session(self, session_id):
        self.sessions.pop(session_id, None)

    async def publish_local_plan(self, plan):
        self.published.append(Event("plan", plan.get("session_id", ""), {"plan": plan}))

    async def notify_outbox(self, path, size):
        self.published.append(Event("file_ready", "", {"path": path, "size": size}))

    async def push_image(self, path, mime, data, caption=""):
        self.published.append(Event("image", "", {"path": path, "mime": mime}))

    async def cancel(self, session_id):
        self.published.append(Event("cancelled", session_id, {}))

    async def list_kimi_sessions(self, cwd=None):
        return []

    async def health(self):
        return {"running": True, "agent": {"name": "stub"}}


@pytest.fixture
def bridge(tmp_path):
    mcp_host._SERVERS.clear()
    settings = Settings(workspace=tmp_path)
    # runtime_doctor reports the configured kernel path; leaving it to
    # default_kimi_binary() means the assertion depends on whether Kimi happens
    # to be installed on the machine running the tests (it is not on CI).
    settings.kimi_bin = str(tmp_path / "kimi")
    b = StubBridge(settings)
    yield b
    mcp_host._SERVERS.clear()


@pytest.fixture
def rpc(bridge):
    return rpc_module.Rpc(bridge)


async def call(bridge, tool, **args):
    return await mcp_host.call_tool(bridge, tool, args)


def js(text):
    return json.loads(text)


# ------------------------------------------------------------------ registry
@pytest.mark.asyncio
async def test_all_tools_are_registered_and_documented(bridge):
    specs = await mcp_host.tool_specs(bridge)
    assert len(specs) >= 30
    for spec in specs:
        assert spec["qualified"] == f"mcp__coomi__{spec['name']}"
        assert spec["description"].strip(), f"{spec['name']} is invisible to the model"
        assert spec["input_schema"].get("type") == "object", spec["name"]
    names = {s["name"] for s in specs}
    for expected in ("memory_write", "memory_search", "memory_read", "update_plan",
                     "create_workflow", "spawn_agent", "agent_wait", "list_skills",
                     "read_skill", "create_loop", "web_search", "show_image",
                     "export_file", "list_pending", "runtime_doctor", "open_session"):
        assert expected in names, expected


@pytest.mark.asyncio
async def test_unknown_tool_raises(bridge):
    with pytest.raises(Exception):
        await call(bridge, "no_such_tool")


# ------------------------------------------------------------------- memory
@pytest.mark.asyncio
async def test_memory_write_search_read_delete(bridge):
    assert "m1" in await call(bridge, "memory_write", name="m1", description="тест",
                              type="project", content="квантовые токены", scope="project")
    hits = js(await call(bridge, "memory_search", query="квантовые"))
    assert hits[0]["name"] == "m1"
    body = js(await call(bridge, "memory_read", name="m1"))
    assert body["content"] == "квантовые токены"
    assert body["scope"] == "project"
    assert "deleted" in await call(bridge, "memory_delete", name="m1")
    assert "not found" in await call(bridge, "memory_read", name="m1")


@pytest.mark.asyncio
async def test_memories_written_without_project_stay_searchable(bridge):
    """Regression: writes used to land in the workspace project while reads
    queried project=None, so a model could write a memory it could not find."""
    await call(bridge, "memory_write", name="vis", description="d", type="project",
               content="проверяемая видимость", scope="project")
    assert any(h["name"] == "vis" for h in js(await call(bridge, "memory_search",
                                                         query="видимость")))
    assert any(m["name"] == "vis" for m in js(await call(bridge, "memory_list")))


@pytest.mark.asyncio
async def test_memory_write_reports_the_reason_for_a_bad_type(bridge):
    """MCP collapses unknown exceptions to "Error executing tool <name>", which is
    useless to the model; tools wrap anticipated failures into ToolError so the
    real message reaches it."""
    with pytest.raises(Exception) as excinfo:
        await call(bridge, "memory_write", name="bad", description="d",
                   type="definitely-not-a-type", content="c")
    assert "type must be one of" in str(excinfo.value)


# -------------------------------------------------------------------- plan
@pytest.mark.asyncio
async def test_update_plan_publishes_a_plan_event(bridge):
    out = js(await call(bridge, "update_plan",
                        steps=[{"step": "изучить", "status": "completed"},
                               {"step": "писать", "status": "in_progress"}],
                        explanation="фаза 5"))
    assert out["ok"] is True
    assert out["plan"][0]["status"] == "completed"
    plans = [e for e in bridge.published if e.type == "plan"]
    assert plans and plans[-1].data["plan"]["explanation"] == "фаза 5"
    assert plans[-1].data["plan"]["session_id"] == "sess-1", "plan must bind to the live session"


@pytest.mark.asyncio
async def test_get_plan_reads_it_back(bridge):
    await call(bridge, "update_plan", steps=[{"step": "шаг", "status": "pending"}])
    stored = js(await call(bridge, "get_plan"))
    assert stored["steps"][0]["step"] == "шаг"


@pytest.mark.asyncio
async def test_update_plan_rejects_two_active_steps(bridge):
    with pytest.raises(Exception) as excinfo:
        await call(bridge, "update_plan", steps=[
            {"step": "a", "status": "in_progress"},
            {"step": "b", "status": "in_progress"}])
    assert "in_progress" in str(excinfo.value)


# --------------------------------------------------------------- workflows
@pytest.mark.asyncio
async def test_create_and_run_workflow(bridge):
    out = js(await call(bridge, "create_workflow", id="wf-1", name="Пайплайн", steps=[
        {"id": "a", "action": "inspect"},
        {"id": "b", "action": "implement", "depends_on": ["a"]},
    ]))
    assert out["id"] == "wf-1" and out["steps"] == ["a", "b"]
    assert any(w["id"] == "wf-1" for w in js(await call(bridge, "list_workflows")))
    got = js(await call(bridge, "get_workflow", id="wf-1"))
    assert got["steps"][0]["action"] == "inspect" and got["name"] == "Пайплайн"
    assert "deleted" in await call(bridge, "delete_workflow", id="wf-1")


@pytest.mark.asyncio
async def test_workflow_dag_is_validated(bridge):
    with pytest.raises(Exception):
        await call(bridge, "create_workflow", id="wf-2", name="bad", steps=[
            {"id": "a", "action": "x", "depends_on": ["b"]},
            {"id": "b", "action": "y", "depends_on": ["a"]}])
    with pytest.raises(Exception):
        await call(bridge, "create_workflow", id="wf-3", name="bad", steps=[])
    assert "not found" in await call(bridge, "get_workflow", id="wf-2")


# ------------------------------------------------------------- sub-agents
@pytest.mark.asyncio
async def test_spawn_agent_creates_a_real_session_and_reports_status(bridge):
    out = js(await call(bridge, "spawn_agent", task="Найти конфиг провайдера"))
    assert bridge.spawned, "no ACP session was created"
    agent_id = out["agent_id"]
    assert out["session"] == agent_id
    status = js(await call(bridge, "agent_status", agent_id=agent_id))
    assert status["task"].startswith("Найти")
    report = js(await call(bridge, "agent_wait", agent_ids=[agent_id]))
    assert report[0]["result"] == "ответ саб-агента"
    await call(bridge, "close_agent", agent_id=agent_id)
    assert js(await call(bridge, "agent_status", agent_id=agent_id))["status"] == "closed"
    assert "unknown agent" in await call(bridge, "agent_status", agent_id="never-existed")


@pytest.mark.asyncio
async def test_spawn_agent_selects_a_profile_mode(bridge):
    for subagent_type, expected_mode in (("reviewer", "plan"), ("coder", "default")):
        bridge.spawned.clear()
        await call(bridge, "spawn_agent", task="x", subagent_type=subagent_type)
        assert bridge.spawned[-1]["mode"] == expected_mode
    bridge.spawned.clear()
    await call(bridge, "spawn_agent", task="x", subagent_type="does-not-exist")
    assert bridge.spawned[-1]["mode"] == "default", "unknown type must fall back to coder"


@pytest.mark.asyncio
async def test_agent_wait_on_an_unknown_id_is_honest(bridge):
    report = js(await call(bridge, "agent_wait", agent_ids=["ghost"]))
    assert report[0]["status"] == "gone"


# ------------------------------------------------------------------ skills
@pytest.mark.asyncio
async def test_list_and_read_skills(bridge):
    created = await call(bridge, "create_skill", name="demo-skill",
                         description="описание", content="ТЕЛО НАВЫКА")
    assert "demo-skill" in created
    names = [s["name"] for s in js(await call(bridge, "list_skills"))]
    assert "demo-skill" in names
    body = await call(bridge, "read_skill", name="demo-skill")
    assert "ТЕЛО НАВЫКА" in body
    assert "not found" in await call(bridge, "read_skill", name="absent-skill")


# -------------------------------------------------------------- files/web
@pytest.mark.asyncio
async def test_export_file_stages_an_absolute_outbox_path(bridge):
    src = bridge.settings.workspace / "artifact.bin"
    src.write_bytes(b"payload")
    out = await call(bridge, "export_file", path=str(src), suggested_name="art.bin")
    staged = out.split("ready to save: ")[-1].strip()
    from pathlib import Path

    assert Path(staged).is_absolute()
    assert Path(staged).read_bytes() == b"payload"
    assert any(e.type == "file_ready" for e in bridge.published), "the UI must be notified"


@pytest.mark.asyncio
async def test_export_file_reports_problems(bridge):
    assert "cannot export" in await call(bridge, "export_file", path="/etc/shadow")
    assert "cannot export" in await call(bridge, "export_file",
                                        path=str(bridge.settings.workspace))


@pytest.mark.asyncio
async def test_import_file_copies_into_the_inbox(bridge, tmp_path):
    source = tmp_path / "external.txt"
    source.write_text("из телефона", encoding="utf-8")
    out = await call(bridge, "import_file", source=str(source))
    from kimi_agent import store

    assert "imported" in out.lower() or "inbox" in out.lower()
    assert list(store.INBOX.glob("*external*")) or "cannot" in out


@pytest.mark.asyncio
async def test_show_image_rejects_a_non_image(bridge):
    txt = bridge.settings.workspace / "not-an-image.txt"
    txt.write_text("hello", encoding="utf-8")
    assert "cannot show image" in await call(bridge, "show_image", path=str(txt))


@pytest.mark.asyncio
async def test_show_image_publishes_a_real_image(bridge):
    png = bridge.settings.workspace / "shot.png"
    png.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6300010000050001" "0d0a2db40000000049454e44ae426082"))
    out = await call(bridge, "show_image", path=str(png), caption="скрин")
    assert "displayed" in out
    assert any(e.type == "image" for e in bridge.published)


@pytest.mark.asyncio
async def test_web_search_never_returns_silently(bridge, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("KEYWORD_SEARCH_URL", raising=False)
    out = await call(bridge, "web_search", query="acp protocol")
    assert isinstance(out, str) and out.strip(), "must explain why nothing was found"


# -------------------------------------------------------------------- loops
@pytest.mark.asyncio
async def test_loop_lifecycle_through_tools(bridge):
    created = js(await call(bridge, "create_loop", objective="Довести до конца", max_turns=3))
    loop_id = created["id"]
    assert created["max_turns"] == 3
    listed = js(await call(bridge, "list_loops"))
    assert any(l["id"] == loop_id for l in listed)
    updated = js(await call(bridge, "update_loop", loop_id=loop_id, status="paused"))
    assert updated["status"] == "paused"
    assert js(await call(bridge, "update_loop", loop_id=loop_id,
                        objective="новая цель"))["id"] == loop_id


@pytest.mark.asyncio
async def test_loop_errors_are_reported_not_swallowed(bridge):
    assert "not found" in await call(bridge, "update_loop", loop_id="ghost")
    assert "not found" in await call(bridge, "run_loop_turn", loop_id="ghost")
    await call(bridge, "create_loop", objective="цель")
    assert "must be" in await call(bridge, "update_loop", loop_id=js(
        await call(bridge, "list_loops"))[0]["id"], status="teleported")


@pytest.mark.asyncio
async def test_run_loop_turn_advances_the_budget(bridge):
    loop_id = js(await call(bridge, "create_loop", objective="step", max_turns=2))["id"]
    first = js(await call(bridge, "run_loop_turn", loop_id=loop_id))
    assert first["turn"] == 1
    second = js(await call(bridge, "run_loop_turn", loop_id=loop_id))
    assert second["turn"] == 2
    third = js(await call(bridge, "run_loop_turn", loop_id=loop_id))
    assert third["status"] == "complete", "the budget must actually stop the loop"


# -------------------------------------------------------------- decisions
@pytest.mark.asyncio
async def test_list_pending_and_submit_answer(bridge):
    assert js(await call(bridge, "list_pending")) == []
    assert "no pending decision" in await call(bridge, "submit_answer", decision_id="ghost")

    bridge.decisions.pending = [{"id": "d1", "kind": "approval", "title": "Bash: rm x"}]
    bridge.decisions.resolve = lambda did, body: did == "d1" and body.get("behavior") == "allow"
    assert "resolved d1" in await call(bridge, "submit_answer", decision_id="d1",
                                      behavior="allow")


@pytest.mark.asyncio
async def test_runtime_doctor_describes_the_stack(bridge):
    out = js(await call(bridge, "runtime_doctor"))
    assert out["agent_health"]["running"] is True
    assert out["settings"]["kimi_bin"].endswith("kimi")
    assert isinstance(out["workspace_free_mb"], int) and out["workspace_free_mb"] > 0
    assert out["subagents"] == 0


@pytest.mark.asyncio
async def test_session_tools_reach_other_sessions(bridge):
    opened = js(await call(bridge, "open_session", cwd="", mode="plan"))
    sid = opened["session_id"]
    assert await call(bridge, "ask_in_session", session_id=sid, prompt="hi")
    assert "unknown session" in await call(bridge, "ask_in_session",
                                          session_id="ghost", prompt="hi")
    assert "compact requested" in await call(bridge, "compact_session", session_id=sid)
    assert "unknown session" in await call(bridge, "compact_session", session_id="ghost")


# --------------------------------------------------------------------- rpc
@pytest.mark.asyncio
async def test_rpc_dispatch_wraps_errors_in_jsonrpc(rpc):
    ok = await rpc.dispatch({"jsonrpc": "2.0", "id": 1, "method": "health"})
    assert ok["result"]["running"] is True
    bad = await rpc.dispatch({"jsonrpc": "2.0", "id": 2, "method": "no/such/method"})
    assert bad["error"]["code"] == -32601
    assert await rpc.dispatch({"jsonrpc": "2.0", "method": "health"}) is None, \
        "notifications must not be answered"


@pytest.mark.asyncio
async def test_rpc_requires_a_string_method(rpc):
    assert (await rpc.dispatch({"jsonrpc": "2.0", "id": 3, "method": 42}))["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_rpc_session_prompt_without_session_is_invalid_params(rpc, bridge):
    bridge.active_session_hint = ""
    bad = await rpc.dispatch({"jsonrpc": "2.0", "id": 4, "method": "session/prompt",
                              "params": {"sessionId": "", "prompt": "x"}})
    assert bad["error"]["code"] == -32602
    assert "session" in bad["error"]["message"].lower()


@pytest.mark.asyncio
async def test_rpc_tool_call_goes_through_the_same_registry(rpc):
    out = await rpc.dispatch({"jsonrpc": "2.0", "id": 5, "method": "coomi/tools/call",
                              "params": {"name": "memory_write", "args": {
                                  "name": "rpc-m", "description": "d",
                                  "type": "project", "content": "через rpc"}}})
    assert "rpc-m" in json.dumps(out["result"], ensure_ascii=False)
    listed = await rpc.dispatch({"jsonrpc": "2.0", "id": 6, "method": "coomi/tools/list"})
    assert len(listed["result"]["tools"]) >= 30


@pytest.mark.asyncio
async def test_rpc_tool_call_reports_errors_as_jsonrpc_errors(rpc):
    bad = await rpc.dispatch({"jsonrpc": "2.0", "id": 7, "method": "coomi/tools/call",
                              "params": {"name": "memory_search"}})
    # A tool that needs `query` fails validation, which MCP reports as an error
    # result rather than an HTTP-style param error; the code must simply not be 0.
    assert bad["error"]["code"] != 0
    assert "query" in bad["error"]["message"] or "Field required" in bad["error"]["message"]
    bad2 = await rpc.dispatch({"jsonrpc": "2.0", "id": 8, "method": "coomi/tools/call",
                               "params": {"name": "memory_write", "args": {
                                   "name": "x", "description": "d", "type": "nope",
                                   "content": "c"}}})
    assert bad2["error"]["code"] == -32603
    assert "type must be one of" in bad2["error"]["message"]


@pytest.mark.asyncio
async def test_rpc_describe_lists_the_acp_vocabulary(rpc):
    methods = rpc.describe()
    for name in ("initialize", "session/new", "session/prompt", "session/cancel",
                 "session/set_mode", "coomi/decide", "coomi/pending"):
        assert name in methods, name


@pytest.mark.asyncio
async def test_decide_requires_an_existing_interaction(rpc):
    """The UI and REPL send {"id": ...}; an unknown id must fail loudly, not
    silently report success."""
    from kimi_agent.rpc import RpcError

    with pytest.raises(RpcError) as excinfo:
        await rpc.call("coomi/decide", {"id": "missing", "behavior": "allow"})
    assert "missing" in str(excinfo.value)

    rpc.bridge.decisions.resolve = lambda did, body: did == "d-ok" and body.get("behavior") == "allow"
    assert await rpc.call("coomi/decide", {"id": "d-ok", "behavior": "allow"}) == {"ok": True}
    with pytest.raises(Exception):
        await rpc.call("coomi/decide", {"id": "d-other", "behavior": "reject"})


@pytest.mark.asyncio
async def test_rpc_inbox_lists_staged_files(rpc):
    from kimi_agent import store

    store.ensure_dirs()
    (store.INBOX / "from-phone.txt").write_text("x", encoding="utf-8")
    out = await rpc.call("coomi/files/inbox", {})
    assert "from-phone.txt" in json.dumps(out, ensure_ascii=False)
    assert str(store.OUTBOX) in json.dumps(out)
    (store.INBOX / "from-phone.txt").unlink()


@pytest.mark.asyncio
async def test_rpc_agent_log_returns_stderr_lines(rpc):
    out = await rpc.call("coomi/agent/log", {"lines": 1})
    assert out["lines"] == ["line 1"]
