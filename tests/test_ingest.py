"""Юнит-тесты инжеста (в CI, без сервисов): чанкинг по заголовкам + окном, дубль-эмбеддер.

Проверяем контракт нарезки (spec/45) и детерминизм дубля, а не внутренности Qdrant —
живые склады проверяет tests/integration/test_storage.py.
"""

import pytest

from agents.embedders import GigaChatEmbedder, make_embedder
from tests.doubles import HashEmbedder
from tools.ingest import (
    OVERLAP_TOKENS,
    WINDOW_TOKENS,
    Chunk,
    chunk_markdown,
)

# --- чанкинг: границы — заголовки --------------------------------------------------------


def test_chunk_splits_on_headings():
    md = (
        "преамбула до первого заголовка\n\n"
        "# Первый\nтекст первой секции\n\n"
        "## Второй\nтекст второй секции\n"
    )
    chunks = chunk_markdown(md)
    assert [c.heading for c in chunks] == ["", "Первый", "Второй"]
    assert chunks[0].text == "преамбула до первого заголовка"
    assert chunks[1].text == "текст первой секции"
    assert chunks[2].text == "текст второй секции"


def test_chunk_skips_empty_sections():
    """Заголовок без тела не даёт пустого чанка — индексировать нечего."""
    chunks = chunk_markdown("# Пустой\n\n# Полный\nесть текст\n")
    assert [c.heading for c in chunks] == ["Полный"]


def test_chunk_all_are_typed_models():
    chunks = chunk_markdown("# Т\nтекст\n")
    assert all(isinstance(c, Chunk) for c in chunks)


# --- чанкинг: крупная секция доразбивается окном с перехлёстом ----------------------------


def test_large_section_windowed_with_overlap():
    words = [f"w{i}" for i in range(1200)]
    md = "# Крупная\n" + " ".join(words)
    chunks = chunk_markdown(md)

    assert len(chunks) == 3  # 1200 словами при окне 500 и шаге 450: старт 0, 450, 900
    assert all(c.heading == "Крупная" for c in chunks)
    # первое окно — ровно WINDOW_TOKENS слов
    assert chunks[0].text.split() == words[:WINDOW_TOKENS]
    # перехлёст: хвост окна N совпадает с головой окна N+1 на OVERLAP_TOKENS слов
    assert chunks[0].text.split()[-OVERLAP_TOKENS:] == chunks[1].text.split()[:OVERLAP_TOKENS]


def test_small_section_is_single_chunk():
    chunks = chunk_markdown("# Мелкая\n" + " ".join(f"w{i}" for i in range(WINDOW_TOKENS)))
    assert len(chunks) == 1


# --- дубль-эмбеддер детерминирован -------------------------------------------------------


def _cos(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_double_is_deterministic_across_instances():
    text = "роль readonly видит только чтение"
    assert HashEmbedder(dim=128).embed([text]) == HashEmbedder(dim=128).embed([text])


def test_double_fixed_dim_and_normalized():
    vec = HashEmbedder(dim=64).embed(["любой текст здесь"])[0]
    assert len(vec) == 64
    assert abs(_cos(vec, vec) - 1.0) < 1e-9  # L2-нормализован → cosine с собой = 1


def test_double_shared_tokens_are_closer():
    emb = HashEmbedder()
    base = emb.embed(["резервное копирование базы"])[0]
    near = emb.embed(["резервное копирование данных"])[0]  # два общих токена из трёх
    far = emb.embed(["совершенно другие непохожие фразы"])[0]
    assert _cos(base, near) > _cos(base, far)


# --- прод-фабрика эмбеддера: gigachat реализован, дубль в прод-путь не попадает ------------


def test_gigachat_embedder_factory_builds_giga_embedder():
    """Прод-фабрика провайдера gigachat даёт GigaChatEmbedder; клиент langchain строится лениво в
    embed() — сама фабрика ключа и extra `brains` не требует (потому тест зелёный в CI)."""
    emb = make_embedder("gigachat", "Embeddings")
    assert isinstance(emb, GigaChatEmbedder)


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="gigachat"):
        make_embedder("stub", "hash-256")
