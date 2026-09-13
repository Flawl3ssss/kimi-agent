"""In-process MCP host for the Coomi tool surface.

Two consumers:
  * Kimi Code connects over the streamable-HTTP MCP transport (advertised in
    `config/mcp.json` as an http server) and sees `mcp__coomi__*` tools;
  * our own RPC calls the same tool registry directly via `call_tool`, so a
    UI action and a model action run identical code.

stdio would force a separate process with no access to the ACP bridge (subagents,
decisions, image push), which is why the transport is HTTP inside this process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

from .tools import make_server

if TYPE_CHECKING:  # pragma: no cover
    from .bridge import KimiCodeBridge

_SERVERS: dict[int, Any] = {}


def get_server(bridge: "KimiCodeBridge") -> Any:
    server = _SERVERS.get(id(bridge))
    if server is None:
        server = make_server(bridge, host=bridge.settings.host, port=bridge.settings.bridge_port)
        _SERVERS[id(bridge)] = server
    return server


async def tool_specs(bridge: "KimiCodeBridge") -> list[dict[str, Any]]:
    tools = await get_server(bridge).list_tools()
    return [
        {
            "name": tool.name,
            "qualified": f"mcp__coomi__{tool.name}",
            "description": tool.description or "",
            "input_schema": tool.input_schema,
        }
        for tool in tools
    ]


def _flatten(result: Any) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        elif getattr(block, "type", None) == "image":
            parts.append(f"[image {getattr(block, 'mime_type', '') or getattr(block, 'mimeType', '')}]")
    if not parts and getattr(result, "structured_content", None):
        parts.append(json.dumps(result.structured_content, ensure_ascii=False))
    return "\n".join(parts)


async def call_tool(
    bridge: "KimiCodeBridge",
    name: str,
    args: dict[str, Any],
    session_id: str = "",
) -> str:
    server = get_server(bridge)
    if session_id:
        bridge.active_session_hint = session_id
    result = await server.call_tool(name, args or {})
    text = _flatten(result)
    if getattr(result, "is_error", False):
        raise RuntimeError(text or f"tool {name} failed")
    return text


async def serve_http(bridge: "KimiCodeBridge", host: str, port: int) -> asyncio.Task:
    """Bring up the streamable-HTTP MCP endpoint and wait until it accepts connections."""
    server = get_server(bridge)

    async def _run() -> None:
        await server.run_streamable_http_async(host=host, port=port, streamable_http_path="/mcp")

    task = asyncio.create_task(_run(), name="coomi-mcp-http")
    deadline = asyncio.get_running_loop().time() + 25
    while asyncio.get_running_loop().time() < deadline:
        if task.done():  # failed fast: surface the real error
            await task
            break
        try:
            _reader, writer = await asyncio.open_connection(host, port)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return task
        except OSError:
            await asyncio.sleep(0.15)
    task.cancel()
    raise TimeoutError(f"MCP http endpoint never came up on {host}:{port}")
