"""Coomi tool surface, exposed to Kimi Code as an in-process MCP server.

Why HTTP and not stdio: Kimi spawns stdio MCP servers as *separate* processes,
which cannot reach the ACP bridge. A streamable-HTTP MCP endpoint served inside
this process lets tools dispatch real work through the bridge — sub-agents as
extra ACP sessions, approvals answered from the UI, images pushed to the chat.

Tool names arrive at the model as ``mcp__coomi__<tool>``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
import shutil
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .. import store
from ..config import VERSION

logger = logging.getLogger("coomi-kimi.tools")

if TYPE_CHECKING:  # pragma: no cover
    from ..bridge import KimiCodeBridge


def make_server(bridge: "KimiCodeBridge", host: str, port: int) -> MCPServer:
    server = MCPServer(
        name="coomi",
        version=VERSION,
        instructions=(
            "Coomi workspace tools: durable memory, structured plans, sub-agents, "
            "saved workflows, skills, autonomous loops, web search, image display "
            "and file transfer. Prefer these over ad-hoc shell for the same jobs."
        ),
    )
    # Registered through a wrapper that keeps the schema identical but turns
    # internal exceptions into ToolErrors, so the model reads the real reason
    # instead of MCP's generic "Error executing tool <name>".
    tool = _exposed_tools(server)
    memories = store.MemoryStore(bridge.settings.workspace)
    plans = store.PlanStore()
    workflows = store.WorkflowStore()
    skills = store.SkillStore([Path(p) for p in _skill_dirs()])
    loops = store.LoopStore()

    def current_session() -> str:
        return bridge.active_session_hint or ""

    # ------------------------------------------------------------- memory
    @tool()
    async def memory_write(
        name: str,
        description: str,
        type: str,
        content: str,
        scope: str = "project",
        project: str = "",
    ) -> str:
        """Create or overwrite a durable memory. scope: local|project|global; type: user|feedback|project|reference."""
        record = memories.write(name, description, type, content, scope, project or None)
        store.audit("memory_write", {"name": name, "scope": scope}, "ok")
        return f"stored {record.scope}/{record.name} ({record.type})"

    @tool()
    async def memory_read(name: str, project: str = "") -> str:
        """Read one memory by name (local > project > global precedence)."""
        record = memories.read(name, project or None)
        if record is None:
            return f"not found: {name}"
        return json.dumps({
            "name": record.name, "description": record.description, "type": record.type,
            "scope": record.scope, "content": record.content,
        }, ensure_ascii=False, indent=1)

    @tool()
    async def memory_search(query: str, limit: int = 8) -> str:
        """Search memories for relevant project/user context."""
        hits = memories.search(query, limit)
        if not hits:
            return "no matches"
        return json.dumps([
            {"name": h.name, "scope": h.scope, "type": h.type, "description": h.description,
             "excerpt": h.content[:400]}
            for h in hits
        ], ensure_ascii=False, indent=1)

    @tool()
    async def memory_list(project: str = "") -> str:
        """List every memory with its scope. project narrows the view to one project's records."""
        items = memories.iter_all(project or None)
        if not items:
            return "no memories stored yet"
        return json.dumps([
            {"name": r.name, "scope": r.scope, "type": r.type, "description": r.description}
            for r in items
        ], ensure_ascii=False, indent=1)

    @tool()
    async def memory_delete(name: str) -> str:
        """Delete the highest-precedence memory with this name."""
        scope = memories.delete(name)
        return f"deleted {scope}/{name}" if scope else f"not found: {name}"

    # ---------------------------------------------------------------- plan
    @tool()
    async def update_plan(steps: list[dict[str, Any]], explanation: str = "") -> str:
        """Record/update the working plan. steps: [{step, status}] with status pending|in_progress|completed; at most one in_progress. It is also surfaced to the user as a plan panel."""
        plan = plans.update(current_session() or "default", steps, explanation)
        await bridge.publish_local_plan(plan)
        return json.dumps({"ok": True, "plan": plan["steps"]}, ensure_ascii=False)

    @tool()
    async def get_plan() -> str:
        """Read the current plan for this session."""
        plan = plans.get(current_session() or "default")
        return json.dumps(plan, ensure_ascii=False) if plan else "no plan recorded"

    # --------------------------------------------------------- sub-agents
    @tool()
    async def spawn_agent(
        task: str,
        subagent_type: str = "coder",
        model: str = "",
        background: bool = True,
    ) -> str:
        """Dispatch a focused sub-agent in its own context window (fresh Kimi Code session). Returns an agent id; poll with agent_status/agent_wait."""
        profile = _SUBAGENT_PROFILES.get(subagent_type, _SUBAGENT_PROFILES["coder"])
        state = await bridge.new_session(mode=profile["mode"])
        agent_id = state.id
        bridge.subagents[agent_id] = {"task": task, "type": subagent_type, "created_at": time.time()}
        text = f"{profile['prompt']}\n\nTASK:\n{task}"
        task_handle = await bridge.prompt(agent_id, text)

        async def _watch() -> None:
            try:
                result = await task_handle
                entry = bridge.subagents.get(agent_id) or {}
                entry["result"] = "".join(
                    bridge.sessions[agent_id].last_text_parts if agent_id in bridge.sessions else []
                )
                entry["status"] = result.get("stop_reason", "done") if isinstance(result, dict) else "done"
                bridge.subagents[agent_id] = entry
            except Exception as exc:  # noqa: BLE001 - report into the record
                bridge.subagents[agent_id] = {
                    **bridge.subagents.get(agent_id, {}), "status": "failed", "error": str(exc)[:400],
                }

        asyncio.create_task(_watch())
        store.audit("spawn_agent", {"type": subagent_type, "task": task[:200]}, agent_id)
        return json.dumps({"agent_id": agent_id, "type": subagent_type, "session": state.id,
                           "background": background})

    @tool()
    async def agent_status(agent_id: str) -> str:
        """Check a sub-agent's status and any partial output."""
        state = bridge.sessions.get(agent_id)
        record = bridge.subagents.get(agent_id)
        if state is None and record is None:
            return f"unknown agent: {agent_id}"
        return json.dumps({
            "agent_id": agent_id,
            "status": (state.status if state else (record or {}).get("status", "unknown")),
            "type": (record or {}).get("type"),
            "task": ((record or {}).get("task") or "")[:300],
            "output_tail": "".join(state.last_text_parts)[-1500:] if state else "",
            "error": (record or {}).get("error"),
        }, ensure_ascii=False)

    @tool()
    async def agent_wait(agent_ids: list[str], timeout_s: int = 600) -> str:
        """Wait for sub-agents to finish and return their final reports."""
        deadline = time.time() + max(1, min(timeout_s, 3600))
        pending = [a for a in agent_ids if a in bridge.sessions]
        while pending and time.time() < deadline:
            running = [a for a in pending
                       if bridge.sessions.get(a) and bridge.sessions[a].status == "running"]
            if not running:
                break
            await asyncio.sleep(1.0)
        report = []
        for agent_id in agent_ids:
            state = bridge.sessions.get(agent_id)
            report.append({
                "agent_id": agent_id,
                "status": state.status if state else "gone",
                "result": ("".join(state.last_text_parts) if state else "")[-4000:],
                "record": {k: v for k, v in (bridge.subagents.get(agent_id) or {}).items()
                           if k in {"type", "status", "error"}},
            })
        return json.dumps(report, ensure_ascii=False, indent=1)

    @tool()
    async def close_agent(agent_id: str) -> str:
        """Stop a sub-agent and release its session."""
        await bridge.close_session(agent_id)
        bridge.subagents[agent_id] = {**bridge.subagents.get(agent_id, {}), "status": "closed"}
        return f"closed {agent_id}"

    # ------------------------------------------------------------ workflow
    @tool()
    async def create_workflow(id: str, name: str, steps: list[dict[str, Any]],
                              description: str = "") -> str:  # noqa: A002 - MCP arg name
        """Save an executable pipeline: steps [{id, action, depends_on, prompt, args}], DAG validated for unknown deps and cycles."""
        wf = store.Workflow(id=id or workflows.new_id(name), name=name, steps=steps,
                            description=description)
        saved = workflows.save(wf)
        return json.dumps({"id": saved.id, "steps": [s["id"] for s in saved.steps]})

    @tool()
    async def list_workflows() -> str:
        """List saved workflows."""
        items = workflows.list()
        if not items:
            return "no workflows saved"
        return json.dumps([
            {"id": w.id, "name": w.name, "steps": len(w.steps), "description": w.description}
            for w in items
        ], ensure_ascii=False, indent=1)

    @tool()
    async def get_workflow(id: str) -> str:  # noqa: A002
        """Print one workflow definition."""
        wf = workflows.get(id)
        return json.dumps(wf.to_dict(), ensure_ascii=False, indent=1) if wf else f"not found: {id}"

    @tool()
    async def delete_workflow(id: str) -> str:  # noqa: A002
        """Delete a workflow definition."""
        return f"deleted {id}" if workflows.delete(id) else f"not found: {id}"

    @tool()
    async def run_workflow(id: str, max_parallel: int = 2) -> str:  # noqa: A002
        """Execute a workflow: dependency-ordered steps, each step a sub-agent turn; later steps receive prior outputs."""
        wf = workflows.get(id)
        if wf is None:
            return f"not found: {id}"
        by_id = {s["id"]: s for s in wf.steps}
        done: dict[str, str] = {}
        results: list[dict[str, Any]] = []
        sem = asyncio.Semaphore(max(1, min(max_parallel, 4)))

        async def run_step(step: dict[str, Any]) -> None:
            async with sem:
                context = "\n\n".join(
                    f"### {dep} output\n{done[dep][:4000]}" for dep in step["depends_on"] if dep in done
                )
                prompt = step["prompt"] or f"Execute step {step['id']} of workflow {wf.name}."
                if context:
                    prompt = f"{prompt}\n\nUPSTREAM RESULTS:\n{context}"
                state = await bridge.new_session(mode="auto")
                try:
                    handle = await bridge.prompt(state.id, prompt)
                    await handle
                    output = "".join(state.last_text_parts)
                except Exception as exc:  # noqa: BLE001
                    output = f"STEP FAILED: {exc}"
                finally:
                    await bridge.close_session(state.id)
                done[step["id"]] = output
                results.append({"step": step["id"], "chars": len(output),
                                "excerpt": output[-800:]})

        ready = [s for s in wf.steps if not s["depends_on"]]
        remaining = [s for s in wf.steps if s["depends_on"]]
        while ready:
            await asyncio.gather(*(run_step(s) for s in ready))
            ready = [s for s in remaining if all(d in done for d in s["depends_on"])]
            remaining = [s for s in remaining if s not in ready]
            if not ready and remaining:
                return json.dumps({"error": "stalled: unresolvable deps", "done": list(done),
                                   "pending": [s["id"] for s in remaining]})
        return json.dumps({"workflow": wf.id, "steps": results}, ensure_ascii=False, indent=1)

    # -------------------------------------------------------------- skills
    @tool()
    async def list_skills() -> str:
        """List discovered skills (own + imported)."""
        items = skills.discover()
        if not items:
            return "no skills found"
        return json.dumps([
            {"name": s["name"], "description": s["description"], "source": s["source"],
             "writable": s["writable"]}
            for s in items
        ], ensure_ascii=False, indent=1)

    @tool()
    async def read_skill(name: str) -> str:
        """Load one skill's full instructions."""
        skill = skills.read(name)
        return skill["content"] if skill else f"skill not found: {name}"

    @tool()
    async def create_skill(name: str, description: str, content: str) -> str:
        """Author a reusable skill (Markdown, YAML frontmatter with name+description)."""
        target = skills.create(name, description, content)
        return f"created {target}"

    # --------------------------------------------------------------- loops
    @tool()
    async def create_loop(objective: str, max_turns: int = 5, token_budget: int = 0) -> str:
        """Start an autonomous loop objective that can be resumed turn by turn."""
        loop = loops.new(objective, max(1, min(max_turns, 50)), token_budget or None)
        return json.dumps({"id": loop.id, "objective": loop.objective, "max_turns": loop.max_turns})

    @tool()
    async def list_loops() -> str:
        """List loops with status and turns used."""
        items = loops.list()
        if not items:
            return "no loops"
        return json.dumps([
            {"id": l.id, "status": l.status, "objective": l.objective[:200],
             "turns_used": l.turns_used, "max_turns": l.max_turns, "session_id": l.session_id}
            for l in items
        ], ensure_ascii=False, indent=1)

    @tool()
    async def run_loop_turn(loop_id: str) -> str:
        """Advance a loop by one turn in its own session; appends to the loop log."""
        loop = loops.get(loop_id)
        if loop is None:
            return f"not found: {loop_id}"
        if loop.status != "active":
            return f"loop {loop.id} is {loop.status}; update_loop status=active to resume"
        if loop.turns_used >= loop.max_turns:
            loop.status = "complete"
            loops.save(loop)
            return json.dumps({"id": loop.id, "status": "complete", "note": "turn budget reached"})
        if loop.session_id and loop.session_id in bridge.sessions:
            state = bridge.sessions[loop.session_id]
        else:
            state = await bridge.new_session(mode="auto")
            loop.session_id = state.id
            loops.save(loop)
        prompt = _loop_turn_prompt(loop, bridge)
        handle = await bridge.prompt(state.id, prompt)
        try:
            await handle
        finally:
            pass
        output = "".join(state.last_text_parts)
        loop.turns_used += 1
        loop.log.append({"turn": loop.turns_used, "ts": time.time(), "excerpt": output[-1200:]})
        loops.save(loop)
        return json.dumps({"id": loop.id, "turn": loop.turns_used, "output": output[-2000:]},
                          ensure_ascii=False)

    @tool()
    async def update_loop(loop_id: str, status: str = "", objective: str = "") -> str:
        """Update a loop's status (active|paused|blocked|complete) or objective."""
        loop = loops.get(loop_id)
        if loop is None:
            return f"not found: {loop_id}"
        if status:
            if status not in {"active", "paused", "blocked", "complete"}:
                return "status must be active|paused|blocked|complete"
            loop.status = status
        if objective:
            loop.objective = objective
        loops.save(loop)
        return json.dumps({"id": loop.id, "status": loop.status})

    # ----------------------------------------------------------------- web
    @tool()
    async def web_search(query: str, max_results: int = 6) -> str:
        """Web search. Needs SERPER_API_KEY (or KEYWORD_SEARCH_URL) in the environment; reports clearly when unset."""
        from .. import websearch

        hits = await websearch.search(query, max_results)
        if isinstance(hits, str):
            return hits
        return json.dumps(hits, ensure_ascii=False, indent=1)

    # -------------------------------------------------------- multimodal
    @tool()
    async def show_image(path: str, caption: str = "") -> str:
        """Display a local image to the user in the chat UI."""
        return await _send_image(bridge, path, caption)

    @tool()
    async def view_image(path: str) -> str:
        """Attach an image to this conversation so the model can actually look at it."""
        resolved = _resolve_image(path, bridge)
        if resolved is None:
            return f"cannot attach image: {path} (unreadable, unsupported, or outside the sandbox)"
        model = bridge.sessions.get(current_session())
        if model is None:
            return "no active session"
        if not _model_supports_images(bridge):
            return (
                f"image saved at {resolved} but the active model "
                f"({model.model}) does not declare image_in; describe it in text instead"
            )
        await bridge.attach_image_to_context(model.id, str(resolved))
        return f"attached {resolved} to the conversation; ask the model about it now"

    # ------------------------------------------------------------- files
    @tool()
    async def import_file(source: str, dest_name: str = "") -> str:
        """Copy a file from outside the workspace into the inbox so it becomes readable."""
        src = Path(os.path.expanduser(source))
        if not src.is_file():
            return f"no such file: {source}"
        store.ensure_dirs()
        target = store.INBOX / (dest_name or src.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        bridge.files.add_root(target.parent)
        return f"imported to {target} ({target.stat().st_size} bytes)"

    @tool()
    async def export_file(path: str, suggested_name: str = "") -> str:
        """Publish a workspace file for the user to save on the device (staged in the outbox + announced in the UI)."""
        src = _resolve_any(path, bridge)
        if src is None or not src.is_file():
            return f"cannot export: {path}"
        store.ensure_dirs()
        target = store.OUTBOX / (suggested_name or src.name)
        shutil.copy2(src, target)
        await bridge.notify_outbox(str(target), src.stat().st_size)
        return f"ready to save: {target}"

    # ---------------------------------------------------------- decisions
    @tool()
    async def list_pending() -> str:
        """List approvals/questions currently waiting on the user."""
        return json.dumps(bridge.decisions.pending, ensure_ascii=False, indent=1)

    @tool()
    async def submit_answer(decision_id: str, behavior: str = "allow",
                           option_id: str = "", content: dict[str, Any] | None = None) -> str:
        """Answer a pending approval or question (used by programmatic clients; the UI has buttons)."""
        ok = bridge.decisions.resolve(decision_id, {
            "behavior": behavior, "option_id": option_id, "content": content or {},
        })
        return f"resolved {decision_id}" if ok else f"no pending decision {decision_id}"

    # -------------------------------------------------------------- system
    @tool()
    async def runtime_doctor() -> str:
        """Report host/guest runtime facts, paths, versions and the agent's own state."""
        health = await bridge.health()
        return json.dumps({
            "settings": bridge.settings.to_public_dict(),
            "agent_health": health,
            "session": bridge.sessions.get(current_session()).describe()
            if current_session() in bridge.sessions else None,
            "subagents": len(bridge.subagents),
            "workspace_free_mb": shutil.disk_usage(str(bridge.settings.workspace)).free // 1024 // 1024,
        }, ensure_ascii=False, indent=1)

    @tool()
    async def open_session(cwd: str = "", mode: str = "", model: str = "") -> str:
        """Create another Kimi Code session (parallel work) and report its id."""
        state = await bridge.new_session(cwd=cwd or None, mode=mode or None, model=model or None)
        return json.dumps({"session_id": state.id, "cwd": state.cwd, "mode": state.mode,
                           "model": state.model})

    @tool()
    async def ask_in_session(session_id: str, prompt: str) -> str:
        """Send a prompt to another session (by id) and wait for its stop reason."""
        if session_id not in bridge.sessions:
            return f"unknown session: {session_id}"
        handle = await bridge.prompt(session_id, prompt)
        result = await handle
        return json.dumps({"stop_reason": result.get("stop_reason"),
                           "output": "".join(bridge.sessions[session_id].last_text_parts)[-4000:]},
                          ensure_ascii=False)

    @tool()
    async def compact_session(session_id: str = "") -> str:
        """Trigger /compact in a session (context diet without a new turn's content)."""
        target = session_id or current_session()
        state = bridge.sessions.get(target)
        if state is None:
            return f"unknown session: {target}"
        handle = await bridge.prompt(target, "/compact")
        await handle
        return f"compact requested for {target}"

    return server


def _wrap_tool(fn):
    """Surface anticipated failures as ToolError(is_error) with their message."""
    import functools
    import inspect

    if inspect.isasyncgenfunction(fn) or not inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def runner(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except (ValueError, KeyError, TypeError, OSError, asyncio.TimeoutError) as exc:
            # An authoring/user-input problem the model can act on. Crash-like
            # exceptions stay unwrapped so they still log with a traceback.
            raise ToolError(f"{exc}") from exc

    return runner


def _exposed_tools(server: MCPServer):
    """A `tool(**kwargs)` registrar that wraps before handing the function over.

    Registration must still see the original signature: the wrapper forwards
    *args/**kwargs, so it is registered through functools.wraps, which keeps
    __name__/__doc__/__wrapped__ and therefore the published schema and the
    structured-output autodetection identical to the unwrapped function.
    """

    def register(**kwargs):
        def decorator(fn):
            return server.tool(**kwargs)(_wrap_tool(fn))

        return decorator

    return register


# ------------------------------------------------------------------ helpers
_SUBAGENT_PROFILES = {
    "coder": {
        "mode": "default",
        "prompt": (
            "You are the implementation engineer. Inspect the repository, make only the "
            "requested code changes, and run the smallest relevant tests. Report changed "
            "files, behavior, tests, and remaining risks. Your final message is the entire "
            "handoff to the calling agent, so make it complete and self-contained."
        ),
    },
    "explore": {
        "mode": "plan",
        "prompt": (
            "You are a read-only codebase explorer. Never modify files. Answer with concrete "
            "file paths, line numbers and short evidence quotes. Your final message is the "
            "entire handoff to the calling agent."
        ),
    },
    "reviewer": {
        "mode": "plan",
        "prompt": (
            "You are a read-only code reviewer. Never edit, delete, commit or format files. "
            "Report only actionable findings with severity, file, line, evidence and a "
            "concrete fix. Return APPROVED when nothing blocking remains."
        ),
    },
    "planner": {
        "mode": "plan",
        "prompt": (
            "You are the planning agent. Do not execute changes; produce an ordered "
            "implementation plan with file-level steps, risks and verification commands. "
            "Your final message is the entire handoff to the calling agent."
        ),
    },
}

_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}


def _skill_dirs() -> list[str]:
    raw = os.environ.get("COOMI_KIMI_SKILL_DIRS", "")
    out = [p for p in raw.split(os.pathsep) if p.strip()]
    for cand in [
        Path.home() / ".coomi" / "skills",
        bridge_default_skill_dir(),
    ]:
        if cand and str(cand) not in out and Path(cand).is_dir():
            out.append(str(cand))
    return out


def bridge_default_skill_dir() -> Path:
    return Path.home() / ".kimi-code" / "skills"


def _resolve_any(path: str, bridge: "KimiCodeBridge") -> Path | None:
    candidate = Path(os.path.expanduser(path))
    authorized = bridge.files.authorize(candidate)
    return authorized if authorized is not None else None


def _resolve_image(path: str, bridge: "KimiCodeBridge") -> Path | None:
    resolved = _resolve_any(path, bridge)
    if resolved is None or not resolved.is_file():
        return None
    if resolved.suffix.lower() not in _IMAGE_TYPES:
        return None
    if resolved.stat().st_size > 12 * 1024 * 1024:
        return None
    return resolved


def _model_supports_images(bridge: "KimiCodeBridge") -> bool:
    """Capability tags come from the model catalog when the bridge has them.
    Unknown means "do not block the attempt": the provider decides."""
    caps = (bridge.active_model_meta or {}).get("capabilities")
    if not isinstance(caps, (list, tuple, set)):
        return True
    return "image_in" in caps


def _loop_turn_prompt(loop: store.Loop, bridge: "KimiCodeBridge") -> str:
    previous = loop.log[-1]["excerpt"] if loop.log else "(no previous turn)"
    return (
        f"Autonomous loop turn {loop.turns_used + 1}/{loop.max_turns}.\n"
        f"OBJECTIVE: {loop.objective}\n"
        f"PREVIOUS TURN (tail):\n{previous}\n\n"
        "Take exactly one coherent step toward the objective, then stop. "
        "End with a line 'LOOP_STATUS: continue' or 'LOOP_STATUS: done' plus why."
    )


async def _send_image(bridge: "KimiCodeBridge", path: str, caption: str) -> str:
    import base64

    resolved = _resolve_image(path, bridge)
    if resolved is None:
        return f"cannot show image: {path} (must be a readable png/jpg/gif/webp inside the sandbox, <=12MB)"
    mime = _IMAGE_TYPES[resolved.suffix.lower()]
    data = base64.b64encode(resolved.read_bytes()).decode()
    await bridge.push_image(str(resolved), mime, data, caption)
    store.audit("show_image", {"path": str(resolved)}, "shown")
    return f"displayed {resolved.name}" + (f" — {caption}" if caption else "")
