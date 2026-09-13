"""Kimi Code ACP session manager.

One `kimi acp` subprocess serves many sessions: the manager owns the process,
the ACP connection, per-session state, the ring buffer of normalized events,
and the fan-out to live subscribers.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

import acp
from acp.schema import (
    ClientCapabilities,
    ElicitationCapabilities,
    ElicitationFormCapabilities,
    EnvVariable,
    FileSystemCapabilities,
    HttpMcpServer,
    HttpHeader,
    Implementation,
    McpServerStdio,
    SseMcpServer,
    TextContentBlock,
    ImageContentBlock,
    TextResourceContents,
    EmbeddedResourceContentBlock,
    ResourceContentBlock,
)

from .client import DecisionBroker, FileService, TerminalService
from .config import Settings, VERSION
from .events import Event, config_options_payload, update_to_event

MAX_LOG_LINES = 400


@dataclass
class SessionState:
    id: str
    cwd: str
    status: str = "idle"  # idle | running | detached
    title: str | None = None
    mode: str = "default"
    model: str | None = None
    thinking: str | None = None
    config_options: list[dict[str, Any]] = field(default_factory=list)
    available_commands: list[dict[str, Any]] = field(default_factory=list)
    plan: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_text_parts: list[str] = field(default_factory=list)
    prompt_task: asyncio.Task[Any] | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cwd": self.cwd,
            "status": self.status,
            "title": self.title,
            "mode": self.mode,
            "model": self.model,
            "thinking": self.thinking,
            "config_options": self.config_options,
            "available_commands": self.available_commands,
            "plan": self.plan,
            "usage": self.usage,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pending": self.status == "running",
        }


class KimiCodeBridge:
    """Owns the `kimi acp` child process and every session on it."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.files = FileService(settings)
        self.terminals = TerminalService(settings, self.files)
        self.listeners: list[asyncio.Queue[Event]] = []
        self.hooks: list[Callable[[Event], None]] = []
        self.decisions = DecisionBroker(settings, self.publish)
        self.sessions: dict[str, SessionState] = {}
        self._subscribers: dict[str, list[asyncio.Queue[Event]]] = {}
        self._buffer: dict[str, deque[Event]] = {}
        self._seq = 0
        self._conn: acp.ClientSideConnection | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_lines: deque[str] = deque(maxlen=MAX_LOG_LINES)
        self._stderr_task: asyncio.Task[None] | None = None
        self._exit_task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self.initialize_result: dict[str, Any] = {}
        self._mcp_servers: list[Any] = []
        # Tool-facing state: which session the model is "in" for implicit calls
        # (memory plan, current model capabilities), plus dispatched sub-agents.
        self.active_session_hint: str = ""
        self.subagents: dict[str, dict[str, Any]] = {}
        self.active_model_meta: dict[str, Any] = {}
        self._closed = False
        self.on_restart: list[Callable[[str], None]] = []

    # ---------------------------------------------------------------- start
    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> dict[str, Any]:
        async with self._start_lock:
            if self.running and self._conn is not None:
                return self.initialize_result
            await self._teardown(kill=True)
            self._closed = False

            process = await asyncio.create_subprocess_exec(
                self.settings.kimi_bin,
                *self.settings.acp_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.settings.workspace),
                env=self.settings.kimi_env(),
                limit=64 * 1024 * 1024,
            )
            assert process.stdin is not None and process.stdout is not None
            conn = acp.connect_to_agent(
                _ClientFacade(self),
                process.stdin,
                process.stdout,
                # Kimi's AskUserQuestion arrives as `elicitation/create`, which
                # the SDK only routes when the unstable surface is enabled.
                use_unstable_protocol=True,
            )
            self._process = process
            self._conn = conn
            self._stderr_task = asyncio.create_task(self._pump_stderr(process))
            self._exit_task = asyncio.create_task(self._watch_exit(process))

            # Read the tool bridge config before any session is created, so that
            # session/new advertises the Coomi MCP server.
            if os.environ.get("COOMI_KIMI_SKIP_MCP", "") in ("1", "true", "yes"):
                self._mcp_servers = []
            else:
                count = self.load_mcp_config()
                self.publish(Event("info", "", {
                    "message": f"coomi MCP bridge: {count} server(s) advertised"
                    if count else "no MCP servers configured (tools not exposed to the model)"}))

            capabilities = ClientCapabilities(
                fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                terminal=True,
                elicitation=ElicitationCapabilities(form=ElicitationFormCapabilities()),
                auth=None,
                session=None,
                plan=None,
            )
            response = await conn.initialize(
                protocol_version=1,
                client_capabilities=capabilities,
                client_info=Implementation(name="coomi-kimi-agent", version=VERSION),
            )
            dump = response.model_dump(by_alias=True, exclude_none=True)
            self.initialize_result = dump
            self.publish(Event("info", "", {"message": "agent initialized", "agent": dump.get("agentInfo"),
                                            "capabilities": dump.get("agentCapabilities")}))
            return dump

    async def _pump_stderr(self, process: asyncio.subprocess.Process) -> None:
        assert process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            self._stderr_lines.append(text)
            if "ERROR" in text or "error" in text.lower():
                self.publish(Event("agent_log", "", {"line": text}))

    async def _watch_exit(self, process: asyncio.subprocess.Process) -> None:
        code = await process.wait()
        if self._closed:
            return
        tail = list(self._stderr_lines)[-20:]
        for session in self.sessions.values():
            session.status = "detached"
            if session.prompt_task and not session.prompt_task.done():
                session.prompt_task.cancel()
            self.decisions.cancel_all(session.id, reason="agent exited")
        self.publish(Event("agent_exited", "", {"exit_code": code, "stderr_tail": tail}))
        self._conn = None
        self._process = None

    async def _teardown(self, kill: bool = False) -> None:
        self._closed = True
        for task in (self._stderr_task, self._exit_task):
            if task and not task.done():
                task.cancel()
        process = self._process
        if process is not None:
            if kill and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            # Reap before the loop closes: dropping an asyncio subprocess transport
            # without waiting leaks a "Event loop is closed" traceback at exit.
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        await self.terminals.close_all()
        self.decisions.cancel_all(None, reason="shutdown")
        self._conn = None
        self._process = None

    async def close(self) -> None:
        for session in self.sessions.values():
            try:
                await self.close_session(session.id)
            except Exception:
                pass
        await self._teardown(kill=True)

    async def restart(self, reason: str = "") -> None:
        await self._teardown(kill=True)
        await self.start()
        self.publish(Event("agent_restarted", "", {"reason": reason}))

    # ---------------------------------------------------------------- events
    def publish(self, event: Event) -> None:
        self._seq += 1
        event.seq = self._seq
        if event.session_id:
            state = self.sessions.get(event.session_id)
            if state is not None:
                state.updated_at = event.ts
            buffer = self._buffer.setdefault(event.session_id, deque(maxlen=4000))
            buffer.append(event)
            for queue in self._subscribers.get(event.session_id, []):
                queue.put_nowait(event)
        for queue in self.listeners:
            queue.put_nowait(event)
        for hook in self.hooks:
            try:
                hook(event)
            except Exception:  # a broken observer must never kill the bridge
                pass

    def session_events(self, session_id: str, since_seq: int = 0) -> list[Event]:
        return [e for e in self._buffer.get(session_id, ()) if e.seq > since_seq]

    def subscribe_session(self, session_id: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.setdefault(session_id, []).append(queue)
        return queue

    def unsubscribe_session(self, session_id: str, queue: asyncio.Queue[Event]) -> None:
        subs = self._subscribers.get(session_id, [])
        if queue in subs:
            subs.remove(queue)

    # ---------------------------------------------------------------- session
    def advertised_mcp_servers(self) -> list[Any]:
        return list(self._mcp_servers)

    def load_mcp_config(self, path: Path | None = None) -> int:
        """Read the bridge MCP config so new sessions advertise our tools.

        Loopback URLs are rewritten against the *active* bridge port: the config
        file ships a default, but `--port` moves the endpoint, and advertising a
        stale URL leaves the model with no tools at all.
        """
        target = path or self.settings.mcp_config_path
        try:
            raw = json.loads(Path(target).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._mcp_servers = []
            return 0
        servers: list[Any] = []
        for name, spec in (raw.get("mcpServers") or {}).items():
            if spec.get("enabled") is False:
                continue
            if spec.get("command"):
                servers.append(McpServerStdio(
                    name=name,
                    command=str(spec["command"]),
                    args=[str(a) for a in spec.get("args") or []],
                    env=[EnvVariable(name=k, value=str(v))
                         for k, v in (spec.get("env") or {}).items()],
                ))
            elif spec.get("url"):
                url = self._retarget_bridge_url(str(spec["url"]))
                headers = [HttpHeader(name=k, value=str(v))
                           for k, v in (spec.get("headers") or {}).items()]
                # The SDK serialises mcpServers with exclude_unset, and the
                # discriminated union needs `type` present in the wire payload,
                # so it must be passed explicitly rather than left to default.
                if spec.get("transport") == "sse":
                    servers.append(SseMcpServer(name=name, url=url,
                                                headers=headers, type="sse"))
                else:
                    servers.append(HttpMcpServer(name=name, url=url,
                                                 headers=headers, type="http"))
        self._mcp_servers = servers
        return len(servers)

    def _retarget_bridge_url(self, url: str) -> str:
        """Point a loopback MCP url at host:bridge_port while keeping its path."""
        import urllib.parse

        try:
            parts = urllib.parse.urlsplit(url)
        except ValueError:
            return url
        if parts.hostname not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
            return url  # a genuinely remote server is not ours to rewrite
        host = "127.0.0.1" if self.settings.host in ("0.0.0.0", "::") else self.settings.host
        rebuilt = parts._replace(netloc=f"{host}:{self.settings.bridge_port}")
        return urllib.parse.urlunsplit(rebuilt)

    async def new_session(
        self,
        cwd: str | None = None,
        additional_directories: list[str] | None = None,
        mode: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        mcp_servers: list[Any] | None = None,
    ) -> SessionState:
        conn = await self._require_conn()
        workdir = str(Path(cwd or self.settings.workspace).resolve())
        extra = additional_directories or list(self.settings.additional_dirs)
        response = await conn.new_session(
            cwd=workdir,
            additional_directories=extra or None,
            mcp_servers=mcp_servers if mcp_servers is not None else (self.advertised_mcp_servers() or None),
        )
        state = SessionState(id=response.session_id, cwd=workdir)
        state.config_options = config_options_payload(getattr(response, "config_options", None)).get(
            "config_options", []
        )
        modes = getattr(response, "modes", None)
        if modes is not None:
            state.mode = getattr(modes, "current_mode_id", "default") or "default"
        for option in state.config_options:
            if option["id"] == "model":
                state.model = option.get("current_value")
            if option["id"] == "thinking":
                state.thinking = option.get("current_value")
        self.sessions[state.id] = state
        self._buffer.setdefault(state.id, deque(maxlen=4000))
        self.active_session_hint = state.id
        if state.model:
            self.active_model_meta = self._model_meta(state.model)
        self.publish(Event("info", state.id, {"message": "session created", "session": state.describe()}))
        try:
            await self.apply_defaults(state, mode=mode, model=model, thinking=thinking)
        except Exception as exc:  # non-fatal: defaults are a convenience
            self.publish(Event("info", state.id, {"message": f"defaults not applied: {exc}"}))
        return state

    async def apply_defaults(
        self,
        state: SessionState,
        mode: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> None:
        conn = await self._require_conn()
        mode = mode or self.settings.default_mode
        model = model or self.settings.default_model
        thinking = thinking or self.settings.default_thinking
        if mode and mode != state.mode:
            try:
                await conn.set_session_mode(state.id, mode)
                state.mode = mode
            except acp.RequestError as exc:
                self.publish(Event("info", state.id, {"message": f"set_mode({mode}) failed: {exc}"}))
        if model:
            try:
                await conn.set_config_option("model", state.id, model)
                state.model = model
            except acp.RequestError as exc:
                self.publish(Event("info", state.id, {"message": f"set_model({model}) failed: {exc}"}))
        if thinking:
            try:
                await conn.set_config_option("thinking", state.id, thinking)
                state.thinking = thinking
            except acp.RequestError as exc:
                self.publish(Event("info", state.id, {"message": f"set_thinking({thinking}) failed: {exc}"}))

    async def load_session(self, session_id: str, cwd: str | None = None) -> SessionState:
        conn = await self._require_conn()
        workdir = str(Path(cwd or self.settings.workspace).resolve())
        await conn.load_session(cwd=workdir, session_id=session_id)
        state = self.sessions.get(session_id) or SessionState(id=session_id, cwd=workdir)
        state.status = "idle"
        self.sessions[session_id] = state
        return state

    async def resume_session(self, session_id: str, cwd: str | None = None) -> SessionState:
        conn = await self._require_conn()
        workdir = str(Path(cwd or self.settings.workspace).resolve())
        await conn.resume_session(session_id, workdir)
        state = self.sessions.get(session_id) or SessionState(id=session_id, cwd=workdir)
        state.status = "idle"
        self.sessions[session_id] = state
        return state

    async def list_kimi_sessions(self, cwd: str | None = None) -> list[dict[str, Any]]:
        conn = await self._require_conn()
        response = await conn.list_sessions(cwd=str(Path(cwd or self.settings.workspace).resolve()))
        out = []
        for item in getattr(response, "sessions", None) or []:
            dump = item.model_dump(by_alias=True, exclude_none=True)
            out.append(dump)
        return out

    async def fork_session(self, session_id: str, cwd: str | None = None) -> SessionState:
        conn = await self._require_conn()
        response = await conn.fork_session(session_id, str(Path(cwd or self.sessions[session_id].cwd).resolve()))
        parent = self.sessions.get(session_id)
        new_id = getattr(response, "session_id", None) or session_id
        state = SessionState(
            id=new_id,
            cwd=str(Path(cwd or (parent.cwd if parent else self.settings.workspace)).resolve()),
            title=f"{parent.title or session_id} (fork)" if parent else None,
            mode=parent.mode if parent else "default",
        )
        if parent:
            state.config_options = list(parent.config_options)
            state.model, state.thinking = parent.model, parent.thinking
        self.sessions[new_id] = state
        self.publish(Event("info", new_id, {"message": "session forked", "from": session_id}))
        return state

    async def close_session(self, session_id: str) -> None:
        conn = self._conn
        state = self.sessions.get(session_id)
        if state and state.prompt_task and not state.prompt_task.done():
            await self.cancel(session_id)
        if conn is not None and self.running:
            try:
                await conn.close_session(session_id)
            except acp.RequestError:
                pass
        if state:
            state.status = "idle"
        self.decisions.forget(session_id)

    async def delete_session(self, session_id: str) -> bool:
        state = self.sessions.pop(session_id, None)
        if state and state.prompt_task and not state.prompt_task.done():
            state.prompt_task.cancel()
        self.decisions.forget(session_id)
        conn = self._conn
        if conn is not None and self.running:
            try:
                # `ext_method` prefixes `_`, which is the JSON-RPC extension
                # namespace; `session/delete` is a real (non-extension) method,
                # so it has to go through the raw connection.
                raw = getattr(conn, "_conn", None)
                if raw is None:
                    raise acp.RequestError.method_not_found("session/delete")
                await raw.send_request("session/delete", {"sessionId": session_id})
                self._buffer.pop(session_id, None)
                return True
            except acp.RequestError as exc:
                self.publish(Event("info", session_id, {"message": f"delete failed: {exc}"}))
        return False

    def known_sessions(self) -> list[dict[str, Any]]:
        return [state.describe() for state in self.sessions.values()]

    def _model_meta(self, model_id: str) -> dict[str, Any]:
        """Model metadata from the advertised picker (capabilities if present)."""
        state = self.sessions.get(self.active_session_hint)
        for option in (state.config_options if state else []):
            if option.get("id") != "model":
                continue
            for choice in option.get("options", []):
                if choice.get("value") == model_id:
                    return {"capabilities": choice.get("capabilities") or [],
                            "name": choice.get("name")}
        return {}

    # ------------------------------------------------------- tool surface
    async def publish_local_plan(self, plan: dict[str, Any]) -> None:
        """Surface a tool-authored plan as a normal ACP `plan` update."""
        session_id = plan.get("session_id") or self.active_session_hint
        entries = [
            {"content": step.get("step", ""), "status": step.get("status", "pending"),
             "priority": "medium"}
            for step in plan.get("steps", [])
        ]
        self.publish(Event("plan", session_id, {"entries": entries, "source": "update_plan"}))

    async def push_image(self, path: str, mime_type: str, data_b64: str, caption: str) -> None:
        """Stream an image into the chat as a normal agent message chunk."""
        session_id = self.active_session_hint
        payload: dict[str, Any] = {"image": {"data": data_b64, "mime_type": mime_type, "uri": path},
                                   "path": path, "caption": caption}
        if caption:
            payload["text"] = f"{caption}\n"
        self.publish(Event("text", session_id, payload))

    async def attach_image_to_context(self, session_id: str, path: str) -> None:
        """Feed an image back as the *user side* of the next turn so the model sees it.

        ACP has no 'inject into history' call; the honest equivalent is a turn
        whose content is the image plus a directive, which Kimi sends to the model.
        """
        import base64

        raw = Path(path).read_bytes()
        mime = "image/png"
        for prefix, guess in ((b"\x89PNG", "image/png"), (b"\xff\xd8", "image/jpeg"),
                              (b"GIF8", "image/gif"), (b"RIFF", "image/webp")):
            if raw.startswith(prefix):
                mime = guess
                break
        await self.prompt(
            session_id,
            "I have attached the image above (also visible at "
            f"{path}). Look at it before answering; I will ask about it next.",
            images=[{"data": base64.b64encode(raw).decode(), "mime_type": mime, "uri": f"file://{path}"}],
        )

    async def notify_outbox(self, path: str, size: int) -> None:
        """Announce a staged export so UIs can offer a save action."""
        self.publish(Event("info", self.active_session_hint, {
            "message": "file ready to save", "export": path, "bytes": size,
        }))

    # ---------------------------------------------------------------- prompt
    async def prompt(
        self,
        session_id: str,
        text: str,
        images: list[dict[str, Any]] | None = None,
        resources: list[dict[str, Any]] | None = None,
        blocks: list[Any] | None = None,
    ) -> asyncio.Task[Any]:
        conn = await self._require_conn()
        state = self.sessions.get(session_id)
        if state is None:
            raise ValueError(f"unknown session: {session_id}")
        if state.prompt_task and not state.prompt_task.done():
            raise RuntimeError("session is already running a turn")
        self.active_session_hint = session_id

        content: list[Any] = list(blocks or [])
        if text:
            content.append(TextContentBlock(type="text", text=text))
        for image in images or []:
            content.append(ImageContentBlock(
                type="image",
                data=image["data"],
                mime_type=image.get("mime_type") or "image/png",
                uri=image.get("uri"),
            ))
        for resource in resources or []:
            if "text" in resource:
                content.append(EmbeddedResourceContentBlock(
                    type="resource",
                    resource=TextResourceContents(
                        uri=resource.get("uri") or f"file://{resource.get('name', 'resource')}",
                        text=resource["text"],
                        mime_type=resource.get("mime_type"),
                    ),
                ))
            else:
                content.append(ResourceContentBlock(
                    type="resource_link",
                    uri=resource.get("uri", ""),
                    name=resource.get("name") or resource.get("uri", "resource"),
                    title=resource.get("title"),
                    mime_type=resource.get("mime_type"),
                ))
        if not content:
            raise ValueError("empty prompt")

        self.publish(Event("turn_started", session_id, {"text": text[:2000] if text else "",
                                                        "parts": len(content)}))
        state.status = "running"
        state.last_text_parts = []
        task = asyncio.create_task(self._run_prompt(session_id, content), name=f"prompt:{session_id}")
        state.prompt_task = task
        return task

    async def _run_prompt(self, session_id: str, content: list[Any]) -> dict[str, Any]:
        conn = await self._require_conn()
        state = self.sessions[session_id]
        try:
            response = await conn.prompt(session_id, content)
            reason = getattr(response, "stop_reason", "end_turn")
            event_type = "turn_cancelled" if reason == "cancelled" else "turn_completed"
            usage = response.model_dump(by_alias=True, exclude_none=True).get("usage")
            self.publish(Event(event_type, session_id, {
                "stop_reason": reason,
                "text": "".join(state.last_text_parts),
                "usage": usage,
            }))
            return {"stop_reason": reason, "usage": usage}
        except asyncio.CancelledError:
            self.publish(Event("turn_cancelled", session_id, {"stop_reason": "cancelled"}))
            raise
        except acp.RequestError as exc:
            self.publish(Event("turn_failed", session_id, {
                "error": str(exc),
                "code": getattr(exc, "code", None),
                "data": getattr(exc, "data", None),
            }))
            raise
        except Exception as exc:
            self.publish(Event("turn_failed", session_id, {"error": f"{type(exc).__name__}: {exc}"}))
            raise
        finally:
            state.status = "idle"

    async def cancel(self, session_id: str) -> None:
        conn = await self._require_conn()
        await conn.cancel(session_id)
        self.decisions.cancel_all(session_id, reason="user cancelled")

    async def set_mode(self, session_id: str, mode_id: str) -> None:
        conn = await self._require_conn()
        await conn.set_session_mode(session_id, mode_id)
        state = self.sessions.get(session_id)
        if state:
            state.mode = mode_id

    async def set_config(self, session_id: str, config_id: str, value: str | bool) -> dict[str, Any]:
        conn = await self._require_conn()
        state = self.sessions.get(session_id)
        if state is not None and isinstance(value, str):
            # Kimi validates picker values against the advertised option set and
            # answers with an opaque error; check first so the message is useful.
            option = next((o for o in state.config_options if o["id"] == config_id), None)
            if option is not None:
                allowed = [str(c["value"]) for c in option.get("options", [])]
                if allowed and value not in allowed:
                    raise ValueError(
                        f"unknown {config_id} value {value!r}; pick one of {allowed}"
                    )
        response = await conn.set_config_option(config_id, session_id, value)
        payload = config_options_payload(getattr(response, "config_options", None))
        state = self.sessions.get(session_id)
        if state:
            state.config_options = payload["config_options"]
            for option in state.config_options:
                if option["id"] == "model":
                    state.model = option.get("current_value")
                elif option["id"] == "thinking":
                    state.thinking = option.get("current_value")
                elif option["id"] == "mode":
                    state.mode = option.get("current_value") or state.mode
        self.publish(Event("config", session_id, payload))
        return payload

    async def raw_request(self, method: str, params: dict[str, Any]) -> Any:
        """Call an ACP method we do not wrap (e.g. `providers/list`)."""
        conn = await self._require_conn()
        return await conn.ext_method(method, params)

    # ---------------------------------------------------------------- plumbing
    async def _require_conn(self) -> acp.ClientSideConnection:
        if not self.running or self._conn is None:
            await self.start()
        if self._conn is None:
            raise RuntimeError("Kimi Code ACP connection unavailable")
        return self._conn

    def apply_update(self, notification: Any) -> None:
        event = update_to_event(notification.session_id, notification.update)
        if event is None:
            return
        state = self.sessions.get(notification.session_id)
        if state is not None:
            if event.type == "text" and event.data.get("text"):
                state.last_text_parts.append(event.data["text"])
            elif event.type == "plan":
                state.plan = event.data.get("entries", [])
            elif event.type == "commands":
                state.available_commands = event.data.get("commands", [])
            elif event.type == "session_info" and event.data.get("title"):
                state.title = event.data["title"]
            elif event.type == "usage":
                state.usage = event.data
        self.publish(event)

    def stderr_tail(self, lines: int = 40) -> list[str]:
        return list(self._stderr_lines)[-lines:]

    async def health(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "kimi_bin": self.settings.kimi_bin,
            "agent": self.initialize_result.get("agentInfo"),
            "capabilities": self.initialize_result.get("agentCapabilities"),
            "sessions": len(self.sessions),
            "mcp_servers": [getattr(s, "name", None) for s in self._mcp_servers],
            "pending_decisions": self.decisions.pending,
            "stderr_tail": self.stderr_tail(12),
        }


class _ClientFacade:
    """Adapts the bridge to the ACP `Client` protocol used by the SDK router."""

    def __init__(self, bridge: KimiCodeBridge) -> None:
        self.bridge = bridge
        self._agent: Any = None

    def on_connect(self, conn: Any) -> None:
        self._agent = conn

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        self.bridge.apply_update(
            acp.schema.SessionNotification(session_id=session_id, update=update)
        )

    async def request_permission(self, options: Any, session_id: str, tool_call: Any, **kwargs: Any) -> Any:
        return await self.bridge.decisions.request_permission(session_id, tool_call, options)

    async def read_text_file(self, path: str, session_id: str, line: Any = None, limit: Any = None, **kwargs: Any) -> Any:
        text = self.bridge.files.read(path, line=line, limit=limit)
        return acp.schema.ReadTextFileResponse(content=text)

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs: Any) -> Any:
        self.bridge.files.write(path, content)
        return acp.schema.WriteTextFileResponse()

    async def create_terminal(self, command: str, session_id: str, args: Any = None, cwd: Any = None,
                              env: Any = None, output_byte_limit: Any = None, **kwargs: Any) -> Any:
        return await self.bridge.terminals.create(
            session_id, command, args=args, cwd=cwd, env=env, output_byte_limit=output_byte_limit
        )

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self.bridge.terminals.output(session_id, terminal_id)

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self.bridge.terminals.wait_for_exit(session_id, terminal_id)

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self.bridge.terminals.kill(session_id, terminal_id)

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        return await self.bridge.terminals.release(session_id, terminal_id)

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any) -> Any:
        session_id = kwargs.pop("session_id", "") or getattr(mode, "session_id", "") or ""
        return await self.bridge.decisions.create_elicitation(
            message, mode, session_id=session_id,
            tool_call_id=kwargs.get("tool_call_id") or getattr(mode, "tool_call_id", None),
            requested_schema=kwargs.get("requested_schema") or getattr(mode, "requested_schema", None),
        )

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        return None
