from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.config import Settings
from harness.domain.enums import ResourceClass
from harness.profile import PROFILE_REL_PATH, load_profile
from harness.tasktool.resources import (
    GIB,
    ResourceDisposition,
    ResourcePolicy,
    ResourceSnapshot,
    default_mem_thresholds,
)


def _snapshot(*, memory_gib: float = 64, load: float = 0.5, cpus: int = 4) -> ResourceSnapshot:
    return ResourceSnapshot(
        mem_available_bytes=int(memory_gib * GIB),
        load_1m=load,
        cpu_count=cpus,
    )


def test_default_resource_policy_allows_healthy_idle_host() -> None:
    decision = ResourcePolicy().decide(snapshot=_snapshot())

    assert decision.disposition is ResourceDisposition.ALLOW
    assert ResourcePolicy().max_tasktool_jobs == 12
    assert ResourcePolicy().max_agent_slots == 12
    assert ResourcePolicy().max_heavy_jobs == 5
    assert ResourcePolicy().max_gates == 4
    assert ResourcePolicy().soft_mem_available_bytes == 16 * GIB
    assert ResourcePolicy().hard_mem_available_bytes == 8 * GIB
    assert ResourcePolicy().load_per_cpu_threshold == 1.1


@pytest.mark.parametrize(
    ("memory_gib", "expected"),
    [
        (10.0, ResourceDisposition.THROTTLE),  # ≤ soft 16 GiB, > hard 8 GiB
        (6.0, ResourceDisposition.PAUSE),  # ≤ hard 8 GiB
    ],
)
def test_memory_thresholds(memory_gib: float, expected: ResourceDisposition) -> None:
    decision = ResourcePolicy().decide(snapshot=_snapshot(memory_gib=memory_gib))

    assert decision.disposition is expected
    assert "MemAvailable" in decision.reason


def test_cpu_relative_load_and_class_capacity_throttle() -> None:
    policy = ResourcePolicy()

    # threshold 1.1 × 4 CPUs = 4.4
    load_decision = policy.decide(snapshot=_snapshot(load=5.0, cpus=4))
    heavy_decision = policy.decide(
        snapshot=_snapshot(),
        requested_class=ResourceClass.HEAVY,
        heavy_jobs=5,
    )

    assert load_decision.disposition is ResourceDisposition.THROTTLE
    assert "load1" in load_decision.reason
    assert heavy_decision.disposition is ResourceDisposition.THROTTLE
    assert "heavy jobs" in heavy_decision.reason


def test_settings_resource_envs_preserve_existing_settings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("BASE_BRANCH", "develop")
    monkeypatch.setenv("MAX_TASKTOOL_JOBS", "4")
    monkeypatch.setenv("HARNESS_MAX_AGENT_SLOTS", "3")
    monkeypatch.setenv("SOFT_MEM_AVAILABLE_BYTES", str(5 * GIB))
    monkeypatch.setenv("HARD_MEM_AVAILABLE_BYTES", str(4 * GIB))

    settings = Settings(root=tmp_path)

    assert settings.base_branch == "develop"
    assert settings.max_tasktool_jobs == 4
    assert settings.max_agent_slots == 3
    assert settings.resource_policy.soft_mem_available_bytes == 5 * GIB
    assert settings.resource_policy.hard_mem_available_bytes == 4 * GIB


def test_profile_backwards_compatible_and_parses_resource_overrides(tmp_path: Path) -> None:
    assert load_profile(tmp_path).gate_commands == ["make check"]

    path = tmp_path / PROFILE_REL_PATH
    path.parent.mkdir()
    path.write_text(
        """
[[gates]]
id = "lint"
cmd = "ruff check"
tasks = ["task-a"]

[resources]
max_tasktool_jobs = 4
max_heavy_jobs = 2
load_per_cpu_threshold = 1.5
""",
        encoding="utf-8",
    )

    profile = load_profile(tmp_path)
    policy = profile.resources.apply(ResourcePolicy())

    assert profile.gate_commands == ["ruff check"]
    assert profile.gates[0].task_ids == ("task-a",)
    assert policy.max_tasktool_jobs == 4
    assert policy.max_heavy_jobs == 2
    assert policy.max_agent_slots == 12
    assert policy.load_per_cpu_threshold == 1.5


