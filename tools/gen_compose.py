"""Генератор docker-compose.agents.yml из чертежа + каталога. Контракт: spec/40 п.4.

Только файлы: с живым брокером не разговаривает (это дело topology apply). Шаблон фиксированный
и маленький; вывод идемпотентен — два прогона на одном входе дают байт-в-байт одинаковый файл.
Шлюз (gateway) — всегда в этом же файле: он живёт с агентами, стартует после topology apply.
"""

from pathlib import Path

import yaml
from agentarium.topology import CatalogType, Topology, load_catalog, load_topology

OUT_PATH = "docker-compose.agents.yml"
CATALOG_PATH = "agents/catalog.yaml"

# Порт /health. Пока единственный потребитель — этот генератор; SDK-сервер /health приходит в S6
# (spec/30) и обязан взять ровно этот порт. При появлении сервера владельца переносим в core.
HEALTH_PORT = 8000

MOUNT_TARGET = "/etc/agentarium/topology.yaml"  # куда монтируется чертёж внутрь контейнера
GATEWAY = "gateway"
# Образ шлюза: build-контекст — КОРЕНЬ репо (Dockerfile тянет core/agentarium + agents/ contract-
# модули всех типов каталога, spec/30/40), а не папка core/gateway — из неё `..` за контекст закрыт.
GATEWAY_BUILD = {"context": ".", "dockerfile": "core/gateway/Dockerfile"}


def _agent_build(build_dir: str) -> dict:
    """Образ агента строится так же, как шлюз: контекст — корень репо, Dockerfile — в папке типа.

    Из папки типа (agents/<тип>) контекст не достал бы core/agentarium и contract-модули соседних
    типов — а Dockerfile обязан положить в образ и SDK, и бандл всех contract-модулей (spec/30/40).
    """
    return {"context": ".", "dockerfile": f"{build_dir}/Dockerfile"}

_MERGE_HINT = (
    "docker compose -f docker-compose.yml -f docker-compose.agents.yml up -d --wait"
)

_HEADER = (
    "# docker-compose.agents.yml — СГЕНЕРИРОВАН `make gen`. Руками не править: перезатрёт gen.\n"
    "# Владелец состава — чертёж {config} + каталог {catalog} (spec/40).\n"
    "#\n"
    "# Запускать ВСЕГДА явным объединением с инфраструктурой:\n"
    "#     {merge}\n"
    "# Шлюз (gateway) живёт здесь же, а не в статичной инфраструктуре: он стартует после\n"
    "# topology apply (его очередь объявляет apply) и перезапускается при смене CONFIG.\n"
)


def _health_test(port: int) -> list[str]:
    # python есть в каждом образе агента — не полагаемся на curl/wget
    probe = f"import urllib.request; urllib.request.urlopen('http://localhost:{port}/health').read()"
    return ["CMD", "python", "-c", probe]


def _healthcheck() -> dict:
    return {
        "test": _health_test(HEALTH_PORT),
        "interval": "10s",
        "timeout": "5s",
        "retries": 5,
        "start_period": "20s",
    }


def _host_path(path: str) -> str:
    """Bind-mount источник обязан начинаться с ./ или /, иначе compose примет за named volume."""
    if path.startswith(("/", "./", "../", "~")):
        return path
    return f"./{path}"


def _service(
    *, build: dict, instance: str | None, config_path: str, ports: list[str] | None = None
) -> dict:
    environment = {"AGENTARIUM_CONFIG": MOUNT_TARGET}
    if instance is not None:
        environment["AGENT_INSTANCE"] = instance  # шлюз имени экземпляра не имеет
    service = {
        "build": build,
        "init": True,  # PID 1 — tini: рабочий процесс убиваем, chaos-сцена честна (spec/50)
        "restart": "unless-stopped",
        "env_file": [".env"],  # корневой .env в контейнеры сам не попадает — канал явный
        "environment": environment,
        "volumes": [f"{_host_path(config_path)}:{MOUNT_TARGET}:ro"],  # чертёж read-only
        "healthcheck": _healthcheck(),
    }
    if ports is not None:
        service["ports"] = ports
    return service


def build_compose(
    topo: Topology,
    catalog: dict[str, CatalogType],
    config_path: str,
) -> dict:
    """Собрать словарь compose: сервис на каждый экземпляр + сервис gateway (spec/40)."""
    services: dict[str, dict] = {}
    for name, inst in topo.agents.items():
        services[name] = _service(
            build=_agent_build(catalog[inst.type].build),
            instance=name,
            config_path=config_path,
        )
    # Шлюз имени экземпляра не имеет; образ — из корня репо по core/gateway/Dockerfile.
    # Порт публикуется наружу: демо и Swagger UI подают заявки с хоста (spec/05, spec/40).
    services[GATEWAY] = _service(
        build=GATEWAY_BUILD,
        instance=None,
        config_path=config_path,
        ports=[f"{HEALTH_PORT}:{HEALTH_PORT}"],
    )
    return {"services": services}


def render(topo: Topology, catalog: dict[str, CatalogType], config_path: str) -> str:
    """YAML-текст файла: детерминированный заголовок + детерминированный дамп. Идемпотентно."""
    header = _HEADER.format(config=config_path, catalog=CATALOG_PATH, merge=_MERGE_HINT)
    body = yaml.safe_dump(
        build_compose(topo, catalog, config_path),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    return header + body


def generate(
    config_path: str, *, catalog_path: str = CATALOG_PATH, out_path: str = OUT_PATH
) -> str:
    """Валидировать чертёж рубежом 1, сгенерировать compose, записать. Возврат — путь файла."""
    catalog = load_catalog(catalog_path)
    topo = load_topology(config_path, catalog)  # рубеж 1: полный список дыр при провале
    text = render(topo, catalog, config_path)
    Path(out_path).write_text(text, encoding="utf-8")
    return out_path


def _cli() -> None:
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("использование: python -m tools.gen_compose CONFIG.yaml")
    out = generate(sys.argv[1])
    print(f"сгенерирован {out}")
    print(f"запуск явным объединением: {_MERGE_HINT}")


if __name__ == "__main__":
    _cli()
