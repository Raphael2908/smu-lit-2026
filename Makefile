.DEFAULT_GOAL := help
SHELL := /bin/bash

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

setup: ## Install Python 3.12 + dependencies
	uv python install 3.12
	uv sync --all-extras
	@echo "Done. 'make test' should pass with no keys and no network."

lint: ## ruff + mypy
	uv run ruff check src tests
	uv run ruff format --check src tests
	uv run mypy src || true

fmt: ## Autoformat
	uv run ruff format src tests
	uv run ruff check --fix src tests

test: ## Offline test suite: no keys, no network (pytest-socket enforces it)
	uv run pytest -q

docker-check: ## Fail with a useful message if the Docker daemon is down
	@docker info >/dev/null 2>&1 || { \
	  echo ""; \
	  echo "  Docker daemon is not running. Start Docker Desktop, then re-run."; \
	  echo "  ('make test' needs no Docker at all.)"; \
	  echo ""; exit 1; }

dev: docker-check ## Primary dev path: postgres+redis in Docker, api+worker native
	docker compose up -d postgres redis
	uv run alembic upgrade head
	@echo "Now run in two shells:"
	@echo "  uv run uvicorn verifier.api.app:app --reload --port 8000"
	@echo "  uv run celery -A verifier.worker.celery_app.celery_app worker -Q default,maintenance -l info"

up: docker-check ## Full stack in Docker
	docker compose up -d --build
	@echo "API on http://localhost:8000/healthz"

down: ## Tear the stack down
	docker compose down

nuke: ## Tear down and delete volumes (fast reset)
	docker compose down -v

migrate: ## Apply migrations
	uv run alembic upgrade head

seed-lists: ## Seed the source trust lists
	uv run python -m verifier.repos.seed_lists

login: ## One-time headed browser sign-in for login-walled sources; persists the profile
	uv run python -m verifier.providers.fetcher_browser --login

smoke: ## POST a sample verification against a running API
	@curl -s -X POST http://localhost:8000/v1/verify \
	  -H 'Content-Type: application/json' \
	  -d '{"question":"What is the test for a duty of care in Singapore?","ai_output":"The Court of Appeal set out a single two-stage test in Spandeck Engineering (S) Pte Ltd v Defence Science & Technology Agency [2007] SGCA 37."}' | python3 -m json.tool

.PHONY: help setup lint fmt test docker-check dev up down nuke migrate seed-lists login smoke
