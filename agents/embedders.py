"""Фабрика эмбеддера по провайдеру из чертежа — единая точка для tools/ingest и агентов rag/auditor.

ollama — чистый httpx-адаптер ядра (agentarium.embedders): платформа ходит по HTTP, torch/langchain
не тянутся (spec/05 слой 2). gigachat — ленивая ветка langchain (extra `brains`): импорт внутри
embed(), сборка фабрики ключа и langchain не требует (юниты и каталог без мозгов). Так gigachat
остаётся поддержан (spec/45: переключается чертежом + переиндексацией), а langchain в ядро не
попадает (spec/00) — ленивая gigachat-ветка живёт в слое агентов/инструментов, не в ядре.

base_url ollama: env OLLAMA_BASE_URL важнее base_url чертежа — с хоста (seed/тесты) адрес
localhost:11434, в контейнере — compose-хост ollama:11434 (тот же приём, что QDRANT_URL в Makefile).
"""

import os

from agentarium.embedders import OllamaEmbedder


class GigaChatEmbedder:
    """storage.Embedder поверх langchain-gigachat. langchain — лениво в embed(): сборка объекта
    ключа и extra `brains` не требует. base_url — из чертежа или GIGACHAT_BASE_URL; TLS-цепочка
    НУЦ Минцифры вшита в пакет gigachat (зависимость langchain-gigachat), spec/05.
    """

    def __init__(self, model: str, base_url: str | None = None):
        self._model = model
        self._base_url = base_url or os.environ.get("GIGACHAT_BASE_URL")
        self._client = None

    def _embeddings(self):
        if self._client is None:
            from langchain_gigachat import GigaChatEmbeddings

            common = {
                "credentials": os.environ["GIGACHAT_CREDENTIALS"],
                "scope": os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS"),
                "model": self._model,
            }
            if self._base_url:
                common["base_url"] = self._base_url
            ca_bundle = os.environ.get("GIGACHAT_CA_BUNDLE_FILE")
            if ca_bundle:
                common["ca_bundle_file"] = ca_bundle
            self._client = GigaChatEmbeddings(**common)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._embeddings().embed_documents(texts)


def make_embedder(provider: str, model: str, base_url: str | None = None):
    """Провайдер из чертежа → эмбеддер. ollama и gigachat поддержаны; иное — громкий отказ.

    Тесты подают свою фабрику с детерминированным дублём — так дубль не пробивается в прод-путь.
    """
    if provider == "ollama":
        resolved = os.environ.get("OLLAMA_BASE_URL") or base_url
        if not resolved:
            raise ValueError(
                "провайдер ollama требует base_url — задай его в блоке "
                "collections.<имя>.embeddings чертежа или в переменной OLLAMA_BASE_URL"
            )
        return OllamaEmbedder(model, resolved)
    if provider == "gigachat":
        return GigaChatEmbedder(model, base_url)
    raise ValueError(
        f"неизвестный провайдер эмбеддера '{provider}' (модель '{model}') — прод индексирует "
        f"ollama или gigachat; для тестов подай свою embedder_factory с дублём"
    )
