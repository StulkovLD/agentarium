"""Интеграционные тесты целевой БД-«пациента» (target-db) под ролью readonly_executor.

Проверяет контракт роли из spec/55: только чтение + pg_monitor + statement_timeout.
- SELECT version() работает под read-only ролью;
- роли демо-заявок (ivanov и др.) читаются из pg_roles / information_schema / pg_auth_members;
- pg_stat_activity видна шире собственной сессии (pg_monitor → pg_read_all_stats);
- любые INSERT/UPDATE/DDL отклоняются (транзакции роли read-only, write-грантов нет);
- statement_timeout выставлен на роли.

Бегут с хоста: дефолт DSN — localhost:5434 (порт target-db наружу); в CI/compose-сети правится
переменной окружения TARGET_DB_DSN (в env.example она указывает на readonly_executor@target-db).
"""

import os

import asyncpg
import pytest
import pytest_asyncio

TARGET_DB_DSN = os.environ.get(
    "TARGET_DB_DSN", "postgresql://readonly_executor:readonly@localhost:5434/billing"
)

# Демо-пользователь для проверки cross-session видимости pg_stat_activity.
# Пароль — фикстура init-скрипта target-db (01-roles.sql), не секрет.
IVANOV_USER = "ivanov"
IVANOV_PASSWORD = "ivanov"


@pytest_asyncio.fixture
async def conn():
    """Подключение под ролью readonly_executor из TARGET_DB_DSN (read-only + pg_monitor)."""
    c = await asyncpg.connect(dsn=TARGET_DB_DSN)
    try:
        yield c
    finally:
        await c.close()


async def test_select_version_works(conn):
    version = await conn.fetchval("SELECT version()")
    assert version.startswith("PostgreSQL")
    assert "16." in version  # пациент — postgres:16


async def test_demo_roles_readable_from_catalog(conn):
    # роль заявки видна в pg_roles
    rolname = await conn.fetchval("SELECT rolname FROM pg_roles WHERE rolname = $1", IVANOV_USER)
    assert rolname == IVANOV_USER

    # членство ivanov в групповой роли billing_rw — из pg_auth_members (ролевая модель spec/55)
    groups = await conn.fetch(
        """
        SELECT g.rolname AS grp
        FROM pg_auth_members m
        JOIN pg_roles u ON u.oid = m.member
        JOIN pg_roles g ON g.oid = m.roleid
        WHERE u.rolname = $1
        """,
        IVANOV_USER,
    )
    assert "billing_rw" in {r["grp"] for r in groups}

    # остальные демо-роли на месте — фикстура наполнена
    present = await conn.fetch(
        "SELECT rolname FROM pg_roles WHERE rolname = ANY($1::text[])",
        ["petrov", "sidorova", "app_billing", "billing_ro", "billing_rw"],
    )
    assert {r["rolname"] for r in present} >= {
        "petrov",
        "sidorova",
        "app_billing",
        "billing_ro",
        "billing_rw",
    }


async def test_information_schema_lists_billing_tables(conn):
    tables = await conn.fetch(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
    )
    names = {r["table_name"] for r in tables}
    assert {"customers", "invoices", "payments"} <= names


async def test_pg_monitor_widens_stat_activity(conn):
    # прямой факт: readonly_executor — член pg_monitor
    is_monitor = await conn.fetchval("SELECT pg_has_role(current_user, 'pg_monitor', 'MEMBER')")
    assert is_monitor is True

    # функционально: открываем чужую сессию (ivanov) и убеждаемся, что видим её текст запроса.
    # Без pg_read_all_stats (входит в pg_monitor) колонка query чужой роли была бы скрыта (NULL).
    other = await asyncpg.connect(dsn=TARGET_DB_DSN, user=IVANOV_USER, password=IVANOV_PASSWORD)
    try:
        await other.fetchval("SELECT 'agentarium_monitor_probe'::text")  # остаётся как last query
        rows = await conn.fetch(
            """
            SELECT usename, query
            FROM pg_stat_activity
            WHERE usename = $1 AND pid <> pg_backend_pid()
            """,
            IVANOV_USER,
        )
        assert rows, "readonly_executor должен видеть чужую сессию ivanov (pg_monitor)"
        assert any((r["query"] or "") != "" for r in rows), (
            "виден текст чужого запроса — pg_read_all_stats из pg_monitor"
        )
    finally:
        await other.close()


async def test_insert_is_denied(conn):
    with pytest.raises(asyncpg.PostgresError):
        await conn.execute(
            "INSERT INTO customers (full_name, email) VALUES ('x', 'x@example.test')"
        )


async def test_update_is_denied(conn):
    with pytest.raises(asyncpg.PostgresError):
        await conn.execute("UPDATE invoices SET amount = amount + 1 WHERE id = 1")


async def test_ddl_is_denied(conn):
    with pytest.raises(asyncpg.PostgresError):
        await conn.execute("CREATE TABLE should_not_exist (id int)")


async def test_statement_timeout_and_read_only_are_set(conn):
    assert await conn.fetchval("SHOW statement_timeout") == "5s"
    assert await conn.fetchval("SHOW default_transaction_read_only") == "on"
