"""HTTP surface: web UI, WebSocket event stream, JSON-RPC endpoint, MCP bridge.

Bind address defaults to 127.0.0.1 (this environment only reaches loopback from
outside the guest; LAN exposure is opt-in via COOMI_KIMI_HOST=0.0.0.0 plus a
host-side port forward).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.web_runner import AppRunner

from .bridge import KimiCodeBridge
from .config import WEB_DIR, VERSION, Settings
from .events import Event
from .mcp_host import serve_http
from .rpc import Rpc

logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


@web.middleware
async def _json_errors(request: web.Request, handler) -> web.Response:
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except Exception as exc:  # keep the socket alive, report honestly
        log = request.app["log"]
        log.exception("handler failed: %s %s", request.method, request.path)
        return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)


def _log_path(settings: Settings) -> Path:
    from . import store

    store.ensure_dirs()
    return store.HOME / "agent-server.log"


def create_app(settings: Settings, bridge: KimiCodeBridge, rpc: Rpc) -> web.Application:
    app = web.Application(middlewares=[_json_errors])
    app["settings"] = settings
    app["bridge"] = bridge
    app["rpc"] = rpc
    app["log"] = logging.getLogger("coomi-kimi")
    app["clients"] = set()

    app.router.add_get("/", lambda r: web.FileResponse(WEB_DIR / "index.html"))
    app.router.add_get("/app.js", lambda r: web.FileResponse(WEB_DIR / "app.js"))
    app.router.add_get("/app.css", lambda r: web.FileResponse(WEB_DIR / "app.css"))
    app.router.add_get("/health", handle_health)
    app.router.add_get("/api/health", handle_health)
    app.router.add_post("/rpc", handle_rpc)
    app.router.add_post("/api/prompt", handle_prompt)
    app.router.add_get("/api/sessions", handle_sessions)
    app.router.add_get("/api/tools", handle_tools)
    app.router.add_get("/events", handle_sse)
    app.router.add_get("/ws", handle_ws)
    app.router.add_post("/decide", handle_decide)
    app.router.add_get("/file", handle_file)
    # The settings screen is what makes the phone build usable: without it there
    # is no way to type an API key into ~/.kimi-code/config.toml.
    app.router.add_get("/api/settings", handle_get_settings)
    app.router.add_post("/api/settings", handle_post_settings)
    app.router.add_post("/api/restart", handle_restart)
    app.router.add_static("/web/", WEB_DIR, show_index=False)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    app.cleanup_ctx.append(broadcast_ctx)
    return app


async def on_startup(app: web.Application) -> None:
    settings: Settings = app["settings"]
    bridge: KimiCodeBridge = app["bridge"]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler(_log_path(settings), encoding="utf-8")],
        force=True,
    )
    app["mcp_task"] = None
    if os.environ.get("COOMI_KIMI_SKIP_MCP", "") not in ("1", "true", "yes"):
        try:
            app["mcp_task"] = await serve_http(bridge, "127.0.0.1", settings.bridge_port)
            app["log"].info("coomi MCP tools on http://127.0.0.1:%d/mcp", settings.bridge_port)
        except Exception as exc:  # the agent still works without our tools
            app["log"].warning("MCP bridge disabled: %s", exc)
    try:
        await bridge.start()
        app["log"].info("Kimi Code attached: %s", bridge.initialize_result.get("agentInfo"))
    except Exception as exc:
        app["log"].error("Kimi Code failed to start: %s", exc)


async def on_shutdown(app: web.Application) -> None:
    task = app.get("mcp_task")
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    for ws in list(app["clients"]):
        await ws.close()
    await app["bridge"].close()


async def broadcast_ctx(app: web.Application):
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=2000)
    app["bridge"].listeners.append(queue)
    app["_event_queue"] = queue

    async def _pump() -> None:
        while True:
            event = await queue.get()
            payload = json.dumps(event.to_dict(), ensure_ascii=False)
            dead = []
            for ws in list(app["clients"]):
                try:
                    await ws.send_str(payload)
                except (ConnectionError, RuntimeError):
                    dead.append(ws)
            for ws in dead:
                app["clients"].discard(ws)

    pump = asyncio.create_task(_pump())
    yield
    pump.cancel()
    try:
        await pump
    except asyncio.CancelledError:
        pass
    app["bridge"].listeners.remove(queue)


# ------------------------------------------------------------------ handlers
async def handle_health(request: web.Request) -> web.Response:
    bridge: KimiCodeBridge = request.app["bridge"]
    health = await bridge.health()
    health["version"] = VERSION
    health["rpc_methods"] = request.app["rpc"].describe()
    return web.json_response(health)


async def handle_rpc(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return web.json_response(
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32700, "message": "invalid JSON body"}}, status=400
        )
    if isinstance(payload, list):
        responses = [await request.app["rpc"].dispatch(item) for item in payload]
        return web.json_response([r for r in responses if r is not None])
    response = await request.app["rpc"].dispatch(payload)
    return web.json_response(response or {"jsonrpc": "2.0", "id": payload.get("id"), "result": None})


async def handle_prompt(request: web.Request) -> web.Response:
    """Plain-REST convenience wrapper over session/prompt."""
    body = await request.json()
    rpc: Rpc = request.app["rpc"]
    response = await rpc.dispatch({
        "jsonrpc": "2.0", "id": 1, "method": "session/prompt",
        "params": {
            "sessionId": body.get("sessionId", ""),
            "prompt": body.get("prompt", body.get("text", "")),
            "images": body.get("images"),
            "wait": bool(body.get("wait", False)),
        },
    })
    status = 200 if "result" in response else 400
    return web.json_response(response.get("result") or response.get("error"), status=status)


async def handle_sessions(request: web.Request) -> web.Response:
    rpc: Rpc = request.app["rpc"]
    result = await rpc.call("session/list", {"cwd": request.query.get("cwd")})
    return web.json_response(result)


async def handle_tools(request: web.Request) -> web.Response:
    from .mcp_host import tool_specs

    tools = await tool_specs(request.app["bridge"])
    return web.json_response({"tools": tools, "count": len(tools)})


async def handle_decide(request: web.Request) -> web.Response:
    body = await request.json()
    bridge: KimiCodeBridge = request.app["bridge"]
    ok = bridge.decisions.resolve(body.get("id", ""), {
        k: v for k, v in body.items() if k != "id"
    })
    return web.json_response({"ok": ok}, status=200 if ok else 404)


async def handle_file(request: web.Request) -> web.Response:
    """Serve sandbox files (images the agent displayed, exports)."""
    import mimetypes

    bridge: KimiCodeBridge = request.app["bridge"]
    path = bridge.files.authorize(request.query.get("path", ""))
    if path is None or not path.is_file():
        raise web.HTTPNotFound(text="not in the sandbox or missing")
    guess, _ = mimetypes.guess_type(str(path))
    return web.FileResponse(path, headers={"Content-Type": guess or "application/octet-stream"})


def _require_loopback(request: web.Request) -> None:
    """Refuse to write credentials when the console is exposed beyond loopback.

    The RPC surface is unauthenticated by design (it is a local tool), so a
    provider write — which stores an API key and then lets the agent execute
    commands — must not be reachable from a network socket.
    """
    settings: Settings = request.app["settings"]
    if settings.host in ("127.0.0.1", "localhost", "::1"):
        return
    expected = os.environ.get("COOMI_KIMI_TOKEN", "")
    if expected and request.headers.get("X-Coomi-Token", "") == expected:
        return
    raise web.HTTPForbidden(
        text="settings are disabled while the host is not loopback; "
             "set COOMI_KIMI_TOKEN and send it as X-Coomi-Token to allow it"
    )


async def handle_get_settings(request: web.Request) -> web.Response:
    from .config import read_provider_summary

    settings: Settings = request.app["settings"]
    payload = read_provider_summary()
    payload["permission_policy"] = settings.permission_policy
    payload["model_in_use"] = settings.default_model
    payload["host"] = settings.host
    return web.json_response(payload)


async def handle_post_settings(request: web.Request) -> web.Response:
    from .config import read_provider_summary, write_provider_config

    _require_loopback(request)
    body = await request.json()
    settings: Settings = request.app["settings"]
    bridge: KimiCodeBridge = request.app["bridge"]
    log = request.app["log"]

    def _caps(raw) -> list[str]:
        if isinstance(raw, str):
            return [c.strip() for c in raw.split(",") if c.strip()]
        return [str(c) for c in (raw or [])]

    try:
        summary = write_provider_config(
            name=body.get("provider") or "custom",
            ptype=body.get("provider_type") or "openai",
            base_url=body.get("base_url", ""),
            # An empty field means "keep the stored key", so the UI never has to
            # echo a secret back. read_provider_summary only exposes has_key.
            api_key=body.get("api_key", "") or _stored_key(body.get("provider") or "custom"),
            model=body.get("model", ""),
            max_context_size=int(body.get("max_context_size") or 204800),
            capabilities=_caps(body.get("capabilities")),
            display_name=body.get("display_name", ""),
            compact_at_percent=int(body.get("compact_at_percent") or 0),
            permission_mode=body.get("permission_mode", ""),
            thinking_enabled=body.get("thinking_enabled"),
            thinking_effort=body.get("thinking_effort", ""),
            use_env_subtable=bool(body.get("use_env_subtable")),
        )
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from exc

    # The kernel reads config.toml at startup and caches model metadata, so a
    # saved provider only takes effect after a restart.
    if body.get("restart", True):
        settings.default_model = summary.get("default_model", settings.default_model)
        if summary.get("permission_mode"):
            settings.permission_policy = summary["permission_mode"]
        try:
            await bridge.restart(reason="provider settings saved")
            summary["restarted"] = True
        except Exception as exc:  # noqa: BLE001 - report, never lose the save
            log.exception("restart after settings write failed")
            summary["restarted"] = False
            summary["restart_error"] = str(exc)
    return web.json_response(summary)


def _stored_key(name: str) -> str:
    """Re-read the key already on disk for *name*, so a blank UI field keeps it."""
    import tomllib

    from .config import PROVIDER_TYPES, provider_config_path

    try:
        data = tomllib.loads(provider_config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    prov = (data.get("providers") or {}).get(name) or {}
    if prov.get("api_key"):
        return str(prov["api_key"])
    env = prov.get("env") or {}
    spec = PROVIDER_TYPES.get(prov.get("type", ""), {})
    return str(env.get(spec.get("key", ""), ""))


async def handle_restart(request: web.Request) -> web.Response:
    """Restart the Kimi Code kernel — the Android shell needs a way to pick up a
    rewritten config.toml without killing the service."""
    _require_loopback(request)
    bridge: KimiCodeBridge = request.app["bridge"]
    try:
        await bridge.restart(reason="requested from the console")
    except Exception as exc:  # noqa: BLE001
        raise web.HTTPInternalServerError(text=f"restart failed: {exc}") from exc
    return web.json_response({"ok": True})


async def handle_sse(request: web.Request) -> web.Response:
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)
    request.app["bridge"].listeners.append(queue)
    try:
        await response.write(b"retry: 3000\n\n")
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=15)
                blob = f"data: {json.dumps(event.to_dict(), ensure_ascii=False)}\n\n"
            except asyncio.TimeoutError:
                blob = ": keepalive\n\n"
            await response.write(blob.encode())
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            request.app["bridge"].listeners.remove(queue)
        except ValueError:
            pass
    return response


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=32 * 1024 * 1024, heartbeat=30)
    await ws.prepare(request)
    request.app["clients"].add(ws)
    rpc: Rpc = request.app["rpc"]
    bridge: KimiCodeBridge = request.app["bridge"]

    hello = {
        "type": "hello",
        "version": VERSION,
        "agent": bridge.initialize_result.get("agentInfo"),
        "sessions": bridge.known_sessions(),
        "pending": bridge.decisions.pending,
        "methods": rpc.describe(),
    }
    await ws.send_str(json.dumps(hello, ensure_ascii=False))
    try:
        async for message in ws:
            if not message.data:
                continue
            try:
                payload = json.loads(message.data)
            except json.JSONDecodeError:
                await ws.send_str(json.dumps({"type": "rpc_error", "error": "invalid JSON"}))
                continue
            if payload.get("method") == "subscribe":
                session_id = payload.get("params", {}).get("sessionId", "")
                after = int(payload.get("params", {}).get("sinceSeq", 0) or 0)
                for event in bridge.session_events(session_id, after)[-400:]:
                    await ws.send_str(json.dumps(event.to_dict(), ensure_ascii=False))
                await ws.send_str(json.dumps({"type": "subscribed", "sessionId": session_id}))
                continue
            response = await rpc.dispatch(payload)
            if response is not None:
                await ws.send_str(json.dumps(response, ensure_ascii=False))
    finally:
        request.app["clients"].discard(ws)
    return ws


def run(settings: Settings) -> None:
    bridge = KimiCodeBridge(settings)
    rpc = Rpc(bridge)
    app = create_app(settings, bridge, rpc)
    logging.getLogger("coomi-kimi").info(
        "coomi-kimi %s — UI http://%s:%d", VERSION, settings.host, settings.port
    )
    web.run_app(app, host=settings.host, port=settings.port, print=None, access_log=None)


if __name__ == "__main__":  # pragma: no cover
    from .config import load_settings

    run(load_settings())