def _cgroup(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "cgroup"
    root.mkdir()
    for name, content in files.items():
        (root / name.replace("_", ".")).write_text(content + "\n", encoding="utf-8")
    return root


def _meminfo(tmp_path: Path, *, total_kib: int, available_kib: int) -> Path:
    path = tmp_path / "meminfo"
    path.write_text(
        f"MemTotal:       {total_kib} kB\nMemAvailable:   {available_kib} kB\n",
        encoding="utf-8",
    )
    return path


def test_capture_prefers_the_cgroup_limit_over_the_host(tmp_path: Path) -> None:
    """A container capped at 4 GiB must not read the host's free memory.

    Regression: admission saw ~100 GiB "available" inside a small cgroup and
    admitted a full wave of agents into a limit it could not see.
    """
    meminfo = _meminfo(tmp_path, total_kib=131_072_000, available_kib=104_857_600)
    cgroup = _cgroup(
        tmp_path,
        memory_max=str(4 * GIB),
        memory_current=str(3 * GIB),
        cpu_max="400000 100000",
    )

    snapshot = ResourceSnapshot.capture(meminfo, cgroup)

    assert snapshot.mem_available_bytes == 1 * GIB  # 4 GiB limit − 3 GiB used
    assert snapshot.cpu_count == 4  # quota, not the host's core count


def test_capture_falls_back_to_host_when_cgroup_is_unlimited(tmp_path: Path) -> None:
    meminfo = _meminfo(tmp_path, total_kib=131_072_000, available_kib=104_857_600)
    cgroup = _cgroup(tmp_path, memory_max="max", cpu_max="max 100000")

    snapshot = ResourceSnapshot.capture(meminfo, cgroup)

    assert snapshot.mem_available_bytes == 104_857_600 * 1024
    assert snapshot.cpu_count == (os.cpu_count() or 1)


def test_capture_survives_a_missing_cgroup_tree(tmp_path: Path) -> None:
    meminfo = _meminfo(tmp_path, total_kib=16_384_000, available_kib=8_192_000)

    snapshot = ResourceSnapshot.capture(meminfo, tmp_path / "no-such-cgroup")

    assert snapshot.mem_available_bytes == 8_192_000 * 1024


def test_thresholds_scale_to_a_small_host(tmp_path: Path) -> None:
    """16/8 GiB literals sat above total RAM on small machines and paused idle runs."""
    meminfo = _meminfo(tmp_path, total_kib=16_777_216, available_kib=8_388_608)  # 16 GiB

    soft, hard = default_mem_thresholds(meminfo, tmp_path / "none")

    assert soft == 2 * GIB
    assert hard == 1 * GIB
    assert hard < soft


def test_thresholds_keep_tuned_values_on_the_big_host(tmp_path: Path) -> None:
    meminfo = _meminfo(tmp_path, total_kib=131_072_000, available_kib=104_857_600)

    soft, hard = default_mem_thresholds(meminfo, tmp_path / "none")

    # ~15.6 / 7.8 GiB — the numbers this deployment was tuned with.
    assert 15 * GIB < soft < 16 * GIB
    assert 7 * GIB < hard < 8 * GIB


def test_thresholds_follow_the_cgroup_limit(tmp_path: Path) -> None:
    meminfo = _meminfo(tmp_path, total_kib=131_072_000, available_kib=104_857_600)
    cgroup = _cgroup(tmp_path, memory_max=str(8 * GIB), memory_current="0")

    soft, hard = default_mem_thresholds(meminfo, cgroup)

    assert soft == 1 * GIB
    assert hard == 512 * 1024**2
