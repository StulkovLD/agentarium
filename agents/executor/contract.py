"""Contract-модуль типа executor: схема plan.ready. Спека: 55, 30.

Лёгкий модуль без мозгов. Контекст заявки (`ParsedRequest`) владеет parser — здесь только
прокладывается дальше, доезжая до самого финала: шлюзу и его recovery-UPSERT нужен request.text
(правило трубы на всех типах, включая финальные, spec/40, spec/55).
"""

from typing import Any

from agentarium import contracts
from pydantic import BaseModel, ConfigDict

from agents.parser.contract import ParsedRequest


class Check(BaseModel):
    """Одна выполненная проверка allowlist: имя, типизированные аргументы, результат (spec/55).

    result — JSON-значение (строки таблицы, число, версия): его форму задаёт инструмент, не схема.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    args: dict[str, Any]
    result: Any


class PlanReadyPayload(BaseModel):
    """Итог executor: план работ + реальные проверки против target-db + вердикт (spec/55)."""

    model_config = ConfigDict(extra="forbid")

    request: ParsedRequest
    plan: list[str]
    checks: list[Check]
    verdict: str
    sources: list[str]


contracts.register("plan.ready", PlanReadyPayload)
