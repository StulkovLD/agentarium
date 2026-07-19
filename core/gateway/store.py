"""Сторона шлюза у таблицы requests. Контракты: spec/45 (схема), spec/40 (запись финала).

Шлюз — единственный писатель requests: `accepted` при рождении заявки, `done`/`failed` по финалу.
Две гонки закрыты одной строкой SQL каждая (spec/40):
  - ingress: INSERT ... ON CONFLICT DO NOTHING — запоздавшая `accepted` не откатит терминал;
  - финал: условный UPSERT DO UPDATE ... WHERE status='accepted' — «первый выигрывает» и создание
    строки, если финал пришёл раньше INSERT (шлюз упал между публикацией и записью).
Транзакционный outbox осознанно за скоупом: условный UPSERT закрывает те же дыры строкой SQL.
"""

import json
import uuid
from typing import Any

import asyncpg

# Ошибки, на которых конверт держится unacked и запись повторяется (spec/40): транзиентный отказ
# Postgres или обрыв соединения. Не отпускаем — nack-requeue сжёг бы delivery-limit за миллисекунды.
DB_ERRORS = (asyncpg.PostgresError, asyncpg.InterfaceError, OSError)


async def insert_accepted(pool: asyncpg.Pool, trace_id: uuid.UUID, text: str) -> None:
    """Родить заявку: `accepted`. ON CONFLICT DO NOTHING — терминальный статус не откатывается."""
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO requests (trace_id, text, status) VALUES ($1, $2, 'accepted') "
            "ON CONFLICT (trace_id) DO NOTHING",
            trace_id,
            text,
        )


async def finalize(
    pool: asyncpg.Pool,
    trace_id: uuid.UUID,
    *,
    status: str,
    result: dict[str, Any],
    text: str | None,
) -> None:
    """Записать финал «первый выигрывает». text задан → создать-или-обновить; None → обновить.

    text есть (извлечён из конверта детерминированным порядком, spec/40) — условный UPSERT:
    нет строки → INSERT терминального статуса (финал раньше INSERT); строка `accepted` → DO UPDATE;
    строка уже терминальна → WHERE status='accepted' ложно, апдейт не срабатывает (идемпотентность).
    text=None — контрактный запас dlq-консюмера (текст не извлёкся): обновить существующую строку,
    не создавая новую из ничего (spec/00: недостающее не выдумываем).
    """
    payload = json.dumps(result, ensure_ascii=False)
    async with pool.acquire() as conn:
        if text is None:
            await conn.execute(
                "UPDATE requests SET status = $2, result = $3::jsonb, updated_at = now() "
                "WHERE trace_id = $1 AND status = 'accepted'",
                trace_id,
                status,
                payload,
            )
        else:
            await conn.execute(
                "INSERT INTO requests (trace_id, text, status, result) "
                "VALUES ($1, $2, $3, $4::jsonb) "
                "ON CONFLICT (trace_id) DO UPDATE "
                "SET status = EXCLUDED.status, result = EXCLUDED.result, updated_at = now() "
                "WHERE requests.status = 'accepted'",
                trace_id,
                text,
                status,
                payload,
            )


async def get_request(pool: asyncpg.Pool, trace_id: uuid.UUID) -> dict[str, Any] | None:
    """Статус и результат заявки для GET. None — нет такой (шлюз ответит 404)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, result FROM requests WHERE trace_id = $1", trace_id
        )
    if row is None:
        return None
    raw = row["result"]
    return {"status": row["status"], "result": json.loads(raw) if raw is not None else None}
