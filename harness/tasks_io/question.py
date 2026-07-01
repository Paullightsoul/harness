"""v2-020: парсер question-блока для question-protocol.

Оркестратор (replan) или воркер могут вернуть maшиночитимый блок:
  {"need_input": true, "questions": [
    {"id": "q1", "question": "...", "options": ["a","b"], "why": "..."}
  ]}

Движок парсит, ставит `NEEDS_CLARIFICATION`, пишет `INPUT_REQUESTED` event
с questions, шлёт Telegram inline-кнопки (если options есть). Пользователь
отвечает `harness answer <run> <task> q1="a"` → `CLARIFICATION_ANSWERED` event,
ответ инжектится в feedback, Run продолжается.

Поддерживает fenced-блок ```question ... ``` или сырой JSON. Не падает на
незнакомых полях.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_Q_FENCE = re.compile(r"```question\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class Question:
    """Один вопрос пользователю из question-блока."""

    id: str                 # "q1"
    question: str           # текст вопроса
    options: list[str] = field(default_factory=list)  # варианты ответа (для inline-кнопок)
    why: str = ""           # почему это важно


@dataclass
class QuestionBlock:
    """Результат парсинга question-блока из output агента."""

    need_input: bool
    questions: list[Question] = field(default_factory=list)
    raw: str = ""           # исходный текст
    parsed: bool = False    # True если удалось достать JSON

    @property
    def has_questions(self) -> bool:
        return self.need_input and bool(self.questions)


def parse_question_block(text: str) -> QuestionBlock:
    """Достать question-блок из вывода агента.

    Ищет fenced-блок ```question ... ``` или сырой JSON с `need_input: true`.
    Возвращает QuestionBlock; `parsed=False` если ничего не нашли.
    """
    raw_json = _extract_question_json(text)
    if raw_json is None:
        return QuestionBlock(need_input=False, raw=text, parsed=False)
    try:
        data: Any = json.loads(raw_json)
    except json.JSONDecodeError:
        return QuestionBlock(need_input=False, raw=text, parsed=False)
    if not isinstance(data, dict):
        return QuestionBlock(need_input=False, raw=text, parsed=False)
    need_input = bool(data.get("need_input", False))
    questions = _parse_questions(data.get("questions", []))
    return QuestionBlock(
        need_input=need_input, questions=questions, raw=text, parsed=True,
    )


def _parse_questions(raw: Any) -> list[Question]:
    if not isinstance(raw, list):
        return []
    out: list[Question] = []
    for q in raw:
        if not isinstance(q, dict):
            continue
        qid = str(q.get("id", ""))
        if not qid:
            continue
        out.append(Question(
            id=qid,
            question=str(q.get("question", "")),
            options=_as_str_list(q.get("options")),
            why=str(q.get("why", "")),
        ))
    return out


def _extract_question_json(text: str) -> str | None:
    m = _Q_FENCE.search(text)
    if m:
        return m.group(1).strip()
    # Fallback: сырой JSON с "need_input".
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start:i + 1]
                if "need_input" in candidate:
                    return candidate
                return None
    return None


def _as_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    if isinstance(v, str):
        return [v]
    return [str(v)]


def format_answers_for_feedback(answers: dict[str, str]) -> str:
    """Форматировать ответы пользователя для инжекта в feedback воркера/оркестратора.

    `answers` — {question_id: answer_text}. Возвращает блок
    `=== ANSWERS FROM USER ===\n q1: ...\n q2: ...\n`.
    """
    if not answers:
        return ""
    lines = ["=== ANSWERS FROM USER ==="]
    for qid, ans in answers.items():
        lines.append(f"{qid}: {ans}")
    return "\n".join(lines) + "\n"


def parse_answer_args(args: list[str]) -> dict[str, str]:
    """Парсить CLI-аргументы ответов: ['q1=value1', 'q2=value2'] → dict.

    Поддерживает `q1=value` и `q1="value with spaces"` (упрощённый shlex).
    """
    out: dict[str, str] = {}
    for arg in args:
        if "=" not in arg:
            continue
        key, _, val = arg.partition("=")
        val = val.strip().strip('"').strip("'")
        out[key.strip()] = val
    return out
