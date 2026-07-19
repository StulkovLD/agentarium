# Команды платформы. Владелец перечня — CLAUDE.md (таблица «Команды»).
CONFIG ?= dba-base
COLLECTION ?= all

.PHONY: test test-integration test-e2e lint gen up demo seed

test:
	uv run pytest -m "not integration" -q

test-integration:
	uv run pytest -m integration -q

lint:
	uv run ruff check core tests

# Цели ниже наполняются слайсами S3–S7 (spec/70). До того — честный отказ, не заглушка-обманка.
gen up demo seed test-e2e:
	@echo "цель '$@' появится в своём слайсе roadmap (spec/70) — сейчас фаза: см. PROGRAM_STATUS.yaml" && exit 1
