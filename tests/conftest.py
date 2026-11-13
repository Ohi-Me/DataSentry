"""Test bootstrap.

Puts ``packages/`` and the repo root on the path so tests import the same way
the services do, and points every test at a throwaway control plane.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "packages"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from datasentry_core.clock import FrozenClock  # noqa: E402
from datasentry_core.config import reset_settings  # noqa: E402
from datasentry_core.store import ControlPlane  # noqa: E402


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(datetime(2026, 8, 13, 12, 0, tzinfo=UTC))


@pytest.fixture
def store(tmp_path) -> ControlPlane:
    reset_settings()
    return ControlPlane(tmp_path / "control.db")


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
