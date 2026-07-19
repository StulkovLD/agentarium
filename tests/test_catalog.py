"""Каталог сверяется с кодом типов: copy в каталоге = манифест класса агента (spec/30).

Ядро не импортирует агентов, но тест — импортирует: он и есть место сверки «лица» с кодом.
"""

from agentarium.__main__ import load_agent_class
from agentarium.topology import load_catalog

CATALOG = load_catalog("agents/catalog.yaml")


def test_catalog_matches_agent_manifests():
    for type_name, entry in CATALOG.items():
        agent_cls = load_agent_class(entry.build)
        assert agent_cls.consumes == entry.consumes, f"{type_name}: consumes расходится с кодом"
        assert agent_cls.produces == entry.produces, f"{type_name}: produces расходится с кодом"


def test_catalog_has_echo_reverse_and_dba_types():
    # echo/reverse (S3) + dba-типы parser/rag/executor (S7) + auditor (S8, конфигурация B).
    assert set(CATALOG) == {"echo", "reverse", "parser", "rag", "executor", "auditor"}
