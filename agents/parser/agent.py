"""Тип parser: текст заявки → интент + сущности, либо честный отказ. Спека: 55, 30.

Мозги — один structured-вызов langchain-gigachat (function calling): смысл решает LLM, кода-
угадывания нет (закон «ноль эвристик», spec/00). Отображение результата LLM в Reply — структурное,
не семантическое: is_request решил LLM, мы лишь выбираем тип конверта.

LangChain импортируется лениво (внутри _chain): импорт модуля для реестра схем и сверки каталога
не тянет мозги — так contract-модуль и манифест доступны образам и тестам без extra `brains`.
"""

import os

from agentarium.agent import Agent
from agentarium.envelope import Envelope, Reply
from pydantic import BaseModel, ConfigDict, Field

from agents.parser import (
    contract,  # noqa: F401 — регистрирует схемы request.* (side effect, spec/30)
)
from agents.parser.contract import Entities, Intent

_SYSTEM = (
    "Ты разбираешь заявки инженеру по администрированию баз данных (DBA). "
    "Определи, является ли текст заявкой на одну из работ: проверка доступов (check_access), "
    "обновление версии PostgreSQL (update_db_version), обновление ОС (update_os), "
    "проверка соответствия/compliance (compliance_check). "
    "Если это заявка — заполни intent и извлеки найденные сущности "
    "(user, database, host, environment, target_version, deadline) — только то, что явно есть. "
    "Если текст не заявка DBA — поставь is_request=false и коротко объясни причину в reason."
)


class ParseResult(BaseModel):
    """Структурированный выход LLM: заявка это или нет, и разбор, если да (function calling)."""

    model_config = ConfigDict(extra="forbid")

    is_request: bool = Field(description="Является ли текст заявкой на работу DBA")
    intent: Intent | None = Field(default=None, description="Тип работы, если это заявка")
    entities: Entities = Field(default_factory=Entities, description="Найденные сущности заявки")
    reason: str | None = Field(default=None, description="Почему не заявка (если is_request=false)")


class ParserAgent(Agent):
    consumes = ["request.new"]
    produces = ["request.parsed", "request.rejected"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._structured = None  # ленивая сборка мозгов на первом handle

    def _chain(self):
        """Собрать structured-LLM один раз. Ленивый импорт: модуль грузится и без extra `brains`."""
        if self._structured is None:
            from langchain_gigachat import GigaChat

            model = self.config["model"]
            common = {
                "credentials": os.environ["GIGACHAT_CREDENTIALS"],
                "scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
                "model": model["name"],
            }
            base_url = model.get("base_url") or os.environ.get("GIGACHAT_BASE_URL")
            if base_url:
                common["base_url"] = base_url
            ca_bundle = os.environ.get("GIGACHAT_CA_BUNDLE_FILE")
            if ca_bundle:
                common["ca_bundle_file"] = ca_bundle
            self._structured = GigaChat(**common).with_structured_output(ParseResult)
        return self._structured

    async def handle(self, envelope: Envelope) -> Reply | None:
        text = envelope.payload["text"]
        result: ParseResult = await self._chain().ainvoke(
            [("system", _SYSTEM), ("human", text)]
        )
        if result.is_request and result.intent is not None:
            return Reply(
                type="request.parsed",
                payload={
                    "text": text,
                    "intent": result.intent,
                    # только найденные сущности (exclude_none) — как в примере конверта spec/20
                    "entities": result.entities.model_dump(exclude_none=True),
                },
            )
        return Reply(
            type="request.rejected",
            payload={"text": text, "reason": result.reason or "не является заявкой DBA"},
        )
