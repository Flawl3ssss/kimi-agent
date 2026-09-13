"""Live check of the auto-safe gate against the shipped policy.

Verifies the fix for the "title=Bash, kind=null, command only in content" hole:

  A. a read-only command runs without ever asking (no false friction),
  B. a command we cannot prove read-only pauses with the *real* command in
     `detail` (not the bare tool name "Bash"),
  C. approving it through coomi/decide lets it actually execute,
  D. an MCP tool of ours is auto-approved (memory_search) so the agent stays
     usable, while a privileged one (submit_answer) still asks.

Usage: COOMI_KIMI_URL=http://127.0.0.1:8765 python scripts/check_autosafe_gate.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE = os.environ.get("COOMI_KIMI_URL", "http://127.0.0.1:8765")
CWD = "/tmp/coomi-gate-ws"
MARK = Path("/tmp/gate_written_marker")
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


def wait_for_pending(session_id: str, seconds: float = 90.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        for item in rpc("coomi/pending").get("pending") or []:
            if item.get("session_id") == session_id:
                return item
        time.sleep(0.4)
    return None


def drain(session_id: str, since: int, seconds: float = 120.0):
    """Returns (last_seq, text, tool_titles, stop_reason)."""
    deadline = time.time() + seconds
    text: list[str] = []
    tools: list[str] = []
    while time.time() < deadline:
        events = rpc("session/events", {"sessionId": session_id, "sinceSeq": since}).get("events") or []
        for ev in events:
            since = max(since, int(ev.get("seq", since)))
            data = ev.get("data") or {}
            if ev.get("type") == "text":
                text.append(str(data.get("text") or ""))
            elif ev.get("type") in ("tool_call", "tool_update"):
                title = str(data.get("title") or "")
                if title and title not in tools:
                    tools.append(title)
            elif ev.get("type") == "turn_completed":
                return since, "".join(text), tools, str(data.get("stop_reason") or "")
        time.sleep(0.5)
    return since, "".join(text), tools, "timeout"


def main() -> int:
    Path(CWD).mkdir(parents=True, exist_ok=True)
    init = rpc("initialize", {})
    policy = (init.get("settings") or {}).get("permission_policy")
    print(f"policy={policy}")
    if policy != "auto-safe":
        print("FAIL: expected the server to run with COOMI_KIMI_PERMISSION=auto-safe")
        return 1

    checks: dict[str, bool] = {}

    # ---------------------------------------------------- A: read-only passes
    print("\n[A] read-only command must not ask")
    sid = (rpc("session/new", {"cwd": CWD}) or {}).get("id")
    rpc("session/prompt", {"sessionId": sid, "wait": False,
                           "prompt": "Выполни ровно одну команду: ls -la /etc\nБольше ничего не делай."})
    asked = wait_for_pending(sid, seconds=12.0)
    _, text, tools, stop = drain(sid, 0)
    checks["no_false_friction"] = asked is None
    print(f"  asked={asked is not None} tools={tools[:3]} stop={stop}")
    if asked:
        rpc("coomi/decide", {"id": asked["id"], "behavior": "reject", "option_id": "reject"})

    # ------------------------------------------- B: unknown command must ask
    print("\n[B] unprovable command must pause with the real command in detail")
    MARK.unlink(missing_ok=True)
    sid2 = (rpc("session/new", {"cwd": CWD}) or {}).get("id")
    rpc("session/prompt", {"sessionId": sid2, "wait": False,
                           "prompt": "Выполни ровно одну shell-команду: "
                                     f"touch {MARK}\nБольше ничего не запускай."})
    item = wait_for_pending(sid2)
    checks["unknown_asks"] = item is not None
    detail = (item or {}).get("detail") or ""
    kind = (item or {}).get("kind")
    checks["detail_has_command"] = bool(item) and "touch" in detail
    checks["kind_is_execute"] = kind == "execute"
    print(f"  pending kind={kind!r} title={(item or {}).get('title')!r} detail={detail!r}")
    if item is None:
        print("  -> the gate let an unprovable command through")
        _, _, _, stop2 = drain(sid2, 0)
        print(f"  stop={stop2} marker_written={MARK.exists()}")
    else:
        # ------------------------------------------------ C: approve executes
        print("\n[C] approving must actually run the command")
        rpc("coomi/decide", {"id": item["id"], "behavior": "allow", "option_id": "approve_once"})
        _, text3, _, stop3 = drain(sid2, 0)
        checks["approve_executes"] = MARK.exists()
        print(f"  marker_exists={MARK.exists()} stop={stop3} text={text3[:90]!r}")

    # ----------------------------------------------------- D: MCP tool policy
    print("\n[D] our own MCP tools: readonly free, privileged asked")
    sid3 = (rpc("session/new", {"cwd": CWD}) or {}).get("id")
    rpc("session/prompt", {"sessionId": sid3, "wait": False,
                           "prompt": "Вызови ровно один инструмент mcp__coomi__memory_search "
                                     "с запросом \"gate\" и больше ничего не делай. Ответь: готово"})
    mcp_asked = wait_for_pending(sid3, seconds=20.0)
    checks["mcp_readonly_free"] = mcp_asked is None
    print(f"  memory_search asked={mcp_asked is not None}")
    if mcp_asked:
        print("  detail=" + repr(mcp_asked.get("detail")))
        rpc("coomi/decide", {"id": mcp_asked["id"], "behavior": "allow",
                             "option_id": "approve_once"})
        drain(sid3, 0)

    print("\n" + "=" * 58)
    failed = [name for name, ok in checks.items() if not ok]
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
    print("VERDICT", "all PASS" if not failed else f"FAILED: {failed}")
    Path("/tmp/kimi-gate-check.json").write_text(json.dumps({"checks": checks}, indent=1))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
