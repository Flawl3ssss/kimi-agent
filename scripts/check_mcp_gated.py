"""Does Kimi gate MCP tool calls through session/request_permission at all?

This decides whether the MCP grading in DecisionBroker is a real control or dead
code. Against a server started with COOMI_KIMI_PERMISSION=manual:

  * if an approval arrives for `mcp__coomi__list_skills`, MCP calls are gated;
  * if the tool runs with no approval, MCP calls are NOT gated — and then a
    model-reachable tool that resolves pending decisions (submit_answer) must be
    closed by an out-of-band secret instead, not by the permission policy.

Usage: COOMI_KIMI_URL=http://127.0.0.1:8775 python scripts/check_mcp_gated.py
"""

from __future__ import annotations

import json
import os
import time
from urllib.request import Request, urlopen

BASE = os.environ.get("COOMI_KIMI_URL", "http://127.0.0.1:8775")
CWD = os.environ.get("COOMI_KIMI_CWD", "/tmp/coomi-gate-ws")
_id = 0


def rpc(method: str, params: dict | None = None, timeout: float = 150.0):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    req = Request(BASE + "/rpc", data=json.dumps(payload).encode(),
                  headers={"content-type": "application/json"}, method="POST")
    with urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode() or "{}")
    return body.get("result", body.get("error", {}))


def pending_for(session_id: str, seconds: float):
    deadline = time.time() + seconds
    seen = []
    while time.time() < deadline:
        for item in rpc("coomi/pending").get("pending") or []:
            if item.get("session_id") == session_id and item["id"] not in [s["id"] for s in seen]:
                seen.append(item)
                return item
        time.sleep(0.4)
    return None


def main() -> int:
    os.makedirs(CWD, exist_ok=True)
    policy = (rpc("initialize", {}).get("settings") or {}).get("permission_policy")
    print("policy =", policy)
    if policy != "manual":
        print("FAIL: run this against COOMI_KIMI_PERMISSION=manual")
        return 1

    sid = (rpc("session/new", {"cwd": CWD}) or {}).get("id")
    rpc("session/prompt", {"sessionId": sid, "wait": False,
                           "prompt": "Вызови ровно один инструмент mcp__coomi__list_skills "
                                     "без аргументов. Больше ничего не делай. Ответь: готово"})
    item = pending_for(sid, 60.0)
    if item is None:
        print("RESULT: MCP tool call produced NO approval -> MCP calls are not gated "
              "by request_permission")
        # let it finish
        deadline = time.time() + 90
        while time.time() < deadline:
            events = rpc("session/events", {"sessionId": sid, "sinceSeq": 0}).get("events") or []
            if any(e.get("type") == "turn_completed" for e in events):
                break
            time.sleep(1)
        tools = [e for e in rpc("session/events", {"sessionId": sid, "sinceSeq": 0}).get("events") or []
                 if e.get("type") in ("tool_call", "tool_update")]
        print("tool events seen:", len(tools))
    else:
        print("RESULT: MCP call IS gated:", json.dumps(
            {"title": item.get("title"), "kind": item.get("kind"),
             "tool_name": item.get("tool_name"), "detail": item.get("detail")},
            ensure_ascii=False))
        rpc("coomi/decide", {"id": item["id"], "behavior": "allow", "option_id": "approve_once"})
        time.sleep(2)
    return 0


if __name__ == "__main__":
    main()
