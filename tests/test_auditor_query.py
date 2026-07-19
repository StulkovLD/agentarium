"""Юнит: сборка аудит-запроса из плана — детерминированная, без LLM. Спека: 55."""

from agents.auditor.query import build_query


def test_query_joins_plan_steps_in_order():
    plan = ["снять текущую версию PostgreSQL", "проверить архивирование WAL", "спланировать окно"]
    q = build_query(plan)
    assert q == "снять текущую версию PostgreSQL проверить архивирование WAL спланировать окно"


def test_query_drops_empty_steps():
    assert build_query(["шаг один", "", "  ", "шаг два"]) == "шаг один шаг два"


def test_query_is_deterministic():
    plan = ["проверить роли ivanov", "сверить с ролевой моделью"]
    assert build_query(plan) == build_query(plan)


def test_query_empty_plan_is_empty_string():
    assert build_query([]) == ""
