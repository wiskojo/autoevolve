# autoevolve

`autoevolve` is a Python port of the JS CLI for git-backed experiment loops with coding agents.

Supports Python `3.10+`.

## Install

```bash
uv sync --no-dev
```

For local development:

```bash
uv sync --group dev
```

That installs `pytest`, `ruff`, `mypy`, `inline-snapshot`, and `dirty-equals`.

## Run

```bash
uv run autoevolve --help
```

Or:

```bash
uv run python -m autoevolve --help
```

## Commands

- `init`
  - Writes a stub `PROBLEM.md` if one does not exist, and preserves an existing `PROBLEM.md`.
- `validate`
- `update`
- `start`
- `record`
- `clean`
- `status`
- `log`
- `show`
- `compare`
- `lineage`
- `recent`
- `best`
- `pareto`

## Test

```bash
uv run --group dev pytest -q
```

```bash
uv run --group dev ruff check .
uv run --group dev mypy src
```
