.PHONY: dev test lint migrate prod-web-deploy

dev:
	uvicorn meshonator.api.app:app --reload --host 0.0.0.0 --port 8080

test:
	pytest

lint:
	ruff check src tests

migrate:
	alembic upgrade head

prod-web-deploy:
	./scripts/deploy_prod_web.sh
