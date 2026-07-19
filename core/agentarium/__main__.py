"""CLI ядра: `python -m agentarium {run|apply|gen}`. Один канал конфигурации — env + чертёж.

Три входа, все три — для make и для контейнеров:
  run          — поднять экземпляр: env AGENT_INSTANCE + чертёж из env AGENTARIUM_CONFIG.
  apply CONFIG — привести живой брокер к чертежу (topology apply, spec/40).
  gen   CONFIG — сгенерировать docker-compose.agents.yml (только файлы, без брокера).

Это launcher: он — единственное место, где ядро импортирует код агента (динамически, ровно один
класс запускаемого экземпляра из его build-папки). Библиотека ядра агентов не импортирует.
"""

import asyncio
import importlib.util
import os
import signal
import sys
from pathlib import Path

from agentarium.agent import Agent
from agentarium.apply import apply_topology, queue_name
from agentarium.bus import Bus
from agentarium.topology import CatalogType, load_catalog, load_topology

CATALOG_PATH = os.environ.get("AGENTARIUM_CATALOG", "agents/catalog.yaml")
DEFAULT_AMQP = "amqp://agentarium:agentarium@localhost:5672/"


def load_agent_class(build_path: str) -> type[Agent]:
    """Импортировать agent.py из build-папки типа и вернуть единственный подкласс Agent.

    Уникальное имя модуля на каждую папку — чтобы echo и reverse не схлопнулись в один `agent`.
    Импорт регистрирует и payload-схемы типа (side effect contract-модуля, spec/30). Если файл уже
    загружен (под любым именем) — переиспользуем его: повторный импорт снова дёрнул бы register и
    упал бы на «схема уже зарегистрирована».
    """
    agent_file = (Path(build_path) / "agent.py").resolve()
    for module in list(sys.modules.values()):
        existing = getattr(module, "__file__", None)
        if existing and Path(existing).resolve() == agent_file:
            return _sole_agent(module, agent_file)

    module_name = f"agentarium_agent_{Path(build_path).name}"
    spec = importlib.util.spec_from_file_location(module_name, agent_file)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не удаётся загрузить агента из {agent_file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return _sole_agent(module, agent_file)


def _sole_agent(module: object, agent_file: Path) -> type[Agent]:
    candidates = [
        obj
        for obj in vars(module).values()
        if isinstance(obj, type) and issubclass(obj, Agent) and obj is not Agent
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"в {agent_file} ожидался ровно один подкласс Agent, найдено {len(candidates)}"
        )
    return candidates[0]


def _register_all_contracts(catalog: dict[str, CatalogType]) -> dict[str, type[Agent]]:
    """Импортировать все типы каталога: реестр схем — бандл всех contract-модулей (spec/30)."""
    return {type_name: load_agent_class(entry.build) for type_name, entry in catalog.items()}


async def _run() -> None:
    instance = os.environ.get("AGENT_INSTANCE")
    config_path = os.environ.get("AGENTARIUM_CONFIG")
    if not instance:
        raise SystemExit("run: не задан AGENT_INSTANCE")
    if not config_path:
        raise SystemExit("run: не задан AGENTARIUM_CONFIG (путь к смонтированному чертежу)")

    catalog = load_catalog(CATALOG_PATH)
    topo = load_topology(config_path, catalog)
    inst = topo.agents.get(instance)  # рубеж 2: имя экземпляра существует в чертеже
    if inst is None:
        raise SystemExit(f"run: экземпляр '{instance}' не найден в чертеже {config_path}")

    classes = _register_all_contracts(catalog)
    agent_cls = classes[inst.type]

    bus = Bus(os.environ.get("RABBITMQ_URL", DEFAULT_AMQP))
    await bus.connect()
    agent = agent_cls(
        instance=instance, bus=bus, queue=queue_name(instance), config=inst.config
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, agent.stop)
    try:
        await agent.run()
    finally:
        await bus.close()


async def _apply(config_path: str) -> None:
    catalog = load_catalog(CATALOG_PATH)
    topo = load_topology(config_path, catalog)
    amqp_url = os.environ.get("RABBITMQ_URL", DEFAULT_AMQP)
    report = await apply_topology(topo, amqp_url=amqp_url)
    print(report.render())


def _gen(config_path: str) -> None:
    from tools.gen_compose import generate  # lazy: run/apply не тянут tools

    out = generate(config_path)
    print(f"сгенерирован {out}")
    print(f"запуск явным объединением: docker compose -f docker-compose.yml -f {out} up -d --wait")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        raise SystemExit("использование: python -m agentarium {run|apply CONFIG|gen CONFIG}")
    command, rest = args[0], args[1:]
    if command == "run":
        asyncio.run(_run())
    elif command == "apply":
        if len(rest) != 1:
            raise SystemExit("использование: python -m agentarium apply CONFIG.yaml")
        asyncio.run(_apply(rest[0]))
    elif command == "gen":
        if len(rest) != 1:
            raise SystemExit("использование: python -m agentarium gen CONFIG.yaml")
        _gen(rest[0])
    else:
        raise SystemExit(f"неизвестная команда '{command}': ожидалось run|apply|gen")


if __name__ == "__main__":
    main()
