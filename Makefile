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

env: ## Create .env from .env.example if absent (all values are optional)
	@test -f .env || { cp .env.example .env; echo "Created .env from .env.example"; }

dev: docker-check env ## Primary dev path: postgres+redis in Docker, api+worker native
	docker compose up -d postgres redis
	uv run alembic upgrade head
	@echo "Now run in two shells:"
	@echo "  uv run uvicorn verifier.api.app:app --reload --host 0.0.0.0 --port 8000"
	@echo "     (The extension calls 127.0.0.1, never 'localhost'. On a dual-stack"
	@echo "      machine 'localhost' resolves to ::1 first and an IPv4-bound server is"
	@echo "      simply not there -- curl falls back to IPv4 and succeeds, Chrome does"
	@echo "      not, so it fails only in the browser and looks like a dead backend."
	@echo "      Do not 'fix' this with --host :: -- that binds IPv6 ONLY on macOS.)"
	@echo "  uv run celery -A verifier.worker.celery_app.celery_app worker -Q default,judge,maintenance -l info"

# NOTE the 'judge' queue in the celery line above. L4 is dispatched to QUEUE_JUDGE
# (worker/tasks.py) so that a 90 s frontier-model call cannot block the deterministic
# queue. A worker started without it consumes the run but never the judge, and the run
# sits at status=judging until the client's poll timeout -- which reads exactly like a
# hung backend. `make up` gets this right on its own; it runs a separate judgeworker.

up: docker-check env ## Full stack in Docker
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

.PHONY: help setup lint fmt test docker-check env dev up down nuke migrate seed-lists login smoke
