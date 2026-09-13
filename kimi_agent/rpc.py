"""JSON-RPC 2.0 surface over the bridge.

Method names follow ACP vocabulary (initialize, session/new, session/prompt,
session/update, session/request_permission, …) so the web UI, the REPL and any
third-party client speak one language; Coomi tooling hangs off `coomi/*`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Callable

from .bridge import KimiCodeBridge
from .config import VERSION
from .events import Event


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND = -32700, -32600, -32601
INVALID_PARAMS, INTERNAL_ERROR, NOT_READY = -32602, -32603, -32002


class Rpc:
    def __init__(self, bridge: KimiCodeBridge) -> None:
        self.bridge = bridge
        self.methods: dict[str, Callable[..., Any]] = {}
        self.register_defaults()

    def method(self, name: str) -> Callable:
        def deco(fn: Callable) -> Callable:
            self.methods[name] = fn
            return fn

        return deco

    def register_defaults(self) -> None:
        bridge = self.bridge

        @self.method("initialize")
        async def initialize(params: dict[str, Any]) -> dict[str, Any]:
            info = await bridge.start()
            return {"protocolVersion": 1, "agentInfo": info.get("agentInfo"),
                    "agentCapabilities": info.get("agentCapabilities"),
                    "host": {"name": "coomi-kimi-agent", "version": VERSION},
                    "settings": bridge.settings.to_public_dict()}

        @self.method("health")
        async def health(params: dict[str, Any]) -> dict[str, Any]:
            return await bridge.health()

        @self.method("session/new")
        async def session_new(params: dict[str, Any]) -> dict[str, Any]:
            state = await bridge.new_session(
                cwd=params.get("cwd"), mode=params.get("mode"), model=params.get("model"),
                thinking=params.get("thinking"),
                additional_directories=params.get("additionalDirectories"),
            )
            return state.describe()

        @self.method("session/list")
        async def session_list(params: dict[str, Any]) -> dict[str, Any]:
            return {"live": bridge.known_sessions(),
                    "onDisk": await bridge.list_kimi_sessions(params.get("cwd"))}

        @self.method("session/load")
        async def session_load(params: dict[str, Any]) -> dict[str, Any]:
            state = await bridge.load_session(params["sessionId"], params.get("cwd"))
            return state.describe()

        @self.method("session/resume")
        async def session_resume(params: dict[str, Any]) -> dict[str, Any]:
            state = await bridge.resume_session(params["sessionId"], params.get("cwd"))
            return state.describe()

        @self.method("session/fork")
        async def session_fork(params: dict[str, Any]) -> dict[str, Any]:
            state = await bridge.fork_session(params["sessionId"], params.get("cwd"))
            return state.describe()

        @self.method("session/close")
        async def session_close(params: dict[str, Any]) -> dict[str, Any]:
            await bridge.close_session(params["sessionId"])
            return {"ok": True}

        @self.method("session/delete")
        async def session_delete(params: dict[str, Any]) -> dict[str, Any]:
            return {"ok": await bridge.delete_session(params["sessionId"])}

        @self.method("session/prompt")
        async def session_prompt(params: dict[str, Any]) -> dict[str, Any]:
            session_id = params.get("sessionId") or bridge.active_session_hint
            if not session_id:
                raise RpcError(INVALID_PARAMS, "no session: call session/new first")
            task = await bridge.prompt(
                session_id,
                params.get("prompt", ""),
                images=params.get("images"),
                resources=params.get("resources"),
            )
            wait = bool(params.get("wait", True))
            if not wait:
                return {"started": True, "sessionId": session_id}
            timeout = float(params.get("timeout", 900))
            try:
                result = await asyncio.wait_for(task, timeout=timeout)
            except asyncio.TimeoutError:
                await bridge.cancel(session_id)
                raise RpcError(INTERNAL_ERROR, f"turn exceeded {timeout:g}s and was cancelled")
            except Exception as exc:  # surfaced as JSON-RPC error below
                raise RpcError(INTERNAL_ERROR, str(exc))
            return {"sessionId": session_id, **result}

        @self.method("session/events")
        async def session_events(params: dict[str, Any]) -> dict[str, Any]:
            session_id = params.get("sessionId") or bridge.active_session_hint
            after = int(params.get("sinceSeq", 0) or 0)
            events = bridge.session_events(session_id, after)
            limit = int(params.get("limit", 500) or 500)
            return {"sessionId": session_id,
                    "events": [e.to_dict() for e in events[-limit:]],
                    "lastSeq": events[-1].seq if events else after}

        @self.method("session/cancel")
        async def session_cancel(params: dict[str, Any]) -> dict[str, Any]:
            await bridge.cancel(params["sessionId"])
            return {"ok": True}

        @self.method("session/set_mode")
        async def session_set_mode(params: dict[str, Any]) -> dict[str, Any]:
            await bridge.set_mode(params["sessionId"], params["modeId"])
            return {"ok": True, "mode": params["modeId"]}

        @self.method("session/set_config_option")
        async def session_set_config_option(params: dict[str, Any]) -> dict[str, Any]:
            payload = await bridge.set_config(
                params["sessionId"], params["configId"], params["value"]
            )
            return payload

        @self.method("coomi/decide")
        async def coomi_decide(params: dict[str, Any]) -> dict[str, Any]:
            ok = bridge.decisions.resolve(params["id"], {
                k: v for k, v in params.items() if k != "id"
            })
            if not ok:
                raise RpcError(NOT_READY, f"no pending decision with id {params['id']}")
            return {"ok": True}

        @self.method("coomi/pending")
        async def coomi_pending(params: dict[str, Any]) -> dict[str, Any]:
            return {"pending": bridge.decisions.pending}

        @self.method("coomi/tools/call")
        async def coomi_tools_call(params: dict[str, Any]) -> dict[str, Any]:
            """Invoke a Coomi tool directly (bypasses the model)."""
            from .mcp_host import call_tool

            result = await call_tool(bridge, params["name"], params.get("args") or {},
                                     session_id=params.get("sessionId"))
            return {"name": params["name"], "result": result}

        @self.method("coomi/tools/list")
        async def coomi_tools_list(params: dict[str, Any]) -> dict[str, Any]:
            from .mcp_host import tool_specs

            return {"tools": await tool_specs(bridge)}

        @self.method("coomi/sessions/prune")
        async def coomi_prune(params: dict[str, Any]) -> dict[str, Any]:
            keep = int(params.get("keep", 3))
            live = sorted(bridge.sessions.values(), key=lambda s: s.updated_at, reverse=True)
            closed = []
            for state in live[keep:]:
                if state.status != "running":
                    await bridge.close_session(state.id)
                    closed.append(state.id)
            return {"closed": closed}

        @self.method("coomi/files/inbox")
        async def coomi_inbox(params: dict[str, Any]) -> dict[str, Any]:
            from . import store

            store.ensure_dirs()
            items = []
            for path in sorted(store.INBOX.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
                if path.is_file():
                    items.append({"name": path.name, "bytes": path.stat().st_size,
                                  "mtime": path.stat().st_mtime})
            return {"inbox": items, "outbox": [str(store.OUTBOX)]}

        @self.method("coomi/agent/log")
        async def coomi_agent_log(params: dict[str, Any]) -> dict[str, Any]:
            return {"lines": bridge.stderr_tail(int(params.get("lines", 60)))}

        @self.method("coomi/raw")
        async def coomi_raw(params: dict[str, Any]) -> dict[str, Any]:
            """Escape hatch: send any ACP method straight to Kimi Code."""
            return await bridge.raw_request(params["method"], params.get("params") or {})

    # ------------------------------------------------------------ dispatch
    async def call(self, method: str, params: dict[str, Any] | None) -> Any:
        handler = self.methods.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"method not found: {method}")
        try:
            return await handler(params or {})
        except RpcError:
            raise
        except KeyError as exc:
            raise RpcError(INVALID_PARAMS, f"missing parameter: {exc.args[0] if exc.args else exc}")
        except TypeError as exc:
            raise RpcError(INVALID_PARAMS, str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    async def dispatch(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        rpc_id = payload.get("id")
        method = payload.get("method")
        if not isinstance(method, str):
            return {"jsonrpc": "2.0", "id": rpc_id,
                    "error": {"code": INVALID_REQUEST, "message": "method must be a string"}}
        try:
            result = await self.call(method, payload.get("params") or {})
        except RpcError as exc:
            error = {"code": exc.code, "message": exc.message}
            if exc.data is not None:
                error["data"] = exc.data
            return {"jsonrpc": "2.0", "id": rpc_id, "error": error}
        if rpc_id is None:
            return None
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def describe(self) -> list[str]:
        return sorted(self.methods)
