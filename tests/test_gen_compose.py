"""Генератор docker-compose.agents.yml: идемпотентность и фиксированный шаблон (spec/40 п.4)."""

import yaml
from agentarium.topology import load_catalog, load_topology

from tools.gen_compose import HEALTH_PORT, MOUNT_TARGET, generate, render

CATALOG = load_catalog("agents/catalog.yaml")
CONFIG = "configs/echo-pair.yaml"


def _services():
    topo = load_topology(CONFIG, CATALOG, environ={})
    text = render(topo, CATALOG, CONFIG)
    return text, yaml.safe_load(text)["services"]


def test_generate_is_idempotent(tmp_path):
    out = tmp_path / "docker-compose.agents.yml"
    generate(CONFIG, out_path=str(out))
    first = out.read_bytes()
    generate(CONFIG, out_path=str(out))
    assert out.read_bytes() == first  # два прогона — байт-в-байт


def test_services_cover_agents_and_gateway():
    _, services = _services()
    assert set(services) == {"echo", "reverse", "gateway"}


def test_agent_service_shape():
    _, services = _services()
    echo = services["echo"]
    assert echo["build"] == "agents/echo"
    assert echo["init"] is True
    assert echo["restart"] == "unless-stopped"
    assert echo["env_file"] == [".env"]
    assert echo["environment"]["AGENT_INSTANCE"] == "echo"
    assert echo["environment"]["AGENTARIUM_CONFIG"] == MOUNT_TARGET
    # чертёж смонтирован read-only, источник начинается с ./ (иначе compose примет за named volume)
    assert echo["volumes"] == [f"./{CONFIG}:{MOUNT_TARGET}:ro"]
    assert str(HEALTH_PORT) in " ".join(echo["healthcheck"]["test"])


def test_gateway_is_stub_without_instance():
    _, services = _services()
    gw = services["gateway"]
    assert "AGENT_INSTANCE" not in gw["environment"]  # у шлюза нет имени экземпляра
    assert gw["build"] == "core/gateway"  # появится в S6
    assert "s6-todo" in gw["image"]
    assert "S6" in gw["labels"]["agentarium.todo"]


def test_header_documents_explicit_merge():
    text, _ = _services()
    assert "-f docker-compose.yml -f docker-compose.agents.yml" in text
