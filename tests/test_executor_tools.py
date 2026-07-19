"""Юнит: allowlist проверок executor — sql-текст фиксирован, аргументы типизированы. Спека: 55.

Ключ безопасности (аудитория ревью — DBA): LLM не пишет SQL. Проверяем, что текст SQL — константа,
значения аргументов НИКОГДА не в тексте (только позиционные $N), а имя вне allowlist и кривые
аргументы — громкий отказ. Живой БД тут нет: bind() собирает (sql, params) без исполнения.
"""

import re

import pytest
from pydantic import ValidationError

from agents.executor.tools import ALLOWLIST, TOOLS, UnknownCheck, bind

EXPECTED = {"pg_version", "user_roles", "user_privileges", "db_size", "active_connections"}


def test_allowlist_is_exactly_the_five_checks():
    assert set(TOOLS) == EXPECTED
    assert set(ALLOWLIST) == EXPECTED


def test_sql_text_is_fixed_and_read_only():
    for name, tool in TOOLS.items():
        assert tool.sql == TOOLS[name].sql  # константа, не f-string
        upper = tool.sql.upper()
        assert upper.startswith("SELECT")  # только чтение
        assert ";" not in tool.sql  # один statement — нельзя подшить второй (write) через ';'
        # write-команды как отдельные слова: \b не ловит GRANT внутри role_table_GRANTS/GRANTEE
        writes = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "GRANT", "TRUNCATE")
        for forbidden in writes:
            assert re.search(rf"\b{forbidden}\b", upper) is None


def test_bind_builds_positional_params_never_in_sql():
    sql, params = bind("user_roles", {"user": "ivanov"})
    assert params == ["ivanov"]
    assert "ivanov" not in sql  # значение не в тексте — только $1
    assert "$1" in sql


def test_user_privileges_binds_both_typed_args():
    sql, params = bind("user_privileges", {"user": "ivanov", "database": "billing"})
    assert params == ["ivanov", "billing"]
    assert "ivanov" not in sql and "billing" not in sql
    assert "$1" in sql and "$2" in sql


def test_noarg_checks_take_no_params():
    for name in ("pg_version", "active_connections"):
        sql, params = bind(name, {})
        assert params == []
        assert "$" not in sql


def test_unknown_check_is_rejected():
    with pytest.raises(UnknownCheck, match="allowlist"):
        bind("run_raw_sql", {"sql": "DROP TABLE users"})


def test_missing_typed_arg_is_rejected():
    with pytest.raises(ValidationError):
        bind("user_roles", {})  # user обязателен


def test_extra_arg_is_rejected():
    with pytest.raises(ValidationError):
        bind("db_size", {"database": "billing", "schema": "public"})  # extra=forbid


def test_pg_version_sql_is_expected():
    assert bind("pg_version", {})[0] == "SELECT version() AS version"
