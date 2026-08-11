"""Скаффолд целевого репозитория: определение языка и генерация .harness/project.toml.

Используется командой `harness init`, чтобы за один шаг подготовить проект на любом
стеке (Python/TS/Go/Rust) к работе harness.
"""

from __future__ import annotations

from pathlib import Path

_PYTHON_PYPROJECT = """[project]
name = "my-project"
version = "0.1.0"
description = "Project scaffolded by harness"
requires-python = ">=3.11"
dependencies = []

[project.optional-dependencies]
dev = ["pytest", "ruff", "mypy"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.ruff]
line-length = 100
target-version = "py311"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B", "SIM", "PL"]
ignore = ["PLR2004", "PLR0913"]

[tool.mypy]
python_version = "3.11"
strict = true
ignore_missing_imports = true

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
"""


_TYPESCRIPT_PACKAGE = """{
  "name": "my-project",
  "version": "0.1.0",
  "description": "Project scaffolded by harness",
  "type": "module",
  "scripts": {
    "lint": "eslint .",
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "build": "tsc"
  },
  "devDependencies": {
    "eslint": "^9",
    "typescript": "^5",
    "vitest": "^2"
  }
}
"""


_GO_MOD = """module example.com/my-project

go 1.22
"""


_CARGO_TOML = """[package]
name = "my-project"
version = "0.1.0"
edition = "2021"

[dependencies]
"""

# Маркер-файл -> язык. Порядок важен (проверяем по очереди).
_MARKERS: tuple[tuple[str, str], ...] = (
    ("composer.json", "php"),
    ("backend/app/composer.json", "php"),
    ("artisan", "php"),
    ("backend/app/artisan", "php"),
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
    "php": """language = "php"
canon = "php-laravel-cycle"

# Scoped gates must not require vendor/ or a live DB — those break the FSM into
# infinite worker retries when composer install was never run in the agent cwd.
[[gates]]
id = "php-syntax"
cmd = "find backend/app/app backend/app/routes backend/app/config admin/app/app -type f -name '*.php' 2>/dev/null | head -200 | xargs -r -n1 php -l >/tmp/harness-php-lint.out 2>&1; if grep -E -v 'No syntax errors detected' /tmp/harness-php-lint.out | grep -q '.'; then cat /tmp/harness-php-lint.out; exit 1; fi"

# Honesty-MVP: phpunit is blocking. Missing vendor fails the gate.
# Soft skip (exit 0 without vendor / swallow fail) only with HARNESS_ALLOW_SOFT_GATES=1
# — ProfileGate hardens soft cmds at runtime when the flag is off.
[[gates]]
id = "phpunit"
cmd = "if [ -x backend/app/vendor/bin/phpunit ]; then cd backend/app && ./vendor/bin/phpunit --testdox; else echo 'phpunit required (vendor missing); set HARNESS_ALLOW_SOFT_GATES=1 only for explicit soft opt-in' >&2; exit 1; fi"

[review]
model = "cursor-grok-4.5-high"
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


_SCAFFOLD_FILES: dict[str, dict[str, str]] = {
    "python": {"pyproject.toml": _PYTHON_PYPROJECT},
    "typescript": {"package.json": _TYPESCRIPT_PACKAGE},
    "go": {"go.mod": _GO_MOD},
    "rust": {"Cargo.toml": _CARGO_TOML},
}


def scaffold_project(repo: Path, language: str) -> list[str]:
    """Создать минимальные файлы проекта, если их ещё нет.

    Возвращает список созданных файлов. Не перезаписывает существующие файлы.
    """
    created: list[str] = []
    for filename, content in _SCAFFOLD_FILES.get(language, {}).items():
        path = repo / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
            created.append(str(path))
    return created
