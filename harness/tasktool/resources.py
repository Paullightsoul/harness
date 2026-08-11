"""Pure, stdlib-only resource admission policy for TaskTool dispatches."""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from harness.domain.enums import ResourceClass

GIB = 1024**3


class ResourceDisposition(StrEnum):
    ALLOW = "allow"
    THROTTLE = "throttle"
    PAUSE = "pause"


CGROUP_V2_ROOT = Path("/sys/fs/cgroup")


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """Host signals are injectable so policy tests never depend on the test server."""

    mem_available_bytes: int | None
    load_1m: float | None
    cpu_count: int

    @classmethod
    def capture(
        cls,
        meminfo_path: Path = Path("/proc/meminfo"),
        cgroup_root: Path = CGROUP_V2_ROOT,
    ) -> ResourceSnapshot:
        """Read the tightest limit that actually applies to this process.

        ``/proc/meminfo`` and ``os.cpu_count()`` describe the *host*. Under a
        cgroup (the ``docker`` sandbox, or harness itself in a container) the host
        numbers are wrong in the dangerous direction: a container capped at 4 GiB
        on a 125 GiB host reads ~100 GiB "available" and admits a full wave of
        agents into a limit it cannot see. Where a cgroup v2 limit exists, take
        the smaller of the two.
        """
        host_available = _read_mem_available(meminfo_path)
        cgroup_available = _read_cgroup_mem_available(cgroup_root)
        available = _tightest(host_available, cgroup_available)

        host_cpus = os.cpu_count() or 1
        quota = _read_cgroup_cpu_quota(cgroup_root)
        cpus = min(host_cpus, quota) if quota is not None else host_cpus
        return cls(
            mem_available_bytes=available,
            # load1 is host-wide even inside a container — the kernel exposes no
            # per-cgroup load. Comparing it against the cgroup's CPU quota is the
            # conservative reading: a busy host backs us off early.
            load_1m=_read_load_average(),
            cpu_count=max(1, cpus),
        )


@dataclass(frozen=True, slots=True)
class ResourceDecision:
    disposition: ResourceDisposition
    reason: str
    snapshot: ResourceSnapshot


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    """Admission limits tuned for the shared /home host (24 CPU / ~125 GiB).

    Defaults admit 12 in-place Task Tool agents; controller clamps jobs/slots
    at ``_HARD_JOB_CEILING`` (16). Mem soft/hard leave headroom before swap
    thrash; load throttle at 1.1×CPU allows fuller utilization while still
    backing off before thrash. Four gate slots let scoped checks from
    independent worktree tasks overlap on 24 cores.

    **Scope: this gates new claims only.** Task Tool jobs run in the IDE, in a
    process tree harness does not own — there is no handle to suspend or kill
    them. A ``PAUSE`` decision stops *issuing* work; everything already dispatched
    keeps running and keeps consuming the memory that triggered the pause. Plan
    the ceilings so that a full wave is survivable, rather than expecting the
    policy to claw back an overcommitted host.
    """

    max_tasktool_jobs: int = 12
    max_agent_slots: int = 12
    max_heavy_jobs: int = 5
    max_gates: int = 4
    soft_mem_available_bytes: int = 16 * GIB
    hard_mem_available_bytes: int = 8 * GIB
    load_per_cpu_threshold: float = 1.1

    def __post_init__(self) -> None:
        integer_limits = (
            self.max_tasktool_jobs,
            self.max_agent_slots,
            self.max_heavy_jobs,
            self.max_gates,
            self.soft_mem_available_bytes,
            self.hard_mem_available_bytes,
        )
        if any(value < 1 for value in integer_limits):
            raise ValueError("resource limits must be positive")
        if self.hard_mem_available_bytes >= self.soft_mem_available_bytes:
            raise ValueError("hard memory threshold must be below soft threshold")
        if self.load_per_cpu_threshold <= 0:
            raise ValueError("load_per_cpu_threshold must be positive")

    def decide(  # noqa: PLR0911
        self,
        *,
        snapshot: ResourceSnapshot | None = None,
        tasktool_jobs: int = 0,
        agent_slots: int = 0,
        heavy_jobs: int = 0,
        gates: int = 0,
        requested_class: ResourceClass = ResourceClass.STANDARD,
    ) -> ResourceDecision:
        # Ordered guard clauses preserve the safety priority in emitted reasons.
        current = snapshot or ResourceSnapshot.capture()
        counters = (tasktool_jobs, agent_slots, heavy_jobs, gates)
        if any(value < 0 for value in counters):
            raise ValueError("resource counters cannot be negative")

        available = current.mem_available_bytes
        if available is not None and available <= self.hard_mem_available_bytes:
            return ResourceDecision(
                ResourceDisposition.PAUSE,
                f"MemAvailable {available} is at or below hard threshold "
                f"{self.hard_mem_available_bytes}",
                current,
            )
        if available is not None and available <= self.soft_mem_available_bytes:
            return ResourceDecision(
                ResourceDisposition.THROTTLE,
                f"MemAvailable {available} is at or below soft threshold "
                f"{self.soft_mem_available_bytes}",
                current,
            )

        load_limit = current.cpu_count * self.load_per_cpu_threshold
        if current.load_1m is not None and current.load_1m >= load_limit:
            return ResourceDecision(
                ResourceDisposition.THROTTLE,
                f"load1 {current.load_1m:.2f} reached CPU-relative threshold "
                f"{load_limit:.2f}",
                current,
            )
        if tasktool_jobs >= self.max_tasktool_jobs:
            return ResourceDecision(
                ResourceDisposition.THROTTLE,
                f"TaskTool jobs reached limit {self.max_tasktool_jobs}",
                current,
            )
        if agent_slots >= self.max_agent_slots:
            return ResourceDecision(
                ResourceDisposition.THROTTLE,
                f"agent slots reached limit {self.max_agent_slots}",
                current,
            )
        if requested_class is ResourceClass.HEAVY and heavy_jobs >= self.max_heavy_jobs:
            return ResourceDecision(
                ResourceDisposition.THROTTLE,
                f"heavy jobs reached limit {self.max_heavy_jobs}",
                current,
            )
        if requested_class is ResourceClass.GATE and gates >= self.max_gates:
            return ResourceDecision(
                ResourceDisposition.THROTTLE,
                f"gates reached limit {self.max_gates}",
                current,
            )
        return ResourceDecision(
            ResourceDisposition.ALLOW,
            "resource policy allows dispatch",
            current,
        )


