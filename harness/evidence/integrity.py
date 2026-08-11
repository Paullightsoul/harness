"""Control-plane attestation for evidence ledger + acceptance seal.

**What this actually guarantees.** Every green row in ``ledger.jsonl`` and every
``acceptance.json`` carries an HMAC produced by the control plane. Hand-editing
either file on disk invalidates the signature, so a forged DONE is *detected* at
the gate. That is tamper-evidence, and it holds against the realistic failure
mode: an agent that edits its own run tree to look finished.

**What it does not guarantee.** This is not a privilege boundary. Task Tool
agents run as the same OS user as the control plane, so anything the control
plane can read, an agent can read — including the key file below — and anything
the control plane honours in its environment, an agent can set, including
``HARNESS_EVIDENCE_CLI_MINT``. An agent that deliberately goes looking can
re-sign whatever it likes. Closing that would take real privilege separation
(separate uid, container, or a signing service the agent cannot call), which the
current single-user host does not provide. Until then, treat attestation as an
audit trail with a tamper alarm, not as authorization. :func:`attestation_posture`
reports what is actually protecting a given host so the gap stays visible instead
of being assumed away.

HMAC secret resolution (in order):
1. ``HARNESS_EVIDENCE_SECRET`` env (operators / CI)
2. Persistent key file under ``$HARNESS_HOME/.harness/secrets/evidence.hmac``
3. Fallback key under ``$XDG_STATE_HOME/harness/evidence.hmac``

The key file is created 0600 and is never written into run trees or agent
prompts. Key material still lives on the same filesystem as the agents — see
above.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any

ALG = "HMAC-SHA256"
ATTESTATION_VERSION = 1

# Sources allowed to satisfy DONE when the evidence gate is on.
_TRUSTED_SOURCES = frozenset(
    {
        "advance",
        "profile_gate",
        "gate_runner",
        "control_plane",
    }
)


def _key_path() -> Path:
    home = (os.environ.get("HARNESS_HOME") or "").strip()
    if home:
        return Path(home).expanduser() / ".harness" / "secrets" / "evidence.hmac"
    xdg = (os.environ.get("XDG_STATE_HOME") or "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "harness" / "evidence.hmac"


def evidence_secret() -> bytes:
    """Return HMAC key bytes (env override or on-disk control-plane secret).

    Creation is exclusive and 0600 from the first byte. The previous version
    wrote the key world-readable and only then chmod'ed it, and two processes
    starting together each generated a different key — the loser's signatures
    then failed to verify against the surviving file.
    """
    env = (os.environ.get("HARNESS_EVIDENCE_SECRET") or "").strip()
    if env:
        return env.encode("utf-8")
    path = _key_path()
    existing = _read_key(path)
    if existing:
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = secrets.token_hex(32).encode("utf-8")
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # Another process won the race; its key is the one on disk.
        winner = _read_key(path)
        if winner:
            return winner
        raise
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw + b"\n")
    return raw


def _read_key(path: Path) -> bytes | None:
    try:
        raw = path.read_bytes().strip()
    except OSError:
        return None
    return raw or None


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sign_payload(payload: dict[str, Any], *, secret: bytes | None = None) -> str:
    key = secret if secret is not None else evidence_secret()
    digest = hmac.new(
        key,
        _canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def verify_payload_sig(
    payload: dict[str, Any],
    sig: str,
    *,
    secret: bytes | None = None,
) -> bool:
    expected = sign_payload(payload, secret=secret)
    return hmac.compare_digest(expected, str(sig or ""))


def attest_evidence_row(row: dict[str, Any], *, secret: bytes | None = None) -> dict[str, Any]:
    """Return a copy of ``row`` with a control-plane attestation block."""
    body = {k: v for k, v in row.items() if k != "attestation"}
    sig = sign_payload(body, secret=secret)
    out = dict(body)
    out["attestation"] = {
        "alg": ALG,
        "v": ATTESTATION_VERSION,
        "sig": sig,
    }
    return out


def verify_evidence_attestation(
    row: dict[str, Any],
    *,
    secret: bytes | None = None,
) -> tuple[bool, str]:
    att = row.get("attestation")
    if not isinstance(att, dict):
        return False, "missing_attestation"
    if str(att.get("alg") or "") != ALG:
        return False, "bad_attestation_alg"
    sig = str(att.get("sig") or "")
    if not sig:
        return False, "missing_attestation_sig"
    body = {k: v for k, v in row.items() if k != "attestation"}
    if not verify_payload_sig(body, sig, secret=secret):
        return False, "attestation_mismatch"
    return True, "ok"


def cli_mint_enabled() -> bool:
    """Whether ``harness evidence add`` may mint green gate rows on this host."""
    return os.environ.get("HARNESS_EVIDENCE_CLI_MINT", "0") == "1"


def attestation_posture() -> dict[str, Any]:
    """Describe what is actually protecting attestation on this host.

    Attestation is tamper-evidence, not a privilege boundary (see module
    docstring). The point of this function is to keep that fact on screen: it
    reports where the key lives, whether its permissions are tight, and whether
    the CLI mint escape hatch is open, so an operator can see the residual risk
    instead of inferring a guarantee that is not there.
    """
    env_secret = bool((os.environ.get("HARNESS_EVIDENCE_SECRET") or "").strip())
    path = _key_path()
    mode: str | None = None
    group_or_world_readable: bool | None = None
    if path.is_file():
        try:
            bits = path.stat().st_mode & 0o777
            mode = format(bits, "04o")
            group_or_world_readable = bool(bits & 0o077)
        except OSError:
            mode = None
    warnings: list[str] = []
    if cli_mint_enabled():
        warnings.append(
            "HARNESS_EVIDENCE_CLI_MINT=1 — `harness evidence add` can mint green "
            "gate rows without running a gate"
        )
    if group_or_world_readable:
        warnings.append(f"key file {path} is {mode}; expected 0600")
    return {
        "key_source": "env" if env_secret else ("file" if path.is_file() else "not_created"),
        "key_path": None if env_secret else str(path),
        "key_mode": mode,
        "cli_mint_enabled": cli_mint_enabled(),
        # Single-user host: agents share the uid that owns the key.
        "privilege_separation": False,
        "guarantee": "tamper-evident audit trail; not an authorization boundary",
        "warnings": warnings,
    }


def evidence_source_trusted(row: dict[str, Any]) -> bool:
    """Gate evidence must come from the control-plane runner, not CLI forge."""
    source = str(row.get("source") or "")
    if source in _TRUSTED_SOURCES:
        return True
    if source.startswith("waive:"):
        return True
    if source == "cli" and cli_mint_enabled():
        return True
    return False


def acceptance_seal_path(acceptance_path: Path) -> Path:
    return acceptance_path.with_name("acceptance.seal")


def write_acceptance_seal(
    acceptance_path: Path,
    *,
    secret: bytes | None = None,
) -> Path:
    """Seal ``acceptance.json`` after a control-plane write."""
    raw = acceptance_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    sig = sign_payload({"sha256": digest}, secret=secret)
    seal = {
        "alg": ALG,
        "v": ATTESTATION_VERSION,
        "sha256": digest,
        "sig": sig,
    }
    path = acceptance_seal_path(acceptance_path)
    path.write_text(
        json.dumps(seal, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def verify_acceptance_seal(
    acceptance_path: Path,
    *,
    secret: bytes | None = None,
) -> tuple[bool, str]:
    seal_path = acceptance_seal_path(acceptance_path)
    if not seal_path.is_file():
        return False, "no_acceptance_seal"
    try:
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False, "acceptance_seal_unreadable"
    if not isinstance(seal, dict):
        return False, "acceptance_seal_invalid"
    if str(seal.get("alg") or "") != ALG:
        return False, "acceptance_seal_bad_alg"
    digest = hashlib.sha256(acceptance_path.read_bytes()).hexdigest()
    if digest != str(seal.get("sha256") or ""):
        return False, "acceptance_seal_content_mismatch"
    if not verify_payload_sig({"sha256": digest}, str(seal.get("sig") or ""), secret=secret):
        return False, "acceptance_seal_sig_mismatch"
    return True, "ok"
