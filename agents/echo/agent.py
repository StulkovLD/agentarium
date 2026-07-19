"""Тривиальный тип echo: возвращает payload как есть. Доказательство «агент без LLM» (spec/00).

Contract-модуль типа приносит схемы своих типов сам — реестр собирается, не редактируется.
"""

from typing import Any

from agentarium import contracts
from agentarium.agent import Agent
from agentarium.envelope import Envelope, Reply
from pydantic import BaseModel, ConfigDict


class EchoPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    text: str
    extra: dict[str, Any] | None = None


contracts.register("echo.request", EchoPayload)
contracts.register("echo.done", EchoPayload)


class EchoAgent(Agent):
    consumes = ["echo.request"]
    produces = ["echo.done"]

    async def handle(self, envelope: Envelope) -> Reply | None:
        return Reply(type="echo.done", payload=envelope.payload)
