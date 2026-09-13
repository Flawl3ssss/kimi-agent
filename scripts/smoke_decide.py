"""Human-in-the-loop check with policy=manual.

Runs against a server started with COOMI_KIMI_PERMISSION=manual and verifies:
  1. a shell command pauses as a pending approval and the model waits for us,
  2. approving it through `coomi/decide` lets the turn finish with real output,
  3. rejecting it stops the command from running at all,
  4. an AskUserQuestion reaches the human as a question, never auto-answered.

Usage: COOMI_KIMI_URL=http://127.0.0.1:8775 python scripts/smoke_decide.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

BASE = os.environ.get("COOMI_KIMI_URL", "http://127.0.0.1:8775")
CWD = os.environ.get("COOMI_KIMI_CWD", "/tmp/coomi-decide-ws")
_id = 0


def rpc(method: str, params: dict | None = None, timeout: float = 120.0):
    global _id
    _id += 1
    payload = {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    req = Request(BASE + "/rpc", data=json.dumps(payload).encode(),
                  headers={"content-type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
    except HTTPError as exc:
        return {"__error__": f"{exc.code} {exc.read().decode()[:200]}"}
    if "error" in body:
        return {"__error__": body["error"]}
    return body.get("result", {})


def pending_for(session_id: str, seconds: float = 90.0) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        for item in rpc("coomi/pending").get("pending") or []:
            if item.get("session_id") == session_id:
                return item
        time.sleep(0.4)
    return None


def collect(session_id: str, since: int, seconds: float = 90.0):
    """Drain events until the turn completes. Returns (last_seq, text, stop_reason, failed)."""
    deadline = time.time() + seconds
    text: list[str] = []
    failed: list[str] = []
    while time.time() < deadline:
        events = rpc("session/events", {"sessionId": session_id, "sinceSeq": since}).get("events") or []
        for ev in events:
            since = max(since, int(ev.get("seq", since)))
            data = ev.get("data") or {}
            kind = ev.get("type")
            if kind == "text":
                text.append(str(data.get("text") or ""))
            elif kind == "tool_update" and data.get("status") == "failed":
                failed.append(json.dumps(data, ensure_ascii=False)[:200])
            elif kind == "turn_completed":
                return since, "".join(text), str(data.get("stop_reason") or ""), failed
        time.sleep(0.5)
    return since, "".join(text), "timeout", failed


def main() -> int:
    os.makedirs(CWD, exist_ok=True)
    init = rpc("initialize", {})
    health = rpc("health")
    settings = init.get("settings") or {}
    policy = settings.get("permission_policy")
    print("mcp advertised:", health.get("mcp_servers"), "| bridge url ok")
    print(f"policy={policy}")
    if policy != "manual":
        print("FAIL: start the server with COOMI_KIMI_PERMISSION=manual")
        return 1

    checks: dict[str, bool] = {}
    report: dict[str, object] = {}

    # ---------------------------------------------------------------- approve
    sid = (rpc("session/new", {"cwd": CWD}) or {}).get("id")
    print("\n[1] approve_once")
    # The oracle is the command's side effect on disk, never the transcript: a
    # model that behaved wrongly may still claim success, and a correct one may
    # quote the marker while explaining itself.
    approve_file = Path(CWD) / "approve_side_effect.txt"
    approve_file.unlink(missing_ok=True)
    rpc("session/prompt", {
        "sessionId": sid,
        "prompt": f"Выполни в терминале ровно одну shell-команду: "
                  f"printf {MARK_OK} > {approve_file}\n"
                  "Ничего больше не запускай и не читай.",
        "wait": False})
    item = pending_for(sid)
    if item is None:
        print("  FAIL: no approval was requested")
        checks["approve"] = False
    else:
        print(f"  pending kind={item.get('kind')} title={item.get('title')!r} detail={item.get('detail')!r} "
              f"options={[o['option_id'] for o in item.get('options') or []]}")
        decided = rpc("coomi/decide", {"id": item["id"], "behavior": "allow",
                                       "option_id": "approve_once"})
        print("  decide ->", json.dumps(decided, ensure_ascii=False)[:120])
        _, text, stop, failed = collect(sid, 0)
        executed = approve_file.exists()
        print(f"  stop={stop} side_effect={executed} text={text[:120]!r} failed={failed[:1]}")
        # "approve" must mean the command really ran, not that the model said so.
        checks["approve"] = bool(executed) and not failed
        report["approve"] = {"pending": item, "text": text, "stop": stop,
                             "failed": failed, "side_effect": executed}

    # ----------------------------------------------------------------- reject
    print("\n[2] reject")
    sid2 = (rpc("session/new", {"cwd": CWD}) or {}).get("id")
    # The oracle is the command's side effect, never its transcript: a correctly
    # refused model still *mentions* the marker when it explains the refusal.
    REJECT_FILE = Path(CWD) / "reject_side_effect.txt"
    REJECT_FILE.unlink(missing_ok=True)
    rpc("session/prompt", {
        "sessionId": sid2,
        "prompt": f"Выполни в терминале ровно одну shell-команду: "
                  f"printf {MARK_NO} > {REJECT_FILE}\n"
                  "Ничего больше не запускай.",
        "wait": False})
    item2 = pending_for(sid2)
    if item2 is None:
        print("  FAIL: no approval was requested")
        checks["reject"] = False
    else:
        print(f"  pending title={item2.get('title')!r}")
        rpc("coomi/decide", {"id": item2["id"], "behavior": "reject", "option_id": "reject"})
        _, text2, stop2, _ = collect(sid2, 0)
        ran = REJECT_FILE.exists()
        print(f"  stop={stop2} text={text2[:160]!r} side_effect_written={ran}")
        checks["reject"] = (not ran) and item2["id"] is not None
        report["reject"] = {"pending": item2, "text": text2, "stop": stop2,
                            "side_effect_written": ran}

    # --------------------------------------------------------------- question
    print("\n[3] AskUserQuestion must reach the human")
    sid3 = (rpc("session/new", {"cwd": CWD}) or {}).get("id")
    rpc("session/prompt", {"sessionId": sid3, "wait": False, "prompt":
                           "С помощью инструмента AskUserQuestion задай мне один вопрос: "
                           '"Какой цвет выбрать?" с вариантами "красный" и "синий". '
                           "Не выбирай сам."})
    item3 = pending_for(sid3, seconds=120)
    if item3 is None:
        print("  no question surfaced (model answered directly)")
        checks["question"] = False
    else:
        opts = [o.get("option_id", "") for o in item3.get("options") or []]
        is_question = item3.get("kind") == "question" or any(o.startswith("q0_") for o in opts)
        print(f"  kind={item3.get('kind')} options={opts}")
        answer = rpc("coomi/decide", {"id": item3["id"], "behavior": "allow",
                                      "option_id": opts[0] if opts else ""})
        print("  decide ->", json.dumps(answer, ensure_ascii=False)[:120])
        _, text3, stop3, _ = collect(sid3, 0)
        print(f"  stop={stop3} text={text3[:140]!r}")
        checks["question"] = is_question
        report["question"] = {"pending": item3, "text": text3}

    print("\nVERDICT")
    for name, passed in checks.items():
        print(f"  {name:9s} {'PASS' if passed else 'FAIL'}")
    with open("/tmp/kimi-decide.json", "w", encoding="utf-8") as fh:
        json.dump({"checks": checks, **report}, fh, ensure_ascii=False, indent=2, default=str)
    print("отчёт: /tmp/kimi-decide.json")
    return 0 if checks and all(checks.values()) else 2


MARK_OK = "COOMI_DECIDE_OK"
MARK_NO = "COOMI_REJECT_TEST"

if __name__ == "__main__":
    sys.exit(main())
