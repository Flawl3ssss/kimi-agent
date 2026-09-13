"""Persistent stores: memory scopes, plans, workflow DAG validation, skills, loops."""

from __future__ import annotations

import json

import pytest

from kimi_agent import store


# ------------------------------------------------------------------- memory
def test_memory_roundtrip_and_lookup():
    mem = store.MemoryStore(workspace=store.HOME)
    rec = mem.write(name="pref-lang", description="user language", type="user",
                    content="Отвечать по-русски.", scope="local")
    assert rec.path.endswith("pref-lang.md")
    loaded = mem.read("pref-lang")
    assert loaded and "по-русски" in loaded.content
    assert loaded.scope == "local"


def test_local_project_global_precedence():
    mem = store.MemoryStore(workspace=store.HOME)
    mem.write("dup", "global copy", "project", "G", scope="global", project=None)
    mem.write("dup", "project copy", "project", "P", scope="project", project="proj-x")
    # project shadows global for its own project only
    assert mem.read("dup", project="proj-x").content == "P"
    assert mem.read("dup", project="other").content == "G"
    assert mem.read("dup").content == "G", "no project => the workspace's own project dir"
    # local wins over everything
    mem.write("dup", "local copy", "project", "L", scope="local")
    assert mem.read("dup", project="proj-x").content == "L"
    assert mem.read("dup", project="other").content == "L"


def test_local_scope_is_per_workspace_not_per_project():
    """`local` is this machine+workspace, so it is visible from any project dir
    opened there; project scope is what separates two projects."""
    mem = store.MemoryStore(workspace=store.HOME)
    mem.write("only-local", "d", "project", "L", scope="local")
    assert mem.read("only-local", project="proj-a").content == "L"
    assert mem.read("only-local", project="proj-b").content == "L"
    assert mem.read("only-local").content == "L"


def test_search_ranks_matches_and_respects_scope():
    mem = store.MemoryStore(workspace=store.HOME)
    mem.write("k1", "token budgets", "project", "лимиты токенов в автокомпакте", scope="project")
    mem.write("k2", "unrelated", "project", "про сборку APK", scope="global")
    hits = mem.search("токенов")
    assert [h.name for h in hits] == ["k1"]
    assert mem.search("nothing-matches-this") == []
    assert any(h.name == "k2" for h in mem.iter_all())


def test_delete_uses_local_project_global_precedence():
    mem = store.MemoryStore(workspace=store.HOME)
    mem.write("gone", "d", "project", "local body", scope="local")
    mem.write("gone", "d", "project", "global body", scope="global")
    assert mem.delete("gone") is not None
    assert mem.read("gone").content == "global body", "only the highest scope is deleted"
    mem.delete("gone")
    assert mem.read("gone") is None


def test_invalid_scope_or_type_is_rejected():
    mem = store.MemoryStore(workspace=store.HOME)
    with pytest.raises(ValueError):
        mem.write("x", "d", "project", "c", scope="stratosphere")
    with pytest.raises(ValueError):
        mem.write("x", "d", "secret", "c", scope="project")


def test_unsafe_names_are_rejected():
    """Names are validated, not silently rewritten: a write must not land outside
    the scope directory, and a rewritten name would confuse a later read()."""
    mem = store.MemoryStore(workspace=store.HOME)
    for bad in ("../evil", "a/b", "with space", "", "-leading", "x" * 65, "emoji-🙂"):
        with pytest.raises(ValueError):
            mem.write(name=bad, description="d", type="project", content="c", scope="global")
    with pytest.raises(ValueError):
        mem.read("../evil")
    from pathlib import Path

    good = mem.write(name="ok.name-1_2", description="d", type="project",
                     content="c", scope="global")
    assert Path(good.path).parent == store.MEMORY / "global"


# -------------------------------------------------------------------- plans
def test_plan_update_tracks_status_and_progress():
    plans = store.PlanStore()
    result = plans.update("s1", [
        {"step": "a", "status": "completed"},
        {"step": "b", "status": "in_progress"},
        {"step": "c", "status": "pending"},
    ], explanation="разведка")
    assert result["completed"] == 1 and result["total"] == 3
    stored = plans.get("s1")
    assert stored["explanation"] == "разведка"
    assert [s["step"] for s in stored["steps"]] == ["a", "b", "c"]


def test_plan_rejects_unknown_status():
    plans = store.PlanStore()
    with pytest.raises(ValueError):
        plans.update("s2", [{"step": "a", "status": "doing"}])


