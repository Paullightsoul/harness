"""Скаффолд целевого репозитория: определение языка и генерация .harness/project.toml.

Используется командой `harness init`, чтобы за один шаг подготовить проект на любом
стеке (Python/TS/Go/Rust) к работе harness.
"""

from __future__ import annotations

from pathlib import Path

# Маркер-файл -> язык. Порядок важен (проверяем по очереди).
_MARKERS: tuple[tuple[str, str], ...] = (
    ("package.json", "typescript"),
    ("pyproject.toml", "python"),
    ("requirements.txt", "python"),
    ("setup.py", "python"),
    ("go.mod", "go"),
    ("Cargo.toml", "rust"),
)

_PROFILES: dict[str, str] = {
    "python": """language = "python"
canon = "python-backend"

[[gates]]
id = "lint"
cmd = "ruff check ."

[[gates]]
id = "types"
cmd = "mypy ."

[[gates]]
id = "test"
cmd = "pytest -q"

[review]
model = "glm-5.2-high"
""",
    "typescript": """language = "typescript"
canon = "typescript-frontend"

[[gates]]
id = "lint"
cmd = "pnpm lint"

[[gates]]
id = "types"
cmd = "pnpm typecheck"

[[gates]]
id = "test"
cmd = "pnpm test --run"

[[gates]]
id = "build"
cmd = "pnpm build"

[review]
model = "glm-5.2-high"
""",
    "go": """language = "go"
canon = "go"

[[gates]]
id = "vet"
cmd = "go vet ./..."

[[gates]]
id = "build"
cmd = "go build ./..."

[[gates]]
id = "test"
cmd = "go test ./..."

[review]
model = "glm-5.2-high"
""",
    "rust": """language = "rust"
canon = "rust"

[[gates]]
id = "fmt"
cmd = "cargo fmt --check"

[[gates]]
id = "clippy"
cmd = "cargo clippy -- -D warnings"

[[gates]]
id = "test"
cmd = "cargo test"

[review]
model = "glm-5.2-high"
""",
}


def detect_language(repo: Path) -> str:
    for marker, lang in _MARKERS:
        if (repo / marker).exists():
            return lang
    return "python"


def default_project_toml(language: str) -> str:
    return _PROFILES.get(language, _PROFILES["python"])
