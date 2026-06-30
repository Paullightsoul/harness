from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from harness.gates.profile_gate import ProfileGate, build_gate
from harness.profile import (
    PROFILE_REL_PATH,
    GateSpec,
    ProfileError,
    ProjectProfile,
    default_profile,
    load_profile,
)


def _write_profile(root: Path, body: str) -> None:
    path = root / PROFILE_REL_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_default_when_no_file(tmp_path: Path) -> None:
    profile = load_profile(tmp_path)
    assert profile == default_profile()
    assert profile.language == "python"
    assert profile.gate_commands == ["make check"]


def test_parse_full_profile(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        """
language = "typescript"
canon = "typescript-frontend"

[[gates]]
id = "lint"
cmd = "pnpm lint"

[[gates]]
id = "types"
cmd = "pnpm typecheck"

[review]
model = "sonnet-4.6"
""",
    )
    profile = load_profile(tmp_path)
    assert profile.language == "typescript"
    assert profile.canon == "typescript-frontend"
    assert profile.gate_commands == ["pnpm lint", "pnpm typecheck"]
    assert profile.review_model == "sonnet-4.6"


def test_gate_without_cmd_raises(tmp_path: Path) -> None:
    _write_profile(tmp_path, '[[gates]]\nid = "lint"\n')
    with pytest.raises(ProfileError):
        load_profile(tmp_path)


def test_broken_toml_raises(tmp_path: Path) -> None:
    _write_profile(tmp_path, "language = ")
    with pytest.raises(ProfileError):
        load_profile(tmp_path)


def test_empty_gates_list_raises(tmp_path: Path) -> None:
    _write_profile(tmp_path, "gates = []\n")
    with pytest.raises(ProfileError):
        load_profile(tmp_path)


def test_gate_passes_when_all_green(tmp_path: Path) -> None:
    profile = ProjectProfile(gates=(GateSpec("a", "true"), GateSpec("b", "true")))
    result = asyncio.run(ProfileGate(profile).check(tmp_path))
    assert result.passed is True


def test_gate_fails_fast_and_names_failing_gate(tmp_path: Path) -> None:
    profile = ProjectProfile(
        gates=(GateSpec("ok", "true"), GateSpec("boom", "false"), GateSpec("never", "true"))
    )
    result = asyncio.run(ProfileGate(profile).check(tmp_path))
    assert result.passed is False
    assert "boom" in result.output


def test_build_gate_uses_profile_from_repo(tmp_path: Path) -> None:
    _write_profile(tmp_path, '[[gates]]\nid = "x"\ncmd = "true"\n')
    gate = build_gate(tmp_path)
    assert asyncio.run(gate.check(tmp_path)).passed is True
