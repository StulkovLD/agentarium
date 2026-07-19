"""Рубеж 1: fail-fast валидация чертежа — по одному сценарию на правило из spec/40.

Проверяем не только факт отказа, но и вменяемость текста дыры: человек должен понять, что чинить.
Чертёж мутируем как словарь (устойчиво к отступам) и дампим в YAML перед загрузкой.
"""

import copy
from collections.abc import Callable

import pytest
import yaml
from agentarium.topology import CatalogType, TopologyError, load_catalog, load_topology

REAL_CATALOG = load_catalog("agents/catalog.yaml")

# Синтетический тип с непустой config_schema — на нём проверяем примитивы llm/str/dsn.
WORKER_CATALOG = {
    "worker": CatalogType(
        build="agents/worker",
        consumes=["echo.request"],
        produces=["worker.done"],
        config_schema={"model": "llm", "collection": "str", "target_db": "dsn"},
    )
}

BASE = {
    "system": "t",
    "entry": "echo.request",
    "agents": {"echo": {"type": "echo"}, "reverse": {"type": "reverse"}},
    "routes": {
        "echo.request": ["echo"],
        "echo.done": ["reverse"],
        "reverse.done": ["gateway"],
        "task.failed": ["gateway"],
    },
    "finals": {"reverse.done": "complete", "task.failed": "fail"},
}

WORKER_BASE = {
    "system": "t",
    "entry": "echo.request",
    "collections": {
        "regs": {"source": "knowledge/regs", "embeddings": {"provider": "g", "model": "E"}}
    },
    "agents": {
        "w": {
            "type": "worker",
            "model": {"provider": "gigachat", "name": "GigaChat-2"},
            "collection": "regs",
            "target_db": "env:DSN",
        }
    },
    "routes": {
        "echo.request": ["w"],
        "worker.done": ["gateway"],
        "task.failed": ["gateway"],
    },
    "finals": {"worker.done": "complete", "task.failed": "fail"},
}


def _write(tmp_path, data) -> str:
    p = tmp_path / "topo.yaml"
    if isinstance(data, str):
        p.write_text(data, encoding="utf-8")
    else:
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(p)


def mutate(base: dict, edit: Callable[[dict], None]) -> dict:
    data = copy.deepcopy(base)
    edit(data)
    return data


def holes(tmp_path, data, *, catalog=REAL_CATALOG, environ=None) -> list[str]:
    with pytest.raises(TopologyError) as exc:
        load_topology(_write(tmp_path, data), catalog, environ=environ or {})
    return exc.value.holes


# --- happy path -----------------------------------------------------------------------------


def test_valid_topology_loads(tmp_path):
    topo = load_topology(_write(tmp_path, BASE), REAL_CATALOG, environ={})
    assert topo.system == "t"
    assert set(topo.agents) == {"echo", "reverse"}


def test_valid_worker_loads_with_env(tmp_path):
    topo = load_topology(_write(tmp_path, WORKER_BASE), WORKER_CATALOG, environ={"DSN": "dsn://x"})
    assert topo.agents["w"].config["target_db"] == "dsn://x"  # env: подставлен


# --- структура и каталог --------------------------------------------------------------------


def test_broken_yaml(tmp_path):
    h = holes(tmp_path, "system: t\n  entry: : :\n   bad")
    assert any("YAML" in x for x in h)


def test_missing_required_top_level(tmp_path):
    data = mutate(BASE, lambda d: (d.pop("routes"), d.pop("finals")))
    h = holes(tmp_path, data)
    assert any("routes" in x for x in h)
    assert any("finals" in x for x in h)


def test_unknown_type(tmp_path):
    data = mutate(BASE, lambda d: d["agents"]["reverse"].__setitem__("type", "nosuch"))
    h = holes(tmp_path, data)
    assert any("nosuch" in x and "каталог" in x for x in h)


def test_gateway_cannot_be_agent(tmp_path):
    data = mutate(BASE, lambda d: d["agents"].__setitem__("gateway", {"type": "echo"}))
    h = holes(tmp_path, data)
    assert any("gateway" in x and "зарезервированное" in x for x in h)


# --- маршруты -------------------------------------------------------------------------------


def test_route_to_nonexistent_instance(tmp_path):
    data = mutate(BASE, lambda d: d["routes"].__setitem__("echo.done", ["ghost"]))
    h = holes(tmp_path, data)
    assert any("ghost" in x and "экземпляра нет" in x for x in h)


def test_route_type_not_in_consumes(tmp_path):
    # reverse.done ведём в echo — echo его не потребляет
    data = mutate(BASE, lambda d: d["routes"].__setitem__("reverse.done", ["echo", "gateway"]))
    h = holes(tmp_path, data)
    assert any("не принимает 'reverse.done' в consumes" in x for x in h)


