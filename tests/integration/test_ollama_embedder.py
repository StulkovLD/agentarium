"""Интеграция: живой Ollama (bge-m3) + инжест реальных регламентов + поиск релевантного чанка.

Маркер integration (клеится conftest папки). Graceful skip, если сервис Ollama недоступен ИЛИ модель
bge-m3 не спулена: 1.2GB тест сам не тянет — прогрев через compose (`make up`/`ollama pull`, см.
Makefile и docker-compose.yml). Проверяет сквозной прод-путь эмбеддера ollama: чертёж dba-base
(provider ollama) → коллекция regulations живой моделью → поиск «проверка доступов пользователя»
находит регламент выдачи/проверки доступов (spec/45, spec/55). Бежит с хоста: адреса — localhost.
"""

import os

import httpx
import pytest
from agentarium.storage import qdrant
from agentarium.topology import load_catalog, load_topology
from qdrant_client import QdrantClient

from agents.embedders import make_embedder
from tools.ingest import ingest

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
MODEL = "bge-m3"
CONFIG = "configs/dba-base.yaml"
CATALOG = "agents/catalog.yaml"
COLLECTION = "regulations"


def _ollama_has_model() -> bool:
    """Ollama жив И bge-m3 спулена — иначе тест мягко пропускается (модель тест сам не тянет)."""
    try:
        tags = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=2.0).json()
    except Exception:
        return False
    names = {m.get("name", "") for m in tags.get("models", [])}
    return any(n == MODEL or n.startswith(f"{MODEL}:") for n in names)


pytestmark = pytest.mark.skipif(
    not _ollama_has_model(),
    reason=f"Ollama на {OLLAMA_BASE_URL} недоступна или модель {MODEL} не спулена",
)


@pytest.fixture
def client():
    c = QdrantClient(url=QDRANT_URL)
    yield c
    if c.collection_exists(COLLECTION):
        c.delete_collection(COLLECTION)
    c.close()


def test_ollama_ingest_and_search_finds_access_regulation(client, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", OLLAMA_BASE_URL)  # фабрика разрешит base_url на localhost
    # dba-base ссылается на env:TARGET_DB_DSN — топологии он нужен для валидации, инжест берёт лишь
    # секцию collections. Значение не используется, но переменная обязана существовать (fail-fast).
    monkeypatch.setenv("TARGET_DB_DSN", "postgresql://readonly_executor@localhost/billing")

    counts = ingest(CONFIG, client=client, collection=COLLECTION, out=lambda _: None)
    assert counts[COLLECTION] > 0

    # Паспорт-сверка: коллекция проиндексирована ollama/bge-m3, чертёж — тем же (spec/45).
    topo = load_topology(CONFIG, load_catalog(CATALOG))
    knowledge = qdrant(COLLECTION, topo, client=client)

    # Вектор запроса — та же модель, что индексировала (иначе mismatch); строим той же фабрикой.
    emb = make_embedder("ollama", MODEL, OLLAMA_BASE_URL)
    vector = emb.embed(["проверка доступов пользователя"])[0]
    hits = knowledge.search(vector, limit=5, score_threshold=0.0)

    assert hits
    assert "access-provisioning" in hits[0].source  # релевантный регламент — первым
