"""HTTP-эмбеддер Ollama: текст → вектор по HTTP, без LLM-фреймворка в ядре. Спека: 05 (слой 2), 45.

Ollama — отдельный сервис compose; ядро ходит к нему чистым httpx (httpx уже зависимость ядра),
torch и langchain в платформу не тянутся — образы агентов не тяжелеют (spec/05). Реализует протокол
storage.Embedder: батч текстов → батч векторов одним запросом; размерность узнаётся из ответа.

Здесь — только чистый провайдер ollama. Фабрика по провайдеру и ленивая gigachat-ветка (langchain)
живут в agents/embedders.py: ядро LLM-фреймворков не знает (spec/00), а gigachat — это langchain.
"""

import httpx

EMBED_PATH = "/api/embed"  # Ollama batch-эндпоинт: {model, input:[...]} → {embeddings:[[...]]}
EMBED_TIMEOUT = 120.0  # первый вызов ждёт подгрузку весов модели в память Ollama — запас


class OllamaEmbedder:
    """storage.Embedder поверх Ollama /api/embed. Батч в одном запросе, размерность — из ответа."""

    def __init__(self, model: str, base_url: str):
        self._model = model
        self._base_url = base_url.rstrip("/")

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = httpx.post(
            f"{self._base_url}{EMBED_PATH}",
            json={"model": self._model, "input": texts},
            timeout=EMBED_TIMEOUT,
        )
        response.raise_for_status()
        embeddings = response.json()["embeddings"]
        if len(embeddings) != len(texts):  # fail-fast: Ollama обязан вернуть вектор на каждый текст
            raise ValueError(
                f"Ollama вернул {len(embeddings)} векторов на {len(texts)} текстов "
                f"(модель {self._model}, {self._base_url})"
            )
        return embeddings
