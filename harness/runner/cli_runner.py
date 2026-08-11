"""Исполнитель через cursor-agent CLI. Тарифицируется из подписки Cursor.

Эквивалент run_agent() из scripts/lib.sh, но асинхронный — чтобы движок мог
гонять несколько воркеров параллельно.

v2-007: prompt всегда сохраняется в файл `.harness/prompts/<uuid>.md` для аудита
(логи раньше не давали увидеть, что именно послали агенту). cursor-agent CLI не
поддерживает `--prompt-file`/stdin (подтверждено docs.cursor.com + gsd-core#669) —
prompt передаётся как argv. Опция `HARNESS_PROMPT_VIA_SHELL=1` обходит `ps`-leakage
через shell `"$(cat file)"` (по умолчанию off, backwards compat).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import uuid
from collections.abc import Callable
from pathlib import Path

from harness.runner.base import AgentResult

# Эвристика: примерная стоимость вызова по моделям (кредиты).
# CLI не отдаёт реальную стоимость машиночитаемо — используем оценки для бюджетирования.
_MODEL_COST_ESTIMATES: dict[str, float] = {
    # Auto — самый дешёвый вариант (обычно fast/локальные)
    "auto": 0.5,
    # Claude/Opus — дорогие reasoning-модели
    "claude-opus-4-8-thinking-high": 8.0,
    "claude-opus-4-8": 6.0,
    "sonnet-4-thinking": 3.0,
    "sonnet-4.6": 2.5,
    "gpt-5": 2.0,
    # Китайские модели — средний сегмент
    "kimi-k2.5": 1.5,
    "glm-5.2-high": 1.2,
    "glm-5.2": 1.0,
}


def _estimate_cost(model: str) -> float:
    """Оценить стоимость вызова по имени модели.

    Тытаемся найти точное совпадение, затем префиксное.
    Дефолт — 1.0 кредита, если модель неизвестна.
    """
    if model in _MODEL_COST_ESTIMATES:
        return _MODEL_COST_ESTIMATES[model]
    for key in sorted(_MODEL_COST_ESTIMATES.keys(), key=len, reverse=True):
        if model.startswith(key):
            return _MODEL_COST_ESTIMATES[key]
    return 1.0


def _prompt_dir(cwd: Path) -> Path:
    """Каталог для сохранения промптов — `.harness/prompts/` в CWD запуска."""
    d = cwd / ".harness" / "prompts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_prompt(prompt: str, cwd: Path, log_path: Path | None) -> Path | None:
    """Сохранить промпт в файл для аудита. Имя связано с log_path, если задан."""
    if log_path is None:
        # Без log_path — анонимный uuid-файл; удалять нечего, оставляем для дебага.
        target = _prompt_dir(cwd) / f"prompt-{uuid.uuid4().hex[:8]}.md"
    else:
        # log_path вроде logs/worker-008-a1.log → prompts/worker-008-a1.md
        stem = log_path.stem.removesuffix(".log")
        target = log_path.parent.parent / ".harness" / "prompts" / f"{stem}.md"
        target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(prompt, encoding="utf-8")
    return target


def _shell_mode_enabled() -> bool:
    return os.environ.get("HARNESS_PROMPT_VIA_SHELL", "0") == "1"


def _keep_prompts() -> bool:
    """Иначе временные uuid-промпты удаляются после запуска (аудит-файлы остаются)."""
    return os.environ.get("HARNESS_KEEP_PROMPTS", "0") == "1"


def _max_prompt_bytes() -> int:
    """Безопасный потолок под argv/`sh -c` (оба пути упираются в один и тот же
    exec()-лимит ОС). Инцидент: worker закоммитил .venv в diff воркера, de-sloppify
    подставил его в prompt → 28MB argv → `[Errno 7] Argument list too long`, ран упал.
    Берём ARG_MAX с запасом на env/argv0/остальные флаги, не весь лимит целиком.
    """
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError):
        arg_max = 2 * 1024 * 1024
    return max(arg_max // 4, 64 * 1024)


class CliRunner:
    def __init__(self, extra_flags: str = "") -> None:
        self._extra_flags = shlex.split(extra_flags) if extra_flags else []

    async def run(
        self,
        prompt: str,
        *,
        model: str,
        cwd: Path,
        log_path: Path | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> AgentResult:
        # CLI runner не поддерживает streaming/heartbeat — тихий no-op.
        if progress_callback:
            progress_callback("started")
        # v2-007: всегда сохраняем промпт в файл для аудита — логи раньше не давали
        # увидеть, что именно послали агенту.
        prompt_file = _save_prompt(prompt, cwd, log_path)

        prompt_bytes = len(prompt.encode("utf-8"))
        limit = _max_prompt_bytes()
        if prompt_bytes > limit:
            # Инцидент: раздутый diff (например, случайно закоммиченный .venv)
            # приводил к `[Errno 7] Argument list too long` и падению всего ран'а.
            # Явная ошибка вместо OS-level crash — движок должен уметь это
            # обработать (например, пометить задачу failed с понятной причиной).
            if progress_callback:
                progress_callback("done")
            location = f" (сохранён в {prompt_file})" if prompt_file else ""
            return AgentResult(
                ok=False,
                text="",
                error=(
                    f"prompt слишком большой для exec(): {prompt_bytes} байт > "
                    f"лимит {limit} байт{location}. Обычно причина — раздутый "
                    f"git diff (например, случайно закоммиченный .venv/node_modules)."
                ),
            )

        extra = list(self._extra_flags)
        try:
            if _shell_mode_enabled() and prompt_file is not None:
                # Shell + "$(cat file)" — скрывает промпт из `ps`/shell history.
                # Безопасно: path — наш tmp-файл, не пользовательский ввод.
                quoted_file = shlex.quote(str(prompt_file))
                shell_cmd = (
                    f'cursor-agent -p "$(cat {quoted_file})" '
                    f'--model {shlex.quote(model)} '
                    f'--output-format text --force '
                    f'{" ".join(shlex.quote(f) for f in extra)}'
                )
                proc = await asyncio.create_subprocess_shell(
                    shell_cmd,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            else:
                # Default: argv через exec — cursor-agent не поддерживает --prompt-file/stdin.
                cmd = [
                    "cursor-agent", "-p", prompt,
                    "--model", model,
                    "--output-format", "text",
                    "--force",
                    *extra,
                ]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(cwd),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
        except FileNotFoundError:
            return AgentResult(ok=False, text="", error="cursor-agent не найден в PATH")

        out_bytes, _ = await proc.communicate()
        text = out_bytes.decode("utf-8", errors="replace")
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(text, encoding="utf-8")

        # Удаляем uuid-промпт (без log_path), если не включён audit-режим.
        # Именованные (с log_path) — остаются для дебага всегда.
        if (
            prompt_file is not None
            and log_path is None
            and not _keep_prompts()
            and "prompt-" in prompt_file.name
        ):
            with contextlib.suppress(OSError):
                prompt_file.unlink()

        # CLI не отдаёт стоимость машиночитаемо — используем эвристическую оценку.
        estimated_cost = _estimate_cost(model)
        if progress_callback:
            progress_callback("done")
        return AgentResult(
            ok=proc.returncode == 0, text=text, cost_credits=estimated_cost,
            cost_kind="estimated",
        )
