"""FileService sandbox, TerminalService and event normalization."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from kimi_agent.client import FileService, TerminalService


@pytest.fixture
def files(settings):
    return FileService(settings)


# ------------------------------------------------------------------ sandbox
def test_relative_paths_resolve_inside_the_workspace(settings, files):
    files.write("sub/hello.txt", "hi")
    assert (Path(settings.workspace) / "sub" / "hello.txt").read_text() == "hi"


def test_paths_outside_the_allowlist_are_refused(settings, files):
    # Roots are the workspace, ~/.coomi-kimi and /tmp. /etc is outside all of them.
    assert files.authorize("/etc/passwd") is None
    assert files.authorize(str(Path("/etc") / "passwd")) is None
    assert files.authorize("/usr/local/bin/proot") is None
    with pytest.raises(FileNotFoundError):
        files.read("/etc/passwd")
    with pytest.raises(FileNotFoundError):
        files.write("/etc/escaped", "x")


def test_path_traversal_cannot_escape(settings, files):
    def allowed(resolved):
        return resolved is None or any(
            str(resolved).startswith(str(root) + os.sep) or str(resolved) == str(root)
            for root in files.roots)

    for candidate in (
        "./a/../../..//etc/passwd", "../../etc/passwd", "/tmp/x/../../../../etc/passwd",
        "/etc/../../etc/passwd", "~/../root/.ssh/id_rsa", "..///etc/shadow",
    ):
        assert allowed(files.authorize(candidate)), candidate
    # realpath collapses `..` before the parent exists, so an over-long climb
    # lands on `/y.txt` and is refused; one level stays inside /tmp and passes.
    assert files.authorize("/tmp/x/../../y.txt") is None
    assert files.authorize("/tmp/a/../b.txt") == Path("/tmp/b.txt")


def test_symlink_pointing_outside_is_blocked(settings, files):
    # The pytest tmp dir lives under /tmp, which *is* an allowed root, so the
    # escape target has to be somewhere genuinely outside the allowlist.
    link = Path(settings.workspace) / "sub" / "link.txt"
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("/etc/passwd")
    # authorize() resolves the link, so the *target's* location decides.
    assert files.authorize(str(link)) is None
    with pytest.raises(FileNotFoundError):
        files.read(str(link))


def test_writing_through_an_escaping_symlink_is_blocked(settings, files):
    link = Path(settings.workspace) / "out"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to("/etc")
    with pytest.raises(FileNotFoundError):
        files.write(str(link / "payload.txt"), "x")
    assert not Path("/etc/payload.txt").exists()


def test_line_window_read(settings, files):
    files.write("many.txt", "\n".join(f"l{i}" for i in range(1, 51)))
    assert files.read("many.txt", line=10, limit=3) == "l10\nl11\nl12"
    assert files.read("many.txt").endswith("l50")


def test_binary_file_does_not_blow_up(settings, files):
    (Path(settings.workspace) / "blob.bin").write_bytes(b"\xff\xfe\x00abc")
    assert "abc" in files.read("blob.bin")


def test_directory_read_raises_io_error(settings, files):
    (Path(settings.workspace) / "adir").mkdir(exist_ok=True)
    with pytest.raises(IsADirectoryError):
        files.read("adir")


def test_the_agent_data_home_is_writable(settings, files):
    from kimi_agent import store

    assert files.authorize(str(store.HOME / "notes.txt")) is not None


# ---------------------------------------------------------------- terminal
@pytest.fixture
def terms(settings):
    return TerminalService(settings, FileService(settings))


@pytest.mark.asyncio
async def test_terminal_runs_and_captures_exit_code(terms):
    created = await terms.create("s", "sh", ["-c", "printf out; exit 3"])
    tid = created.terminal_id
    done = await terms.wait_for_exit("s", tid)
    assert done.exit_code == 3
    assert done.signal is None
    assert "out" in (await terms.output("s", tid)).output
    await terms.release("s", tid)


@pytest.mark.asyncio
async def test_terminal_respects_cwd_and_env(terms, settings):
    created = await terms.create(
        "s", "sh", ["-c", "pwd; echo $MYVAR"],
        cwd=str(settings.workspace),
        env=[{"name": "MYVAR", "value": "42"}],
    )
    done = await terms.wait_for_exit("s", created.terminal_id)
    text = (await terms.output("s", created.terminal_id)).output or ""
    assert done.exit_code == 0
    assert Path(settings.workspace).name in text
    assert "42" in text
    await terms.release("s", created.terminal_id)


@pytest.mark.asyncio
async def test_cwd_outside_the_allowlist_is_rejected(terms):
    import acp

    with pytest.raises(acp.RequestError):
        await terms.create("s", "sh", ["-c", "pwd"], cwd="/etc")


@pytest.mark.asyncio
async def test_kill_reports_the_signal(terms):
    created = await terms.create("s", "sleep", ["60"])
    await terms.kill("s", created.terminal_id)
    done = await terms.wait_for_exit("s", created.terminal_id)
    assert done.signal == "SIGKILL", "ACP reports the signal name, not a number"
    assert done.exit_code is None
    await terms.release("s", created.terminal_id)


@pytest.mark.asyncio
async def test_output_reports_the_exit_status_after_completion(terms):
    created = await terms.create("s", "sh", ["-c", "exit 7"])
    await terms.wait_for_exit("s", created.terminal_id)
    out = await terms.output("s", created.terminal_id)
    assert out.exit_status is not None and out.exit_status.exit_code == 7
    await terms.release("s", created.terminal_id)


@pytest.mark.asyncio
async def test_output_byte_limit_is_enforced(terms):
    created = await terms.create("s", "sh", ["-c", "yes x | head -n 2000"],
                                 output_byte_limit=100)
    await terms.wait_for_exit("s", created.terminal_id)
    out = await terms.output("s", created.terminal_id)
    assert len(out.output or "") <= 200
    await terms.release("s", created.terminal_id)


@pytest.mark.asyncio
async def test_unknown_terminal_raises_on_every_verb(terms):
    import acp

    for verb in (terms.output, terms.kill, terms.release, terms.wait_for_exit):
        with pytest.raises(acp.RequestError):
            await verb("s", "term_missing")


@pytest.mark.asyncio
async def test_close_all_reaps_running_processes(terms):
    created = await terms.create("s", "sleep", ["30"])
    pid = terms._terminals[created.terminal_id].pid
    await terms.close_all()
    assert terms._terminals == {}
    import os

    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail(f"pid {pid} survived close_all()")


# ------------------------------------------------------------------ events
def test_update_to_event_maps_every_known_session_update():
    import acp.schema as schema

    from kimi_agent.events import update_to_event

    text = schema.TextContentBlock(type="text", text="hello")
    cases = [
        (schema.AgentMessageChunk(sessionUpdate="agent_message_chunk", content=text), "text"),
        (schema.AgentThoughtChunk(sessionUpdate="agent_thought_chunk", content=text), "thinking"),
        (schema.UserMessageChunk(sessionUpdate="user_message_chunk", content=text), "user_message"),
        (schema.ToolCallStart(sessionUpdate="tool_call", tool_call_id="t",
                              title="Read: a", kind="read", status="pending"), "tool_call"),
        (schema.ToolCallProgress(sessionUpdate="tool_call_update", tool_call_id="t",
                                 status="completed"), "tool_update"),
        (schema.AgentPlanUpdate(sessionUpdate="plan", entries=[
            schema.PlanEntry(content="step", priority="medium", status="pending")]), "plan"),
        (schema.CurrentModeUpdate(sessionUpdate="current_mode_update",
                                  current_mode_id="plan"), "mode"),
        (schema.UsageUpdate(sessionUpdate="usage_update", used=10, size=100), "usage"),
        (schema.SessionInfoUpdate(sessionUpdate="session_info_update", title="x"), "session_info"),
        (schema.ConfigOptionUpdate(sessionUpdate="config_option_update",
                                   config_options=[]), "config"),
        (schema.AvailableCommandsUpdate(sessionUpdate="available_commands_update",
                                        available_commands=[]), "commands"),
    ]
    for update, expected in cases:
        event = update_to_event("s", update)
        assert event is not None, expected
        assert event.type == expected, (expected, event and event.type)


def test_plan_entries_keep_content_status_priority():
    import acp.schema as schema

    from kimi_agent.events import update_to_event

    plan = schema.AgentPlanUpdate(sessionUpdate="plan", entries=[
        schema.PlanEntry(content="step 1", priority="high", status="in_progress")])
    event = update_to_event("s", plan)
    assert event.data["entries"][0] == {
        "content": "step 1", "status": "in_progress", "priority": "high"}


def test_plan_removed_is_forwarded_verbatim():
    import acp.schema as schema

    from kimi_agent.events import update_to_event

    event = update_to_event("s", schema.AgentPlanRemovedUpdate(
        sessionUpdate="plan_removed", plan_id="p1"))
    assert event.type == "info"
    assert event.data["raw_type"] == "plan_removed"


def test_tool_call_payload_keeps_diff_terminal_and_locations():
    import acp.schema as schema

    from kimi_agent.events import update_to_event

    call = schema.ToolCallStart(
        sessionUpdate="tool_call", tool_call_id="t", title="Edit: a.py", kind="edit",
        status="pending",
        content=[schema.FileEditToolCallContent(type="diff", path="a.py",
                                                old_text="1", new_text="2"),
                 schema.TerminalToolCallContent(type="terminal", terminal_id="term1"),
                 schema.ContentToolCallContent(type="content", content=schema.TextContentBlock(
                     type="text", text="out"))],
        locations=[schema.ToolCallLocation(path="a.py", line=3)],
        raw_input={"path": "a.py"},
    )
    data = update_to_event("s", call).data
    assert data["input"] == {"path": "a.py"}
    assert data["locations"][0]["path"] == "a.py"
    kinds = [item["type"] for item in data["content"]]
    assert kinds == ["diff", "terminal", "content"]
    assert data["content"][0]["new_text"] == "2"
    assert data["content"][1]["terminal_id"] == "term1"
    assert data["content"][2]["text"] == "out"


def test_none_valued_fields_are_omitted():
    import acp.schema as schema

    from kimi_agent.events import update_to_event

    data = update_to_event("s", schema.ToolCallStart(
        sessionUpdate="tool_call", tool_call_id="t", title="Read: a", kind="read",
        status="pending")).data
    assert "input" not in data and "content" not in data and "locations" not in data


def test_unrouted_update_is_forwarded_as_info():
    from kimi_agent.events import update_to_event

    class Weird:
        session_update = "agent_plan_content_update"

        def model_dump(self, **kwargs):
            return {"sessionUpdate": "agent_plan_content_update", "whatever": 1}

    event = update_to_event("s", Weird())
    assert event.type == "info"
    assert event.data["raw_type"] == "agent_plan_content_update"


def test_update_without_kind_is_dropped():
    from kimi_agent.events import update_to_event

    assert update_to_event("s", {}) is None
    assert update_to_event("s", None) is None


def test_event_serialization_is_json_safe():
    import json

    from kimi_agent.events import Event

    payload = json.dumps(Event("text", "s", {"text": "привет"}).to_dict(), ensure_ascii=False)
    assert "привет" in payload
