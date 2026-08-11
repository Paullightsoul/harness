"""Гейт поверх профиля проекта: гоняет команды из `.harness/project.toml`.

Каждый гейт — команда оболочки, exit code которой и есть вердикт. Проверки идут
последовательно и fail-fast: первый красный гейт останавливает прогон, его хвост
вывода уходит воркеру/ревьюеру как фидбэк.

Phase 1.5 Honesty: soft/optional gates (phpunit-optional, ``|| exit 0`` swallow)
are rewritten to blocking semantics unless ``allow_soft_gates=True`` /
``HARNESS_ALLOW_SOFT_GATES=1``. Per-gate stdout is written under evidence/gates/.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from harness.gates.base import GateResult, GateRunResult
from harness.profile import ProjectProfile, load_profile
from harness.sandbox.base import Sandbox
from harness.sandbox.local import LocalSandbox

_TAIL_LINES = 60
_SOFT_ID_RE = re.compile(r"optional|soft|skip|non.?blocking", re.I)


def soft_gates_allowed(explicit: bool | None = None) -> bool:
    if explicit is not None:
        return explicit
    return os.environ.get("HARNESS_ALLOW_SOFT_GATES", "0") == "1"


def is_soft_gate(gate_id: str, cmd: str) -> bool:
    if _SOFT_ID_RE.search(gate_id):
        return True
    cmd_l = cmd.lower()
    markers = (
        "phpunit skipped",
        "skipped (no vendor)",
        "non-blocking",
        "|| true",
        "exit 0",
    )
    if any(m in cmd_l for m in markers):
        # ``exit 0`` alone is noisy; require soft context.
        if "exit 0" in cmd_l and not (
            "optional" in gate_id.lower()
            or "skip" in cmd_l
            or "non-blocking" in cmd_l
            or "|| {" in cmd
            or "||{" in cmd
        ):
            return False
        return True
    if re.search(r"\|\|\s*\{[^}]*exit\s+0", cmd, re.I | re.S):
        return True
    return False


def harden_soft_cmd(gate_id: str, cmd: str) -> str:
    """Rewrite soft phpunit/skip patterns into blocking sensors.

    When honesty mode refuses soft gates, missing vendor or phpunit failure
    must fail the gate (exit 1), not swallow with exit 0.
    """
    gid = gate_id.lower()
    if "phpunit" in gid or "phpunit" in cmd.lower():
        # Prefer the well-known ZY / scaffold layout; fall back to generic fail.
        return (
            "if [ -x backend/app/vendor/bin/phpunit ]; then "
            "cd backend/app && ./vendor/bin/phpunit --testdox; "
            "else echo 'phpunit required (HARNESS_ALLOW_SOFT_GATES=0); "
            "vendor/bin/phpunit missing' >&2; exit 1; fi"
        )
    if "optional" in gid or "skip" in cmd.lower():
        return (
            f"echo 'soft gate {gate_id!r} refused "
            f"(set HARNESS_ALLOW_SOFT_GATES=1 to opt in)' >&2; exit 1"
        )
    # Strip ``|| { …; exit 0; }`` swallow patterns by wrapping in a hard fail hint.
    if re.search(r"\|\|\s*\{[^}]*exit\s+0", cmd, re.I | re.S):
        return (
            f"echo 'soft failure swallow refused for gate {gate_id!r} "
            f"(HARNESS_ALLOW_SOFT_GATES=0)' >&2; exit 1"
        )
    return cmd


class ProfileGate:
    def __init__(
        self,
        profile: ProjectProfile,
        sandbox: Sandbox | None = None,
        *,
        allow_soft_gates: bool | None = None,
    ) -> None:
        self._profile = profile
        self._sandbox: Sandbox = sandbox or LocalSandbox()
        self._allow_soft = soft_gates_allowed(allow_soft_gates)

    async def check(
        self,
        cwd: Path,
        *,
        task_id: str | None = None,
        full: bool = False,
        log_dir: Path | None = None,
    ) -> GateResult:
        runs: list[GateRunResult] = []
        for gate in self._profile.gates:
            if not full and task_id is not None and gate.task_ids and task_id not in gate.task_ids:
                continue
            soft = is_soft_gate(gate.id, gate.cmd)
            cmd = gate.cmd
            if soft and not self._allow_soft:
                cmd = harden_soft_cmd(gate.id, gate.cmd)
            code, out = await self._sandbox.exec(cmd, cwd)
            log_path = ""
            if log_dir is not None:
                gate_log_dir = log_dir / gate.id
                gate_log_dir.mkdir(parents=True, exist_ok=True)
                log_file = gate_log_dir / "stdout.txt"
                log_file.write_text(out or "", encoding="utf-8")
                log_path = str(log_file)
            runs.append(
                GateRunResult(
                    gate_id=gate.id,
                    cmd=cmd,
                    exit_code=code,
                    log_path=log_path,
                    soft=soft,
                    skipped=False,
                )
            )
            if code != 0:
                tail = "\n".join(out.splitlines()[-_TAIL_LINES:])
                return GateResult(
                    passed=False,
                    output=f"[gate FAIL: {gate.id}] {cmd}\n{tail}",
                    gate_runs=runs,
                )
        return GateResult(passed=True, output="all gates passed", gate_runs=runs)


def build_gate(
    repo_root: Path,
    sandbox: Sandbox | None = None,
    *,
    allow_soft_gates: bool | None = None,
) -> ProfileGate:
    """Фабрика: грузит профиль из репозитория и возвращает гейт по нему."""
    return ProfileGate(
        load_profile(repo_root),
        sandbox,
        allow_soft_gates=allow_soft_gates,
    )
