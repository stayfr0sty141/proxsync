.DEFAULT_GOAL := help
AGENT_VENV := agent/.venv
BACKEND_VENV := backend/.venv
# The agent must also run on PVE 8 (Python 3.11); the dashboard targets the LXC's 3.13.
BACKEND_PYTHON ?= python3.13

.PHONY: help agent-install agent-test agent-lint agent-format agent-types agent-check agent-run \
        backend-install backend-test backend-lint backend-format backend-types backend-check \
        backend-run backend-migrate backend-revision \
        frontend-install frontend-test frontend-lint frontend-format frontend-types \
        frontend-check frontend-dev frontend-build check clean \
        version version-check audit release dev-setup dev dev-agent

help: ## Show available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

agent-install: ## Create the agent virtualenv and install dependencies
	python3 -m venv $(AGENT_VENV)
	$(AGENT_VENV)/bin/pip install --upgrade pip
	cd agent && .venv/bin/pip install -e ".[dev]" httpx2

agent-test: ## Run the agent test suite
	cd agent && .venv/bin/pytest -q

agent-lint: ## Lint the agent
	cd agent && .venv/bin/ruff check . && .venv/bin/ruff format --check .

agent-format: ## Format the agent
	cd agent && .venv/bin/ruff format . && .venv/bin/ruff check --fix .

agent-types: ## Type-check the agent (strict)
	cd agent && .venv/bin/mypy app tests

agent-check: agent-lint agent-types agent-test ## Lint, type-check and test the agent

agent-run: ## Run the agent locally (no TLS; development only)
	cd agent && .venv/bin/python -m app

backend-install: ## Create the backend virtualenv and install dependencies
	$(BACKEND_PYTHON) -m venv $(BACKEND_VENV)
	$(BACKEND_VENV)/bin/pip install --upgrade pip
	cd backend && .venv/bin/pip install -e ".[dev]" httpx2

backend-test: ## Run the backend test suite
	cd backend && .venv/bin/pytest -q

backend-lint: ## Lint the backend
	cd backend && .venv/bin/ruff check . && .venv/bin/ruff format --check .

backend-format: ## Format the backend
	cd backend && .venv/bin/ruff format . && .venv/bin/ruff check --fix .

backend-types: ## Type-check the backend (strict)
	cd backend && .venv/bin/mypy app tests

backend-check: backend-lint backend-types backend-test ## Lint, type-check and test the backend

backend-migrate: ## Apply database migrations
	cd backend && .venv/bin/alembic upgrade head

backend-revision: ## Autogenerate a migration: make backend-revision m="add x"
	cd backend && .venv/bin/alembic revision --autogenerate -m "$(m)"

backend-run: ## Run the dashboard API locally
	cd backend && .venv/bin/python -m app

frontend-install: ## Install the frontend dependencies
	cd frontend && npm install

frontend-test: ## Run the frontend test suite (vitest)
	cd frontend && npm test

frontend-lint: ## Lint the frontend (eslint + prettier check)
	cd frontend && npm run lint && npm run format

frontend-format: ## Format the frontend (prettier write)
	cd frontend && npm run format:write

frontend-types: ## Type-check the frontend (tsc --noEmit)
	cd frontend && npm run typecheck

frontend-check: frontend-lint frontend-types frontend-test ## Lint, type-check and test the frontend

frontend-dev: ## Run the Next.js dev server
	cd frontend && npm run dev

frontend-build: ## Build the frontend for production
	cd frontend && npm run build

dev-setup: ## Bootstrap the dev environment (venvs, .env files, DB migrate)
	./scripts/dev-setup.sh

dev: ## Run backend + frontend together (Ctrl-C stops both)
	./scripts/dev-run.sh

dev-agent: ## Run backend + frontend + agent together (no TLS, dev only)
	./scripts/dev-run.sh --agent

check: agent-check backend-check frontend-check ## Run every check in the repository

clean: ## Remove caches and build artefacts
	find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \
		-o -name .mypy_cache -o -name '*.egg-info' \) -prune -exec rm -rf {} +

version: ## Print the version declared by each component
	@printf 'backend  '; grep -m1 '^version' backend/pyproject.toml | cut -d'"' -f2
	@printf 'agent    '; grep -m1 '^version' agent/pyproject.toml | cut -d'"' -f2
	@printf 'frontend '; grep -m1 '"version"' frontend/package.json | cut -d'"' -f4

version-check: ## Fail if the three component versions disagree
	@b=$$(grep -m1 '^version' backend/pyproject.toml | cut -d'"' -f2); \
	 a=$$(grep -m1 '^version' agent/pyproject.toml | cut -d'"' -f2); \
	 f=$$(grep -m1 '"version"' frontend/package.json | cut -d'"' -f4); \
	 if [ "$$b" = "$$a" ] && [ "$$a" = "$$f" ]; then \
	   echo "All components at $$b"; \
	 else \
	   echo "Version mismatch: backend=$$b agent=$$a frontend=$$f"; exit 1; \
	 fi

audit: ## Audit dependencies (pip-audit for python, npm audit for the frontend)
	cd backend && .venv/bin/pip install --quiet pip-audit && .venv/bin/pip-audit --strict --desc
	cd agent && .venv/bin/pip install --quiet pip-audit && .venv/bin/pip-audit --strict --desc
	cd frontend && npm audit --omit=dev --audit-level=high

release: version-check check ## Verify versions agree, then run every check (the release gate)
	@echo "Release gate passed. Tag with: git tag -a v$$(grep -m1 '^version' backend/pyproject.toml | cut -d'\"' -f2) -m 'ProxSync release'"
