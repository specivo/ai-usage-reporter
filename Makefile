# Development tasks for specivo-ai-usage-reporter. See README.md and CONTRIBUTING.md.

.PHONY: install test lint format hermes-schema-check

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

# The Hermes Agent release the hermes source is checked against (schema_version 33).
HERMES_REF ?= 8da31006f3bcf70d0df05fb2b5a334eec4db2e07
# A local hermes-agent checkout to check instead of cloning HERMES_REF.
HERMES_SRC ?=

hermes-schema-check:
ifeq ($(HERMES_SRC),)
	rm -rf .hermes-src
	git clone --quiet --filter=blob:none https://github.com/NousResearch/hermes-agent .hermes-src
	git -C .hermes-src checkout --quiet $(HERMES_REF)
	uv run python scripts/hermes_schema_check.py .hermes-src
else
	uv run python scripts/hermes_schema_check.py $(HERMES_SRC)
endif
