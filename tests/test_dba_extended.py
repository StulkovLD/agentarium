"""Рубеж 1 на живых чертежах dba-base / dba-extended: конфигурация B валидна и переключает финал.

Без брокера (только файлы, spec/40): чертёж конфигурации B проходит валидацию рубежа 1, а маршруты
и финал переключены на аудит — плана-финала больше нет, финал теперь audit.done.
"""

from agentarium.topology import load_catalog, load_topology

CATALOG = load_catalog("agents/catalog.yaml")
ENV = {"TARGET_DB_DSN": "postgresql://readonly_executor@localhost/billing"}


def test_dba_base_loads():
    topo = load_topology("configs/dba-base.yaml", CATALOG, environ=ENV)
    assert set(topo.agents) == {"parser", "knowledge", "executor"}
    assert topo.finals["plan.ready"] == "complete"  # в базе финал — план


def test_dba_extended_passes_rubezh_1():
    topo = load_topology("configs/dba-extended.yaml", CATALOG, environ=ENV)
    # те же кубики + auditor (spec/55)
    assert set(topo.agents) == {"parser", "knowledge", "executor", "auditor"}
    assert topo.agents["auditor"].type == "auditor"
    assert topo.agents["auditor"].config["collection"] == "incidents"


def test_dba_extended_switches_routes_to_auditor():
    topo = load_topology("configs/dba-extended.yaml", CATALOG, environ=ENV)
    assert topo.routes["plan.ready"] == ["auditor"]  # план уходит аудитору, не в gateway
    assert topo.routes["audit.done"] == ["gateway"]  # финал заявки — аудит


def test_dba_extended_final_is_audit_done_only():
    topo = load_topology("configs/dba-extended.yaml", CATALOG, environ=ENV)
    assert topo.finals["audit.done"] == "complete"
    assert "plan.ready" not in topo.finals  # план финалом быть перестал — гонки двух финалов нет


def test_dba_extended_declares_incidents_collection():
    topo = load_topology("configs/dba-extended.yaml", CATALOG, environ=ENV)
    incidents = topo.collections["incidents"]
    assert incidents.source == "knowledge/incidents"
    assert incidents.embeddings.provider == "ollama"  # bge-m3/Ollama-эмбеддинги (spec/05, spec/55)
    assert incidents.embeddings.model == "bge-m3"
