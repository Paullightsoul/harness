"""Выбор песочницы по конфигу и профилю проекта.

local (дефолт) — на хосте. docker — в контейнере из образа профиля. Если запрошен
docker, но образ в профиле не задан, безопасно откатываемся на local.
"""

from __future__ import annotations

from harness.profile import ProjectProfile
from harness.sandbox.base import Sandbox
from harness.sandbox.docker import DockerSandbox
from harness.sandbox.local import LocalSandbox


def build_sandbox(kind: str, profile: ProjectProfile) -> Sandbox:
    if kind == "docker" and profile.sandbox_image:
        return DockerSandbox(profile.sandbox_image, network=profile.sandbox_network)
    return LocalSandbox()
