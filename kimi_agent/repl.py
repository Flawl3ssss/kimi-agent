"""Terminal REPL: the same bridge, no browser.

Enter sends a turn; `!cmd` runs a shell command through the agent's Bash tool
via a direct tool call; slash commands drive sessions, modes, models and the
decision dock.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from .bridge import KimiCodeBridge
from .config import Settings
from .events import Event
from .rpc import Rpc

console = Console()


class Repl:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.bridge = KimiCodeBridge(settings)
        self.rpc = Rpc(self.bridge)
        self.session_id = ""
        self.busy = False
        self.current_text = console
        self._spinner = None

    # ------------------------------------------------------------ rendering
    def on_event(self, event: Event) -> None:
        sid = event.session_id
        if sid and sid != self.session_id and sid in self.bridge.subagents:
            self._subagent_line(event)
            return
        if event.type == "thinking":
            self._inline("dim", event.data.get("text", ""))
        elif event.type == "text":
            if event.data.get("image"):
                console.print(f"[teal]image shown: {escape(event.data.get('path', ''))}[/teal]")
            else:
                self._inline("", event.data.get("text", ""))
        elif event.type == "tool_call":
            console.print(f"\n[yellow]→ {escape(event.data.get('title') or event.data.get('kind') or 'tool')}[/yellow]")
        elif event.type == "tool_update" and event.data.get("status") in {"failed", "completed"}:
            mark = "red ✗" if event.data["status"] == "failed" else "green ✓"
            console.print(f"[{mark.split()[0]}]{mark.split()[1]} {escape(event.data.get('title') or '')}[/]")
        elif event.type == "plan":
            self._plan(event.data.get("entries", []))
        elif event.type == "usage":
            u = event.data
            if u.get("size"):
                console.print(f"[dim]context {u['used']}/{u['size']} ({100 * u['used'] / u['size']:.0f}%)[/dim]")
        elif event.type == "turn_completed":
            self._end_stream()
            reason = event.data.get("stop_reason")
            if reason != "end_turn":
                console.print(f"[amber]turn ended: {reason}[/amber]")
        elif event.type == "turn_failed":
            self._end_stream()
            console.print(f"[red]✗ {escape(str(event.data.get('error')))[:400]}[/red]")
        elif event.type in {"approval_request", "question_request"}:
            asyncio.create_task(self._prompt_decision(event.data, event.type))
        elif event.type == "agent_exited":
            console.print(f"[red]agent exited (code {event.data.get('exit_code')}) — restart with /restart[/red]")

    def _subagent_line(self, event: Event) -> None:
        short = event.session_id.replace("session_", "#")[:8]
        if event.type == "tool_call":
            console.print(f"  [dim]\\[[short]] → {escape(event.data.get('title') or '')}[/dim]")
        elif event.type == "turn_completed":
            console.print(f"  [dim]\\[[short]] done[/dim]")

    def _inline(self, style: str, text: str) -> None:
        if not text:
            return
        if not hasattr(self, "_buf"):
            self._buf = ""
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            console.print(line, style=style or None, markup=False, highlight=False)

    def _end_stream(self) -> None:
        if getattr(self, "_buf", ""):
            console.print(self._buf, markup=False, highlight=False)
            self._buf = ""
        self.busy = False

    def _plan(self, entries: list[dict[str, Any]]) -> None:
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column("state", width=3)
        table.add_column("step")
        for entry in entries:
            glyph = {"completed": "[green]✓[/green]", "in_progress": "[teal]●[/teal]"}.get(
                entry.get("status"), "[dim]○[/dim]")
            table.add_row(glyph, escape(entry.get("content", "")))
        console.print(Panel(table, title="план", border_style="dim", expand=False))

    # ------------------------------------------------------------ decisions
    async def _prompt_decision(self, data: dict[str, Any], kind: str) -> None:
        did = data.get("id")
        if not did:
            return
        opts = data.get("options") or []
        if kind == "question_request":
            console.print(f"\n[cyan]? {escape(data.get('message') or 'нужен ответ')}[/cyan]")
            for q in data.get("questions", []):
                console.print(f"  {escape(q.get('question') or q.get('id'))}")
                for i, o in enumerate(q.get("options", []), 1):
                    console.print(f"    [b]{i}[/b] {escape(str(o.get('label')))}")
                choice = await self._ask_input("номер или текст ответа (Enter — пропустить): ")
                await self.rpc.call("coomi/decide", {
                    "id": did, "behavior": "answer",
                    "content": {f"q{data.get('questions', []).index(q)}": choice or ""},
                })
                return
        else:
            title = escape(data.get("title") or "подтверждение")
            console.print(f"\n[amber]⚠ {title}[/amber]")
            for block in (data.get("content") or [])[:1]:
                if block.get("type") == "diff":
                    console.print(Syntax(block.get("new_text", "")[:1200], "diff", theme="monokai", word_wrap=True))
            for i, option in enumerate(opts, 1):
                console.print(f"  [b]{i}[/b] {escape(option.get('name'))} [dim]({option.get('kind')})[/dim]")
            raw = await self._ask_input("номер варианта (Enter — 1, n — отклонить): ")
            behaviour, option_id = "reject", ""
            if raw.strip().lower() in {"", "1", "y", "yes"}:
                behaviour, option_id = "allow", self._option(opts, 0)
            elif raw.strip().lower() in {"2", "always", "a"}:
                behaviour, option_id = "allow_session", self._option(opts, 1) or self._option(opts, 0)
            elif raw.strip().isdigit():
                index = int(raw.strip()) - 1
                if 0 <= index < len(opts):
                    kind_of = opts[index].get("kind", "")
                    behaviour = "reject" if kind_of.startswith("reject") else (
                        "allow_session" if kind_of == "allow_always" else "allow")
                    option_id = opts[index].get("option_id", "")
            await self.rpc.call("coomi/decide", {"id": did, "behavior": behaviour, "option_id": option_id})

    @staticmethod
    def _option(opts: list[dict[str, Any]], index: int) -> str:
        return opts[index].get("option_id", "") if 0 <= index < len(opts) else ""

    async def _ask_input(self, label: str) -> str:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, lambda: input(label))
        except (EOFError, KeyboardInterrupt):
            return "n"

    # --------------------------------------------------------------- commands
    async def handle(self, line: str) -> bool:
        if line.startswith("!"):
            return await self._shell(line[1:].strip())
        if not line.startswith("/"):
            await self._send(line)
            return True
        head, _, rest = line.partition(" ")
        cmd = head.lstrip("/")
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            console.print(f"[red]неизвестная команда /{cmd}[/red] — /help")
            return True
        await handler(rest.strip())
        return True

    async def _shell(self, command: str) -> bool:
        if not command:
            return True
        from . import store

        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.settings.workspace),
        )
        raw, _ = await proc.communicate()
        text = raw.decode("utf-8", "replace")
        console.print(Panel(text[-4000:] or "(пусто)", title=f"$ {escape(command)}",
                            border_style="dim", expand=False))
        store.audit("shell", {"command": command}, f"exit {proc.returncode}")
        return True

    async def _send(self, text: str) -> bool:
        await self._ensure_session()
        try:
            handle = await self.bridge.prompt(self.session_id, text)
            self.busy = True
            await handle
        except Exception as exc:
            self.busy = False
            console.print(f"[red]✗ {escape(str(exc))[:300]}[/red]")
        return True

    async def _ensure_session(self) -> None:
        if self.session_id in self.bridge.sessions:
            return
        state = await self.bridge.new_session()
        self.session_id = state.id
        console.print(f"[dim]сессия {state.id[:16]}… режим {state.mode}, модель {state.model}[/dim]")

    async def _cmd_help(self, _: str) -> None:
        console.print(Panel(
            "[b]/new[/b] новая сессия · [b]/sessions[/b] список · [b]/switch <id>[/b] переключить\n"
            "[b]/mode <m>[/b] default|plan|auto|yolo · [b]/model <id>[/b] · [b]/thinking <v>[/b]\n"
            "[b]/plan[/b] текущий план · [b]/tools[/b] инструменты Coomi · [b]/pending[/b] решения\n"
            "[b]/decide <id> allow|always|reject[/b] · [b]/cancel[/b] прервать ход\n"
            "[b]/doctor[/b] диагностика · [b]/log[/b] лог агента · [b]!cmd[/b] shell · [b]/quit[/b]",
            title="Coomi · Kimi Code", border_style="teal", expand=False))

    async def _cmd_new(self, _: str) -> None:
        state = await self.bridge.new_session()
        self.session_id = state.id
        console.print(f"[green]сессия {state.id[:16]}…[/green]")

    async def _cmd_sessions(self, _: str) -> None:
        table = Table(show_header=True, header_style="dim")
        for col in ("id", "статус", "режим", "модель", "токены", "title"):
            table.add_column(col)
        for s in self.bridge.known_sessions():
            table.add_row(s["id"][:14], s["status"], s["mode"], (s["model"] or "")[:18],
                          str((s["usage"] or {}).get("used", "")), (s["title"] or "")[:28])
        console.print(table)

    async def _cmd_switch(self, arg: str) -> None:
        if arg:
            self.session_id = arg
            console.print(f"[dim]сессия → {arg[:16]}…[/dim]")

    async def _cmd_mode(self, arg: str) -> None:
        await self._ensure_session()
        try:
            await self.rpc.call("session/set_mode", {"sessionId": self.session_id, "modeId": arg})
            console.print(f"[green]режим: {arg}[/green]")
        except Exception as exc:
            console.print(f"[red]{escape(str(exc))[:200]}[/red]")

    async def _cmd_model(self, arg: str) -> None:
        await self._set("model", arg)

    async def _cmd_thinking(self, arg: str) -> None:
        await self._set("thinking", arg)

    async def _set(self, config_id: str, value: str) -> None:
        await self._ensure_session()
        try:
            await self.rpc.call("session/set_config_option", {
                "sessionId": self.session_id, "configId": config_id, "value": value})
            console.print(f"[green]{config_id} = {value}[/green]")
        except Exception as exc:
            console.print(f"[red]{escape(str(exc))[:280]}[/red]")

    async def _cmd_plan(self, _: str) -> None:
        state = self.bridge.sessions.get(self.session_id)
        self._plan(state.plan if state else [])

    async def _cmd_tools(self, _: str) -> None:
        result = await self.rpc.call("coomi/tools/list", {})
        for tool in result["tools"]:
            console.print(f"  [b]{tool['qualified']}[/b] — {tool['description'][:110]}")

    async def _cmd_pending(self, _: str) -> None:
        result = await self.rpc.call("coomi/pending", {})
        if not result["pending"]:
            console.print("[dim]нет ожидающих решений[/dim]")
        for item in result["pending"]:
            console.print(f"  {item['id']}  [amber]{escape(str(item.get('title') or item.get('message')))}[/amber]")

    async def _cmd_decide(self, arg: str) -> None:
        parts = arg.split()
        if len(parts) < 2:
            console.print("[red]использование: /decide <id> allow|always|reject[/red]")
            return
        mapping = {"allow": "allow", "always": "allow_session", "reject": "reject"}
        try:
            await self.rpc.call("coomi/decide", {
                "id": parts[0], "behavior": mapping.get(parts[1], parts[1])})
            console.print("[green]решение отправлено[/green]")
        except Exception as exc:
            console.print(f"[red]{escape(str(exc))[:200]}[/red]")

    async def _cmd_cancel(self, _: str) -> None:
        if self.session_id:
            await self.bridge.cancel(self.session_id)
            console.print("[amber]прерываю…[/amber]")

    async def _cmd_restart(self, _: str) -> None:
        await self.bridge.restart("manual")
        self.session_id = ""
        console.print("[green]агент перезапущен[/green]")

    async def _cmd_log(self, arg: str) -> None:
        for line in self.bridge.stderr_tail(int(arg or 30)):
            console.print(f"[dim]{escape(line[:200])}[/dim]")

    async def _cmd_doctor(self, _: str) -> None:
        health = await self.bridge.health()
        console.print(Panel(json.dumps(health, ensure_ascii=False, indent=2)[:2500],
                            title="health", border_style="dim", expand=False))

    async def _cmd_quit(self, _: str) -> None:
        self.running = False

    # ------------------------------------------------------------------ loop
    async def run(self) -> int:
        if not self.settings.kimi_bin:
            console.print("[red]Kimi Code не найден: установите или укажите --bin[/red]")
            return 2
        self.bridge.hooks.append(self.on_event)
        try:
            info = await self.bridge.start()
        except Exception as exc:
            console.print(f"[red]не удалось запустить kimi acp: {escape(str(exc))[:300]}[/red]")
            return 2
        agent = info.get("agentInfo") or {}
        console.print(Panel(
            f"[b]Coomi на Kimi Code[/b] {agent.get('name', '')} {agent.get('version', '')}\n"
            f"workspace [dim]{self.settings.workspace}[/dim] · политики решений "
            f"[dim]{self.settings.permission_policy}[/dim]\n/i → /help",
            border_style="teal", expand=False))
        self.running = True
        loop = asyncio.get_running_loop()
        while self.running:
            try:
                line = await loop.run_in_executor(None, lambda: input("› "))
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]out[/dim]")
                break
            line = line.strip()
            if not line:
                continue
            if not await self.handle(line):
                break
        await self.bridge.close()
        return 0


async def chat(settings: Settings) -> int:
    return await Repl(settings).run()
