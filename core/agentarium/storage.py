"""Общие склады: Postgres (состояние) и Qdrant (знания). Контракт: spec/45.

Два склада — два вопроса: Postgres «что происходит с заявками», Qdrant «что система знает».
Доступ агентов к складам — только через этот модуль: проверки паспорта живут в одном месте.
Fail-fast: отсутствующий паспорт или несопоставимые вектора — громкий отказ, не тихий мусор.

Владелец записи в Qdrant (пересоздание коллекции, паспорт) — инжест (tools/ingest, spec/45);
здесь — контракт (модель паспорта, id=0, метрика) и сторона чтения: сверка + поиск.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable

import asyncpg
from pydantic import BaseModel, ConfigDict
from qdrant_client import QdrantClient, models

from agentarium.topology import Topology

MIGRATION = Path(__file__).parent / "migrations" / "001_requests.sql"

PASSPORT_ID = 0  # служебная точка-паспорт: зарезервированный id, поиск её исключает (spec/45)
DISTANCE = models.Distance.COSINE  # метрика знаний — cosine (вектора несравнимы, spec/45)


# --- Postgres: склад состояния ----------------------------------------------------------


async def postgres_pool(dsn: str) -> asyncpg.Pool:
    """Пул asyncpg с применённой миграцией. Один SQL-файл, на старте, идемпотентно (spec/45)."""
    pool = await asyncpg.create_pool(dsn)
    sql = MIGRATION.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    return pool


# --- Эмбеддер: текст → вектор -----------------------------------------------------------


@runtime_checkable
class Embedder(Protocol):
    """Контракт эмбеддера: батч текстов → батч векторов. Размерность узнаётся пробным вызовом.

    Прод-фабрика по провайдеру — в tools/ingest (gigachat поверх langchain — не в ядре, spec/00);
    для CI и юнитов — детерминированный дубль (tests/), в прод-путь он не попадает.
    """

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# --- Qdrant: склад знаний + паспорт-сверка -----------------------------------------------


class Passport(BaseModel):
    """Паспорт коллекции — служебная точка id=0: отпечаток конфига эмбеддингов (spec/45)."""

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    dim: int
    distance: str
    created_at: str
    source: str


class Hit(BaseModel):
    """Находка поиска: чанк знаний + его релевантность. Типизированный выход, не словарь."""

    model_config = ConfigDict(extra="forbid")

    score: float
    source: str
    heading: str
    text: str


class PassportMismatch(Exception):
    """Паспорт коллекции разошёлся с чертежом/конфигом Qdrant. Вектора несравнимы — fail-fast."""


class Knowledge:
    """Проверенный доступ к коллекции: поиск, исключающий паспорт id=0 (spec/45)."""

    def __init__(self, client: QdrantClient, collection: str):
        self._client = client
        self._collection = collection

    def search(
        self, vector: list[float], *, limit: int = 5, score_threshold: float | None = None
    ) -> list[Hit]:
        """Ближайшие чанки к вектору. Паспорт id=0 исключён фильтром — не мусорит (spec/45)."""
        response = self._client.query_points(
            self._collection,
            query=vector,
            query_filter=models.Filter(
                must_not=[models.HasIdCondition(has_id=[PASSPORT_ID])]
            ),
            limit=limit,
            score_threshold=score_threshold,
            with_payload=True,
        )
        return [
            Hit(
                score=point.score,
                source=point.payload["source"],
                heading=point.payload["heading"],
                text=point.payload["text"],
            )
            for point in response.points
        ]


def qdrant(collection: str, topology: Topology, *, client: QdrantClient) -> Knowledge:
    """Прочитать паспорт id=0 и сверить по осям spec/45; вернуть проверенный доступ.

    Оси и их эталоны:
      provider/model — против блока collections чертежа;
      dim/distance   — против фактического конфига коллекции в Qdrant;
      created_at/source — информационные, не сверяются.
    Любое расхождение — громкий отказ: тихий мусорный поиск запрещён законом fail-fast.
    """
    block = topology.collections.get(collection)
    if block is None:
        raise PassportMismatch(
            f"коллекция '{collection}' не описана в секции collections чертежа '{topology.system}'"
        )
    passport = _read_passport(client, collection)
    expected = block.embeddings
    if passport.provider != expected.provider or passport.model != expected.model:
        raise PassportMismatch(
            f"коллекция {collection} проиндексирована {passport.model}/{passport.provider} "
            f"({passport.dim}), в чертеже {expected.model} — исправь чертёж или переиндексируй"
        )
    actual_dim, actual_distance = _collection_config(client, collection)
    if passport.dim != actual_dim or passport.distance != actual_distance:
        raise PassportMismatch(
            f"коллекция {collection}: паспорт заявляет {passport.dim}/{passport.distance}, "
            f"а коллекция в Qdrant — {actual_dim}/{actual_distance}; переиндексируй `make seed`"
        )
    return Knowledge(client, collection)


def _read_passport(client: QdrantClient, collection: str) -> Passport:
    if not client.collection_exists(collection):
        raise PassportMismatch(
            f"коллекции {collection} нет в Qdrant — прогони `make seed COLLECTION={collection}`"
        )
    records = client.retrieve(collection, ids=[PASSPORT_ID], with_payload=True)
    if not records:
        raise PassportMismatch(
            f"в коллекции {collection} нет паспорта id={PASSPORT_ID} — переиндексируй `make seed`"
        )
    return Passport.model_validate(records[0].payload)


def _collection_config(client: QdrantClient, collection: str) -> tuple[int, str]:
    """Фактические размерность и метрика коллекции из Qdrant (безымянный единственный вектор)."""
    vectors = client.get_collection(collection).config.params.vectors
    if isinstance(vectors, dict):  # именованные вектора не используем — берём единственный
        vectors = next(iter(vectors.values()))
    distance = vectors.distance
    return vectors.size, distance.value if hasattr(distance, "value") else str(distance)
