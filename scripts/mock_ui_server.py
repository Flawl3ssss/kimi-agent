#!/usr/bin/env python3
"""Mock of kimi_agent/server.py good enough to render the real web UI.

Replays one representative turn (thinking -> tool call with a diff -> approval
request -> plan -> usage) over /ws, so a screenshot shows the console the way it
looks while working. Nothing here talks to Kimi; it only feeds app.js the same
message shapes server.py uses.
"""
import asyncio
import json
from pathlib import Path

from aiohttp import web

WEB = Path(__file__).resolve().parent.parent / "web"
SID = "session_ab12cd34"

CFG = [
    {"id": "mode", "name": "Режим", "current_value": "agent",
     "options": [{"value": "plan", "name": "plan"}, {"value": "agent", "name": "agent"}]},
    {"id": "model", "name": "Модель", "current_value": "gg/qwen3.8-flash",
     "options": [{"value": "gg/qwen3.8-flash", "name": "qwen3.8-flash"},
                {"value": "gg/deepseek-v3", "name": "deepseek-v3"}]},
    {"id": "thinking", "name": "Думы", "current_value": "on",
     "options": [{"value": "on", "name": "вкл"}, {"value": "off", "name": "выкл"}]},
]

SESSIONS = [{
    "id": SID, "title": "Починить сборку APK", "status": "running", "mode": "agent",
    "model": "qwen3.8-flash", "cwd": "/workspace",
    "usage": {"used": 186_000, "size": 204_800},
    "config_options": CFG,
    "plan": [
        {"content": "Найти причину падения сборки", "status": "completed"},
        {"content": "Исправить префикс deps/ в упаковке", "status": "in_progress"},
        {"content": "Пересобрать APK и проверить распаковку", "status": "pending"},
    ],
}]

SCRIPT = [
    ("turn_started", {"text": "Прогони тесты/сборку и почини то, что падает."}),
    ("thinking", {"text": "Смотрю лог сборки: падает на проверке целостности после распаковки."}),
    ("tool_call", {"tool_call_id": "t1", "title": "Read", "kind": "read", "status": "pending",
                   "input": {"path": "logs/agent-service.log"}}),
    ("tool_update", {"tool_call_id": "t1", "status": "completed",
                     "output": "missing after unpack: __init__.py"}),
    ("tool_call", {"tool_call_id": "t2", "title": "Edit", "kind": "edit", "status": "pending",
                   "input": {"path": "scripts/package_payload.sh"},
                   "content": [{"type": "diff", "path": "scripts/package_payload.sh",
                                "old_text": 'tar -C "$STAGE" --format=gnu -czf "$ASSETS/deps.tar.gz.bin" deps',
                                "new_text": 'tar -C "$STAGE/deps" --format=gnu -czf "$ASSETS/deps.tar.gz.bin" .'}]}),
    ("tool_update", {"tool_call_id": "t2", "status": "in_progress"}),
    ("approval_request", {"id": "ap1", "title": "Bash", "detail": "bash scripts/package_payload.sh",
                          "content": [{"type": "content", "text": "Пересобрать payload и запустить CI?"}],
                          "options": [{"option_id": "o1", "name": "Разрешить", "kind": "allow_once"},
                                      {"option_id": "o2", "name": "Всегда (сессия)", "kind": "allow_always"},
                                      {"option_id": "o3", "name": "Отклонить", "kind": "reject"}],
                          "auto_resolve_after": 25}),
    ("plan", {"entries": SESSIONS[0]["plan"]}),
    ("config", {"config_options": CFG}),
    ("usage", {"used": 186_000, "size": 204_800}),
    ("text", {"text": "Причина — архив deps нёс собственный префикс deps/, а распаковка идёт прямо в каталог назначения. Правлю упаковку и добавляю самолечение уже установленного runtime."}),
    ("tool_update", {"tool_call_id": "t2", "status": "completed", "output": "ok: 2032 entries, prefix-free"}),
]


async def ws_handler(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    await ws.send_json({
        "type": "hello", "version": "0.1.0",
        "agent": {"name": "kimi-code", "version": "0.42.0"},
        "sessions": SESSIONS, "pending": [], "methods": [],
    })

    async def pump():
        # Give the page time to pick the session first: app.js filters events by
        # the active session id, and a real user opens a session before it talks.
        await asyncio.sleep(3.0)
        for kind, data in SCRIPT:
            await asyncio.sleep(0.25)
            await ws.send_json({"type": kind, "session_id": SID, "data": data})

    task = asyncio.create_task(pump())
    try:
        async for msg in ws:
            if not msg.data:
                continue
            try:
                req = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if req.get("method") == "subscribe":
                await ws.send_json({"type": "subscribed", "id": req.get("id")})
                continue
            result = {
                "initialize": {"agentInfo": {"version": "0.42.0"},
                               "settings": {"workspace": "/workspace"}},
                "session/list": {"live": SESSIONS},
                "session/new": SESSIONS[0],
                "session/events": {"events": []},
                "coomi/tools/list": {"tools": [{"name": n} for n in
                    ("read_file write_file edit_file shell search list_dir apply_patch fetch "
                     "view_image show_image local_shell runtime_doctor request_user_input "
                     "request_file_import request_file_export create_loop get_loop update_loop "
                     "list_skills read_skill install_skill uninstall_skill list_mcp configure_mcp "
                     "spawn_agent wait_agent close_agent memory_list memory_read memory_search "
                     "memory_write memory_delete update_plan create_workflow get_workflow "
                     "save_workflow delete_workflow").split()]},
                "coomi/raw": {"ok": True},
                "coomi/decide": {"ok": True},
            }.get(req.get("method"), {})
            if req.get("id") is not None:
                await ws.send_json({"jsonrpc": "2.0", "id": req["id"], "result": result})
    finally:
        task.cancel()
    return ws


async def settings_get(_):
    return web.json_response({
        "provider": "gg", "provider_type": "openai_compatible",
        "base_url": "https://api.b.ai/v1", "model": "qwen3.8-flash",
        "has_key": True, "context_window": 204800, "compact_at_percent": 90,
        "permission_mode": "manual", "thinking": True, "env_key": False,
        "provider_types": ["kimi", "anthropic", "openai", "openai_responses",
                           "google-genai", "vertexai"],
        "providers": [{"name": "gg", "type": "openai_compatible", "model": "qwen3.8-flash"}],
    })


async def tools(_):
    return web.json_response({"count": 34})


async def health(_):
    return web.json_response({"ok": True, "bridge": "ready"})


app = web.Application()
app.router.add_get("/", lambda r: web.FileResponse(WEB / "index.html"))
app.router.add_get("/app.js", lambda r: web.FileResponse(WEB / "app.js"))
app.router.add_get("/app.css", lambda r: web.FileResponse(WEB / "app.css"))
app.router.add_get("/ws", ws_handler)
app.router.add_get("/api/settings", settings_get)
app.router.add_get("/api/tools", tools)
app.router.add_get("/health", health)
app.router.add_get("/api/health", health)

if __name__ == "__main__":
    web.run_app(app, host="127.0.0.1", port=8799, print=lambda *a: None)
