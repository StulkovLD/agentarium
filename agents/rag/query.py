"""Сборка текста запроса rag из интента и сущностей — детерминированная, без мозгов. Спека: 55.

Вектор запроса считается из intent+entities (spec/55): здесь только собирается текст под эмбеддер.
Это код, не эвристика над смыслом: интент и сущности уже извлёк parser (LLM), мы лишь склеиваем их
в строку — порядок фиксирован, пустые сущности отбрасываются. Юнит-тестируемо без LLM и Qdrant.
"""

from agents.parser.contract import ParsedRequest

# Порядок сущностей в запросе фиксирован — тот же вход даёт тот же текст (детерминизм для теста).
_ENTITY_ORDER = ("user", "database", "host", "environment", "target_version", "deadline")


def build_query(request: ParsedRequest) -> str:
    """intent + непустые сущности (в фиксированном порядке) → строка для эмбеддера.

    Интент ведёт запрос (регламент ищется по типу работы), сущности уточняют. Порядок и отбор
    пустых — детерминированы: сборка запроса из типизированных полей, а не разбор сырого текста.
    """
    parts = [request.intent]
    dumped = request.entities.model_dump()
    parts.extend(str(dumped[key]) for key in _ENTITY_ORDER if dumped[key])
    return " ".join(parts)
