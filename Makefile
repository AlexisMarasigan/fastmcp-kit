.PHONY: help install dev test lint typecheck check format \
        stack-up stack-down stack-clean gen-dashboards \
        verify-docs all

help:
	@echo "mcp-toolkit — common targets"
	@echo ""
	@echo "  install         Sync runtime + dev deps via uv"
	@echo "  dev             Install with all extras (observability, redis, otel, fallback)"
	@echo "  test            Run pytest with coverage"
	@echo "  lint            Run ruff check + format check"
	@echo "  typecheck       Run mypy strict"
	@echo "  format          Apply ruff format"
	@echo "  verify-docs     Run Clara invariant checks"
	@echo "  check           lint + typecheck + verify-docs + test"
	@echo ""
	@echo "  stack-up        Bring up Prometheus + Grafana + MCP server (docker compose)"
	@echo "  stack-down      Stop the stack, keep volumes"
	@echo "  stack-clean     Stop the stack and drop volumes"
	@echo "  gen-dashboards  Regenerate Grafana dashboards from the demo toolkit"
	@echo ""
	@echo "  all             Same as check"

install:
	uv sync --frozen --group dev

dev:
	uv sync --frozen --group dev --extra observability --extra redis --extra otel

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

typecheck:
	uv run mypy

format:
	uv run ruff format .
	uv run ruff check --fix .

verify-docs:
	uv run python scripts/verify_docs.py

check: lint typecheck verify-docs test

all: check

stack-up:
	docker compose -f deploy/observability-stack/compose.dev.yaml up -d --build

stack-down:
	docker compose -f deploy/observability-stack/compose.dev.yaml down

stack-clean:
	docker compose -f deploy/observability-stack/compose.dev.yaml down -v

gen-dashboards:
	uv run mcp-toolkit gen-dashboards \
		--out deploy/observability-stack/grafana/dashboards
