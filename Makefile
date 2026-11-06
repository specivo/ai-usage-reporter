# Development tasks for specivo-ai-usage-reporter. See README.md and CONTRIBUTING.md.

.PHONY: install test lint format

install:
	uv sync

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy

format:
	uv run ruff format .
	uv run ruff check --fix .