def default_mem_thresholds(
    meminfo_path: Path = Path("/proc/meminfo"),
    cgroup_root: Path = CGROUP_V2_ROOT,
) -> tuple[int, int]:
    """(soft, hard) MemAvailable thresholds scaled to this machine's capacity.

    The literal 16/8 GiB defaults were sized for one 125 GiB host. On anything
    smaller they sit above total RAM, so the very first ``next`` call pauses the
    run for "hard memory threshold" on an idle machine. Scaling by capacity keeps
    the tuned numbers on the big host (125 GiB → ~15.6/7.8 GiB) and produces sane
    ones elsewhere. Explicit env values always win — see ``Settings``.
    """
    total = _tightest(
        _read_mem_total(meminfo_path), _read_int(cgroup_root / "memory.max")
    )
    if not total:
        return 16 * GIB, 8 * GIB
    soft = max(512 * 1024**2, int(total * _SOFT_MEM_FRACTION))
    hard = max(256 * 1024**2, int(total * _HARD_MEM_FRACTION))
    if hard >= soft:  # pragma: no cover - only reachable on absurdly small hosts
        hard = soft // 2
    return soft, hard


_SOFT_MEM_FRACTION = 0.125
_HARD_MEM_FRACTION = 0.0625


def _read_meminfo_field(path: Path, field_name: str) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, raw_value = line.partition(":")
        if key != field_name or not separator:
            continue
        parts = raw_value.split()
        if not parts:
            return None
        try:
            kibibytes = int(parts[0])
        except ValueError:
            return None
        return kibibytes * 1024
    return None


def _read_mem_total(path: Path) -> int | None:
    return _read_meminfo_field(path, "MemTotal")


def _read_mem_available(path: Path) -> int | None:
    return _read_meminfo_field(path, "MemAvailable")


def _read_load_average() -> float | None:
    try:
        return float(os.getloadavg()[0])
    except (AttributeError, OSError):
        return None


def _tightest(*values: int | None) -> int | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


def _read_int(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw or raw == "max":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _read_cgroup_mem_available(cgroup_root: Path) -> int | None:
    """Headroom left inside the cgroup v2 memory limit, or None when unlimited."""
    limit = _read_int(cgroup_root / "memory.max")
    if limit is None:
        return None
    current = _read_int(cgroup_root / "memory.current") or 0
    return max(0, limit - current)


def _read_cgroup_cpu_quota(cgroup_root: Path) -> int | None:
    """Effective CPU count from cgroup v2 ``cpu.max`` ("<quota> <period>")."""
    try:
        raw = (cgroup_root / "cpu.max").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    parts = raw.split()
    if not parts or parts[0] == "max":
        return None
    try:
        quota = int(parts[0])
        period = int(parts[1]) if len(parts) > 1 else 100_000
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    # Round up: half a core still lets one job run.
    return max(1, -(-quota // period))
