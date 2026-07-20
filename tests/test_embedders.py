"""Юнит: httpx-эмбеддер Ollama и фабрика по провайдеру (в CI, без сервисов). Спека: 05 (слой 2), 45.

Мокаем httpx.post — проверяем контракт адаптера (батч одним запросом, размерность из ответа) и
разрешение base_url фабрикой (env важнее чертежа). Живой Ollama проверяет integration-ярус.
"""

import httpx
import pytest
from agentarium.embedders import EMBED_PATH, OllamaEmbedder

from agents.embedders import GigaChatEmbedder, make_embedder


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --- OllamaEmbedder: батч, размерность из ответа, нормализация URL -------------------------


def test_ollama_embedder_batches_and_reads_dim(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse({"embeddings": [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]})

    monkeypatch.setattr(httpx, "post", fake_post)

    vectors = OllamaEmbedder("bge-m3", "http://ollama:11434").embed(["первый", "второй"])

    assert captured["url"] == f"http://ollama:11434{EMBED_PATH}"
    # батч одним запросом: оба текста в input
    assert captured["json"] == {"model": "bge-m3", "input": ["первый", "второй"]}
    assert len(vectors) == 2
    assert len(vectors[0]) == 3  # размерность — из ответа, не хардкод


def test_ollama_embedder_trims_trailing_slash(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured["url"] = url
        return _FakeResponse({"embeddings": [[1.0]]})

    monkeypatch.setattr(httpx, "post", fake_post)
    OllamaEmbedder("bge-m3", "http://ollama:11434/").embed(["x"])
    assert captured["url"] == f"http://ollama:11434{EMBED_PATH}"


def test_ollama_embedder_rejects_count_mismatch(monkeypatch):
    monkeypatch.setattr(
        httpx, "post", lambda url, json, timeout: _FakeResponse({"embeddings": [[1.0]]})
    )
    with pytest.raises(ValueError, match="векторов"):  # 1 вектор на 2 текста — fail-fast
        OllamaEmbedder("bge-m3", "http://ollama:11434").embed(["a", "b"])


# --- make_embedder: провайдеры и разрешение base_url --------------------------------------


def test_factory_ollama_uses_chart_base_url(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    emb = make_embedder("ollama", "bge-m3", "http://ollama:11434")
    assert isinstance(emb, OllamaEmbedder)
    assert emb._base_url == "http://ollama:11434"


def test_factory_ollama_env_overrides_chart(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    emb = make_embedder("ollama", "bge-m3", "http://ollama:11434")
    assert emb._base_url == "http://localhost:11434"  # с хоста env важнее base_url чертежа


def test_factory_ollama_requires_base_url(monkeypatch):
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="base_url"):
        make_embedder("ollama", "bge-m3", None)


def test_factory_gigachat_is_lazy(monkeypatch):
    # gigachat-ветка ленива: сборка объекта langchain не импортирует (extra `brains` не нужен).
    emb = make_embedder("gigachat", "Embeddings")
    assert isinstance(emb, GigaChatEmbedder)


def test_gigachat_embedder_builds_client_and_calls_embed(monkeypatch):
    """Код-путь дефолтного (API) провайдера: фабрика собирает GigaChatEmbeddings с нужными
    параметрами и зовёт embed_documents. Живой API не трогаем (у freemium эмбеддинги платные) —
    доказываем, что вызов сформирован верно; ответ API — на стороне проверяющего с полным ключом.
    """
    import sys
    import types

    captured = {}

    class FakeEmbeddings:
        def __init__(self, **kwargs):
            captured["init"] = kwargs

        def embed_documents(self, texts):
            captured["texts"] = texts
            return [[0.1, 0.2, 0.3] for _ in texts]

    fake_mod = types.ModuleType("langchain_gigachat")
    fake_mod.GigaChatEmbeddings = FakeEmbeddings
    monkeypatch.setitem(sys.modules, "langchain_gigachat", fake_mod)
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "test-key")
    monkeypatch.setenv("GIGACHAT_CA_BUNDLE_FILE", "/certs/ca.pem")

    emb = make_embedder("gigachat", "Embeddings", base_url="https://api.giga.chat/v1")
    vectors = emb.embed(["регламент доступа", "ролевая модель"])

    assert captured["init"]["credentials"] == "test-key"
    assert captured["init"]["model"] == "Embeddings"
    assert captured["init"]["base_url"] == "https://api.giga.chat/v1"
    assert captured["init"]["ca_bundle_file"] == "/certs/ca.pem"
    assert captured["texts"] == ["регламент доступа", "ролевая модель"]
    assert len(vectors) == 2 and len(vectors[0]) == 3


def test_factory_rejects_unknown_provider():
    with pytest.raises(ValueError, match="ollama или gigachat"):
        make_embedder("stub", "hash-256")
