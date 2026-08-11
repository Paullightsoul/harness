"""Профиль целевого проекта — делает harness языко-независимым (ось 3, ADR-0003).

Гейты перестают быть зашитым `make check`. Каждый репозиторий описывает свой набор
машинных проверок (computational sensors) в `.harness/project.toml`, например:

    language = "typescript"
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

    [review]
    model = "glm-5.2-high"

Если файла нет — берётся дефолт (`make check`), чтобы не ломать существующие репо.
Формат TOML, а не YAML: парсится stdlib-модулем `tomllib` (Python 3.11+) без
внешних зависимостей (канон workspace: zero new deps).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from harness.tasktool.resources import ResourcePolicy

PROFILE_REL_PATH = ".harness/project.toml"

# Дефолт сохраняет прежнее поведение: единственный гейт `make check`.
_DEFAULT_GATE_ID = "make-check"
_DEFAULT_GATE_CMD = "make check"


@dataclass(frozen=True)
class GateSpec:
    """Один машинный гейт: команда оболочки, чей exit code = вердикт."""

    id: str
    cmd: str
    # Empty means the historical all-tasks behavior. Execution filtering is
    # deliberately deferred until pull dispatch consumes this metadata.
    task_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceOverrides:
    """Optional project-local overrides; None inherits the process Settings."""

    max_tasktool_jobs: int | None = None
    max_agent_slots: int | None = None
    max_heavy_jobs: int | None = None
    max_gates: int | None = None
    soft_mem_available_bytes: int | None = None
    hard_mem_available_bytes: int | None = None
    load_per_cpu_threshold: float | None = None

    def apply(self, base: ResourcePolicy) -> ResourcePolicy:
        return ResourcePolicy(
            max_tasktool_jobs=self.max_tasktool_jobs or base.max_tasktool_jobs,
            max_agent_slots=self.max_agent_slots or base.max_agent_slots,
            max_heavy_jobs=self.max_heavy_jobs or base.max_heavy_jobs,
            max_gates=self.max_gates or base.max_gates,
            soft_mem_available_bytes=(
                self.soft_mem_available_bytes or base.soft_mem_available_bytes
            ),
            hard_mem_available_bytes=(
                self.hard_mem_available_bytes or base.hard_mem_available_bytes
            ),
            load_per_cpu_threshold=(
                self.load_per_cpu_threshold or base.load_per_cpu_threshold
            ),
        )


@dataclass(frozen=True)
class ProjectProfile:
    """Языко-независимое описание проверок и канона целевого репозитория."""

    language: str = "python"
    canon: str = "python-backend"
    gates: tuple[GateSpec, ...] = field(
        default_factory=lambda: (GateSpec(_DEFAULT_GATE_ID, _DEFAULT_GATE_CMD),)
    )
    review_model: str | None = None
    sandbox_image: str | None = None     # образ для Docker-песочницы гейтов (None = локально)
    sandbox_network: str = "none"        # сеть контейнера (none = изоляция)
    resources: ResourceOverrides = field(default_factory=ResourceOverrides)

    @property
    def gate_commands(self) -> list[str]:
        """Список команд гейтов — инжектится воркеру, чтобы он гонял их сам."""
        return [g.cmd for g in self.gates]


class ProfileError(ValueError):
    """Профиль есть, но он некорректен (битый TOML / нет команд у гейта)."""


def default_profile() -> ProjectProfile:
    """Профиль для репозиториев без `.harness/project.toml` (обратная совместимость)."""
    return ProjectProfile()


def load_profile(repo_root: Path) -> ProjectProfile:
    """Читает `.harness/project.toml` из корня репозитория. Нет файла → дефолт."""
    path = repo_root / PROFILE_REL_PATH
    if not path.exists():
        return default_profile()

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ProfileError(f"не удалось прочитать {path}: {exc}") from exc

    return _parse_profile(data, source=path)


def _parse_profile(data: dict[str, object], *, source: Path) -> ProjectProfile:
    gates = _parse_gates(data.get("gates"), source=source)
    review = data.get("review")
    review_model: str | None = None
    if isinstance(review, dict):
        model = review.get("model")
        review_model = model if isinstance(model, str) and model else None

    sandbox = data.get("sandbox")
    sandbox_image: str | None = None
    sandbox_network = "none"
    if isinstance(sandbox, dict):
        image = sandbox.get("image")
        sandbox_image = image if isinstance(image, str) and image else None
        sandbox_network = _as_str(sandbox.get("network"), "none")

    resources = _parse_resources(data.get("resources"), source=source)

    return ProjectProfile(
        language=_as_str(data.get("language"), "python"),
        canon=_as_str(data.get("canon"), "python-backend"),
        gates=gates,
        review_model=review_model,
        sandbox_image=sandbox_image,
        sandbox_network=sandbox_network,
        resources=resources,
    )


def _parse_gates(raw: object, *, source: Path) -> tuple[GateSpec, ...]:
    if raw is None:
        return default_profile().gates
    if not isinstance(raw, list) or not raw:
        raise ProfileError(f"{source}: секция [[gates]] должна быть непустым списком")

    gates: list[GateSpec] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ProfileError(f"{source}: gate #{i} должен быть таблицей")
        cmd = item.get("cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            raise ProfileError(f"{source}: gate #{i} без обязательного поля cmd")
        gate_id = item.get("id")
        raw_task_ids = item.get("task_ids", item.get("tasks"))
        task_ids = _optional_string_tuple(
            raw_task_ids,
            field_name=f"gate #{i} task_ids",
            source=source,
        )
        gates.append(
            GateSpec(id=_as_str(gate_id, f"gate-{i}"), cmd=cmd.strip(), task_ids=task_ids)
        )
    return tuple(gates)


def _parse_resources(raw: object, *, source: Path) -> ResourceOverrides:
    if raw is None:
        return ResourceOverrides()
    if not isinstance(raw, dict):
        raise ProfileError(f"{source}: секция [resources] должна быть таблицей")
    return ResourceOverrides(
        max_tasktool_jobs=_optional_positive_int(raw, "max_tasktool_jobs", source),
        max_agent_slots=_optional_positive_int(raw, "max_agent_slots", source),
        max_heavy_jobs=_optional_positive_int(raw, "max_heavy_jobs", source),
        max_gates=_optional_positive_int(raw, "max_gates", source),
        soft_mem_available_bytes=_optional_positive_int(
            raw, "soft_mem_available_bytes", source
        ),
        hard_mem_available_bytes=_optional_positive_int(
            raw, "hard_mem_available_bytes", source
        ),
        load_per_cpu_threshold=_optional_positive_float(
            raw, "load_per_cpu_threshold", source
        ),
    )


def _optional_positive_int(
    data: dict[object, object],
    name: str,
    source: Path,
) -> int | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ProfileError(f"{source}: resources.{name} должен быть положительным integer")
    return value


def _optional_positive_float(
    data: dict[object, object],
    name: str,
    source: Path,
) -> float | None:
    value = data.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ProfileError(f"{source}: resources.{name} должен быть положительным number")
    return float(value)


def _optional_string_tuple(
    value: object,
    *,
    field_name: str,
    source: Path,
) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ProfileError(f"{source}: {field_name} должен быть списком непустых строк")
    return tuple(item.strip() for item in value if isinstance(item, str))


def _as_str(value: object, default: str) -> str:
    return value if isinstance(value, str) and value else default
