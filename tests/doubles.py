"""Детерминированный эмбеддер-дубль для CI и юнитов (spec/05, spec/70).

Feature hashing: токены текста → фиксированные бакеты вектора, накопление знаковых вкладов,
L2-нормализация. Детерминизм — из hashlib (не salted hash()); общие токены дают близкие вектора,
поэтому cosine-поиск по общим словам осмыслен. Живой GigaChat-эмбеддер приходит в S7 — этот дубль
живёт в tests/ и в прод-путь не попадает (фабрика прода его не знает).
"""

import hashlib
import math
import re

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class HashEmbedder:
    """Реализует протокол storage.Embedder. Фиксированная размерность, hash-based вектора."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dim
            vec[bucket] += 1.0 if digest[4] & 1 else -1.0
        norm = math.sqrt(sum(component * component for component in vec))
        if norm == 0.0:  # пустой текст → орт, не ноль: cosine требует ненулевой вектор
            vec[0] = 1.0
            return vec
        return [component / norm for component in vec]
