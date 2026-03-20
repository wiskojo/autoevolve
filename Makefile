.PHONY: sync format format-check lint mypy test build check

sync:
	uv sync --extra dev

format:
	uv run --extra dev ruff format .
	uv run --extra dev ruff check --fix .

format-check:
	uv run --extra dev ruff format --check .

lint:
	uv run --extra dev ruff check .

mypy:
	uv run --extra dev mypy src

test:
	uv run --extra dev pytest -q

build:
	uv build

check:
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) mypy
	$(MAKE) test
