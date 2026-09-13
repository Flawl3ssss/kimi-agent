"""Normalized event model.

ACP ``session/update`` carries a wide union of typed notifications. Every
consumer of this agent (web UI, CLI, HTTP clients) wants one flat, append-only
event stream, so the conversion lives here and nowhere else.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

EVENT_TYPES = {
    "turn_started",
    "text",
    "thinking",
    "user_message",
    "tool_call",
    "tool_update",
    "plan",
    "commands",
    "config",
    "session_info",
    "usage",
    "mode",
    "turn_completed",
    "turn_cancelled",
    "turn_failed",
    "approval_request",
    "approval_resolved",
    "question_request",
    "question_resolved",
    "agent_log",
    "agent_exited",
    "agent_restarted",
    "info",
}


@dataclass(slots=True)
class Event:
    type: str
    session_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    seq: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "session_id": self.session_id,
            "data": self.data,
            "ts": self.ts,
            "seq": self.seq,
        }


def content_blocks_to_payload(update: Any) -> dict[str, Any]:
    """Turn an ACP content block (text/image/resource) into a JSON payload."""
    payload: dict[str, Any] = {}
    content = getattr(update, "content", None)
    if content is None:
        return payload

    kind = getattr(content, "type", None) or getattr(content, "model_fields_set", None)
    if kind == "text" or hasattr(content, "text"):
        payload["text"] = getattr(content, "text", "") or ""
    elif kind == "image":
        payload["image"] = {
            "data": getattr(content, "data", ""),
            "mime_type": getattr(content, "mime_type", ""),
            "uri": getattr(content, "uri", None),
        }
    elif kind == "resource_link":
        payload["resource_link"] = {
            "uri": getattr(content, "uri", ""),
            "name": getattr(content, "name", ""),
            "mime_type": getattr(content, "mime_type", None),
        }
    elif kind == "resource":
        resource = getattr(content, "resource", None)
        payload["resource"] = {
            "uri": getattr(resource, "uri", ""),
            "text": getattr(resource, "text", None),
            "mime_type": getattr(resource, "mime_type", None),
        }
    else:
        payload["text"] = str(content)
    return payload


def tool_call_payload(call: Any) -> dict[str, Any]:
    """Serialize a ToolCallStart / ToolCallProgress / ToolCallUpdate."""
    out: dict[str, Any] = {
        "tool_call_id": getattr(call, "tool_call_id", None),
        "title": getattr(call, "title", None),
        "kind": getattr(call, "kind", None),
        "status": getattr(call, "status", None),
    }
    raw_input = getattr(call, "raw_input", None)
    if raw_input is not None:
        out["input"] = raw_input
    raw_output = getattr(call, "raw_output", None)
    if raw_output is not None:
        out["output"] = raw_output

    contents: list[dict[str, Any]] = []
    for item in getattr(call, "content", None) or []:
        item_type = getattr(item, "type", None)
        if item_type == "content":
            contents.append({"type": "content", **content_blocks_to_payload(item)})
        elif item_type == "diff":
            contents.append({
                "type": "diff",
                "path": getattr(item, "path", ""),
                "old_text": getattr(item, "old_text", None),
                "new_text": getattr(item, "new_text", ""),
            })
        elif item_type == "terminal":
            contents.append({"type": "terminal", "terminal_id": getattr(item, "terminal_id", "")})
        else:
            contents.append({"type": "unknown", "repr": str(item)})
    if contents:
        out["content"] = contents

    locations = [
        {"path": getattr(loc, "path", ""), "line": getattr(loc, "line", None)}
        for loc in (getattr(call, "locations", None) or [])
    ]
    if locations:
        out["locations"] = locations
    return {k: v for k, v in out.items() if v is not None}


def plan_payload(update: Any) -> dict[str, Any]:
    entries = []
    for entry in getattr(update, "entries", None) or []:
        entries.append({
            "content": getattr(entry, "content", ""),
            "status": getattr(entry, "status", "pending"),
            "priority": getattr(entry, "priority", "medium"),
        })
    return {"entries": entries}


def config_options_payload(conn_options: Any) -> dict[str, Any]:
    options = []
    for option in conn_options or []:
        choices = []
        for choice in getattr(option, "options", None) or []:
            if hasattr(choice, "value"):
                choices.append({
                    "value": choice.value,
                    "name": getattr(choice, "name", choice.value),
                    "description": getattr(choice, "description", None),
                })
            else:  # grouped options
                for nested in getattr(choice, "options", None) or []:
                    choices.append({
                        "value": nested.value,
                        "name": getattr(nested, "name", nested.value),
                        "description": getattr(nested, "description", None),
                        "group": getattr(choice, "name", None),
                    })
        options.append({
            "id": getattr(option, "id", None),
            "name": getattr(option, "name", None),
            "category": getattr(option, "category", None),
            "type": getattr(option, "type", None),
            "current_value": getattr(option, "current_value", None),
            "options": choices,
        })
    return {"config_options": options}


def update_to_event(session_id: str, update: Any) -> Event | None:
    """Map one ACP ``session/update`` onto a normalized Event, or drop it."""
    kind = getattr(update, "session_update", None)
    if kind is None:
        dump = update.model_dump(by_alias=True, exclude_none=True) if hasattr(update, "model_dump") else {}
        kind = dump.get("sessionUpdate")
    if not kind:
        return None

    if kind == "agent_message_chunk":
        return Event("text", session_id, content_blocks_to_payload(update))
    if kind == "agent_thought_chunk":
        return Event("thinking", session_id, content_blocks_to_payload(update))
    if kind == "user_message_chunk":
        return Event("user_message", session_id, content_blocks_to_payload(update))
    if kind == "tool_call":
        return Event("tool_call", session_id, tool_call_payload(update))
    if kind == "tool_call_update":
        return Event("tool_update", session_id, tool_call_payload(update))
    if kind == "plan":
        return Event("plan", session_id, plan_payload(update))
    if kind == "available_commands_update":
        commands = [
            {
                "name": getattr(cmd, "name", ""),
                "description": getattr(cmd, "description", ""),
            }
            for cmd in (getattr(update, "available_commands", None) or [])
        ]
        return Event("commands", session_id, {"commands": commands})
    if kind == "config_option_update":
        return Event("config", session_id, config_options_payload(getattr(update, "config_options", None)))
    if kind == "session_info_update":
        return Event("session_info", session_id, {
            "title": getattr(update, "title", None),
            "updated_at": getattr(update, "updated_at", None),
        })
    if kind == "usage_update":
        cost = getattr(update, "cost", None)
        return Event("usage", session_id, {
            "used": getattr(update, "used", 0),
            "size": getattr(update, "size", 0),
            "cost": None if cost is None else {
                "amount": getattr(cost, "amount", None),
                "currency": getattr(cost, "currency", None),
            },
        })
    if kind == "current_mode_update":
        return Event("mode", session_id, {"mode": getattr(update, "current_mode_id", "")})
    # AgentPlanContentUpdate / AgentPlanRemovedUpdate and friends: forward raw.
    if hasattr(update, "model_dump"):
        dump = update.model_dump(by_alias=True, exclude_none=True)
        return Event("info", session_id, {"raw_type": kind, "raw": dump})
    return None
