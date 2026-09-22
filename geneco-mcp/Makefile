.PHONY: install dev run run-ssp migrate test lint format

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ../shared -e .

dev:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e ../shared -e ".[dev]"

run:
	.venv/bin/python -m geneco_mcp.run crm

run-ssp:
	.venv/bin/python -m geneco_mcp.run ssp

migrate:
	.venv/bin/alembic upgrade head

test:
	.venv/bin/pytest -v

lint:
	.venv/bin/ruff check .

format:
	.venv/bin/ruff format .
