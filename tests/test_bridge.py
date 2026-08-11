"""v2-022b: file-spool bridge protocol tests.

Проверяем atomic write, poll, cleanup, list_pending_requests — без IDE-стороны.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from harness.runner.bridge import (
    PROTO_VERSION,
    BridgeProtocolError,
    BridgeRequest,
    BridgeResponse,
    cleanup,
    cleanup_stale,
    inspect_pending_requests,
    list_orphan_responses,
    list_pending_requests,
    new_request_id,
    parse_response_file,
    read_response,
    response_to_dict,
    wait_response,
    write_request,
    write_response,
)


def test_new_request_id_is_unique_and_short() -> None:
    a = new_request_id()
    b = new_request_id()
    assert a != b
    assert len(a) <= 32


def test_write_request_is_atomic_and_readable(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    req = BridgeRequest(
        id="req-1", prompt="hello", model="auto", cwd="/repo",
        created_at="2026-07-02T12:00:00+00:00",
    )
    path = write_request(spool, req)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == "req-1"
    assert data["prompt"] == "hello"
    assert data["proto"] == PROTO_VERSION
    assert data["subagent_type"] == "generalPurpose"
    assert data["role"] == "worker"  # v2-022c: default role
    # .tmp не осталось после atomic write
    assert not path.with_suffix(".json.tmp").exists()


def test_bridge_request_role_orchestrator_roundtrip(tmp_path: Path) -> None:
    """role="orchestrator" сохраняется на диске и читается обратно через
    list_pending_requests (используется root chat для выбора протокола)."""
    spool = tmp_path / "bridge"
    req = BridgeRequest(
        id="req-orch", prompt="plan", model="auto", cwd="/repo",
        created_at="t", role="orchestrator",
    )
    write_request(spool, req)
    pending = list_pending_requests(spool)
    assert len(pending) == 1
    assert pending[0].role == "orchestrator"


def test_read_response_none_when_missing(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    assert read_response(spool, "missing") is None


def test_write_response_is_atomic_and_canonical(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    path = write_response(spool, BridgeResponse(id="r-1", ok=True, text="done"))
    assert json.loads(path.read_text(encoding="utf-8")) == response_to_dict(
        BridgeResponse(id="r-1", ok=True, text="done")
    )
    assert not path.with_suffix(".json.tmp").exists()


def test_parse_response_file_ignores_unknown_keys(tmp_path: Path) -> None:
    spool = tmp_path / "bridge" / "responses"
    spool.mkdir(parents=True)
    path = spool / "r-1.json"
    path.write_text(json.dumps({
        "id": "r-1", "ok": True, "text": "done",
        "unknown_field": "ignored", "cost_credits": 1.5,
        "tokens_in": 100, "tokens_out": 50, "agent_id": "ag-1",
        "proto": PROTO_VERSION,
    }), encoding="utf-8")
    resp = parse_response_file(path)
    assert resp.ok
    assert resp.text == "done"
    assert resp.cost_credits == 1.5
    assert resp.tokens_in == 100
    assert resp.agent_id == "ag-1"


def test_response_to_dict_roundtrip(tmp_path: Path) -> None:
    resp = BridgeResponse(id="r-1", ok=True, text="hi", agent_id="ag-1")
    d = response_to_dict(resp)
    assert d["id"] == "r-1"
    assert d["ok"] is True
    assert d["proto"] == PROTO_VERSION


def test_read_response_rejects_id_mismatch(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    write_response(spool, BridgeResponse(id="r-1", ok=True))
    response_path = spool / "responses" / "expected.json"
    (spool / "responses" / "r-1.json").rename(response_path)
    with pytest.raises(BridgeProtocolError, match="id mismatch"):
        read_response(spool, "expected")


def test_read_response_rejects_protocol_mismatch(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    response_dir = spool / "responses"
    response_dir.mkdir(parents=True)
    (response_dir / "r-1.json").write_text(
        json.dumps({"id": "r-1", "ok": True, "proto": "999"}),
        encoding="utf-8",
    )
    with pytest.raises(BridgeProtocolError, match="protocol mismatch"):
        read_response(spool, "r-1")


@pytest.mark.asyncio
async def test_wait_response_returns_when_written(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    resp_dir = spool / "responses"
    resp_dir.mkdir(parents=True)

    async def fake_responder() -> None:
        await asyncio.sleep(0.05)
        write_response(spool, BridgeResponse(id="r-1", ok=True, text="ok"))

    task = asyncio.create_task(fake_responder())
    resp = await wait_response(spool, "r-1", timeout=2.0, poll_interval=0.01)
    await task
    assert resp is not None
    assert resp.ok
    assert resp.text == "ok"


@pytest.mark.asyncio
async def test_wait_response_timeout_returns_none(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    resp = await wait_response(spool, "nope", timeout=0.05, poll_interval=0.01)
    assert resp is None


def test_cleanup_removes_both_files(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    req = BridgeRequest(
        id="c-1", prompt="x", model="auto", cwd="/r", created_at="t",
    )
    write_request(spool, req)
    resp_path = spool / "responses" / "c-1.json"
    resp_path.parent.mkdir(parents=True, exist_ok=True)
    resp_path.write_text(
        json.dumps({"id": "c-1", "ok": True, "text": "", "proto": PROTO_VERSION}),
        encoding="utf-8",
    )
    cleanup(spool, "c-1")
    assert not (spool / "requests" / "c-1.json").exists()
    assert not resp_path.exists()


def test_cleanup_best_effort_missing_files(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    # не должно падать на отсутствующих файлах
    cleanup(spool, "never-existed")


def test_list_pending_requests_skips_processed(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    r1 = BridgeRequest(id="a-1", prompt="p1", model="auto", cwd="/r", created_at="t1")
    r2 = BridgeRequest(id="a-2", prompt="p2", model="auto", cwd="/r", created_at="t2")
    write_request(spool, r1)
    write_request(spool, r2)
    # Запишем response для первого — он должен быть пропущен
    resp_dir = spool / "responses"
    resp_dir.mkdir(parents=True, exist_ok=True)
    (resp_dir / "a-1.json").write_text(
        json.dumps({"id": "a-1", "ok": True, "proto": PROTO_VERSION}),
        encoding="utf-8",
    )

    pending = list_pending_requests(spool)
    ids = [p.id for p in pending]
    assert ids == ["a-2"]


def test_list_pending_requests_empty_when_no_dir(tmp_path: Path) -> None:
    spool = tmp_path / "nope"
    assert list_pending_requests(spool) == []


def test_list_pending_requests_skips_invalid_json(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    req_dir = spool / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    (req_dir / "broken.json").write_text("not json", encoding="utf-8")
    (req_dir / "array.json").write_text("[]", encoding="utf-8")
    (req_dir / "bad-timeout.json").write_text(
        json.dumps({"timeout_hint_sec": "not-a-number"}),
        encoding="utf-8",
    )
    assert list_pending_requests(spool) == []
    assert inspect_pending_requests(spool) == []


def test_inspect_pending_requests_reports_age_and_role(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    path = write_request(
        spool,
        BridgeRequest(
            id="old-worker",
            prompt="secret",
            model="auto",
            cwd="/repo",
            created_at="t",
            role="reviewer",
        ),
    )
    path.touch()
    observed_at = path.stat().st_mtime + 30
    records = inspect_pending_requests(spool, now=observed_at)
    assert [(record.id, record.role, record.age_seconds) for record in records] == [
        ("old-worker", "reviewer", 30),
    ]


def test_list_orphan_responses_detects_only_unmatched_files(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    write_response(spool, BridgeResponse(id="orphan", ok=True))
    write_request(
        spool,
        BridgeRequest(id="paired", prompt="x", model="auto", cwd="/r", created_at="t"),
    )
    write_response(spool, BridgeResponse(id="paired", ok=True))
    assert [record.id for record in list_orphan_responses(spool)] == ["orphan"]


def test_cleanup_stale_keeps_fresh_in_flight_records(tmp_path: Path) -> None:
    spool = tmp_path / "bridge"
    stale = write_request(
        spool,
        BridgeRequest(id="stale", prompt="x", model="auto", cwd="/r", created_at="t"),
    )
    fresh = write_request(
        spool,
        BridgeRequest(id="fresh", prompt="x", model="auto", cwd="/r", created_at="t"),
    )
    observed_at = max(stale.stat().st_mtime, fresh.stat().st_mtime) + 100
    stale.touch()
    fresh.touch()
    stale_timestamp = observed_at - 60
    fresh_timestamp = observed_at - 5
    os.utime(stale, (stale_timestamp, stale_timestamp))
    os.utime(fresh, (fresh_timestamp, fresh_timestamp))

    removed = cleanup_stale(spool, max_age_seconds=30, now=observed_at)
    assert removed == [stale]
    assert not stale.exists()
    assert fresh.exists()
