.PHONY: sync format format-check lint mypy test build check

sync:
	uv sync --group dev

format:
	uv run --group dev ruff format .
	uv run --group dev ruff check --fix .

format-check:
	uv run --group dev ruff format --check .

lint:
	uv run --group dev ruff check .

mypy:
	uv run --group dev mypy src

test:
	uv run --group dev pytest -q

build:
	uv build

check:
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) mypy
	$(MAKE) test
