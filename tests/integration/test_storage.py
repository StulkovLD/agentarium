"""Интеграционные тесты складов против живых Postgres и Qdrant (сервис-контейнеры compose).

Постгрес: миграция применяется и идемпотентна; pool пишет и читает requests.
Qdrant: инжест создаёт коллекцию с полным паспортом id=0; повторный seed пересоздаёт без дублей;
поиск с порогом возвращает релевантный чанк и исключает паспорт; сверка проходит на совпадении
и громко падает при смене модели в чертеже. Эмбеддер — детерминированный дубль (spec/45, spec/70).

Бегут с хоста: дефолты DSN/URL — localhost; в CI/compose-сети правятся переменными окружения.
"""

import os
import uuid

import pytest
from agentarium.storage import (
    MIGRATION,
    PASSPORT_ID,
    Passport,
    PassportMismatch,
    postgres_pool,
    qdrant,
)
from agentarium.topology import load_catalog, load_topology
from qdrant_client import QdrantClient

from tests.doubles import HashEmbedder
from tools.ingest import ingest

POSTGRES_DSN = os.environ.get(
    "POSTGRES_DSN", "postgresql://agentarium:agentarium@localhost:5433/agentarium"
)
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
CONFIG = "configs/storage-test.yaml"
CATALOG = "agents/catalog.yaml"
COLLECTION = "test-docs"


def _double_factory(provider: str, model: str) -> HashEmbedder:
    """Тестовая фабрика: детерминированный дубль вместо живого эмбеддера — так дубль вне прода."""
    return HashEmbedder()


def _topology():
    return load_topology(CONFIG, load_catalog(CATALOG))


# --- Postgres ----------------------------------------------------------------------------


async def test_migration_applies_and_is_idempotent():
    pool = await postgres_pool(POSTGRES_DSN)  # первое применение внутри postgres_pool
    try:
        sql = MIGRATION.read_text(encoding="utf-8")
        async with pool.acquire() as conn:
            await conn.execute(sql)  # повторное применение на старте не падает — идемпотентно
            regclass = await conn.fetchval("SELECT to_regclass('public.requests')")
        assert str(regclass) == "requests"
    finally:
        await pool.close()


async def test_pool_writes_and_reads_request():
    pool = await postgres_pool(POSTGRES_DSN)
    trace = uuid.uuid4()
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO requests (trace_id, text, status) VALUES ($1, $2, $3)",
                trace,
                "почисти старые логи",
                "accepted",
            )
            row = await conn.fetchrow(
                "SELECT text, status, result FROM requests WHERE trace_id = $1", trace
            )
        assert row["text"] == "почисти старые логи"
        assert row["status"] == "accepted"
        assert row["result"] is None
    finally:
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE trace_id = $1", trace)
        await pool.close()


# --- Qdrant: инжест, паспорт, идемпотентность, поиск, сверка ------------------------------


@pytest.fixture
def client():
    c = QdrantClient(url=QDRANT_URL)
    yield c
    if c.collection_exists(COLLECTION):
        c.delete_collection(COLLECTION)
    c.close()


def test_ingest_writes_collection_and_full_passport(client):
    counts = ingest(CONFIG, client=client, embedder_factory=_double_factory, out=lambda _: None)

    assert counts[COLLECTION] > 0
    assert client.collection_exists(COLLECTION)

    records = client.retrieve(COLLECTION, ids=[PASSPORT_ID], with_payload=True)
    passport = Passport.model_validate(records[0].payload)
    assert passport.provider == "stub"
    assert passport.model == "hash-256"
    assert passport.dim == HashEmbedder().dim
    assert passport.distance == "Cosine"
    assert passport.source == "knowledge/test-docs"
    assert passport.created_at  # непустой ISO-таймштамп


def test_reseed_recreates_without_duplicates(client):
    first = ingest(CONFIG, client=client, embedder_factory=_double_factory, out=lambda _: None)
    count_after_first = client.count(COLLECTION).count

    second = ingest(CONFIG, client=client, embedder_factory=_double_factory, out=lambda _: None)
    count_after_second = client.count(COLLECTION).count

    assert first == second  # тот же вход → то же число чанков
    assert count_after_first == count_after_second  # пересоздание целиком, не дозапись дублей


def test_search_returns_relevant_chunk_and_excludes_passport(client):
    ingest(CONFIG, client=client, embedder_factory=_double_factory, out=lambda _: None)
    knowledge = qdrant(COLLECTION, _topology(), client=client)

    query = HashEmbedder().embed(
        ["еженедельно воскресенье инкрементальные сутки точка восстановления"]
    )[0]
    hits = knowledge.search(query, limit=3, score_threshold=0.0)

    assert hits
    assert "backup.md" in hits[0].source          # релевантный документ — про бэкапы
    assert hits[0].heading == "Расписание бэкапов"  # и ровно тот чанк, чьи слова в запросе
    assert all(".md" in hit.source for hit in hits)  # паспорт id=0 (source = каталог) исключён


def test_passport_match_passes_then_model_change_fails_loudly(client):
    ingest(CONFIG, client=client, embedder_factory=_double_factory, out=lambda _: None)

    good = _topology()
    qdrant(COLLECTION, good, client=client)  # совпадение паспорта и чертежа — проходит молча

    changed = good.model_copy(deep=True)
    changed.collections[COLLECTION].embeddings.model = "bge-m3"  # смена модели эмбеддингов
    with pytest.raises(PassportMismatch, match="исправь чертёж или переиндексируй"):
        qdrant(COLLECTION, changed, client=client)
