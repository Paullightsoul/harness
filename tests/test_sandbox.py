from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from harness.profile import ProjectProfile
from harness.sandbox.docker import DockerSandbox
from harness.sandbox.factory import build_sandbox
from harness.sandbox.local import LocalSandbox


def _docker_ok() -> bool:
    try:
        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def test_local_sandbox_success(tmp_path: Path) -> None:
    code, _ = asyncio.run(LocalSandbox().exec("true", tmp_path))
    assert code == 0


def test_local_sandbox_failure(tmp_path: Path) -> None:
    code, _ = asyncio.run(LocalSandbox().exec("false", tmp_path))
    assert code != 0


def test_factory_local_by_default() -> None:
    sb = build_sandbox("local", ProjectProfile())
    assert isinstance(sb, LocalSandbox)


def test_factory_docker_without_image_falls_back_local() -> None:
    sb = build_sandbox("docker", ProjectProfile())  # образ не задан
    assert isinstance(sb, LocalSandbox)


def test_factory_docker_with_image() -> None:
    sb = build_sandbox("docker", ProjectProfile(sandbox_image="python:3.12"))
    assert isinstance(sb, DockerSandbox)


@pytest.mark.skipif(not _docker_ok(), reason="docker недоступен")
def test_docker_sandbox_runs_in_container(tmp_path: Path) -> None:
    # postgres:16 уже подтянут для durable-плейна и содержит sh.
    sb = DockerSandbox("postgres:16", network="none")
    code, out = asyncio.run(sb.exec("echo sandbox-ok", tmp_path))
    assert code == 0
    assert "sandbox-ok" in out


@pytest.mark.skipif(not _docker_ok(), reason="docker недоступен")
def test_docker_sandbox_nonzero_exit(tmp_path: Path) -> None:
    sb = DockerSandbox("postgres:16", network="none")
    code, _ = asyncio.run(sb.exec("exit 3", tmp_path))
    assert code == 3
