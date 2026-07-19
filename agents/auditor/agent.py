"""Тип auditor: план работ → сверка с историей инцидентов. Спека: 55, 45, 30 (конфигурация B).

Мозги — эмбеддер из чертежа (PoC: bge-m3/Ollama) + GigaChat: план (executor.plan) → вектор
(auditor.query, детерминизм) → поиск в коллекции incidents экземпляра (top-5, порог) → LLM-сверка
«на этих граблях уже стояли» → audit.done с замечаниями. Контекст заявки и итог executor
прокладываются дальше нетронутыми (труба, spec/55): auditor лишь добавляет блок audit — обогащённый
финал конфигурации B.

Клиент Qdrant, сверка паспорта и сборка эмбеддера — в __init__ (fail-fast на старте, sync); chat-LLM
— лениво на первом handle (импорт модуля для каталога/тестов не тянет extra `brains`). Устройство —
как у rag (эмбеддер + Qdrant), плюс chat-LLM для формулировки замечаний.
"""

import asyncio
import os

from agentarium import storage
from agentarium.agent import Agent
from agentarium.envelope import Envelope, Reply
from agentarium.topology import load_catalog, load_topology
from pydantic import BaseModel, ConfigDict, Field
from qdrant_client import QdrantClient

from agents.auditor import contract  # noqa: F401 — регистрирует схему audit.done (spec/30)
from agents.auditor.query import build_query
from agents.embedders import make_embedder
from agents.executor.contract import PlanReadyPayload

TOP_K = 5  # spec/55
# Порог схожести: 0.0 = «отдай ближайшие 5», выше — режь шум под честное warnings: []. Тюнится на
# живой коллекции incidents (нужен ключ); floor оставлен явным, а не выдуман наугад (как у rag).
SCORE_THRESHOLD = 0.0
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_CATALOG = "agents/catalog.yaml"

_AUDIT_SYS = (
    "Ты — аудитор плана работ DBA. Тебе дан план и карточки прошлых инцидентов, где команда уже "
    "наступала на грабли. Сверь план с инцидентами: если план ведёт к уже случавшейся проблеме или "
    "не учитывает урок инцидента — сформулируй замечание одной фразой, сославшись на его суть. "
    "Замечания только по делу: нет пересечения плана с инцидентом — warnings пустой. Не выдумывай "
    "проблем, которых в карточках нет."
)


class AuditWarnings(BaseModel):
    """Структурированный выход LLM: замечания аудита (function calling). Пусто — граблей нет."""

    model_config = ConfigDict(extra="forbid")

    warnings: list[str] = Field(
        default_factory=list, description="Замечания «на этих граблях уже стояли», по делу"
    )


def _incidents(hits: list) -> str:
    """Найденные карточки инцидентов → текст под сверку. Пусто — честно говорим об этом LLM."""
    if not hits:
        return "(похожих инцидентов не найдено)"
    return "\n\n".join(f"[{h.source} · {h.heading}]\n{h.text}" for h in hits)


class AuditorAgent(Agent):
    consumes = ["plan.ready"]
    produces = ["audit.done"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        collection = self.config["collection"]
        client = QdrantClient(url=os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL))
        topo = load_topology(
            os.environ["AGENTARIUM_CONFIG"],
            load_catalog(os.environ.get("AGENTARIUM_CATALOG", DEFAULT_CATALOG)),
        )
        # storage.qdrant сверяет паспорт коллекции с чертежом — расхождение падает громко на старте.
        self._incidents = storage.qdrant(collection, topo, client=client)
        # Эмбеддер той же модели/провайдера, что индексировали incidents (иначе вектора несравнимы).
        cfg = topo.collections[collection].embeddings
        self._embedder = make_embedder(cfg.provider, cfg.model, cfg.base_url)
        self._auditor = None  # ленивая сборка chat-LLM (structured output)

    def _common_creds(self) -> dict:
        common = {
            "credentials": os.environ["GIGACHAT_CREDENTIALS"],
            "scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
        }
        base_url = os.environ.get("GIGACHAT_BASE_URL")
        if base_url:
            common["base_url"] = base_url
        ca_bundle = os.environ.get("GIGACHAT_CA_BUNDLE_FILE")
        if ca_bundle:
            common["ca_bundle_file"] = ca_bundle
        return common

    def _llm(self):
        """Chat-LLM для замечаний. Модель — из config экземпляра (chat, не эмбеддер)."""
        if self._auditor is None:
            from langchain_gigachat import GigaChat

            model = self.config["model"]
            common = self._common_creds()
            base_url = model.get("base_url") or common.get("base_url")
            if base_url:
                common["base_url"] = base_url
            self._auditor = GigaChat(model=model["name"], **common).with_structured_output(
                AuditWarnings
            )
        return self._auditor

    async def handle(self, envelope: Envelope) -> Reply | None:
        payload = PlanReadyPayload.model_validate(envelope.payload)  # типизированно, для запроса
        query = build_query(payload.plan)
        vector = (await asyncio.to_thread(self._embedder.embed, [query]))[0]
        hits = self._incidents.search(vector, limit=TOP_K, score_threshold=SCORE_THRESHOLD)
        result = await self._llm().ainvoke(
            [
                ("system", _AUDIT_SYS),
                (
                    "human",
                    f"План:\n{payload.plan}\n\nВердикт executor:\n{payload.verdict}\n\n"
                    f"Инциденты:\n{_incidents(hits)}",
                ),
            ]
        )
        # Блок plan.ready прокладывается ДАЛЬШЕ нетронутым (труба, spec/55): исходный payload как
        # есть, добавлен только audit — обогащённый финал конфигурации B.
        return Reply(
            type="audit.done",
            payload={**envelope.payload, "audit": {"warnings": result.warnings}},
        )
