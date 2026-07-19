"""Fail-fast секретов на старте шлюза. Контракт: spec/70 (S6), закон fail-fast spec/00.

Шлюз — точка входа всей системы: на старте он отказывается подниматься, если в живом окружении
уцелел плейсхолдер из env.example (человек скопировал env.example → .env и не вписал значение).
Тихо стартовать с фейковым секретом запрещено тем же законом, что и тихая деградация.

Маркер плейсхолдера — конвенция env.example (строка вида KEY=положи-сюда-...). Это не эвристика
над смыслом текста, а детерминированная разметка канала: как префикс `env:` в чертеже (spec/40).
Живой GigaChat-ключ проверяется отдельно — tools/smoke_gigachat.py бьётся в реальный API.
"""

import os
from pathlib import Path

PLACEHOLDER_MARKER = "положи-сюда"  # конвенция env.example: значение-плейсхолдер несёт этот маркер
DEFAULT_ENV_EXAMPLE = "env.example"


class SecretsError(Exception):
    """В живом окружении уцелел плейсхолдер из env.example — старт запрещён (fail-fast)."""


def _placeholders(env_example: Path) -> dict[str, str]:
    """KEY → значение-плейсхолдер из env.example (только строки с маркером)."""
    out: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if PLACEHOLDER_MARKER in value:
            out[key.strip()] = value.strip()
    return out


def check_secrets(
    *,
    environ: dict[str, str] | None = None,
    env_example_path: str | os.PathLike[str] | None = None,
) -> None:
    """Проверить, что ни один плейсхолдер env.example не уцелел в живом окружении.

    Отсутствующий ключ — не нарушение: конфиг без LLM (echo-цепочка) секрета не требует, а тем,
    кому он нужен, откажет уже сам агент. Нарушение — ровно уцелевший плейсхолдер: значение задано
    и осталось шаблоном. Найдено — громкий отказ с человеческим списком; иначе тихо возвращаемся.
    """
    env = os.environ if environ is None else environ
    path = Path(env_example_path or os.environ.get("AGENTARIUM_ENV_EXAMPLE", DEFAULT_ENV_EXAMPLE))
    offenders: list[str] = []
    for key, placeholder in _placeholders(path).items():
        live = env.get(key)
        if live is not None and (live == placeholder or PLACEHOLDER_MARKER in live):
            offenders.append(key)
    if offenders:
        raise SecretsError(
            "секреты не заданы — в окружении уцелели плейсхолдеры env.example: "
            f"{sorted(offenders)}. Скопируй env.example в .env и впиши реальные значения "
            "(например GIGACHAT_CREDENTIALS — authorization key из "
            "https://developers.sber.ru/gigachat)."
        )
