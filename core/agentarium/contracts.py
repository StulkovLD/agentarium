"""Реестр payload-схем: TYPE → Pydantic-модель. Контракт: spec/30 «Схемы payload».

Реестр собирается, а не редактируется: базовые типы объявляет ядро,
схемы своих produces каждый тип агента приносит в собственном contract-модуле.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

_REGISTRY: dict[str, type[BaseModel]] = {}


class ContractError(Exception):
    """Нарушение контракта конверта/payload. Не ретраится: повторение не чинит неправильное."""


class TaskFailedPayload(BaseModel):
    """Базовый тип ядра: ошибка обработки — тоже конверт (spec/20)."""

    model_config = ConfigDict(extra="forbid")

    failed_type: str
    reason: str
    attempts: int
    original_payload: dict[str, Any]


def register(type_name: str, model: type[BaseModel]) -> None:
    existing = _REGISTRY.get(type_name)
    if existing is not None and existing is not model:
        raise ContractError(
            f"схема типа '{type_name}' уже зарегистрирована ({existing.__name__}): "
            f"один владелец на знание — spec/30"
        )
    _REGISTRY[type_name] = model


def validate_payload(type_name: str, payload: dict[str, Any]) -> None:
    """Валидация на границе: несовместимые данные ловятся до handle, не глубиной агента."""
    model = _REGISTRY.get(type_name)
    if model is None:
        raise ContractError(f"тип '{type_name}' не имеет схемы в реестре — spec/30")
    try:
        model.model_validate(payload)
    except Exception as exc:
        raise ContractError(f"payload типа '{type_name}' не проходит схему: {exc}") from exc


register("task.failed", TaskFailedPayload)
