---
name: code-quality
description: >
  Пасс на чистку кода: дублирование, мёртвый код, вложенные условия,
  нарушения стиля. Для ревьюера — как чеклист, для воркера — на шаге REFACTOR.
metadata:
  origin: ECC/sentry-code-simplifier (adapted)
  harness_role: reviewer, worker
---

# Code Quality — чистка и упрощение

## Когда активировать

- **Воркер**: на шаге REFACTOR в TDD-цикле (после GREEN).
- **Ревьюер**: при оценке качества кода (поверх функциональной проверки).

## Чеклист ревьюера

### 1. Дублирование
- [ ] Нет copy-paste кода (> 5 строк повторяются → extract).
- [ ] Нет дублирования логики в тестах (используй fixtures/helpers).
- [ ] Нет повторных определений одного и того же DTO/типа.

### 2. Мёртвый код
- [ ] Нет закомментированных блоков кода.
- [ ] Нет неиспользуемых импортов (ruff/eslint ловят).
- [ ] Нет функций, которые нигде не вызываются.
- [ ] Нет TODO без тикета/ссылки.

### 3. Сложность
- [ ] Нет вложенных условий > 3 уровней (выделить в early return / guard clause).
- [ ] Функции < 50 строк (иначе разбить).
- [ ] Нет God-объектов (класс с > 10 методами → разделить).

### 4. Именование
- [ ] Имена переменных отражают содержимое (не `x`, `tmp`, `data`).
- [ ] Булевы переменные: `is_*`, `has_*`, `can_*`.
- [ ] Функции: глагол + существительное (`create_user`, `validate_input`).

### 5. Обработка ошибок
- [ ] Нет bare `except:` — всегда конкретный тип исключения.
- [ ] Нет `except Exception: pass` — логирование или re-raise.
- [ ] Ошибки не раскрывают внутренности (stack trace, SQL).

## Рефакторинг-паттерны для воркера

### Early Return (вместо вложенных if)
```python
# БЫЛО:
def process(data):
    if data:
        if data.valid:
            if data.ready:
                return do_work(data)

# СТАЛО:
def process(data):
    if not data:
        return None
    if not data.valid:
        raise ValidationError("invalid")
    if not data.ready:
        raise StateError("not ready")
    return do_work(data)
```

### Extract Method (вместо длинных функций)
```python
# БЫЛО: 80-строчная функция
async def handle_order(order):
    # 20 строк валидации
    # 30 строк бизнес-логики
    # 30 строк нотификаций

# СТАЛО:
async def handle_order(order):
    validated = await _validate_order(order)
    result = await _process_order(validated)
    await _notify_stakeholders(result)
    return result
```

## Связь с harness

- Гейты проекта (lint, types) уже ловят часть проблем автоматически.
- Ревьюер использует этот чеклист для VERDICT — качество кода влияет
  на решение APPROVE/CHANGES.
- Воркер применяет рефакторинг-паттерны на шаге REFACTOR TDD-цикла.
