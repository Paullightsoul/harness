"""Песочницы исполнения (ADR-0003, L5/substrate).

Гейты (а в перспективе и воркер) исполняются через абстракцию Sandbox: на хосте
(LocalSandbox) или в изолированном контейнере (DockerSandbox). Контейнер даёт
безопасность (нет доступа к хосту/сети по умолчанию) и горизонтальный масштаб
(N агентов = N контейнеров на нодах). Образ задаётся профилем проекта per-language.
"""

from __future__ import annotations

from harness.sandbox.base import Sandbox
from harness.sandbox.docker import DockerSandbox
from harness.sandbox.factory import build_sandbox
from harness.sandbox.local import LocalSandbox

__all__ = ["Sandbox", "LocalSandbox", "DockerSandbox", "build_sandbox"]
