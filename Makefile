.PHONY: sync
sync:
	uv sync

.PHONY: format
format:
	uv run ruff format .
	uv run ruff check --fix .

.PHONY: format-check
format-check:
	uv run ruff format --check .

.PHONY: lint
lint:
	uv run ruff check .

.PHONY: typecheck
typecheck:
	uv run mypy src

.PHONY: test
test:
	uv run pytest -q

.PHONY: build
build:
	uv build

.PHONY: check
check: format-check lint typecheck test
