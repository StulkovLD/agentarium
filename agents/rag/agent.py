"""Тип rag: интент + сущности → фрагменты регламентов из Qdrant. Спека: 55, 45, 30.

Мозги — GigaChat Embeddings: запрос из intent+entities (rag.query, детерминированно) → вектор →
поиск в коллекции экземпляра (top-5, порог). Паспорт-сверка уже в storage.qdrant (spec/45) — вектор
запроса считается той же моделью, что индексировала коллекцию (из блока collections чертежа), иначе
вектора несравнимы. Пусто — честное chunks: [] (решает следующий агент, один владелец на решение).

Клиент Qdrant и сверка паспорта — в __init__ (fail-fast на старте, sync); эмбеддер langchain —
лениво на первом handle (импорт модуля для каталога/тестов не тянет extra `brains`).
"""

import os

from agentarium import storage
from agentarium.agent import Agent
from agentarium.envelope import Envelope, Reply
from agentarium.topology import load_catalog, load_topology
from qdrant_client import QdrantClient

from agents.parser.contract import ParsedRequest
from agents.rag import contract  # noqa: F401 — регистрирует схему knowledge.found (spec/30)
from agents.rag.query import build_query

TOP_K = 5  # spec/55
# Порог схожести: 0.0 = «отдай ближайшие 5», выше — режь шум под честное chunks: []. Число тюнится
# на живой коллекции (нужен ключ + regulations от S7a); floor оставлен явным, а не выдуман наугад.
SCORE_THRESHOLD = 0.0
DEFAULT_QDRANT_URL = "http://localhost:6333"
DEFAULT_CATALOG = "agents/catalog.yaml"


class RagAgent(Agent):
    consumes = ["request.parsed"]
    produces = ["knowledge.found"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        collection = self.config["collection"]
        client = QdrantClient(url=os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL))
        topo = load_topology(
            os.environ["AGENTARIUM_CONFIG"],
            load_catalog(os.environ.get("AGENTARIUM_CATALOG", DEFAULT_CATALOG)),
        )
        # storage.qdrant сверяет паспорт коллекции с чертежом — расхождение падает громко на старте.
        self._knowledge = storage.qdrant(collection, topo, client=client)
        self._embeddings_cfg = topo.collections[collection].embeddings
        self._embeddings = None  # ленивая сборка эмбеддера

    def _embedder(self):
        """GigaChat Embeddings той же модели, что индексировала коллекцию (иначе mismatch)."""
        if self._embeddings is None:
            from langchain_gigachat import GigaChatEmbeddings

            common = {
                "credentials": os.environ["GIGACHAT_CREDENTIALS"],
                "scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
                "model": self._embeddings_cfg.model,
            }
            base_url = os.environ.get("GIGACHAT_BASE_URL")
            if base_url:
                common["base_url"] = base_url
            ca_bundle = os.environ.get("GIGACHAT_CA_BUNDLE_FILE")
            if ca_bundle:
                common["ca_bundle_file"] = ca_bundle
            self._embeddings = GigaChatEmbeddings(**common)
        return self._embeddings

    async def handle(self, envelope: Envelope) -> Reply | None:
        request = ParsedRequest.model_validate(envelope.payload)  # типизированно, для запроса
        vector = await self._embedder().aembed_query(build_query(request))
        hits = self._knowledge.search(vector, limit=TOP_K, score_threshold=SCORE_THRESHOLD)
        chunks = [{"text": h.text, "source": h.source, "heading": h.heading} for h in hits]
        # Блок request прокладывается ДАЛЬШЕ нетронутым (труба, spec/55): исходный payload как есть.
        return Reply(
            type="knowledge.found",
            payload={"request": envelope.payload, "chunks": chunks},
        )
