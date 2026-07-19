"""Юнит: сборка запроса rag из интента и сущностей — детерминированная, без LLM. Спека: 55."""

from agents.parser.contract import Entities, ParsedRequest
from agents.rag.query import build_query


def _req(intent, **entities):
    return ParsedRequest(text="t", intent=intent, entities=Entities(**entities))


def test_query_leads_with_intent_then_entities():
    q = build_query(_req("check_access", user="ivanov", database="billing", environment="prod"))
    assert q == "check_access ivanov billing prod"  # интент ведёт, порядок сущностей фиксирован


def test_query_drops_empty_entities():
    q = build_query(_req("update_db_version", database="billing", target_version="16.3"))
    assert q == "update_db_version billing 16.3"  # host/user/... пусты — отброшены


def test_query_is_deterministic():
    r = _req("compliance_check", database="billing")
    assert build_query(r) == build_query(r)


def test_query_intent_only_when_no_entities():
    assert build_query(_req("update_os")) == "update_os"
