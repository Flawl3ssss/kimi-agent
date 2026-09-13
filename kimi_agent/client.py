"""ACP client-side callbacks: approvals, filesystem, terminals, elicitation.

Kimi Code delegates a surprising amount of capability to the client: file reads
and writes can be routed back here, shell execution can run in client-owned
terminals, and human-in-the-loop decisions (tool approval, structured
questions) arrive as reverse RPC. This module implements all of it.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import mimetypes
import os
import re
import shlex
import signal as signal_module
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import acp
from acp.schema import (
    AcceptElicitationResponse,
    AllowedOutcome,
    ContentToolCallContent,
    DeclineElicitationResponse,
    DeniedOutcome,
    ReadTextFileResponse,
    TerminalOutputResponse,
    TextContentBlock,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from .config import Settings
from .events import Event, tool_call_payload

# Permission kinds that are never auto-approved under `auto-safe`.
DESTRUCTIVE_KINDS = {"delete", "move"}
# AskUserQuestion is bridged through `session/request_permission` too (see
# packages/acp-server/src/question.ts): options are `q{n}_opt_<i>` plus a
# `q{n}_skip`, and the synthetic tool call carries kind `other`. Questions must
# never be auto-answered, so this namespace vetoes every policy below it.
QUESTION_OPTION_NS = re.compile(r"^q\d+_(opt_|skip)")
# Plan review (ExitPlanMode) rides the same channel with `plan_*` ids.
PLAN_OPTION_NS = re.compile(r"^plan_(opt_\d+|approve|revise|reject_and_exit)$")
DANGEROUS_PATTERNS = [
    r"\brm\s+(-[a-z]*[rf][a-z]*\s+)+(/|~|\$HOME|\*)",
    r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\s*\{", r"\bshutdown\b", r"\breboot\b",
    r"\bgit\s+push\s+--force", r"\bchmod\s+-R\s+777\s+/", r"\bcurl\b.*\|\s*(ba)?sh",
    r"\bwget\b.*\|\s*(ba)?sh",
]
SAFE_KINDS = {"read", "search", "fetch", "think"}

# Our own MCP server exposes ~34 tools. Auto-approving all of them would defeat
# the gate; asking on every one would make the agent useless. So they are graded
# once, here:
#  * read-only helpers run freely under auto-safe,
#  * state-writing helpers are allowed but remembered as a session grant,
#  * the rest always reach a human (see the two veto sets below).
SAFE_MCP_TOOLS = {
    "memory_read", "memory_search", "memory_list", "get_plan", "get_workflow",
    "list_workflows", "list_skills", "list_loops", "list_pending", "read_skill",
    "view_image", "runtime_doctor", "agent_status", "agent_wait", "web_search",
}
EDITING_MCP_TOOLS = {
    "memory_write", "memory_delete", "update_plan", "create_workflow",
    "save_workflow", "delete_workflow", "create_skill", "create_loop",
    "update_loop", "show_image", "import_file", "export_file", "open_session",
    "ask_in_session", "compact_session",
}
# These launch work nobody has looked at, so they are never auto-approved.
PRIVILEGED_MCP_TOOLS = {"spawn_agent", "close_agent", "run_workflow", "run_loop_turn"}
# Resolves pending interactions *for* the human. Beyond not auto-approving it, a
# stored grant must not waive it either: one "always allow" would otherwise let
# the model approve every permission it ever asks for.
SELF_ANSWERING_MCP_TOOLS = {"submit_answer"}

# --- auto-safe shell vocabulary -------------------------------------------
# Everything not listed here is unknown to us and therefore goes to a human.
READ_ONLY_COMMANDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "which", "whoami", "date", "echo",
    "grep", "rg", "du", "df", "env", "printenv", "uname", "id", "stat", "file",
    "tree", "awk", "sort", "uniq", "diff", "basename", "dirname", "realpath",
    "jq", "sha256sum", "md5sum", "sed", "find", "python3", "python", "node",
    "git", "tar", "curl", "wget",
}
# Interpreters may run script files, but inline code is arbitrary execution.
INLINE_CODE_FLAGS = {
    "python3": {"-c"}, "python": {"-c"}, "node": {"-e", "--eval", "--print", "-p"},
    "perl": {"-e"}, "ruby": {"-e"}, "php": {"-r"},
}
GIT_READ_ONLY_SUBCOMMANDS = {
    "git": {"status", "diff", "log", "show", "branch", "blame", "ls-files",
            "rev-parse", "describe", "shortlog"},
}
# Commands that are read-only unless they carry one of these flags.
WRITE_FLAG_COMMANDS = {
    "sed": {"-i", "--in-place"},
    "npm": {"install", "i", "add", "uninstall", "remove", "publish", "link"},
    "pip": {"install", "uninstall"}, "pip3": {"install", "uninstall"},
    "uv": {"pip"},
}
NETWORK_WRITE_FLAGS = {"-o", "-O", "--output", "--output-document", "-T",
                       "--upload-file", "-d", "--data", "--data-ascii",
                       "--data-binary", "--data-urlencode", "-F", "--form",
                       "--form-string", "-P", "--proxy", "-X", "--request", "--method"}
_HTTP_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
FIND_WRITE_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir",
                      "-fprintf", "-fprint", "-fls"}


@dataclass(slots=True)
class PendingInteraction:
    """A decision the client owes the agent: permission or elicitation."""

    id: str
    kind: str  # "approval" | "question"
    session_id: str
    payload: dict[str, Any]
    future: asyncio.Future
    created_at: float = field(default_factory=time.time)

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "session_id": self.session_id,
            "created_at": self.created_at,
            **self.payload,
        }


class DecisionBroker:
    """Routes reverse-RPC decisions to the UI, or resolves them by policy."""

    def __init__(self, settings: Settings, publish: Callable[[Event], None]) -> None:
        self.settings = settings
        self.publish = publish
        self.policy = settings.permission_policy
        self._pending: dict[str, PendingInteraction] = {}
        self._counter = 0
        # session_id -> {"titles": set, "tools": set}. "approve_always" must never
        # leak from one session into another, so grants are keyed by session.
        self._grants: dict[str, dict[str, set[str]]] = {}
        self.decision_log: list[dict[str, Any]] = []

    def new_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}_{int(time.time() * 1000):x}_{self._counter}"

    @property
    def pending(self) -> list[dict[str, Any]]:
        return [interaction.describe() for interaction in self._pending.values()]

    def cancel_all(self, session_id: str | None = None, reason: str = "cancelled") -> None:
        for interaction in list(self._pending.values()):
            if session_id and interaction.session_id != session_id:
                continue
            self.resolve(interaction.id, {"behavior": "cancelled", "reason": reason})

    def forget(self, session_id: str) -> None:
        """Drop per-session grants and pending items when a session goes away."""
        self._grants.pop(session_id, None)
        self.cancel_all(session_id, reason="session_closed")

    def resolve(self, decision_id: str, decision: dict[str, Any]) -> bool:
        interaction = self._pending.pop(decision_id, None)
        if interaction is None:
            return False
        if not interaction.future.done():
            interaction.future.set_result(decision)
        self.publish(Event("approval_resolved" if interaction.kind == "approval"
                           else "question_resolved", interaction.session_id,
                           {"id": decision_id, "decision": decision}))
        return True

    # -- policy -------------------------------------------------------------
    def auto_decision(
        self,
        tool_kind: str | None,
        title: str,
        tool_name: str = "",
        option_ids: tuple[str, ...] = (),
        session_id: str = "",
        content: Any = None,
    ) -> str | None:
        """Return 'allow' / 'allow_session' / None (meaning: ask the human)."""
        # A question bridged through request_permission is not an approval: only
        # a human can answer it. Same for plan review, which offers a choice.
        if any(QUESTION_OPTION_NS.match(o) for o in option_ids):
            return None
        if any(PLAN_OPTION_NS.match(o) for o in option_ids) and self.policy not in ("auto-all", "yolo"):
            return None
        tool_kind = tool_kind or infer_tool_kind(tool_name)
        leaf = (tool_name or "").rsplit("__", 1)[-1]
        grants = self._grants.get(session_id) or {}
        session_key = self._grant_key(tool_kind, title, tool_name, content)
        # A self-answering tool is never auto-approved and never honours a stored
        # grant: `submit_answer` resolves pending interactions on the human's
        # behalf, so one grant would let the model approve all its later
        # permissions by itself. `auto-all`/`yolo` remain an explicit override.
        blocked = leaf in SELF_ANSWERING_MCP_TOOLS and self.policy not in ("auto-all", "yolo")
        if self.policy in ("auto-all", "yolo"):
            return "allow"
        # An "always allow" for one command must not cover a destructive one, so
        # the danger veto is evaluated before any grant lookup.
        text = "\n".join([title or "", *content_texts(content)])
        dangerous = any(
            re.search(pattern, text, re.IGNORECASE) for pattern in DANGEROUS_PATTERNS
        )
        if not blocked and not dangerous:
            if session_key and session_key in grants.get("titles", set()):
                return "allow"
            if tool_name and tool_name in grants.get("tools", set()):
                return "allow"

        if self.policy in ("manual", "ask"):
            return None

        # auto-safe
        if dangerous:
            return None
        if tool_kind in DESTRUCTIVE_KINDS:
            return None
        if blocked:
            return None
        if tool_name.startswith("mcp__"):
            # Our own server: graded above instead of the generic kind rules.
            if leaf in SAFE_MCP_TOOLS:
                return "allow"
            if leaf in EDITING_MCP_TOOLS:
                return "allow_session"
            return None
        if tool_kind in SAFE_KINDS:
            return "allow"
        if tool_kind == "execute":
            return self._shell_decision(self._command_text(tool_name, title, content))
        if tool_kind == "edit":
            return "allow_session"
        # Unknown tool with no declared kind: never assume it is harmless.
        return None

    def _grant_key(self, tool_kind: str | None, title: str, tool_name: str,
                   content: Any = None) -> str:
        """What an "always allow" actually memorises.

        For shell tools the *command*, not the title: Kimi titles are the bare
        tool name ("Bash"), so keying grants on the title would authorise every
        future command after approving one harmless call.
        """
        kind = tool_kind or infer_tool_kind(tool_name or _tool_name_from_title(title))
        if kind == "execute":
            command = self._command_text(tool_name or _tool_name_from_title(title),
                                         title, content)
            return command.strip()
        return (title or "").strip()

    def _command_text(self, tool_name: str, title: str, content: Any) -> str:
        """Recover the shell command a permission request is really about.

        Kimi Code sends `title: "Bash"` and keeps the command only in `content`,
        wrapped in the boilerplate sentence "Requesting approval to Running: ...".
        Deciding on the title alone would approve or reject blindly, so the command
        is taken from whichever field actually carries it, and any extra content
        lines are folded in so a chained payload cannot hide its tail.
        """
        command = title
        if tool_name and command.startswith(tool_name):
            command = command[len(tool_name):]
        command = strip_boilerplate(command.lstrip(" :-\t"))
        extras = [
            cleaned for cleaned in (strip_boilerplate(t) for t in content_texts(content))
            if cleaned and cleaned != command
        ]
        if not command:
            return extras[0] if extras else ""
        return "\n".join([command, *extras])

    def _shell_decision(self, title: str) -> str | None:
        """auto-safe: allow a command only when *every* segment is read-only.

        A shell title can chain several programs (`ls && rm -rf /`), so the
        verdict is the strictest of its segments; anything not positively known
        to be read-only goes to the human.
        """
        command = title
        for prefix in ("Bash:", "bash:", "Shell:", "shell:", "Execute:", "run_command:"):
            if command.startswith(prefix):
                command = command[len(prefix):].strip()
                break
        segments = [s for s in re.split(r"\s*(?:\|\||&&|;|\n)\s*", command) if s.strip()]
        decisions = [self._segment_decision(segment) for segment in segments]
        if any(decision is None for decision in decisions):
            return None
        return "allow" if decisions else None

    def _segment_decision(self, segment: str) -> str | None:
        try:
            parts = shlex.split(segment)
        except ValueError:
            return None
        # shlex keeps redirections as separate tokens ("ls > f" -> [ls, >, f])
        # or glued ("ls 2>/dev/null"). Anything that writes to a sink we cannot
        # see goes to the human; the two null sinks are pure noise suppression.
        BENIGN_REDIRECTS = {"2>/dev/null", "2>&1", ">/dev/null"}
        args: list[str] = []
        index = 0
        while index < len(parts):
            token = parts[index]
            if token in BENIGN_REDIRECTS:
                index += 1
                continue
            if token in {">", ">>", "2>", "2>>"}:
                target = parts[index + 1] if index + 1 < len(parts) else ""
                if target in {"/dev/null"}:
                    index += 2
                    continue
                return None
            if token in {"|", "<", "<<<", ";", "&", "$(", "`"} or token.startswith("<"):
                return None
            if "$(" in token or "`" in token or token.startswith("~"):
                return None  # command substitution can hide anything
            if ">" in token or "<" in token or "|" in token:
                return None
            args.append(token)
            index += 1
        if not args:
            return None
        head = args[0]
        rest = args[1:]
        flags = {a for a in rest if a.startswith("-")}

        if head in INLINE_CODE_FLAGS:
            return None if flags & INLINE_CODE_FLAGS[head] else "allow"
        if head in GIT_READ_ONLY_SUBCOMMANDS:
            allowed = GIT_READ_ONLY_SUBCOMMANDS[head]
            return "allow" if rest and rest[0] in allowed else None
        if head == "tar":
            # Listing (-t) is read-only; create/extract write to disk.
            if any(re.match(r"^-[a-z]*t[a-z]*$", f) or f == "--list" for f in flags):
                return "allow"
            return None
        if head in {"curl", "wget"}:
            if flags & NETWORK_WRITE_FLAGS:
                return None
            verbs = {a.upper() for a in rest if a.upper() in _HTTP_METHODS}
            if flags & {"-X", "--request", "--method"} and not verbs <= {"GET", "HEAD"}:
                return None
            return "allow"
        if head in WRITE_FLAG_COMMANDS:
            return None if set(rest) & WRITE_FLAG_COMMANDS[head] else "allow"
        if head == "find" and set(rest) & FIND_WRITE_ACTIONS:
            return None
        if head in READ_ONLY_COMMANDS:
            return "allow"
        return None

    def remember_grant(self, interaction: PendingInteraction, option_id: str) -> None:
        tool_name = interaction.payload.get("tool_name") or ""
        title = interaction.payload.get("title") or ""
        if option_id in {"approve_always", "approve_for_session", "allow_always"}:
            leaf = (tool_name or "").rsplit("__", 1)[-1]
            if leaf in SELF_ANSWERING_MCP_TOOLS or leaf in PRIVILEGED_MCP_TOOLS:
                return  # "always allow" must not cover tools that act for the human
            key = self._grant_key(
                interaction.payload.get("kind"), title, tool_name,
                interaction.payload.get("content"),
            )
            if key:
                self._grants.setdefault(interaction.session_id, {}).setdefault(
                    "titles", set()).add(key)
            # The tool-name bucket grants *every* future call of that tool, which
            # is acceptable for file reads but not for a shell: approving
            # `ls` must never pre-approve `rm -rf /`.
            kind = interaction.payload.get("kind") or infer_tool_kind(tool_name)
            if kind != "execute" and tool_name:
                self._grants.setdefault(interaction.session_id, {}).setdefault(
                    "tools", set()).add(tool_name)

    # -- reverse RPC --------------------------------------------------------
    async def request_permission(
        self,
        session_id: str,
        tool_call: Any,
        options: list[Any],
        **kwargs: Any,
    ) -> acp.RequestPermissionResponse:
        payload = tool_call_payload(tool_call) if hasattr(tool_call, "tool_call_id") else {
            "raw": str(tool_call)
        }
        tool_kind = payload.get("kind")
        title = payload.get("title") or ""
        serialized_options = [_serialize_option(opt) for opt in options]
        option_ids = tuple(str(o["option_id"]) for o in serialized_options)
        decision = self.auto_decision(
            tool_kind,
            title,
            _tool_name_from_title(title),
            option_ids,
            session_id,
            payload.get("content", []),
        )

        if decision is None:
            # The UI must show what is actually being approved. Kimi's title is
            # only the tool name, so the recovered command / diff is passed along.
            effective_kind = tool_kind or infer_tool_kind(_tool_name_from_title(title))
            detail = ""
            if effective_kind == "execute":
                detail = self._command_text(
                    _tool_name_from_title(title), title, payload.get("content", [])
                ).replace("\n", " ")
            else:
                texts = [strip_boilerplate(t) for t in content_texts(payload.get("content", []))]
                detail = next((t for t in texts if t), "")
            interaction = await self._ask(
                kind="approval",
                session_id=session_id,
                payload={
                    "tool_call": payload,
                    "title": title,
                    "kind": effective_kind,
                    "detail": detail[:2000],
                    "options": serialized_options,
                    "tool_name": _tool_name_from_title(title),
                    "question": bool(any(QUESTION_OPTION_NS.match(o) for o in option_ids)),
                    "plan_review": bool(any(PLAN_OPTION_NS.match(o) for o in option_ids)),
                    "content": payload.get("content", []),
                },
            )
            answer = await self._await_interaction(interaction)
            behavior = answer.get("behavior", "reject")
            if behavior == "allow":
                option_id = answer.get("option_id") or _pick_option(serialized_options, "allow_once")
                chosen = option_id
            elif behavior == "allow_session":
                option_id = answer.get("option_id") or _pick_option(serialized_options, "allow_always")
                chosen = option_id
                if interaction is not None:
                    self.remember_grant(interaction, option_id)
            else:
                option_id = answer.get("option_id") or _pick_option(
                    serialized_options, "reject_once", "reject_always"
                )
                chosen = option_id
        else:
            option_id = (
                _pick_option(serialized_options, "allow_always")
                if decision == "allow_session"
                else _pick_option(serialized_options, "allow_once")
            )
            chosen = option_id
            self.decision_log.append({
                "ts": time.time(), "session_id": session_id, "auto": decision,
                "tool_call_id": payload.get("tool_call_id"), "title": title, "kind": tool_kind,
            })

        if not option_id:
            return acp.RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        self.publish(Event("approval_resolved", session_id, {
            "tool_call_id": payload.get("tool_call_id"),
            "option_id": option_id,
            "auto": decision is not None,
        }))
        return acp.RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id=option_id))

    async def create_elicitation(
        self,
        message: str,
        mode: Any,
        session_id: str = "",
        tool_call_id: str | None = None,
        requested_schema: Any = None,
        **kwargs: Any,
    ) -> Any:
        schema = _schema_to_questions(requested_schema)
        interaction = await self._ask(
            kind="question",
            session_id=session_id,
            payload={
                "message": message,
                "mode": getattr(mode, "mode", str(mode)),
                "tool_call_id": tool_call_id,
                "questions": schema,
            },
        )
        answer = await self._await_interaction(interaction)
        if answer.get("behavior") in ("reject", "cancelled"):
            return DeclineElicitationResponse(action="decline")
        content = answer.get("content") or {}
        if not content and answer.get("message"):
            content = {"answer": answer["message"]}
        return AcceptElicitationResponse(action="accept", content=content)

    async def _ask(self, kind: str, session_id: str, payload: dict[str, Any]) -> PendingInteraction:
        decision_id = self.new_id("appr" if kind == "approval" else "quest")
        loop = asyncio.get_running_loop()
        interaction = PendingInteraction(
            id=decision_id, kind=kind, session_id=session_id, payload=payload,
            future=loop.create_future(),
        )
        self._pending[decision_id] = interaction
        event_type = "approval_request" if kind == "approval" else "question_request"
        timeout = self.settings.auto_approve_within_seconds
        if timeout and kind == "approval":
            interaction.payload["auto_resolve_after"] = timeout
        self.publish(Event(event_type, session_id, {"id": decision_id, **payload}))
        return interaction

    async def _await_interaction(self, interaction: PendingInteraction) -> dict[str, Any]:
        timeout = self.settings.auto_approve_within_seconds
        # A question is never auto-answered on a timer: only a human can answer it.
        if interaction.kind == "approval" and timeout and not interaction.payload.get("question"):
            try:
                return await asyncio.wait_for(interaction.future, timeout=timeout)
            except asyncio.TimeoutError:
                self._pending.pop(interaction.id, None)
                self.publish(Event("info", interaction.session_id, {
                    "message": "approval auto-granted after timeout", "id": interaction.id,
                }))
                return {"behavior": "allow"}
        return await interaction.future


def _tool_name_from_title(title: str) -> str:
    """Kimi Code sets the permission prompt's `title` to the bare tool name
    (`buildPermissionToolCallUpdate`), while other hosts use ``Tool: detail``.
    Accept both, including the MCP-style ``mcp__server__tool: detail``."""
    match = re.match(r"^\s*(mcp__[A-Za-z0-9_-]+__[A-Za-z0-9_-]+)", title or "")
    if match:
        return match.group(1)
    head = title.split(":", 1)[0].strip() if ":" in title else title.strip()
    return head


# Kimi Code's request_permission toolCall carries only `title` (= the raw tool
# name, e.g. "Bash") plus human-readable `content`; it never sets `kind`
# (packages/acp-server/src/approval.ts::buildPermissionToolCallUpdate). Treating
# a missing kind as safe would auto-approve every shell command, so the kind is
# inferred from the tool name and the real command is recovered from content.
# The names below are the registry Kimi actually ships
# (packages/agent-core-v2/src/agent/tools/**/<x>Tool.ts: `readonly name = '…'`).
EXECUTE_TOOL_NAMES = {
    "bash", "shell", "execute", "exec", "run", "run_command", "command",
    "terminal", "local_shell", "shell_command",
}
DESTRUCTIVE_TOOL_NAMES = {"rm", "remove", "delete", "rmdir", "unlink", "drop"}
EDIT_TOOL_NAMES = {
    "write", "write_file", "edit", "edit_file", "apply_patch", "patch",
    "create_file", "multiedit", "replace",
}
READ_TOOL_NAMES = {
    "read", "read_file", "view", "cat", "list_dir", "ls",
    "readmediafile", "tasklist", "taskoutput", "waitfor",
}
SEARCH_TOOL_NAMES = {"grep", "glob", "search", "websearch", "web_search"}
FETCH_TOOL_NAMES = {"fetch", "fetchurl", "webfetch", "curl"}
APPROVAL_BOILERPLATE = re.compile(
    # Kimi phrases differ per tool: "Running: <command>" for Bash, a plain
    # "Approve <tool>" for MCP calls. The verb must be followed by whitespace or a
    # colon, so a command that merely starts with such a word ("allow-list.sh")
    # is left intact.
    r"^\s*(?:requesting approval to\s+)?(?:approval[:\s-]+)?(?:do you want (?:me )?to\s+)?"
    # The separator is mandatory: a command that merely begins with one of these
    # words ("allow-list.sh") must survive untouched.
    r"(?:(?:running|executing|about to run|to run|approving|approve|allowing|allow)"
    r"(?:\s+|[:\-]\s+))?",
    re.IGNORECASE,
)


def content_texts(content: Any) -> list[str]:
    """Flatten an ACP toolCall content array (text / diff entries) to strings."""
    out: list[str] = []
    for entry in content or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "diff":
            body = "\n".join(filter(None, [
                f"diff {entry.get('path', '')}",
                entry.get("old_text"),
                entry.get("new_text"),
            ]))
        else:
            payload = entry.get("content")
            body = payload.get("text") if isinstance(payload, dict) else entry.get("text")
        if isinstance(body, str) and body:
            out.append(body)
    return out


def strip_boilerplate(text: str) -> str:
    """Remove the "Requesting approval to Running: " wrapper Kimi adds."""
    return APPROVAL_BOILERPLATE.sub("", text or "").strip()


def infer_tool_kind(tool_name: str) -> str | None:
    """Map a tool name to an ACP ToolKind when the agent omits `kind`."""
    name = (tool_name or "").strip()
    if not name:
        return None
    if name.startswith("mcp__"):
        leaf = name.rsplit("__", 1)[-1]
        if leaf == "shell":
            return "execute"
        if leaf == "read_file":
            return "read"
        if leaf in {"write_file", "edit_file"}:
            return "edit"
        if leaf in {"grep_files", "search"}:
            return "search"
        if leaf in {"fetch", "web_search", "webfetch"}:
            return "fetch"
        return "other"
    bare = name.lower().rsplit(".", 1)[-1]
    if bare in EXECUTE_TOOL_NAMES:
        return "execute"
    if bare in DESTRUCTIVE_TOOL_NAMES:
        return "delete"
    if bare in EDIT_TOOL_NAMES:
        return "edit"
    if bare in READ_TOOL_NAMES:
        return "read"
    if bare in SEARCH_TOOL_NAMES:
        return "search"
    if bare in FETCH_TOOL_NAMES:
        return "fetch"
    return "other"


def _serialize_option(opt: Any) -> dict[str, Any]:
    """Normalise one PermissionOption for the UI and for `_pick_option`.

    A missing `kind` is deliberately left empty rather than defaulted to
    `allow_once`: stamping a default made every option look like the same
    polarity, so a request for "allow_once" matched the *first* entry in the list
    -- which for some agents is `approve_always`, i.e. a standing grant handed out
    where a one-shot approval was meant. Empty kind falls through to the token
    heuristic in `_pick_option`, and if that matches nothing the call is
    cancelled, which is the safe failure mode.
    """
    return {
        "option_id": getattr(opt, "option_id", str(opt)),
        "name": getattr(opt, "name", str(opt)),
        "kind": getattr(opt, "kind", "") or "",
    }


def _option_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[_\W]+", (text or "").lower()) if t]


# Option ids differ per agent (`allow_once` in the ACP spec, `approve_once` in
# Kimi Code), so the fallback matches on polarity plus the trailing word instead
# of a prefix. It used to be `option_id.startswith(tuple(kinds))`, and since
# `tuple("allow_once")` is a bag of letters, nearly every option matched: asking
# for a single allow could return a standing "always" grant.
ALLOW_OPTION_WORDS = {"allow", "approve", "accept", "yes"}
DENY_OPTION_WORDS = {"reject", "deny", "cancel", "block", "no"}


def _pick_option(options: list[dict[str, Any]], *kinds: str) -> str:
    for kind in kinds:
        for option in options:
            if option.get("kind") == kind:
                return option["option_id"]
    for option in options:
        option_id = str(option.get("option_id", ""))
        parts = _option_tokens(option_id)
        for kind in kinds:
            tokens = _option_tokens(kind)
            if not tokens:
                continue
            wants_deny = tokens[0].startswith("reject")
            polarity_ok = any(p in (DENY_OPTION_WORDS if wants_deny
                                     else ALLOW_OPTION_WORDS) for p in parts)
            tail = tokens[-1] if len(tokens) > 1 else ""
            if polarity_ok and (not tail or tail in parts):
                return option_id
    return ""


def _schema_to_questions(schema: Any) -> list[dict[str, Any]]:
    """Best-effort conversion of an MCP-style elicitation schema to questions."""
    if schema is None:
        return []
    dump = schema.model_dump(by_alias=True, exclude_none=True) if hasattr(schema, "model_dump") else dict(schema or {})
    properties = dump.get("properties") or {}
    questions: list[dict[str, Any]] = []
    for key, spec in properties.items():
        if not isinstance(spec, dict):
            continue
        entry: dict[str, Any] = {
            "id": key,
            "question": spec.get("title") or key,
            "description": spec.get("description"),
            "type": spec.get("type") or "string",
            "default": spec.get("default"),
        }
        enum = spec.get("enum") or [
            item.get("value") if isinstance(item, dict) else item
            for item in (spec.get("oneOf") or spec.get("anyOf") or [])
            if isinstance(item, dict)
        ]
        if enum:
            entry["options"] = [{"value": v, "label": str(v)} for v in enum if v is not None]
        if spec.get("type") == "array" or "items" in spec:
            entry["multi_select"] = True
        questions.append(entry)
    return questions


# ---------------------------------------------------------------------------
# Sandboxed filesystem + client-owned terminals
# ---------------------------------------------------------------------------
class FileService:
    """Serves ``fs/read_text_file`` / ``fs/write_text_file`` for the agent."""

    def __init__(self, settings: Settings) -> None:
        self.roots: list[Path] = []
        self.add_root(settings.workspace)
        for extra in settings.additional_dirs:
            self.add_root(Path(extra))
        self.add_root(Path(os.environ.get("COOMI_KIMI_HOME", str(Path.home() / ".coomi-kimi"))))
        self.add_root(Path("/tmp"))

    def add_root(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            return
        if resolved not in self.roots:
            self.roots.append(resolved)

    def authorize(self, path: str | os.PathLike[str]) -> Path | None:
        candidate = Path(os.path.expanduser(str(path)))
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate
        for root in self.roots:
            try:
                resolved.relative_to(root)
            except ValueError:
                continue
            return resolved
        return None

    def read(self, path: str, line: int | None = None, limit: int | None = None) -> str:
        authorized = self.authorize(path)
        if authorized is None:
            raise FileNotFoundError(f"path outside the workspace allowlist: {path}")
        if authorized.is_dir():
            raise IsADirectoryError(f"{path} is a directory")
        raw = authorized.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = 0
        if line is not None:
            start = max(int(line) - 1, 0)
        end = start + int(limit) if limit else len(lines)
        return "\n".join(lines[start:end])

    def write(self, path: str, content: str) -> None:
        authorized = self.authorize(path)
        if authorized is None:
            raise FileNotFoundError(f"path outside the workspace allowlist: {path}")
        authorized.parent.mkdir(parents=True, exist_ok=True)
        authorized.write_text(content, encoding="utf-8")


class TerminalService:
    """Implements the ACP terminal API with real subprocesses."""

    def __init__(self, settings: Settings, files: FileService) -> None:
        self.settings = settings
        self.files = files
        self._terminals: dict[str, asyncio.subprocess.Process] = {}
        self._output: dict[str, str] = {}
        self._exit: dict[str, WaitForTerminalExitResponse] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"term_{int(time.time() * 1000):x}_{self._counter}"

    async def create(
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[Any] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> acp.CreateTerminalResponse:
        terminal_id = self._new_id()
        program = command if not args else [command, *args]
        workdir = Path(cwd) if cwd else self.settings.workspace
        if cwd and self.files.authorize(workdir) is None:
            raise acp.RequestError.invalid_params({"cwd": "outside the workspace allowlist"})
        environment = dict(os.environ)
        for item in env or []:
            name = getattr(item, "name", None) or (item.get("name") if isinstance(item, dict) else None)
            value = getattr(item, "value", None) or (item.get("value") if isinstance(item, dict) else None)
            if name is not None:
                environment[name] = str(value if value is not None else "")
        limit = int(output_byte_limit or 50_000)

        if isinstance(program, str):
            process = await asyncio.create_subprocess_shell(
                program,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(workdir),
                env=environment,
            )
        else:
            process = await asyncio.create_subprocess_exec(
                *program,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL,
                cwd=str(workdir),
                env=environment,
            )
        self._terminals[terminal_id] = process
        self._output[terminal_id] = ""
        self._locks[terminal_id] = asyncio.Lock()
        lock = self._locks[terminal_id]
        asyncio.create_task(self._pump(terminal_id, process, limit, lock))
        return acp.CreateTerminalResponse(terminal_id=terminal_id)

    async def _pump(self, terminal_id: str, process: asyncio.subprocess.Process,
                    limit: int, lock: asyncio.Lock) -> None:
        chunks: list[str] = []
        size = 0
        assert process.stdout is not None
        while True:
            piece = await process.stdout.read(8192)
            if not piece:
                break
            text = piece.decode("utf-8", errors="replace")
            chunks.append(text)
            size += len(text)
            if size > limit:
                joined = "".join(chunks)[-limit:]
                chunks, size = [joined], len(joined)
            async with lock:
                self._output[terminal_id] = "".join(chunks)
        code = await process.wait()
        async with lock:
            self._output[terminal_id] = "".join(chunks)
            self._exit[terminal_id] = self._status(code)

    async def output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        if terminal_id not in self._terminals:
            raise acp.RequestError.resource_not_found({"terminal_id": "unknown terminal"})
        async with self._locks[terminal_id]:
            text = self._output.get(terminal_id, "")
            exit_status = self._exit.get(terminal_id)
        truncated = False
        limit = 50_000
        if len(text.encode()) > limit:
            text = text.encode()[-limit:].decode("utf-8", errors="replace")
            truncated = True
        return TerminalOutputResponse(
            output=text, truncated=truncated,
            exit_status=acp.schema.TerminalExitStatus(
                exit_code=exit_status.exit_code if exit_status else None,
                signal=exit_status.signal if exit_status else None,
            ) if exit_status else None,
        )

    @staticmethod
    def _status(code: int | None) -> WaitForTerminalExitResponse:
        """ACP reports `exitCode` as an integer and `signal` as a *string*.

        asyncio gives -N for death by signal N, so translate to the signal name
        and leave the exit code null, exactly as the schema describes.
        """
        if code is None:
            return WaitForTerminalExitResponse(exit_code=None, signal=None)
        if code < 0:
            try:
                name = signal_module.Signals(-code).name
            except ValueError:
                name = str(-code)
            return WaitForTerminalExitResponse(exit_code=None, signal=name)
        return WaitForTerminalExitResponse(exit_code=code, signal=None)

    async def wait_for_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> WaitForTerminalExitResponse:
        process = self._terminals.get(terminal_id)
        if process is None:
            raise acp.RequestError.resource_not_found({"terminal_id": "unknown terminal"})
        code = await process.wait()
        status = self._status(code)
        lock = self._locks.get(terminal_id)
        if lock is not None:
            async with lock:
                self._exit[terminal_id] = status
        else:
            self._exit[terminal_id] = status
        return status

    async def kill(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        process = self._terminals.get(terminal_id)
        if process is None:
            raise acp.RequestError.resource_not_found({"terminal_id": "unknown terminal"})
        try:
            process.kill()
        except ProcessLookupError:
            pass
        return acp.schema.KillTerminalResponse()

    async def release(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        # Drop the bookkeeping first so the pump task stops writing, then make
        # sure the child is actually reaped - otherwise every released terminal
        # leaves a zombie plus an open pipe behind.
        if terminal_id not in self._terminals:
            raise acp.RequestError.resource_not_found({"terminal_id": "unknown terminal"})
        process = self._terminals.pop(terminal_id, None)
        self._output.pop(terminal_id, None)
        self._exit.pop(terminal_id, None)
        if process is not None:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except (asyncio.TimeoutError, ProcessLookupError):
                    pass
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.feed_eof()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
        self._locks.pop(terminal_id, None)
        return acp.schema.ReleaseTerminalResponse()

    async def close_all(self) -> None:
        for terminal_id in list(self._terminals):
            try:
                await self.release("", terminal_id)
            except Exception:
                pass


__all__ = [
    "DecisionBroker",
    "FileService",
    "TerminalService",
    "ContentToolCallContent",
    "TextContentBlock",
    "WriteTextFileResponse",
    "ReadTextFileResponse",
    "base64",
    "binascii",
    "mimetypes",
    "json",
]
