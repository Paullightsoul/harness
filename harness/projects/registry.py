"""Реестр проектов на VPS: один harness управляет парком репозиториев.

Хранится в projects.json в корне. Каждый проект — путь к git-репозиторию и
его base-ветка; harness запускает в нём планирование и исполнение.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Project:
    name: str
    repo_path: str
    base_branch: str = "main"


class ProjectRegistry:
    def __init__(self, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, Project]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return {name: Project(**data) for name, data in raw.items()}

    def _save(self, projects: dict[str, Project]) -> None:
        self._path.write_text(
            json.dumps({n: asdict(p) for n, p in projects.items()}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add(self, project: Project) -> None:
        projects = self._load()
        projects[project.name] = project
        self._save(projects)

    def get(self, name: str) -> Project | None:
        return self._load().get(name)

    def list(self) -> list[Project]:
        return sorted(self._load().values(), key=lambda p: p.name)