def test_produces_without_route(tmp_path):
    # убираем маршрут echo.done — echo его производит, а лететь некуда
    data = mutate(BASE, lambda d: d["routes"].pop("echo.done"))
    h = holes(tmp_path, data)
    assert any("производит 'echo.done'" in x and "некуда" in x for x in h)


def test_entry_not_routed(tmp_path):
    data = mutate(BASE, lambda d: d.__setitem__("entry", "nothing.here"))
    h = holes(tmp_path, data)
    assert any("entry 'nothing.here' не замкнут" in x for x in h)


# --- финалы двусторонне замкнуты на gateway -------------------------------------------------


def test_final_not_routed_to_gateway(tmp_path):
    data = mutate(BASE, lambda d: d["routes"].__setitem__("reverse.done", ["reverse"]))
    h = holes(tmp_path, data)
    assert any("финал 'reverse.done' не маршрутизирован в gateway" in x for x in h)


def test_gateway_route_not_in_finals(tmp_path):
    # echo.done ведём в gateway, но в finals его нет — обратная сторона проверки
    data = mutate(BASE, lambda d: d["routes"].__setitem__("echo.done", ["reverse", "gateway"]))
    h = holes(tmp_path, data)
    assert any("в gateway ведёт 'echo.done'" in x and "нет в finals" in x for x in h)


def test_task_failed_not_routed(tmp_path):
    data = mutate(BASE, lambda d: d["routes"].pop("task.failed"))
    h = holes(tmp_path, data)
    assert any("task.failed" in x and "не маршрутизирован" in x for x in h)


def test_task_failed_not_in_finals(tmp_path):
    data = mutate(BASE, lambda d: d["finals"].pop("task.failed"))
    h = holes(tmp_path, data)
    assert any("task.failed" in x and "finals" in x for x in h)


# --- достижимый complete-финал ровно один ---------------------------------------------------


def test_two_reachable_completes(tmp_path):
    def edit(d):
        d["routes"]["echo.done"] = ["reverse", "gateway"]
        d["finals"]["echo.done"] = "complete"

    h = holes(tmp_path, mutate(BASE, edit))
    assert any("complete-финал должен быть ровно один" in x for x in h)


def test_zero_reachable_completes(tmp_path):
    data = mutate(BASE, lambda d: d["finals"].__setitem__("reverse.done", "fail"))
    h = holes(tmp_path, data)
    assert any("complete-финал должен быть ровно один" in x and "найдено 0" in x for x in h)


# --- config_schema: примитивы llm / str / dsn на синтетическом типе worker ------------------


def test_config_missing_required_field(tmp_path):
    data = mutate(WORKER_BASE, lambda d: d["agents"]["w"].pop("model"))
    h = holes(tmp_path, data, catalog=WORKER_CATALOG, environ={"DSN": "d"})
    assert any("не задано обязательное поле конфига 'model'" in x for x in h)


def test_config_llm_not_a_block(tmp_path):
    data = mutate(WORKER_BASE, lambda d: d["agents"]["w"].__setitem__("model", "just-a-string"))
    h = holes(tmp_path, data, catalog=WORKER_CATALOG, environ={"DSN": "d"})
    assert any("поле 'model' (llm)" in x for x in h)


def test_config_str_empty(tmp_path):
    data = mutate(WORKER_BASE, lambda d: d["agents"]["w"].__setitem__("collection", ""))
    h = holes(tmp_path, data, catalog=WORKER_CATALOG, environ={"DSN": "d"})
    assert any("поле 'collection' (str)" in x and "непустой строкой" in x for x in h)


def test_config_unknown_field(tmp_path):
    data = mutate(WORKER_BASE, lambda d: d["agents"]["w"].__setitem__("modell", "typo"))
    h = holes(tmp_path, data, catalog=WORKER_CATALOG, environ={"DSN": "d"})
    assert any("поле 'modell' не описано в config_schema" in x for x in h)


def test_collection_reference_missing_block(tmp_path):
    data = mutate(WORKER_BASE, lambda d: d.pop("collections"))
    h = holes(tmp_path, data, catalog=WORKER_CATALOG, environ={"DSN": "d"})
    assert any("ссылается на коллекцию 'regs'" in x for x in h)


def test_env_missing_variable(tmp_path):
    h = holes(tmp_path, WORKER_BASE, catalog=WORKER_CATALOG, environ={})
    assert any("DSN" in x and "переменную окружения" in x for x in h)


def test_catalog_bad_primitive(tmp_path):
    bad = {
        "bad": {
            "build": "agents/bad",
            "consumes": ["x.y"],
            "produces": ["x.z"],
            "config_schema": {"field": "nosuch_primitive"},
        }
    }
    p = _write(tmp_path, bad)
    with pytest.raises(TopologyError) as exc:
        load_catalog(p)
    assert any("nosuch_primitive" in x for x in exc.value.holes)
