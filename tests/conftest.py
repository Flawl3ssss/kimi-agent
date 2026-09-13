"""Pytest fixtures.

The store module resolves its data directory at import time from
COOMI_KIMI_HOME, so the env var has to point at a throwaway folder before
kimi_agent.store is imported anywhere.
"""

from __future__ import annotations

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="coomi-kimi-tests-")
os.environ["COOMI_KIMI_HOME"] = _TMP
os.environ.setdefault("COOMI_KIMI_PERMISSION", "auto-safe")

import pytest  # noqa: E402


@pytest.fixture(scope="session")
def data_home() -> str:
    return _TMP


@pytest.fixture
def settings(tmp_path):
    from pathlib import Path

    from kimi_agent.config import Settings

    return Settings(workspace=Path(tmp_path).resolve())
