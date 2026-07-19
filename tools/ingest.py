"""Инжест базы знаний: markdown → чанки → вектора → Qdrant с паспортом id=0. Контракт: spec/45.

Предметки не знает: раскладку knowledge/<коллекция>/ владеет чертёж (секция collections, spec/40).
Идемпотентен: коллекция пересоздаётся целиком, повторный прогон не плодит дублей.
Живого эмбеддера здесь нет — фабрика по провайдеру (gigachat → отказ до S7); тесты подают
детерминированный дубль своей фабрикой, так дубль не пробивается в прод-путь (spec/70).
"""

import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from agentarium.storage import DISTANCE, PASSPORT_ID, Embedder, Passport, embedder
from agentarium.topology import Collection, load_catalog, load_topology
from pydantic import BaseModel, ConfigDict
from qdrant_client import QdrantClient, models

CATALOG_PATH = "agents/catalog.yaml"
DEFAULT_QDRANT_URL = "http://localhost:6333"

WINDOW_TOKENS = 500  # секция крупнее ~500 токенов доразбивается окном (spec/45)
OVERLAP_TOKENS = 50  # перехлёст соседних окон — чтобы граница окна не рвала мысль пополам

EmbedderFactory = Callable[[str, str], Embedder]

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


# --- чанкинг: границы — заголовки, крупные секции — окном ---------------------------------


class Chunk(BaseModel):
    """Единица индексации: заголовок секции + текст. Payload в Qdrant — {source, heading, text}."""

    model_config = ConfigDict(extra="forbid")

    heading: str
    text: str


def chunk_markdown(md: str) -> list[Chunk]:
    """Резать markdown по заголовкам; секцию крупнее окна — окном с перехлёстом (spec/45)."""
    heading = ""
    body: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        if text:
            sections.append((heading, text))

    for line in md.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            flush()
            heading = match.group(2).strip()
            body = []
        else:
            body.append(line)
    flush()

    chunks: list[Chunk] = []
    for section_heading, text in sections:
        for window in _window(text):
            chunks.append(Chunk(heading=section_heading, text=window))
    return chunks


def _window(text: str) -> list[str]:
    words = text.split()
    if len(words) <= WINDOW_TOKENS:
        return [text]
    step = WINDOW_TOKENS - OVERLAP_TOKENS
    windows: list[str] = []
    for start in range(0, len(words), step):
        windows.append(" ".join(words[start : start + WINDOW_TOKENS]))
        if start + WINDOW_TOKENS >= len(words):
            break
    return windows


def _collect(source_dir: Path) -> list[tuple[str, Chunk]]:
    """Все *.md коллекции → (источник, чанк). Источник — путь файла: адрес чанка для цитаты."""
    items: list[tuple[str, Chunk]] = []
    for path in sorted(source_dir.glob("*.md")):
        source = path.as_posix()
        for chunk in chunk_markdown(path.read_text(encoding="utf-8")):
            items.append((source, chunk))
    return items


# --- инжест одной коллекции --------------------------------------------------------------


def ingest_collection(
    client: QdrantClient, emb: Embedder, *, name: str, collection: Collection
) -> int:
    """Пересоздать коллекцию, счесть вектора, записать паспорт id=0 и чанки. Возврат — счёт."""
    source_dir = Path(collection.source)
    if not source_dir.is_dir():
        raise FileNotFoundError(
            f"коллекция '{name}': каталога '{collection.source}' нет — нечего индексировать"
        )
    items = _collect(source_dir)
    if not items:
        raise ValueError(
            f"коллекция '{name}': в '{collection.source}' нет *.md — индексировать нечего"
        )

    dim = len(emb.embed(["размерность"])[0])  # размерность — пробным вызовом, не хардкод (spec/45)
    vectors = emb.embed([chunk.text for _, chunk in items])

    _recreate(client, name, dim)

    passport = Passport(
        provider=collection.embeddings.provider,
        model=collection.embeddings.model,
        dim=dim,
        distance=DISTANCE.value,
        created_at=datetime.now(UTC).isoformat(),
        source=collection.source,
    )
    points = [
        models.PointStruct(id=PASSPORT_ID, vector=[1.0] * dim, payload=passport.model_dump())
    ]
    for point_id, ((source, chunk), vector) in enumerate(
        zip(items, vectors, strict=True), start=1
    ):
        points.append(
            models.PointStruct(
                id=point_id,
                vector=vector,
                payload={"source": source, "heading": chunk.heading, "text": chunk.text},
            )
        )
    client.upsert(name, points=points)
    return len(items)


def _recreate(client: QdrantClient, name: str, dim: int) -> None:
    """Пересоздать коллекцию целиком — источник идемпотентности инжеста (spec/45)."""
    if client.collection_exists(name):
        client.delete_collection(name)
    client.create_collection(
        name, vectors_config=models.VectorParams(size=dim, distance=DISTANCE)
    )


# --- инжест по чертежу -------------------------------------------------------------------


def ingest(
    config_path: str,
    *,
    client: QdrantClient,
    collection: str | None = None,
    embedder_factory: EmbedderFactory = embedder,
    catalog_path: str = CATALOG_PATH,
    out: Callable[[str], None] = print,
) -> dict[str, int]:
    """Прочитать секцию collections чертежа и проиндексировать все (или одну COLLECTION)."""
    catalog = load_catalog(catalog_path)
    topo = load_topology(config_path, catalog)
    if not topo.collections:
        raise ValueError(f"в чертеже {config_path} нет секции collections — индексировать нечего")

    if collection is None:
        targets = dict(topo.collections)
    else:
        block = topo.collections.get(collection)
        if block is None:
            raise ValueError(
                f"коллекции '{collection}' нет в collections чертежа {config_path}: "
                f"{sorted(topo.collections)}"
            )
        targets = {collection: block}

    counts: dict[str, int] = {}
    for name, block in targets.items():
        emb = embedder_factory(block.embeddings.provider, block.embeddings.model)
        counts[name] = ingest_collection(client, emb, name=name, collection=block)
        out(f"коллекция {name}: {counts[name]} чанков проиндексировано из {block.source}")
    return counts


def _cli() -> None:
    import os

    args = sys.argv[1:]
    if not args:
        raise SystemExit("использование: python -m tools.ingest CONFIG.yaml [COLLECTION|all]")
    config_path = args[0]
    collection = args[1] if len(args) > 1 else os.environ.get("COLLECTION")
    if collection == "all":  # 'all' — по умолчанию: индексируем все коллекции (spec/45)
        collection = None
    client = QdrantClient(url=os.environ.get("QDRANT_URL", DEFAULT_QDRANT_URL))
    ingest(config_path, client=client, collection=collection)


if __name__ == "__main__":
    _cli()
