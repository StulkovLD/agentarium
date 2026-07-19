"""Тривиальный тип reverse: переворачивает text. Второй тип — доказательство «новый тип без
правки ядра» (spec/70, слайс S3; запасной вариант требования №3 в spec/60).

Contract-модуль приносит схему своего produces сам — реестр собирается, не редактируется (spec/30).
Схему входного echo.done владеет тип echo; здесь она не регистрируется — в образе reverse она
приезжает бандлом contract-модулей всех типов каталога (spec/30).
"""

from agentarium import contracts
from agentarium.agent import Agent
from agentarium.envelope import Envelope, Reply
from pydantic import BaseModel, ConfigDict


class ReversePayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str


contracts.register("reverse.done", ReversePayload)


class ReverseAgent(Agent):
    consumes = ["echo.done"]
    produces = ["reverse.done"]

    async def handle(self, envelope: Envelope) -> Reply | None:
        text = envelope.payload["text"]
        return Reply(type="reverse.done", payload={"text": text[::-1]})
