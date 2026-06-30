from __future__ import annotations

from harness.domain.enums import Verdict
from harness.scheduler.engine import _parse_verdict


def test_parse_approve() -> None:
    assert _parse_verdict("отчёт...\nVERDICT: APPROVE") == Verdict.APPROVE


def test_parse_changes() -> None:
    assert _parse_verdict("VERDICT: CHANGES\nисправь X") == Verdict.CHANGES


def test_defaults_to_changes_when_absent() -> None:
    assert _parse_verdict("ревьюер ничего не написал") == Verdict.CHANGES


def test_takes_last_verdict() -> None:
    assert _parse_verdict("VERDICT: CHANGES\n...\nVERDICT: APPROVE") == Verdict.APPROVE
