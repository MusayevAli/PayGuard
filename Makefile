SEED ?= 42
OUT ?= sim/world
UV ?= uv

.PHONY: world run test lint fonts fava

world:
	$(UV) run python -m sim.generate_world --seed $(SEED) --out $(OUT)

run:
	PAYGUARD_WORLD_DIR=$(OUT) $(UV) run uvicorn api.main:app --host 127.0.0.1 --port 8000 --ws-ping-interval 15 --ws-ping-timeout 30

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy

fonts:
	$(UV) run python scripts/prepare_fonts.py

fava:
	$(UV) run fava ledger/payguard.beancount --host 127.0.0.1 --port 5000
