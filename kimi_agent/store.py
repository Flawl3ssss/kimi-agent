"""Persistent stores backing the Coomi-style tools.

Layout under ``~/.coomi-kimi`` (override with ``COOMI_KIMI_HOME``)::

    memory/<scope>/<name>.md   frontmatter + body, precedence local>project>global
    plans/<session>.json       structured update_plan state
    workflows/<id>.json        saved executable pipelines
    skills/<name>/SKILL.md     generated skills (Kimi's own skill dirs stay read-only)
    loops/<id>.json            autonomous loop state
    audit.jsonl                one line per tool call
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

HOME = Path(os.environ.get("COOMI_KIMI_HOME", str(Path.home() / ".coomi-kimi")))
MEMORY = HOME / "memory"
PLANS = HOME / "plans"
WORKFLOWS = HOME / "workflows"
SKILLS = HOME / "skills"
LOOPS = HOME / "loops"
INBOX = HOME / "inbox"
OUTBOX = HOME / "outbox"
AUDIT = HOME / "audit.jsonl"

SCOPES = ("local", "project", "global")
MEMORY_TYPES = ("user", "feedback", "project", "reference")

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def ensure_dirs() -> None:
    for path in (MEMORY, PLANS, WORKFLOWS, SKILLS, LOOPS, INBOX, OUTBOX):
        path.mkdir(parents=True, exist_ok=True)


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid name {name!r}: use letters/digits/dot/dash/underscore, <=64 chars")
    return name


def _frontmatter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"')
    return meta, parts[2].lstrip("\n")


def _render(meta: dict[str, Any], body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {json.dumps(str(value), ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body.rstrip() + "\n"


def audit(tool: str, args: dict[str, Any], result: str) -> None:
    """Append-only audit trail; never raises out of a tool call."""
    try:
        ensure_dirs()
        record = {
            "ts": time.time(),
            "tool": tool,
            "args": {k: _shrink(v) for k, v in args.items()},
            "result": _shrink(result),
        }
        with AUDIT.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _shrink(value: Any, limit: int = 400) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…(+{len(value) - limit})"
    if isinstance(value, dict):
        return {k: _shrink(v, limit) for k, v in list(value.items())[:12]}
    if isinstance(value, list):
        return [_shrink(v, limit) for v in value[:8]]
    return value


# --------------------------------------------------------------------------
# memory
# --------------------------------------------------------------------------
@dataclass(slots=True)
class MemoryRecord:
    name: str
    description: str
    type: str
    scope: str
    content: str
    path: str = ""
    updated_at: float = 0.0


class MemoryStore:
    """Four scopes with a stable read precedence.

    local  -> per-workspace  (memory/local/<workspace-slug>/)
    project-> repository-scoped (given explicit `project` key, else cwd slug)
    global -> user-wide
    A read/search walks local, project, global in that order and the first
    hit for a given name wins, mirroring how the parent product layers context.
    """

    def __init__(self, workspace: Path | str) -> None:
        self.workspace = Path(workspace).resolve()

    def _slug(self, scope: str, project: str | None = None) -> str:
        if scope == "global":
            return "global"
        if scope == "local":
            return f"local::{self.workspace.name}"
        return f"project::{(project or self.workspace.name)}"

    def _dir(self, scope: str, project: str | None = None) -> Path:
        target = MEMORY / self._slug(scope, project).replace("::", os.sep + "")
        target.mkdir(parents=True, exist_ok=True)
        return target

    def write(
        self,
        name: str,
        description: str,
        type: str,
        content: str,
        scope: str = "project",
        project: str | None = None,
    ) -> MemoryRecord:
        name = _safe_name(name)
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {MEMORY_TYPES}")
        target = self._dir(scope, project) / f"{name}.md"
        record = {
            "name": name,
            "description": description or name,
            "type": type,
            "scope": scope,
            "updated_at": time.time(),
        }
        target.write_text(_render(record, content), encoding="utf-8")
        return MemoryRecord(
            name=name, description=record["description"], type=type, scope=scope,
            content=content, path=str(target), updated_at=record["updated_at"],
        )

    def read(self, name: str, project: str | None = None) -> MemoryRecord | None:
        name = _safe_name(name)
        for scope in SCOPES:
            path = self._dir(scope, project) / f"{name}.md"
            if path.is_file():
                return self._load(path, scope)
        return None

    def delete(self, name: str, project: str | None = None) -> str | None:
        name = _safe_name(name)
        # Delete what read() would actually return. Walking SCOPES and removing
        # the first hit would drop the effective record but leave a shadowed
        # copy behind, making the memory reappear on the next read.
        existing = self.read(name, project)
        if existing is None:
            return None
        Path(existing.path).unlink(missing_ok=True)
        return existing.scope

    def iter_all(self, project: str | None = None) -> list[MemoryRecord]:
        out: list[MemoryRecord] = []
        for scope in SCOPES:
            base = self._dir(scope, project)
            for path in sorted(base.glob("*.md")):
                record = self._load(path, scope)
                if record:
                    out.append(record)
        return out

    def search(self, query: str, limit: int = 10, project: str | None = None) -> list[MemoryRecord]:
        terms = [t.lower() for t in re.split(r"[^\w]+", query or "") if t]
        scored: list[tuple[int, MemoryRecord]] = []
        for record in self.iter_all(project):
            haystack = f"{record.name} {record.description} {record.content}".lower()
            score = sum(3 if term in record.name else 1 for term in terms if term in haystack)
            if score:
                scored.append((score, record))
        scored.sort(key=lambda pair: (-pair[0], -pair[1].updated_at))
        return [record for _, record in scored[: max(1, limit)]]

    def _load(self, path: Path, scope: str) -> MemoryRecord | None:
        try:
            meta, body = _frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return None
        return MemoryRecord(
            name=meta.get("name", path.stem),
            description=meta.get("description", ""),
            type=meta.get("type", "project"),
            scope=scope,
            # _render() terminates the file with a newline, so the stored body
            # always gains one; drop it again to keep write->read stable.
            content=body.rstrip("\n"),
            path=str(path),
            updated_at=float(meta.get("updated_at") or path.stat().st_mtime),
        )


# --------------------------------------------------------------------------
# plans
# --------------------------------------------------------------------------
PLAN_STATUSES = ("pending", "in_progress", "completed")


class PlanStore:
    def path(self, session_id: str) -> Path:
        return PLANS / f"{_safe_name(session_id.replace(':', '_'))}.json"

    def update(self, session_id: str, steps: list[dict[str, Any]], explanation: str = "") -> dict[str, Any]:
        cleaned: list[dict[str, Any]] = []
        for step in steps:
            label = str(step.get("step") or "").strip()
            status = str(step.get("status") or "pending")
            if not label:
                continue
            if status not in PLAN_STATUSES:
                raise ValueError(f"status must be one of {PLAN_STATUSES}")
            cleaned.append({"step": label, "status": status})
        if not cleaned:
            raise ValueError("plan needs at least one step")
        running = [c for c in cleaned if c["status"] == "in_progress"]
        if len(running) > 1:
            raise ValueError("at most one step may be in_progress")
        plan = {
            "session_id": session_id,
            "steps": cleaned,
            "explanation": explanation,
            "updated_at": time.time(),
        }
        # A compact progress summary so clients don't have to recount statuses.
        plan["total"] = len(cleaned)
        plan["completed"] = sum(1 for c in cleaned if c["status"] == "completed")
        plan["in_progress"] = sum(1 for c in cleaned if c["status"] == "in_progress")
        self.path(session_id).write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
        return plan

    def get(self, session_id: str) -> dict[str, Any] | None:
        path = self.path(session_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None


# --------------------------------------------------------------------------
# workflows
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Workflow:
    id: str
    name: str
    steps: list[dict[str, Any]]
    description: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Workflow":
        return cls(
            id=str(data["id"]), name=str(data["name"]), steps=list(data.get("steps") or []),
            description=str(data.get("description") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


class WorkflowStore:
    def path(self, wf_id: str) -> Path:
        return WORKFLOWS / f"{_safe_name(wf_id)}.json"

    def validate(self, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not steps:
            raise ValueError("a workflow needs at least one step")
        cleaned: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in steps:
            step_id = str(raw.get("id") or "").strip()
            if not step_id:
                raise ValueError("every step needs an `id`")
            if step_id in seen:
                raise ValueError(f"duplicate step id: {step_id}")
            seen.add(step_id)
            action = str(raw.get("action") or "").strip()
            if not action:
                raise ValueError(f"step {step_id!r} needs an `action`")
            deps = [str(d) for d in (raw.get("depends_on") or [])]
            cleaned.append({
                "id": step_id,
                "action": action,
                "depends_on": deps,
                "prompt": str(raw.get("prompt") or ""),
                "args": raw.get("args") or {},
            })
        for step in cleaned:
            for dep in step["depends_on"]:
                if dep not in seen:
                    raise ValueError(f"step {step['id']!r} depends on unknown step {dep!r}")
        # cycle check
        visiting: set[str] = set()
        done: set[str] = set()
        by_id = {s["id"]: s for s in cleaned}

        def walk(node: str) -> None:
            if node in done:
                return
            if node in visiting:
                raise ValueError(f"dependency cycle at step {node!r}")
            visiting.add(node)
            for dep in by_id[node]["depends_on"]:
                walk(dep)
            visiting.discard(node)
            done.add(node)

        for step_id in by_id:
            walk(step_id)
        return cleaned

    def save(self, wf: Workflow) -> Workflow:
        wf.steps = self.validate(wf.steps)
        wf.updated_at = time.time()
        self.path(wf.id).write_text(json.dumps(wf.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return wf

    def get(self, wf_id: str) -> Workflow | None:
        path = self.path(wf_id)
        if not path.is_file():
            return None
        try:
            return Workflow.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def list(self) -> list[Workflow]:
        out: list[Workflow] = []
        for path in sorted(WORKFLOWS.glob("*.json")):
            try:
                out.append(Workflow.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return out

    def delete(self, wf_id: str) -> bool:
        path = self.path(wf_id)
        if path.is_file():
            path.unlink()
            return True
        return False

    @staticmethod
    def new_id(name: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "workflow").lower()).strip("-")[:40] or "workflow"
        return f"{slug}-{uuid.uuid4().hex[:6]}"


# --------------------------------------------------------------------------
# skills
# --------------------------------------------------------------------------
class SkillStore:
    """Own skills (writable) + imported Coomi skills (read-only)."""

    def __init__(self, extra_dirs: list[Path] | None = None) -> None:
        self.own = SKILLS
        self.own.mkdir(parents=True, exist_ok=True)
        self.readonly_dirs = [p for p in (extra_dirs or []) if p.is_dir()]

    def discover(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for root, source in [(self.own, "coomi-kimi"), *[(d, "imported") for d in self.readonly_dirs]]:
            for skill_file in sorted(root.glob("**/SKILL.md")):
                meta, body = self._parse(skill_file)
                if not meta.get("name") and not body.strip():
                    continue
                # A SKILL.md without frontmatter is not a loadable skill: it has
                # no name to read_skill by and no description to advertise.
                if not meta.get("name"):
                    continue
                found.append({
                    "name": meta["name"],
                    "description": meta.get("description", ""),
                    "source": source,
                    "path": str(skill_file),
                    "writable": source == "coomi-kimi",
                    "preview": body[:200],
                })
        return found

    def read(self, name: str) -> dict[str, Any] | None:
        for skill in self.discover():
            if skill["name"] == name:
                meta, body = _frontmatter(Path(skill["path"]).read_text(encoding="utf-8"))
                return {**skill, "content": body}
        return None

    def create(self, name: str, description: str, content: str) -> Path:
        name = _safe_name(name)
        target = self.own / name / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _render({"name": name, "description": description, "type": "skill"}, content), encoding="utf-8"
        )
        return target

    @staticmethod
    def _parse(path: Path) -> tuple[dict[str, str], str]:
        try:
            return _frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            return {}, ""


# --------------------------------------------------------------------------
# loops
# --------------------------------------------------------------------------
@dataclass(slots=True)
class Loop:
    id: str
    objective: str
    status: str = "active"  # active|paused|blocked|complete
    token_budget: int | None = None
    max_turns: int = 5
    turns_used: int = 0
    session_id: str | None = None
    log: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class LoopStore:
    def path(self, loop_id: str) -> Path:
        return LOOPS / f"{_safe_name(loop_id)}.json"

    def save(self, loop: Loop) -> Loop:
        loop.updated_at = time.time()
        self.path(loop.id).write_text(
            json.dumps(asdict(loop), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return loop

    def new(self, objective: str, max_turns: int, token_budget: int | None) -> Loop:
        loop = Loop(
            id=f"loop-{uuid.uuid4().hex[:8]}", objective=objective,
            max_turns=max_turns, token_budget=token_budget,
        )
        return self.save(loop)

    def get(self, loop_id: str) -> Loop | None:
        path = self.path(loop_id)
        if not path.is_file():
            return None
        try:
            return Loop(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def list(self) -> list[Loop]:
        out: list[Loop] = []
        for path in sorted(LOOPS.glob("*.json")):
            loop = self.get(path.stem)
            if loop:
                out.append(loop)
        return out


ensure_dirs()