def test_plan_requires_a_step_label():
    plans = store.PlanStore()
    with pytest.raises(ValueError):
        plans.update("s3", [{"status": "pending"}])


def test_missing_plan_reads_as_none():
    assert store.PlanStore().get("nope") is None


# --------------------------------------------------------------- workflows
def test_workflow_accepts_a_valid_dag():
    wf = store.Workflow(id="wf-a", name="Pipeline", steps=[
        {"id": "1", "action": "inspect", "depends_on": []},
        {"id": "2", "action": "implement", "depends_on": ["1"]},
        {"id": "3", "action": "test", "depends_on": ["1", "2"]},
    ])
    saved = store.WorkflowStore().save(wf)
    assert [s["id"] for s in saved.steps] == ["1", "2", "3"]
    assert store.WorkflowStore().get("wf-a").name == "Pipeline"


def test_workflow_rejects_cycles_and_dangling_deps():
    ws = store.WorkflowStore()
    with pytest.raises(ValueError, match="cycle"):
        ws.save(store.Workflow(id="cyc", name="c", steps=[
            {"id": "a", "action": "x", "depends_on": ["b"]},
            {"id": "b", "action": "y", "depends_on": ["a"]},
        ]))
    with pytest.raises(ValueError, match="unknown step"):
        ws.save(store.Workflow(id="dang", name="d", steps=[
            {"id": "a", "action": "x", "depends_on": ["ghost"]}]))
    with pytest.raises(ValueError, match="duplicate"):
        ws.save(store.Workflow(id="dup", name="d", steps=[
            {"id": "a", "action": "x"}, {"id": "a", "action": "y"}]))
    with pytest.raises(ValueError, match="action"):
        ws.save(store.Workflow(id="noact", name="d", steps=[{"id": "a"}]))


def test_self_dependency_is_a_cycle():
    with pytest.raises(ValueError, match="cycle"):
        store.WorkflowStore().save(store.Workflow(
            id="self", name="d", steps=[{"id": "a", "action": "x", "depends_on": ["a"]}]))


def test_workflow_list_and_delete():
    ws = store.WorkflowStore()
    ws.save(store.Workflow(id="wf-l", name="Listed", steps=[{"id": "1", "action": "x"}]))
    assert "wf-l" in [w.id for w in ws.list()]
    assert ws.delete("wf-l") is True
    assert ws.delete("wf-l") is False
    assert ws.get("wf-l") is None


# ------------------------------------------------------------------ skills
def test_skill_discovery_reads_frontmatter(tmp_path, monkeypatch):
    skill = tmp_path / "demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Демонстрационный навык\n---\n\nШаги.\n",
        encoding="utf-8")
    skills = store.SkillStore(extra_dirs=[tmp_path])
    found = {s["name"]: s for s in skills.discover()}
    assert "demo" in found
    assert found["demo"]["description"].startswith("Демонстрац")
    assert "Шаги" in skills.read("demo")["content"]


def test_skill_discovery_skips_directories_without_frontmatter(tmp_path):
    junk = tmp_path / "junk"
    junk.mkdir()
    (junk / "SKILL.md").write_text("no frontmatter here", encoding="utf-8")
    (tmp_path / "not-a-skill.txt").write_text("x", encoding="utf-8")
    assert store.SkillStore(extra_dirs=[tmp_path]).discover() == []


def test_skill_create_then_read(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "SKILLS", tmp_path / "skills")
    path = store.SkillStore().create("mine", "own skill", "body text")
    assert path.exists()
    got = store.SkillStore(extra_dirs=[tmp_path / "skills"]).read("mine")
    assert got and "body text" in got["content"]


# ------------------------------------------------------------------- loops
def test_loop_lifecycle():
    loops = store.LoopStore()
    loop = loops.new(objective="Довести фазу 5", max_turns=4, token_budget=100000)
    assert loop.status == "active"
    loops.save(loop)
    again = loops.get(loop.id)
    assert again.objective.startswith("Довести")
    assert again.token_budget == 100000


# ------------------------------------------------------------------- audit
def test_audit_is_append_only_jsonl():
    store.audit("tool_a", {"secret": "value"}, "ok")
    store.audit("tool_b", {"n": 1}, "error")
    lines = store.AUDIT.read_text(encoding="utf-8").strip().splitlines()
    tail = [json.loads(line) for line in lines][-2:]
    assert tail[0]["tool"] == "tool_a" and tail[1]["result"] == "error"


def test_audit_args_are_shrunk():
    store.audit("big", {"blob": "x" * 5000}, "ok")
    last = json.loads(store.AUDIT.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert len(json.dumps(last)) < 2000
