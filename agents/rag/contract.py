"""Contract-модуль типа rag: схема knowledge.found. Спека: 55, 30, 45.

Лёгкий модуль без мозгов. Форму разобранной заявки (`ParsedRequest`) владеет parser — здесь она
только импортируется и прокладывается дальше нетронутой (правило трубы, spec/55). rag регистрирует
ровно свой produces — knowledge.found; схему request.parsed приносит contract-модуль parser.
"""

from agentarium import contracts
from pydantic import BaseModel, ConfigDict

from agents.parser.contract import ParsedRequest


class Chunk(BaseModel):
    """Фрагмент регламента из Qdrant: текст + адрес для цитаты (spec/55)."""

    model_config = ConfigDict(extra="forbid")

    text: str
    source: str
    heading: str


class KnowledgeFoundPayload(BaseModel):
    """Найденные регламенты + контекст заявки. Пусто — честное chunks: [] (решает executor)."""

    model_config = ConfigDict(extra="forbid")

    request: ParsedRequest
    chunks: list[Chunk]


contracts.register("knowledge.found", KnowledgeFoundPayload)
