from __future__ import annotations

from harness.domain.enums import Verdict
from harness.scheduler.engine import _parse_verdict


def test_parse_approve() -> None:
    assert _parse_verdict("отчёт...\nVERDICT: APPROVE") == Verdict.APPROVE


def test_parse_changes() -> None:
    assert _parse_verdict("VERDICT: CHANGES\nисправь X") == Verdict.CHANGES


def test_returns_none_when_absent() -> None:
    """v2-006: malformed-вывод без VERDICT → None, движок эскалирует (не CHANGES)."""
    assert _parse_verdict("ревьюер ничего не написал") is None


def test_takes_last_verdict() -> None:
    assert _parse_verdict("VERDICT: CHANGES\n...\nVERDICT: APPROVE") == Verdict.APPROVE


def test_markdown_bold_wrapped_verdict() -> None:
    assert _parse_verdict("сводка...\n**VERDICT: APPROVE**") == Verdict.APPROVE


def test_heading_prefixed_verdict() -> None:
    assert _parse_verdict("## VERDICT: CHANGES\nправь Y") == Verdict.CHANGES


def test_typo_aprove_returns_none() -> None:
    """Опечатка `APROVE` (без P) — не должна интерпретироваться как APPROVE/CHANGES."""
    assert _parse_verdict("## VERDICT: APROVE") is None


def test_lowercase_verdict() -> None:
    """Регистронезависимый поиск — `verdict: approve` тоже валиден."""
    assert _parse_verdict("verdict: approve") == Verdict.APPROVE


def test_empty_text_returns_none() -> None:
    assert _parse_verdict("") is None
