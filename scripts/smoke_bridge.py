"""Live smoke test of the ACP bridge: streaming, client terminals, multi-session."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kimi_agent.bridge import KimiCodeBridge  # noqa: E402
from kimi_agent.config import load_settings  # noqa: E402
from kimi_agent.events import Event  # noqa: E402

VERBOSE = {"tool_call", "tool_update", "approval_request", "question_request",
           "turn_completed", "turn_failed", "agent_exited", "plan", "usage"}


async def main() -> int:
    settings = load_settings()
    bridge = KimiCodeBridge(settings)
    seen: list[Event] = []

    def on_event(event: Event) -> None:
        seen.append(event)
        if event.type in VERBOSE:
            print(f"[{event.type}] {json.dumps(event.data, ensure_ascii=False)[:240]}", flush=True)
        elif event.type == "text" and len([e for e in seen if e.type == 'text']) < 3:
            print(f"[text] {event.data.get('text', '')[:120]!r}", flush=True)

    bridge.hooks.append(on_event)

    info = await bridge.start()
    print("agent:", json.dumps(info.get("agentInfo")))

    state = await bridge.new_session()
    print(f"session: {state.id} mode={state.mode} model={state.model}")
    print("config_options:", [o["id"] for o in state.config_options])

    task = await bridge.prompt(
        state.id,
        "Run `echo COOMI_TERM_OK && uname -m` with the Bash tool, then reply with exactly one "
        "short sentence quoting what it printed.",
    )
    result = await asyncio.wait_for(task, timeout=300)
    print("prompt result:", json.dumps(result, ensure_ascii=False))

    types: dict[str, int] = {}
    for ev in seen:
        types[ev.type] = types.get(ev.type, 0) + 1
    print("event histogram:", json.dumps(types))

    blob = json.dumps([e.data for e in seen], ensure_ascii=False)
    used_terminal = '"terminal"' in blob
    print("client terminal RPC observed:", used_terminal)

    second = await bridge.new_session()
    print("second session:", second.id, "-> concurrent sessions OK")
    listed = await bridge.list_kimi_sessions()
    print("sessions on disk:", len(listed))

    await bridge.close_session(state.id)
    await bridge.close()

    ok = "COOMI_TERM_OK" in blob and result.get("stop_reason") == "end_turn"
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
