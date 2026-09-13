"""End-to-end check: does Kimi Code see and call the coomi MCP tools?

Drives the running web host through its JSON-RPC surface (/rpc), creates a
session, asks the model to invoke two of our MCP tools, and inspects the event
stream for the resulting tool cards.

Usage: COOMI_KIMI_URL=http://127.0.0.1:8765 python scripts/smoke_e2e_mcp.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.environ.get("COOMI_KIMI_URL", "http://127.0.0.1:8765")
_TIMEOUT = float(os.environ.get("COOMI_KIMI_TIMEOUT", "240"))
_id = 0


def rpc(method: str, params: dict | None = None, timeout: float | None = None):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    req = Request(BASE + "/rpc", data=json.dumps(payload).encode(),
                  headers={"content-type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout or min(120.0, _TIMEOUT)) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except HTTPError as exc:
        return {"__error__": f"{exc.code} {exc.read().decode()[:400]}"}
    if "__error__" in body:
        return body
    if "error" in body:
        return {"__error__": body["error"]}
    return body.get("result", {})


def get(path: str):
    try:
        with urlopen(BASE + path, timeout=15) as resp:
            return json.loads(resp.read().decode() or "{}")
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        return {"__error__": str(exc)}


def main() -> int:
    health = get("/health")
    print("agent:", json.dumps(health.get("agent"), ensure_ascii=False),
          "| mcpServers advertised:", health.get("bridge", {}).get("mcp_servers")
          or health.get("mcp_servers"))
    tools = get("/api/tools")
    names = [t.get("name") for t in tools.get("tools", [])]
    print(f"tools registered on the bridge: {len(names)}")
    if not names:
        print("FAIL: no MCP tools registered")
        return 1

    sess = rpc("session/new", {"cwd": os.environ.get("COOMI_KIMI_CWD", "/workspace/kimi-agent")})
    sid = sess.get("id") or sess.get("sessionId")
    if not sid:
        print("FAIL: session/new:", json.dumps(sess, ensure_ascii=False)[:400])
        return 1
    print("session:", sid, "| mode:", sess.get("mode"), "| model:", sess.get("model"),
          "| configOptions:", len(sess.get("config_options") or sess.get("configOptions") or []))

    marker = f"e2e-{int(time.time())}"
    prompt = (
        f"Вызови инструмент mcp__coomi__memory_write с аргументами "
        f"name='{marker}', description='проверка MCP-моста', type='project', "
        "content='Kimi Code вызывает инструменты Coomi через ACP-мост.' "
        "Затем вызови mcp__coomi__list_skills. "
        "После этого ответь ровно одним словом: готово. "
        "Не используй никакие другие инструменты и не пиши код."
    )
    started = rpc("session/prompt", {"sessionId": sid, "prompt": prompt, "wait": False})
    if "__error__" in started:
        print("FAIL: session/prompt:", started["__error__"])
        return 1

    deadline = time.time() + _TIMEOUT
    since = 0
    hits: list[str] = []
    tool_lines: list[str] = []
    usage = None
    answer = []
    end_reason = None
    while time.time() < deadline:
        page = rpc("session/events", {"sessionId": sid, "sinceSeq": since, "limit": 400})
        events = page.get("events") or []
        for ev in events:
            since = max(since, int(ev.get("seq", since)))
            kind = ev.get("type")
            data = ev.get("data") or {}
            if kind in ("tool_call", "tool_update"):
                label = str(data.get("tool") or data.get("name") or "")
                title = str(data.get("title") or "")
                line = f"{kind} {label or title} [{data.get('status')}]"
                if line not in tool_lines:
                    tool_lines.append(line)
                    print("  •", line, "|", title[:70])
                if "coomi" in (label + title).lower() or "memory_write" in (label + title):
                    hits.append(line)
            elif kind == "usage":
                usage = data
            elif kind == "text":
                answer.append(str(data.get("text") or ""))
            elif kind == "turn_completed":
                end_reason = data.get("stop_reason") or data.get("stopReason")
            elif kind in ("error", "agent_exited"):
                print("  !!", kind, json.dumps(data, ensure_ascii=False)[:240])
        if end_reason is not None:
            break
        time.sleep(1.0)

    print("stopReason:", end_reason)
    print("usage:", json.dumps(usage, ensure_ascii=False) if usage else "нет usage_update")
    print("answer:", ("".join(answer)).strip()[:200] or "(пусто)")

    verify = rpc("coomi/tools/call", {"name": "memory_search", "args": {"query": marker}})
    found = marker in json.dumps(verify, ensure_ascii=False)
    print("memory_search подтверждает запись:", found)

    ok = bool(hits) and found
    print("VERDICT:", "PASS — Kimi Code видит и вызывает инструменты Coomi через MCP"
          if ok else "FAIL — модель не дошла до наших инструментов")
    with open("/tmp/kimi-e2e-mcp.json", "w", encoding="utf-8") as fh:
        json.dump({"session": sid, "tools": names, "tool_lines": tool_lines,
                   "hits": hits, "usage": usage, "answer": "".join(answer),
                   "verify": verify, "ok": ok}, fh, ensure_ascii=False, indent=2)
    print("отчёт: /tmp/kimi-e2e-mcp.json")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
