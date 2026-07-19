"""Контрактные тесты схем выходов dba-типов (в CI, без LLM). Спека: 55, 30.

Проверяем контракт-трубу: каждый тип валидирует свой payload по схеме владельца, а финальные типы
несут request.text (шлюзу и его recovery-UPSERT он нужен, spec/40). Валидация — через реестр схем
(contracts.validate_payload), как её делает SDK на границе handle.
"""

import pytest
from agentarium import contracts
from gateway.consumers import extract_text

# Импорт contract-модулей регистрирует их схемы в реестре (side effect, spec/30).
from agents.auditor.contract import AuditDonePayload  # noqa: F401
from agents.executor.contract import Check, PlanReadyPayload  # noqa: F401
from agents.parser.contract import INTENTS, Entities, ParsedRequest  # noqa: F401
from agents.rag.contract import KnowledgeFoundPayload  # noqa: F401

_PARSED = {
    "text": "проверь доступы ivanov на проде billing",
    "intent": "check_access",
    "entities": {"user": "ivanov", "database": "billing", "environment": "prod"},
}


# --- request.parsed ----------------------------------------------------------------------


def test_request_parsed_accepts_valid():
    contracts.validate_payload("request.parsed", _PARSED)


def test_request_parsed_rejects_unknown_intent():
    with pytest.raises(contracts.ContractError):
        contracts.validate_payload("request.parsed", {**_PARSED, "intent": "drop_table"})


def test_request_parsed_rejects_unknown_entity_key():
    bad = {**_PARSED, "entities": {"user": "ivanov", "schema": "public"}}
    with pytest.raises(contracts.ContractError):
        contracts.validate_payload("request.parsed", bad)


def test_all_intents_are_accepted():
    for intent in INTENTS:
        contracts.validate_payload(
            "request.parsed", {"text": "t", "intent": intent, "entities": {}}
        )


# --- request.rejected --------------------------------------------------------------------


def test_request_rejected_shape():
    contracts.validate_payload("request.rejected", {"text": "ерунда :)", "reason": "не заявка"})
    with pytest.raises(contracts.ContractError):
        contracts.validate_payload("request.rejected", {"text": "нет reason"})


# --- knowledge.found ---------------------------------------------------------------------


def test_knowledge_found_carries_request_and_chunks():
    payload = {
        "request": _PARSED,
        "chunks": [
            {"text": "выдача доступов", "source": "knowledge/regulations/access.md", "heading": "Доступы"}  # noqa: E501
        ],
    }
    contracts.validate_payload("knowledge.found", payload)


def test_knowledge_found_accepts_empty_chunks():
    contracts.validate_payload("knowledge.found", {"request": _PARSED, "chunks": []})


# --- plan.ready: финал, несёт request.text (труба до самого конца) ------------------------


def _plan_ready():
    return {
        "request": _PARSED,
        "plan": ["снять текущие роли ivanov", "сверить с регламентом"],
        "checks": [
            {"name": "user_roles", "args": {"user": "ivanov"}, "result": [{"role": "billing_ro"}]}
        ],
        "verdict": "у ivanov роль billing_ro — соответствует регламенту чтения",
        "sources": ["knowledge/regulations/access.md"],
    }


def test_plan_ready_accepts_valid():
    contracts.validate_payload("plan.ready", _plan_ready())


def test_plan_ready_rejects_missing_verdict():
    payload = _plan_ready()
    del payload["verdict"]
    with pytest.raises(contracts.ContractError):
        contracts.validate_payload("plan.ready", payload)


def test_finals_carry_text_for_gateway():
    # Труба: шлюз извлекает текст детерминированным порядком payload.request.text → payload.text.
    assert extract_text(_plan_ready()) == _PARSED["text"]
    assert extract_text({"text": "ерунда :)", "reason": "не заявка"}) == "ерунда :)"


def test_check_model_is_typed():
    check = Check(name="pg_version", args={}, result="PostgreSQL 16.1")
    assert check.name == "pg_version"


# --- audit.done: финал конфигурации B, те же поля plan.ready + audit, несёт request.text ----------


def _audit_done():
    # audit.done = plan.ready нетронутый + блок audit (труба, spec/55): auditor лишь добавляет его.
    return {**_plan_ready(), "audit": {"warnings": ["архивирование WAL стоит проверить до окна"]}}


def test_audit_done_accepts_valid():
    contracts.validate_payload("audit.done", _audit_done())


def test_audit_done_accepts_empty_warnings():
    # Пусто — план граблей не повторяет (spec/55), это валидный обогащённый финал.
    contracts.validate_payload("audit.done", {**_plan_ready(), "audit": {"warnings": []}})


def test_audit_done_rejects_missing_audit():
    with pytest.raises(contracts.ContractError):
        contracts.validate_payload("audit.done", _plan_ready())  # нет блока audit


def test_audit_done_rejects_audit_without_warnings():
    with pytest.raises(contracts.ContractError):
        contracts.validate_payload("audit.done", {**_plan_ready(), "audit": {}})


def test_audit_done_carries_text_to_gateway():
    # Труба до самого конца: финал B несёт request.text шлюзу и его recovery-UPSERT (spec/40).
    assert extract_text(_audit_done()) == _PARSED["text"]
