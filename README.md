# autoevolve

`autoevolve` is a Python port of the JS CLI for git-backed experiment loops with coding agents.

Supports Python `3.10+`.

## Install

```bash
uv sync
```

For test tooling:

```bash
uv sync --extra dev
```

That installs `pytest`, `ruff`, and `mypy`.

## Run

```bash
uv run autoevolve help
```

Or:

```bash
uv run python -m autoevolve help
```

## Commands

- `init`
- `validate`
- `start`
- `record`
- `clean`
- `status`
- `list`
- `show`
- `compare`
- `graph`
- `recent`
- `best`
- `pareto`

## Test

```bash
uv run --extra dev pytest -q
```

```bash
uv run --extra dev ruff check .
uv run --extra dev mypy src
```
