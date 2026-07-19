"""Конверт — единственная форма сообщения между агентами. Контракт: spec/20-envelope.md.

Конверт собирает SDK, а не человек: агент в `handle` получает готовый Envelope
и возвращает Reply — все служебные поля заполняет платформа.
"""

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Тип сообщения: нижний регистр, точечная нотация — request.new, plan.ready, task.failed
_TYPE_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")

ENVELOPE_VERSION = "1"


class Envelope(BaseModel):
    """Единица обмена на шине. Адресата нет: маршрутизация — по type, декларативно (spec/40)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope: Literal["1"] = ENVELOPE_VERSION
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    trace_id: uuid.UUID
    causation_id: uuid.UUID | None = None
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    producer: str = Field(min_length=1)
    type: str
    payload: dict[str, Any]
    meta: dict[str, Any] = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _type_dotted_lowercase(cls, v: str) -> str:
        if not _TYPE_PATTERN.fullmatch(v):
            raise ValueError(
                f"тип конверта '{v}' не по контракту: нижний регистр, точечная нотация "
                f"(например request.new) — spec/20"
            )
        return v

    @field_validator("ts")
    @classmethod
    def _ts_aware_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("ts обязан быть timezone-aware (UTC) — spec/20")
        return v.astimezone(UTC)

    def child(self, *, producer: str, type: str, payload: dict[str, Any]) -> "Envelope":
        """Следующий конверт цепочки: trace_id едет дальше, causation_id указывает на родителя."""
        return Envelope(
            trace_id=self.trace_id,
            causation_id=self.id,
            producer=producer,
            type=type,
            payload=payload,
        )


class Reply(BaseModel):
    """То, что возвращает handle агента. SDK превращает Reply в Envelope через child()."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    payload: dict[str, Any]

    @field_validator("type")
    @classmethod
    def _type_dotted_lowercase(cls, v: str) -> str:
        if not _TYPE_PATTERN.fullmatch(v):
            raise ValueError(f"тип Reply '{v}' не по контракту (spec/20)")
        return v
