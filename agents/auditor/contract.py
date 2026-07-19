"""Contract-модуль типа auditor: схема audit.done. Спека: 55, 30.

Лёгкий модуль без мозгов. Обогащённый финал конфигурации B: те же поля plan.ready + audit.warnings.
Контекст заявки (`ParsedRequest`) и итог executor (`Check`, план, вердикт, источники) прокладываются
дальше нетронутыми (правило трубы, spec/55): audit.done доезжает до финала, шлюзу нужен request.text
(spec/40). Новое здесь — только блок audit: замечания «на этих граблях уже стояли».

Схема audit.done приезжает вместе с auditor (в его пакете), общий пакет контрактов не трогается —
дословно spec/30: добавление нового типа не редактирует чужой реестр.
"""

from agentarium import contracts
from pydantic import BaseModel, ConfigDict

from agents.executor.contract import Check
from agents.parser.contract import ParsedRequest


class Audit(BaseModel):
    """Замечания аудита плана по истории инцидентов. Пусто — план граблей не повторяет (spec/55)."""

    model_config = ConfigDict(extra="forbid")

    warnings: list[str]


class AuditDonePayload(BaseModel):
    """Обогащённый финал: план + проверки + вердикт + источники (прокладка) + audit (spec/55)."""

    model_config = ConfigDict(extra="forbid")

    request: ParsedRequest
    plan: list[str]
    checks: list[Check]
    verdict: str
    sources: list[str]
    audit: Audit


contracts.register("audit.done", AuditDonePayload)
