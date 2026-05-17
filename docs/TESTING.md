# Testing

## Layout

```
tests/
  unit/                  # Mirrors src/ layout. Default `pytest` run hits these.
  integration/           # `-m integration`. Hits a running compose stack.
  e2e/                   # `-m e2e`. Full server + MCP client round-trips.
```

## Markers

| Marker | Selected by default? | What it does |
|---|---|---|
| (none) | ✓ | Pure unit tests. No network. |
| `integration` | ✗ | Requires `docker compose up` from `deploy/observability-stack/`. |
| `e2e` | ✗ | Spawns the server in-process and exercises it end-to-end. |

`pyproject.toml` excludes `integration` from the default run via `-m "not integration"`. Opt in with `pytest -m integration` or `pytest tests/integration`.

## Coverage

80% gate enforced in CI via `--cov-fail-under=80`. Run locally:

```bash
uv run pytest --cov-report=html
open htmlcov/index.html
```

## Fixtures of note

- `tests/conftest.py` — `SpyLogger`, `temp_settings` (env override).
- `tests/integration/conftest.py` — `compose_stack` (assumes `make stack-up` already ran).
- `tests/e2e/conftest.py` — `running_server` (in-process uvicorn + cleanup).
