# Команды платформы. Владелец перечня — CLAUDE.md (таблица «Команды»).
CONFIG ?= dba-base
COLLECTION ?= all

.PHONY: test test-integration test-e2e lint gen apply up demo seed

test:
	uv run pytest -m "not integration" -q

test-integration:
	uv run pytest -m integration -q

lint:
	uv run ruff check core tools tests agents

# gen — валидация рубежа 1 + генерация docker-compose.agents.yml. Только файлы: с брокером не говорит.
gen:
	uv run python -m agentarium gen configs/$(CONFIG).yaml

# apply — привести живой брокер к чертежу (topology apply, spec/40). Вызывается отдельно от gen;
# в порядке make up (S7) стоит после инфраструктуры up+wait — раньше объявлять AMQP-объекты некому.
apply:
	uv run python -m agentarium apply configs/$(CONFIG).yaml

# Цели ниже наполняются слайсами S5–S7 (spec/70). До того — честный отказ, не заглушка-обманка.
up demo seed test-e2e:
	@echo "цель '$@' появится в своём слайсе roadmap (spec/70) — сейчас фаза: см. PROGRAM_STATUS.yaml" && exit 1
