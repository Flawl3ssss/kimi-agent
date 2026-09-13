"""Entry point: coomi-kimi <command>.

serve   web console + HTTP/WS + MCP bridge (default)
chat    rich terminal REPL over the same bridge
ask     one-shot prompt, prints the final text
doctor  runtime/config/agent diagnostics
mcp     run only the MCP tool bridge (for use from a plain `kimi` TUI)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
from pathlib import Path

from .bridge import KimiCodeBridge
from .config import VERSION, load_settings
from .rpc import Rpc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="coomi-kimi", description=__doc__.splitlines()[0])
    parser.add_argument("--bin", dest="kimi_bin", help="path to the Kimi Code executable")
    parser.add_argument("-w", "--workspace", help="workspace directory (default: /workspace)")
    parser.add_argument("--mode", help="default session mode: default|plan|auto|yolo")
    parser.add_argument("--model", help="default model picker id")
    parser.add_argument("--permission", choices=["manual", "auto-safe", "auto-all"],
                        help="approval policy for client-side permissions")
    parser.add_argument("--port", type=int, help="web console port (default 8765)")
    parser.add_argument("--no-mcp", action="store_true", help="do not expose Coomi tools to the agent")
    parser.add_argument("-V", "--version", action="version", version=f"coomi-kimi {VERSION}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="web console + API (default)")
    sub.add_parser("chat", help="terminal REPL")
    p_ask = sub.add_parser("ask", help="one-shot prompt")
    p_ask.add_argument("prompt", nargs="+")
    p_ask.add_argument("--json", action="store_true", help="emit the final event stream as JSON")
    sub.add_parser("doctor", help="diagnostics")
    sub.add_parser("install", help="install the coomi agent profile for Kimi Code")
    sub.add_parser("mcp", help="run only the MCP tool bridge on stdio-less HTTP")
    return parser


def apply_args(settings, args) -> None:
    if args.kimi_bin:
        settings.kimi_bin = args.kimi_bin
    if args.workspace:
        settings.workspace = Path(args.workspace).expanduser().resolve()
        settings.workspace.mkdir(parents=True, exist_ok=True)
    if args.mode:
        settings.default_mode = args.mode
    if args.model:
        settings.default_model = args.model
    if args.permission:
        settings.permission_policy = args.permission
    if args.port:
        settings.port = args.port
        settings.bridge_port = args.port + 1
    if args.no_mcp:
        os.environ["COOMI_KIMI_SKIP_MCP"] = "1"


def install_profile(settings) -> int:
    """Copy the agent profile into Kimi's user scope + a starter AGENTS.md.

    Kimi discovers agents from `$KIMI_CODE_HOME/agents/`; project scope would
    also work but this console is workspace-agnostic.
    """
    src = Path(__file__).resolve().parent.parent / "config" / "coomi-agent.md"
    kimi_home = Path(os.environ.get("KIMI_CODE_HOME", str(Path.home() / ".kimi-code")))
    dst_dir = kimi_home / "agents"
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst_dir / "coomi.md")
    # MCP bridge config for plain `kimi`/`kimi web` sessions (the ACP path
    # advertises it per-session, but the TUI should get the tools too).
    mcp_cfg = settings.mcp_config_path
    if mcp_cfg.is_file():
        payload = json.loads(mcp_cfg.read_text(encoding="utf-8"))
        servers = {k: v for k, v in payload.get("mcpServers", {}).items() if k == "coomi"}
        kimi_home.mkdir(parents=True, exist_ok=True)
        (kimi_home / "mcp.json").write_text(
            json.dumps({"mcpServers": servers}, indent=2) + "\n", encoding="utf-8"
        )
    print(f"agent profile -> {dst_dir / 'coomi.md'}")
    print(f"mcp config    -> {kimi_home / 'mcp.json'} (needs `coomi-kimi mcp` running)")
    print("start the console with: coomi-kimi serve   then open the printed URL")
    return 0


async def run_doctor(settings) -> int:
    bridge = KimiCodeBridge(settings)
    rpc = Rpc(bridge)
    out: dict[str, object] = {"settings": settings.to_public_dict()}
    try:
        await bridge.start()
        out["initialize"] = await rpc.call("initialize", {})
        state = await bridge.new_session()
        out["session"] = state.describe()
        from .mcp_host import tool_specs

        out["tools"] = [t["qualified"] for t in await tool_specs(bridge)]
        handle = await bridge.prompt(state.id, "Reply with exactly: COOMI_DOCTOR_OK")
        await asyncio.wait_for(handle, timeout=180)
        out["echo"] = "".join(state.last_text_parts)[:200]
        await bridge.close_session(state.id)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        out["stderr_tail"] = bridge.stderr_tail(15)
        await bridge.close()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if "error" not in out else 1


async def run_ask(settings, prompt: str, as_json: bool) -> int:
    bridge = KimiCodeBridge(settings)
    events: list[dict] = []
    if not as_json:
        from rich.console import Console
        from rich.markup import escape

        console = Console()
        state_label = {"thinking": "dim", "text": "", "tool_call": "yellow",
                       "tool_update": None, "plan": "cyan", "turn_failed": "red"}

        def hook(event) -> None:
            if event.type == "text" and event.data.get("text"):
                console.print(escape(event.data["text"]), end="", markup=True, highlight=False)
            elif event.type == "thinking":
                console.print(f"[dim]… {escape((event.data.get('text') or '')[:120])}[/dim]", end="")
            elif event.type == "tool_call":
                console.print(f"\n[yellow]→ {escape(event.data.get('title') or '')}[/yellow]")
            elif event.type == "turn_failed":
                console.print(f"\n[red]✗ {escape(str(event.data.get('error')))}[/red]")

        bridge.hooks.append(hook)
    else:
        bridge.hooks.append(lambda e: events.append(e.to_dict()))
    try:
        await bridge.start()
        state = await bridge.new_session()
        handle = await bridge.prompt(state.id, prompt)
        result = await asyncio.wait_for(handle, timeout=1800)
        text = "".join(state.last_text_parts)
        if as_json:
            print(json.dumps({"result": result, "text": text, "events": events}, ensure_ascii=False))
        else:
            console.print()
        return 0 if result.get("stop_reason") == "end_turn" else 1
    finally:
        await bridge.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings()
    apply_args(settings, args)

    if not settings.kimi_bin or not Path(settings.kimi_bin).is_file():
        sys.stderr.write(
            "Kimi Code executable not found.\n"
            "  install:  curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash\n"
            "  or pass:  --bin /path/to/kimi  (env COOMI_KIMI_BIN)\n"
        )
        return 2

    command = args.command or "serve"
    if command == "install":
        return install_profile(settings)
    if command == "doctor":
        return asyncio.run(run_doctor(settings))
    if command == "ask":
        return asyncio.run(run_ask(settings, " ".join(args.prompt), args.json))
    if command == "mcp":
        from .mcp_host import serve_http

        async def _mcp() -> None:
            bridge = KimiCodeBridge(settings)
            task = await serve_http(bridge, "127.0.0.1", settings.bridge_port)
            print(f"Coomi tools on http://127.0.0.1:{settings.bridge_port}/mcp — Ctrl-C to stop")
            try:
                await asyncio.Event().wait()
            finally:
                task.cancel()

        try:
            asyncio.run(_mcp())
        except KeyboardInterrupt:
            pass
        return 0
    if command == "chat":
        from .repl import chat

        return asyncio.run(chat(settings))

    from .server import run

    print(f"Coomi console: http://{settings.host}:{settings.port}  (MCP bridge :{settings.bridge_port})")
    run(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
