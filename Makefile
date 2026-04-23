.PHONY: dev test lint migrate

dev:
	uvicorn meshonator.api.app:app --reload --host 0.0.0.0 --port 8080

test:
	pytest

lint:
	ruff check src tests

migrate:
	alembic upgrade head
