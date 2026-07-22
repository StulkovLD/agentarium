"""Генератор docker-compose.agents.yml: идемпотентность и фиксированный шаблон (spec/40 п.4)."""

import yaml
from agentarium.topology import load_catalog, load_topology

from tools.gen_compose import DEFAULT_GIGACHAT_CA_BUNDLE, HEALTH_PORT, MOUNT_TARGET, generate, render

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
    # build-контекст агента — корень репо, Dockerfile в папке типа: образу нужны core/agentarium и
    # бандл contract-модулей всех типов (spec/30/40), из папки agents/echo не достать (как у шлюза).
    assert echo["build"] == {"context": ".", "dockerfile": "agents/echo/Dockerfile"}
    assert echo["init"] is True
    assert echo["restart"] == "unless-stopped"
    assert echo["env_file"] == [".env"]
    assert echo["environment"]["AGENT_INSTANCE"] == "echo"
    assert echo["environment"]["AGENTARIUM_CONFIG"] == MOUNT_TARGET
    assert echo["environment"]["GIGACHAT_CA_BUNDLE_FILE"] == (
        "${GIGACHAT_CA_BUNDLE_FILE:-" + DEFAULT_GIGACHAT_CA_BUNDLE + "}"
    )
    # чертёж смонтирован read-only, источник начинается с ./ (иначе compose примет за named volume)
    assert echo["volumes"] == [f"./{CONFIG}:{MOUNT_TARGET}:ro"]
    assert str(HEALTH_PORT) in " ".join(echo["healthcheck"]["test"])
    assert "ports" not in echo  # агенты порт наружу не публикуют — только шлюз (вход системы)


def test_gateway_service_builds_from_repo_root_and_publishes_port():
    _, services = _services()
    gw = services["gateway"]
    assert "AGENT_INSTANCE" not in gw["environment"]  # у шлюза нет имени экземпляра
    # build-контекст — корень репо, Dockerfile в core/gateway: образу нужны core/agentarium и
    # agents/ contract-модули всех типов каталога (spec/30/40), из папки core/gateway их не достать.
    assert gw["build"] == {"context": ".", "dockerfile": "core/gateway/Dockerfile"}
    assert "image" not in gw  # заглушки s6-todo больше нет — образ реальный
    assert "labels" not in gw
    assert gw["volumes"] == [f"./{CONFIG}:{MOUNT_TARGET}:ro"]  # чертёж смонтирован read-only
    assert str(HEALTH_PORT) in " ".join(gw["healthcheck"]["test"])
    # порт шлюза опубликован наружу: демо и Swagger UI подают заявки с хоста (spec/05, spec/40)
    assert gw["ports"] == [f"{HEALTH_PORT}:{HEALTH_PORT}"]


def test_header_documents_explicit_merge():
    text, _ = _services()
    assert "-f docker-compose.yml -f docker-compose.agents.yml" in text
