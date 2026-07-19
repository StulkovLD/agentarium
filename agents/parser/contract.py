"""Contract-модуль типа parser: схемы request.new / request.parsed / request.rejected. Спека: 55.

Лёгкий модуль без мозгов (только Pydantic) — так его кладут в каждый образ каталога, включая
шлюз, а потребитель валидирует вход по схеме типа-производителя, не имея его кода (spec/30).

Владелец формы разобранной заявки (`ParsedRequest`, `Entities`, набор интентов) — этот тип:
он её производит, а rag и executor прокладывают её дальше нетронутой (правило трубы, spec/55).
Схему входного entry-типа `request.new` регистрирует тоже parser — его единственный потребитель.
"""

from typing import Literal

from agentarium import contracts
from pydantic import BaseModel, ConfigDict

# Интенты и сущности — дословно из spec/55. Смысл решает LLM (structured output), не код.
INTENTS = ("check_access", "update_db_version", "update_os", "compliance_check")
Intent = Literal["check_access", "update_db_version", "update_os", "compliance_check"]


class Entities(BaseModel):
    """Сущности заявки: что нашлось. Все поля опциональны — извлекается только присутствующее."""

    model_config = ConfigDict(extra="forbid")

    user: str | None = None
    database: str | None = None
    host: str | None = None
    environment: str | None = None
    target_version: str | None = None
    deadline: str | None = None


class RequestNewPayload(BaseModel):
    """Payload entry-типа: заявка как её подал человек (контракт entry — {text}, spec/40)."""

    model_config = ConfigDict(extra="forbid")

    text: str


class ParsedRequest(BaseModel):
    """Разобранная заявка — переиспользуемый блок: едет в knowledge.found и plan.ready.

    Payload типа request.parsed — это ровно она: {text, intent, entities} (spec/55).
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    intent: Intent
    entities: Entities


class RequestRejectedPayload(BaseModel):
    """Не-заявка: честный отказ. Финал по маршруту прямо в шлюз, путь виден в трейсе (spec/55)."""

    model_config = ConfigDict(extra="forbid")

    text: str
    reason: str


contracts.register("request.new", RequestNewPayload)
contracts.register("request.parsed", ParsedRequest)
contracts.register("request.rejected", RequestRejectedPayload)
