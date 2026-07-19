"""Allowlist типизированных read-only проверок против target-db. Спека: 55, закон fail-fast spec/00.

Аудитория ревью — DBA, потому безопасность по-взрослому:
- LLM **не пишет SQL**. Он выбирает имя проверки из этого allowlist и даёт типизированные аргументы;
  сырой SQL здесь — фиксированная строка-константа на инструмент, аргументы едут отдельными
  позиционными параметрами ($1, $2) — конкатенации значения в текст запроса не существует.
- Весь набор — только чтение. Исполнение — под READ ONLY транзакцией и statement_timeout (agent.py).

Мозгов тут нет: модуль импортируется юнит-тестами без langgraph и без живой БД (sql-текст и сборка
параметров проверяются в лоб). Исполнение (`run_check`) принимает готовое asyncpg-соединение.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Args(BaseModel):
    """База аргументов инструмента: лишние/недостающие поля — громкий отказ (типизация)."""

    model_config = ConfigDict(extra="forbid")


class NoArgs(_Args):
    pass


class UserArg(_Args):
    user: str


class UserDatabaseArgs(_Args):
    user: str
    database: str


class DatabaseArg(_Args):
    database: str


@dataclass(frozen=True)
class Tool:
    """Именованная проверка: фиксированный SQL + схема аргументов + сборка позиционных $N."""

    name: str
    args_model: type[_Args]
    sql: str
    params: Callable[[_Args], list[Any]]


# Фиксированные SQL — константы, только чтение. Значения аргументов НИКОГДА не в тексте: только $N.
_PG_VERSION = "SELECT version() AS version"

_USER_ROLES = (
    "SELECT r.rolname AS role "
    "FROM pg_roles r "
    "JOIN pg_auth_members m ON m.roleid = r.oid "
    "JOIN pg_roles u ON u.oid = m.member "
    "WHERE u.rolname = $1 "
    "ORDER BY r.rolname"
)

_USER_PRIVILEGES = (
    "SELECT table_schema, table_name, privilege_type "
    "FROM information_schema.role_table_grants "
    "WHERE grantee = $1 AND table_catalog = $2 "
    "ORDER BY table_schema, table_name, privilege_type"
)

_DB_SIZE = (
    "SELECT pg_size_pretty(pg_database_size($1)) AS size, "
    "pg_database_size($1) AS size_bytes"
)

_ACTIVE_CONNECTIONS = (
    "SELECT count(*) AS active FROM pg_stat_activity WHERE state = 'active'"
)


TOOLS: dict[str, Tool] = {
    "pg_version": Tool("pg_version", NoArgs, _PG_VERSION, lambda a: []),
    "user_roles": Tool("user_roles", UserArg, _USER_ROLES, lambda a: [a.user]),
    "user_privileges": Tool(
        "user_privileges", UserDatabaseArgs, _USER_PRIVILEGES, lambda a: [a.user, a.database]
    ),
    "db_size": Tool("db_size", DatabaseArg, _DB_SIZE, lambda a: [a.database]),
    "active_connections": Tool("active_connections", NoArgs, _ACTIVE_CONNECTIONS, lambda a: []),
}

ALLOWLIST = tuple(TOOLS)


class UnknownCheck(Exception):
    """LLM выбрал имя вне allowlist — громкий отказ, не тихое исполнение чего попало (spec/00)."""


def bind(name: str, raw_args: dict[str, Any]) -> tuple[str, list[Any]]:
    """Проверка allowlist + типизация аргументов → (фиксированный SQL, позиционные параметры).

    Имя вне allowlist → UnknownCheck; аргументы не по схеме → ValidationError. Значения аргументов
    возвращаются ОТДЕЛЬНО от SQL — интерполяции в текст нет по построению. Юнит-тестируемо без БД.
    """
    tool = TOOLS.get(name)
    if tool is None:
        raise UnknownCheck(
            f"проверка '{name}' не в allowlist {ALLOWLIST} — сырой SQL от LLM не исполняется"
        )
    args = tool.args_model.model_validate(raw_args)
    return tool.sql, tool.params(args)


def catalog_description() -> str:
    """Описание allowlist для планировщика LLM: имена проверок и их обязательные поля."""
    lines = []
    for name, tool in TOOLS.items():
        fields = ", ".join(tool.args_model.model_fields) or "без аргументов"
        lines.append(f"- {name}({fields})")
    return "\n".join(lines)


async def run_check(conn: Any, name: str, raw_args: dict[str, Any]) -> list[dict[str, Any]]:
    """Исполнить проверку allowlist на готовом asyncpg-соединении. Строки → список словарей.

    Транзакция READ ONLY и statement_timeout — забота вызывающего (agent.py): здесь только чтение
    фиксированного SQL с типизированными параметрами.
    """
    sql, params = bind(name, raw_args)
    rows = await conn.fetch(sql, *params)
    return [dict(row) for row in rows]
