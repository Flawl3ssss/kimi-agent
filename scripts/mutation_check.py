"""Mutation control for the permission gate.

A green test suite proves nothing until the regressions it claims to prevent are
reintroduced and shown to fail. Each entry restores client.py in a `finally`
block, so an interrupted run cannot leave the tree mutated (which a hand-run sed
absolutely did once).

Usage: .venv/bin/python scripts/mutation_check.py
Exit 0 only when every mutation is caught.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "kimi_agent" / "client.py"
TESTS = "tests/test_policy.py"
TIMEOUT = 120

# name -> (old snippet, new snippet)
MUTATIONS: dict[str, tuple[str, str]] = {
    # The integration path, not just the pure function: if the call site stops
    # forwarding `content`, `auto_decision` sees an empty command and the whole
    # fix degrades to deciding on the bare title "Bash" — while every test that
    # calls `auto_decision` directly stays green.
    "call site stops forwarding content to the policy": (
        "            session_id,\n            payload.get(\"content\", []),\n        )",
        "            session_id,\n        )",
    ),
    "kind=None treated as safe (the original hole)": (
        '        tool_kind = tool_kind or infer_tool_kind(tool_name)\n'
        '        leaf = (tool_name or "").rsplit("__", 1)[-1]',
        '        leaf = (tool_name or "").rsplit("__", 1)[-1]',
    ),
    "decides on the title instead of the real command": (
        '        text = "\\n".join([title or "", *content_texts(content)])',
        '        text = title or ""',
    ),
    "shell decision ignores extracted command": (
        "            return self._shell_decision(self._command_text("
        "tool_name, title, content))",
        "            return self._shell_decision(text)",
    ),
    "grants checked before the danger veto": (
        "        if not blocked and not dangerous:",
        "        if not blocked:",
    ),
    "Bash grant keyed on the title again": (
        '        kind = tool_kind or infer_tool_kind(tool_name or _tool_name_from_title(title))\n'
        '        if kind == "execute":',
        '        kind = tool_kind or infer_tool_kind(tool_name or _tool_name_from_title(title))\n'
        '        if False:',
    ),
    "execute grant widened to the whole tool": (
        '            if kind != "execute" and tool_name:',
        '            if tool_name:',
    ),
    "option kind defaulted to allow_once (escalation)": (
        '        "kind": getattr(opt, "kind", "") or "",',
        '        "kind": getattr(opt, "kind", "allow_once"),',
    ),
    "read-only MCP tools no longer free": (
        "            if leaf in SAFE_MCP_TOOLS:",
        "            if False:",
    ),
    "submit_answer auto-approved again": (
        '        blocked = leaf in SELF_ANSWERING_MCP_TOOLS and self.policy not in ("auto-all", "yolo")',
        "        blocked = False",
    ),
}


def run_tests() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, capture_output=True, text=True, timeout=TIMEOUT,
    )
    return proc.returncode, proc.stdout[-800:]


def main() -> int:
    if not TARGET.exists():
        print(f"missing {TARGET}")
        return 2
    backup = Path(tempfile.mkdtemp(prefix="mutation-")) / "client.py"
    shutil.copy2(TARGET, backup)
    original = TARGET.read_text()
    uncaught: list[str] = []
    broken: list[str] = []

    try:
        for name, (old, new) in MUTATIONS.items():
            if old not in original:
                broken.append(name)
                print(f"SKIP (snippet not found)  {name}")
                continue
            TARGET.write_text(original.replace(old, new, 1))
            print(f"\n=== mutation: {name}")
            try:
                code, out = run_tests()
            except subprocess.TimeoutExpired:
                print("  TIMEOUT (uncaught, and the suite hangs)")
                uncaught.append(name + " [hang]")
                continue
            if code == 0:
                print("  NOT CAUGHT — the suite stayed green")
                uncaught.append(name)
            else:
                failed = [ln.split("::")[-1].split(" ")[0]
                          for ln in out.splitlines() if ln.startswith("FAILED")]
                print(f"  caught by {len(failed)} test(s): {failed[:3]}")
    finally:
        TARGET.write_text(original)
        restored = TARGET.read_text() == original
        print(f"\nclient.py restored: {restored}")

    print("=" * 60)
    if broken:
        print("stale mutation snippets (update this script):", *broken, sep="\n  - ")
    if uncaught:
        print("UNCAUGHT regressions:", *uncaught, sep="\n  - ")
        return 1
    print(f"VERDICT: all {len(MUTATIONS) - len(broken)} regressions are caught")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
