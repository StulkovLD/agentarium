"""Контрактные тесты конверта — проверяют spec/20, не внутренности."""

import json
import uuid
from datetime import datetime

import pytest
from agentarium import Envelope, Reply
from pydantic import ValidationError


def make(**overrides):
    base = dict(
        trace_id=uuid.uuid4(),
        producer="parser",
        type="request.parsed",
        payload={"text": "проверь доступы ivanov", "intent": "check_access", "entities": {}},
    )
    base.update(overrides)
    return Envelope(**base)


def test_roundtrip_json():
    env = make()
    restored = Envelope.model_validate_json(env.model_dump_json())
    assert restored == env


def test_defaults_are_filled_by_platform():
    env = make()
    assert env.envelope == "1"
    assert isinstance(env.id, uuid.UUID)
    assert env.causation_id is None
    assert env.ts.tzinfo is not None
    assert env.meta == {}


def test_type_must_be_dotted_lowercase():
    for bad in ["RequestNew", "request", "request..new", "REQUEST.NEW", "request new", ""]:
        with pytest.raises(ValidationError):
            make(type=bad)


def test_required_fields():
    with pytest.raises(ValidationError):
        Envelope(producer="x", type="a.b", payload={})  # нет trace_id


def test_naive_ts_rejected():
    with pytest.raises(ValidationError):
        make(ts=datetime(2026, 1, 1))  # noqa: DTZ001 — намеренно naive


def test_unknown_fields_forbidden():
    with pytest.raises(ValidationError):
        make(to="knowledge")  # адресата в конверте нет — принцип spec/20


def test_envelope_is_immutable():
    env = make()
    with pytest.raises(ValidationError):
        env.producer = "другой"  # type: ignore[misc]


def test_child_keeps_trace_and_links_causation():
    parent = make()
    child = parent.child(
        producer="knowledge", type="knowledge.found", payload={"chunks": [], "request": {}}
    )
    assert child.trace_id == parent.trace_id
    assert child.causation_id == parent.id
    assert child.id != parent.id


def test_reply_validates_type():
    with pytest.raises(ValidationError):
        Reply(type="НеТип", payload={})


def test_json_schema_generates():
    schema = Envelope.model_json_schema()
    assert set(schema["required"]) >= {"trace_id", "producer", "type", "payload"}
    json.dumps(schema)  # схема сериализуема — публикуемая проекция контракта
