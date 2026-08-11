#!/usr/bin/env python3
"""Fail when ruff / mypy findings exceed the recorded baseline.

The repo carries pre-existing lint and typing debt (line-length, complexity in
`controller.py`, a few `Any` leaks). Making CI red on arrival would just get it
switched off, and demanding a big cleanup before any CI exists gets neither. So
this ratchets instead: the counts may go **down** freely, never up.

When you legitimately reduce the debt, rerun with ``--update`` and commit the
new baseline — that locks in the improvement.

    python scripts/quality_ratchet.py            # check (CI)
    python scripts/quality_ratchet.py --update   # re-record after a cleanup
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

BASELINE_PATH = Path(__file__).with_name("quality_baseline.json")
ROOT = Path(__file__).resolve().parents[1]


def _count_ruff() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", ".", "--output-format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"ruff failed to run: {proc.stderr.strip()}")
    return len(json.loads(proc.stdout or "[]"))


def _count_mypy() -> int:
    proc = subprocess.run(
        [sys.executable, "-m", "mypy", "harness/"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"mypy failed to run: {proc.stderr.strip()}")
    return sum(1 for line in proc.stdout.splitlines() if ": error:" in line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--update", action="store_true", help="re-record the baseline from current counts"
    )
    args = parser.parse_args()

    current = {"ruff": _count_ruff(), "mypy": _count_mypy()}

    if args.update:
        BASELINE_PATH.write_text(
            json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"baseline updated: {current}")
        return 0

    if not BASELINE_PATH.is_file():
        print(f"no baseline at {BASELINE_PATH}; run with --update", file=sys.stderr)
        return 1
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))

    failed = False
    for tool, count in sorted(current.items()):
        allowed = int(baseline.get(tool, 0))
        if count > allowed:
            print(f"❌ {tool}: {count} findings, baseline allows {allowed}")
            failed = True
        elif count < allowed:
            print(f"✅ {tool}: {count} (baseline {allowed}) — run --update to lock this in")
        else:
            print(f"✅ {tool}: {count} (at baseline)")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
