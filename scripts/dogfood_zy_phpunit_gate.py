#!/usr/bin/env python3
"""Dogfood: ProfileGate honesty path → HMAC-attested ledger on ZY-2nd-flight.

Runs a *scoped* phpunit subset (Unit/ExampleTest by default) through ProfileGate
semantics (soft-swallow refused), writes stdout under the run evidence dir, and
appends an attested ledger row with source=profile_gate.

Usage:
  export HARNESS_HOME=/home/1.harness-v4
  cd /home/1.harness-v4 && . .venv/bin/activate
  python scripts/dogfood_zy_phpunit_gate.py [--repo /home/ZY-2nd-flight] \\
      [--filter tests/Unit/ExampleTest.php]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure v4 package imports when run as a script.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

os.environ.setdefault("HARNESS_HOME", str(_ROOT))
os.environ.setdefault("HARNESS_ALLOW_SOFT_GATES", "0")
os.environ.setdefault("HARNESS_EVIDENCE_GATE", "1")
os.environ.setdefault("HARNESS_USE_RUN_ROOTS", "1")


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("/home/ZY-2nd-flight"))
    parser.add_argument(
        "--filter",
        default="tests/Unit/ExampleTest.php",
        help="phpunit path/filter relative to backend/app",
    )
    parser.add_argument("--task-id", default="dogfood-001")
    args = parser.parse_args()

    from harness.evidence.integrity import verify_evidence_attestation  # noqa: PLC0415
    from harness.evidence.ledger import (  # noqa: PLC0415
        list_evidence,
        record_gate_run,
    )
    from harness.gates.profile_gate import (  # noqa: PLC0415
        ProfileGate,
        harden_soft_cmd,
        is_soft_gate,
    )
    from harness.ingest import new_run_id  # noqa: PLC0415
    from harness.profile import GateSpec, ProjectProfile, load_profile  # noqa: PLC0415
    from harness.tenant.run_layout import ensure_run_layout  # noqa: PLC0415

    repo = args.repo.resolve()
    if not (repo / ".harness" / "project.toml").is_file():
        print(f"FAIL: no .harness/project.toml under {repo}", file=sys.stderr)
        return 2

    phpunit = repo / "backend" / "app" / "vendor" / "bin" / "phpunit"
    if not phpunit.is_file():
        print(f"FAIL: phpunit missing at {phpunit}", file=sys.stderr)
        return 2

    # Soft swallow pattern (must be hardened away).
    softish = (
        "if [ -x backend/app/vendor/bin/phpunit ]; then "
        "cd backend/app && ./vendor/bin/phpunit --testdox; "
        "else echo 'phpunit skipped (no vendor)'; exit 0; fi"
    )
    assert is_soft_gate("phpunit-optional", softish)
    hardened = harden_soft_cmd("phpunit", softish)
    assert "exit 1" in hardened

    # Match ZY project.toml honesty path: s1_phpunit.sh (PHP 8.4 wrapper),
    # scoped to one known file so dogfood stays short.
    scoped_cmd = (
        f"bash scripts/s1_phpunit.sh --testdox --filter ExampleTest"
        if args.filter == "tests/Unit/ExampleTest.php"
        else f"bash scripts/s1_phpunit.sh --testdox {args.filter}"
    )
    profile = load_profile(repo)
    # Keep real project gates for honesty inventory, but run only scoped phpunit
    # for the dogfood sensor (full Unit suite is too long for a smoke).
    dogfood_profile = ProjectProfile(
        language=profile.language,
        canon=profile.canon,
        gates=(GateSpec(id="phpunit", cmd=scoped_cmd),),
        review_model=profile.review_model,
        resources=profile.resources,
    )

    run_id = new_run_id()
    paths = ensure_run_layout(
        repo,
        run_id,
        tenant_id=os.environ.get("USER", "dogfood"),
        goal="dogfood phpunit ProfileGate",
        project="ZY-2nd-flight",
        prefer_modern=True,
    )
    log_dir = paths.evidence_dir / "gates" / args.task_id / "local"
    gate = ProfileGate(dogfood_profile, allow_soft_gates=False)
    result = await gate.check(repo, task_id=args.task_id, full=True, log_dir=log_dir)

    evidence_ids: list[str] = []
    for gate_run in result.gate_runs:
        record = record_gate_run(
            paths.root,
            run_id=run_id,
            task_id=args.task_id,
            gate_id=gate_run.gate_id,
            exit_code=gate_run.exit_code,
            log_path=gate_run.log_path,
            source="profile_gate",
        )
        evidence_ids.append(record.id)

    rows = list_evidence(paths.root)
    attested = []
    for row in rows:
        ok, reason = verify_evidence_attestation(row)
        attested.append({"id": row.get("id"), "ok": ok, "reason": reason, "source": row.get("source")})

    summary = {
        "ok": result.passed,
        "run_id": run_id,
        "run_root": str(paths.root),
        "gate_passed": result.passed,
        "gate_output_tail": "\n".join((result.output or "").splitlines()[-20:]),
        "evidence_ids": evidence_ids,
        "attestation": attested,
        "soft_gates_allowed": False,
        "scoped_cmd": scoped_cmd,
        "ledger": str(paths.root / "evidence" / "ledger.jsonl"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    out_path = paths.root / "dogfood-phpunit-summary.json"
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nsummary_written={out_path}", file=sys.stderr)

    if not result.passed:
        return 1
    if not attested or not all(a["ok"] for a in attested):
        print("FAIL: ledger attestation missing/invalid", file=sys.stderr)
        return 1
    if any(a.get("source") != "profile_gate" for a in attested):
        print("FAIL: unexpected evidence source", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
