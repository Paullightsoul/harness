from __future__ import annotations

import os
from pathlib import Path

from harness.dotenv import load_dotenv
from harness.scaffold import default_project_toml, detect_language


def test_detect_typescript(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert detect_language(tmp_path) == "typescript"


def test_detect_python(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    assert detect_language(tmp_path) == "python"


def test_detect_go_and_rust(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module x", encoding="utf-8")
    assert detect_language(tmp_path) == "go"


def test_detect_default_python(tmp_path: Path) -> None:
    assert detect_language(tmp_path) == "python"


def test_default_profile_has_gates() -> None:
    for lang in ("python", "typescript", "go", "rust"):
        toml = default_project_toml(lang)
        assert "[[gates]]" in toml and f'language = "{lang}"' in toml


def test_load_dotenv(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text('# comment\nexport FOO="bar"\nBAZ=qux\nEMPTY=\n', encoding="utf-8")
    os.environ.pop("FOO", None)
    os.environ.pop("BAZ", None)
    try:
        n = load_dotenv(env)
        assert n >= 2
        assert os.environ["FOO"] == "bar"
        assert os.environ["BAZ"] == "qux"
    finally:
        os.environ.pop("FOO", None)
        os.environ.pop("BAZ", None)
        os.environ.pop("EMPTY", None)


def test_load_dotenv_does_not_override(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("FOO=fromfile\n", encoding="utf-8")
    os.environ["FOO"] = "preset"
    try:
        load_dotenv(env)
        assert os.environ["FOO"] == "preset"
    finally:
        os.environ.pop("FOO", None)


def test_load_dotenv_missing_file(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path / "nope.env") == 0
