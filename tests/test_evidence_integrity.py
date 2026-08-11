"""Attestation key handling and posture reporting.

``harness/evidence/integrity.py`` is the root of the honesty story and had no
test coverage at all. These pin the two things that are actually true of it: the
key is created safely, and the guarantee it provides is reported honestly.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness.evidence.integrity import (
    ALG,
    attest_evidence_row,
    attestation_posture,
    cli_mint_enabled,
    evidence_secret,
    evidence_source_trusted,
    sign_payload,
    verify_evidence_attestation,
    write_acceptance_seal,
)
from harness.evidence.integrity import (
    verify_acceptance_seal as verify_seal,
)


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HARNESS_EVIDENCE_SECRET", raising=False)
    monkeypatch.delenv("HARNESS_EVIDENCE_CLI_MINT", raising=False)
    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "control-plane"))


def _key_file(tmp_path: Path) -> Path:
    return tmp_path / "control-plane" / ".harness" / "secrets" / "evidence.hmac"


def test_key_is_created_0600_without_a_world_readable_window(tmp_path: Path) -> None:
    """Regression: the key was written with default perms, then chmod'ed after."""
    secret = evidence_secret()

    path = _key_file(tmp_path)
    assert path.is_file()
    assert secret
    assert path.stat().st_mode & 0o777 == 0o600


def test_key_is_stable_across_calls(tmp_path: Path) -> None:
    first = evidence_secret()
    second = evidence_secret()
    assert first == second


def test_existing_key_wins_over_generating_a_new_one(tmp_path: Path) -> None:
    """Regression: concurrent first-use generated two keys; the loser's

    signatures then failed to verify against the file that survived.
    """
    path = _key_file(tmp_path)
    path.parent.mkdir(parents=True)
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    path.write_bytes(b"pre-existing-key\n")

    assert evidence_secret() == b"pre-existing-key"


def test_env_secret_overrides_the_key_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_SECRET", "operator-supplied")
    assert evidence_secret() == b"operator-supplied"
    assert attestation_posture()["key_source"] == "env"


def test_attestation_round_trip_and_tamper_detection() -> None:
    row = {"id": "ev-1", "gate": "pytest", "exit_code": 0, "source": "profile_gate"}
    attested = attest_evidence_row(row)

    ok, reason = verify_evidence_attestation(attested)
    assert (ok, reason) == (True, "ok")
    assert attested["attestation"]["alg"] == ALG

    forged = dict(attested)
    forged["exit_code"] = 1
    ok, reason = verify_evidence_attestation(forged)
    assert ok is False
    assert reason == "attestation_mismatch"


def test_acceptance_seal_detects_edited_ledger(tmp_path: Path) -> None:
    acceptance = tmp_path / "acceptance.json"
    acceptance.write_text(json.dumps({"items": [{"id": "a", "passes": False}]}), "utf-8")
    write_acceptance_seal(acceptance)

    assert verify_seal(acceptance) == (True, "ok")

    acceptance.write_text(json.dumps({"items": [{"id": "a", "passes": True}]}), "utf-8")
    ok, reason = verify_seal(acceptance)
    assert ok is False
    assert reason == "acceptance_seal_content_mismatch"


def test_posture_reports_tamper_evidence_not_authorization() -> None:
    posture = attestation_posture()

    # The honest claim: same-uid agents can read the key, so this is an audit
    # trail with a tamper alarm, not a privilege boundary.
    assert posture["privilege_separation"] is False
    assert "not an authorization boundary" in posture["guarantee"]
    assert posture["warnings"] == []


def test_posture_warns_when_cli_mint_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_EVIDENCE_CLI_MINT", "1")

    assert cli_mint_enabled() is True
    assert evidence_source_trusted({"source": "cli"}) is True
    warnings = attestation_posture()["warnings"]
    assert any("CLI_MINT" in w for w in warnings)


def test_posture_warns_on_loose_key_permissions(tmp_path: Path) -> None:
    evidence_secret()
    path = _key_file(tmp_path)
    path.chmod(0o644)

    posture = attestation_posture()

    assert posture["key_mode"] == "0644"
    assert any("expected 0600" in w for w in posture["warnings"])


def test_cli_source_is_untrusted_by_default() -> None:
    assert evidence_source_trusted({"source": "cli"}) is False
    assert evidence_source_trusted({"source": "profile_gate"}) is True
    assert evidence_source_trusted({"source": "waive:operator"}) is True


def test_signature_depends_on_the_key() -> None:
    payload = {"sha256": "abc"}
    assert sign_payload(payload, secret=b"k1") != sign_payload(payload, secret=b"k2")
