.PHONY: sync format format-check lint mypy test build check

sync:
	uv sync

format:
	uv run ruff format .
	uv run ruff check --fix .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

mypy:
	uv run mypy src

test:
	uv run pytest -q

build:
	uv build

check:
	$(MAKE) format-check
	$(MAKE) lint
	$(MAKE) mypy
	$(MAKE) test
