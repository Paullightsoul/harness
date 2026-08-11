"""Shared pytest fixtures for Harness V4.

- Puts the repo root on ``sys.path`` so sibling test helpers import cleanly
  without requiring ``PYTHONPATH=/home/1.harness-v4``.
- Unit tests default honesty OFF (FakeLifecycle / no real gates). Production
  v4 default remains ON; honesty tests re-enable explicitly or ``delenv``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _v4_test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    # FakeLifecycle unit paths do not run ProfileGate; keep DONE unblocked
    # unless a test opts into honesty.
    monkeypatch.setenv("HARNESS_EVIDENCE_GATE", "0")
    monkeypatch.setenv("HARNESS_EVIDENCE_SECRET", "test-hmac-secret-harness-v4")
