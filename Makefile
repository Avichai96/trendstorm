# ==============================================================================
# TrendStorm AI — Developer Makefile
# ==============================================================================
# Convention: every target is documented. `make help` is the source of truth.
# All docker compose invocations go through this file so devs don't have to
# remember -f flags.
# ==============================================================================

.DEFAULT_GOAL := help
SHELL := /bin/bash

# Compose file composition
COMPOSE_BASE  := -f docker/docker-compose.yml
COMPOSE_OBS   := -f docker/docker-compose.obs.yml
COMPOSE_DEV   := -f docker/docker-compose.dev.yml
COMPOSE_APP   := -f docker/docker-compose.app.yml
COMPOSE       := docker compose $(COMPOSE_BASE)
COMPOSE_FULL  := docker compose $(COMPOSE_BASE) $(COMPOSE_OBS) $(COMPOSE_DEV)
COMPOSE_WITH_APP := docker compose $(COMPOSE_BASE) $(COMPOSE_APP)

# Colors for output
GREEN  := $(shell printf '\033[0;32m')
YELLOW := $(shell printf '\033[0;33m')
RED    := $(shell printf '\033[0;31m')
NC     := $(shell printf '\033[0m')

# -----------------------------------------------------------------------------
# Help
# -----------------------------------------------------------------------------
.PHONY: help
help: ## Show this help
	@echo ""
	@echo "$(GREEN)TrendStorm AI — Dev Commands$(NC)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-20s$(NC) %s\n", $$1, $$2}'
	@echo ""

# -----------------------------------------------------------------------------
# Environment
# -----------------------------------------------------------------------------
.PHONY: env
env: ## Create .env from .env.example if missing
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "$(GREEN)Created .env from .env.example$(NC)"; \
		echo "$(YELLOW)Edit .env to add your API keys.$(NC)"; \
	else \
		echo ".env already exists."; \
	fi

.PHONY: keyfile
keyfile: ## Generate Mongo replica set keyfile (already committed; regenerate if needed)
	@mkdir -p docker/config/mongo
	@openssl rand -base64 756 > docker/config/mongo/keyfile
	@chmod 400 docker/config/mongo/keyfile
	@echo "$(GREEN)Mongo keyfile generated.$(NC)"

# -----------------------------------------------------------------------------
# Lifecycle: bring stack up/down
# -----------------------------------------------------------------------------
.PHONY: up
up: env ## Start core stack (data + LLM plane) in background
	$(COMPOSE) up -d
	@$(MAKE) --no-print-directory wait
	@echo "$(GREEN)Core stack is up.$(NC)"

.PHONY: up-obs
up-obs: env ## Start core + observability
	docker compose $(COMPOSE_BASE) $(COMPOSE_OBS) up -d
	@$(MAKE) --no-print-directory wait
	@echo "$(GREEN)Core + observability is up.$(NC)"
	@echo "  Grafana:  http://localhost:3000 (admin/admin)"
	@echo "  Jaeger:   http://localhost:16686"
	@echo "  Prom:     http://localhost:9090"

.PHONY: up-all
up-all: env ## Start everything (core + obs + dev UIs)
	$(COMPOSE_FULL) up -d
	@$(MAKE) --no-print-directory wait
	@echo "$(GREEN)Full stack is up.$(NC)"
	@echo "  Kafka UI:    http://localhost:8080"
	@echo "  Mongo Exp:   http://localhost:8081 (admin/admin)"
	@echo "  Redis Cmdr:  http://localhost:8082 (admin/admin)"
	@echo "  MinIO:       http://localhost:9001 (minioadmin/minioadmin)"
	@echo "  Grafana:     http://localhost:3000 (admin/admin)"
	@echo "  Jaeger:      http://localhost:16686"

# -----------------------------------------------------------------------------
# App services (api + worker containers)
# -----------------------------------------------------------------------------
.PHONY: build-app
build-app: ## Build api, orchestrator-worker, and scout-worker images
	$(COMPOSE_WITH_APP) build api orchestrator-worker scout-worker knowledge-worker analyst-worker

.PHONY: up-app
up-app: env ## Start core infra + api + orchestrator/scout/knowledge/analyst workers
	$(COMPOSE_WITH_APP) up -d
	@$(MAKE) --no-print-directory wait
	@echo "$(GREEN)App + infra running.$(NC) API: http://localhost:8080"

.PHONY: down-app
down-app: ## Stop app services (leave infrastructure running)
	$(COMPOSE_WITH_APP) stop api orchestrator-worker scout-worker
	$(COMPOSE_WITH_APP) rm -f api orchestrator-worker scout-worker

.PHONY: logs-api
logs-api: ## Tail the API container logs
	$(COMPOSE_WITH_APP) logs -f api

.PHONY: logs-worker
logs-worker: ## Tail the orchestrator worker logs
	$(COMPOSE_WITH_APP) logs -f orchestrator-worker

.PHONY: logs-scout
logs-scout: ## Tail the scout worker logs
	$(COMPOSE_WITH_APP) logs -f scout-worker

.PHONY: scale-worker
scale-worker: ## Scale workers (use SCALE=N, e.g. `make scale-worker SCALE=3`)
	$(COMPOSE_WITH_APP) up -d --scale orchestrator-worker=$${SCALE:-2} orchestrator-worker
	@echo "$(GREEN)Workers scaled to $${SCALE:-2}.$(NC)"

.PHONY: seed-indexes
seed-indexes: ## Create MongoDB indexes (run once after `make up`)
	uv run python scripts/seed_mongo_indexes.py

.PHONY: down
down: ## Stop the stack (preserves volumes)
	$(COMPOSE_FULL) down

.PHONY: nuke
nuke: ## Stop AND delete all data volumes (DANGER)
	@echo "$(RED)This will delete all data. Press Ctrl-C to abort.$(NC)"
	@sleep 3
	$(COMPOSE_FULL) down -v
	@echo "$(GREEN)Stack nuked.$(NC)"

.PHONY: restart
restart: ## Restart core stack
	$(COMPOSE) restart

# -----------------------------------------------------------------------------
# Observability of the stack itself
# -----------------------------------------------------------------------------
.PHONY: ps
ps: ## Show running services and health
	$(COMPOSE_FULL) ps

.PHONY: logs
logs: ## Tail logs for all services (Ctrl-C to exit)
	$(COMPOSE_FULL) logs -f --tail=50

.PHONY: logs-mongo
logs-mongo: ## Tail mongo logs
	$(COMPOSE) logs -f mongo

.PHONY: logs-kafka
logs-kafka: ## Tail kafka logs
	$(COMPOSE) logs -f kafka

.PHONY: wait
wait: ## Block until all healthchecks pass
	@echo "Waiting for services to be healthy..."
	@python3 scripts/healthcheck.py || (echo "$(RED)Some services failed to become healthy.$(NC)" && $(MAKE) --no-print-directory ps && exit 1)

# -----------------------------------------------------------------------------
# Stack inspection
# -----------------------------------------------------------------------------
.PHONY: mongo-shell
mongo-shell: ## Open a mongosh shell against the replica set
	docker exec -it trendstorm-mongo mongosh \
		--username root --password rootpass --authenticationDatabase admin

.PHONY: redis-cli
redis-cli: ## Open a redis-cli against the local Redis
	docker exec -it trendstorm-redis redis-cli

.PHONY: kafka-topics
kafka-topics: ## List Kafka topics
	docker exec -it trendstorm-kafka kafka-topics --bootstrap-server kafka:9092 --list

.PHONY: kafka-describe
kafka-describe: ## Describe all Kafka topics (partitions, configs)
	docker exec -it trendstorm-kafka kafka-topics --bootstrap-server kafka:9092 --describe

.PHONY: ollama-list
ollama-list: ## List installed Ollama models
	docker exec -it trendstorm-ollama ollama list

# -----------------------------------------------------------------------------
# Quick checks
# -----------------------------------------------------------------------------
.PHONY: check
check: ## Full health check: containers, replica set, topics, models
	@python3 scripts/healthcheck.py --verbose

.PHONY: smoke
smoke: ## Tiny end-to-end sanity check (Mongo write/read, Kafka produce/consume)
	@python3 scripts/smoke_test.py

# -----------------------------------------------------------------------------
# Python application
# -----------------------------------------------------------------------------
.PHONY: install
install: ## Install Python dependencies via uv
	uv sync --all-groups

.PHONY: install-prod
install-prod: ## Install runtime deps only (no dev/agents/llm/rag groups)
	uv sync --no-dev

.PHONY: run
run: ## Run the API locally (requires `make up` first)
	uv run uvicorn trendstorm.api.main:app --host 0.0.0.0 --port 8080 --reload

.PHONY: run-dev
run-dev: ## Run with pretty console logging (overrides .env)
	APP__LOG_FORMAT=console APP__LOG_LEVEL=DEBUG \
		uv run uvicorn trendstorm.api.main:app --host 0.0.0.0 --port 8080 --reload

.PHONY: run-worker
run-worker: ## Run the orchestrator worker locally
	uv run python -m trendstorm.orchestration.workers.orchestrator_worker

.PHONY: run-worker-dev
run-worker-dev: ## Run the worker with pretty console logging
	APP__LOG_FORMAT=console APP__LOG_LEVEL=DEBUG \
		uv run python -m trendstorm.orchestration.workers.orchestrator_worker

.PHONY: worker-review-timeout
worker-review-timeout: ## Run the review timeout sweeper worker locally
	uv run python -m trendstorm.orchestration.workers.review_timeout_worker

# -----------------------------------------------------------------------------
# Python SDK
# -----------------------------------------------------------------------------
.PHONY: sdk-install
sdk-install: ## Install trendstorm-shared + SDK in editable mode
	pip install -e packages/trendstorm-shared && \
	pip install -e "sdk/python[dev]"

.PHONY: sdk-test
sdk-test: ## Run SDK unit tests (pure, no network I/O)
	cd sdk/python && pytest tests/unit -m unit -v

.PHONY: sdk-test-integration
sdk-test-integration: ## Run SDK integration tests (requires TRENDSTORM_API_KEY + make up-app)
	cd sdk/python && pytest tests/integration -m integration -v

.PHONY: sdk-docs
sdk-docs: ## Build SDK docs locally (output: sdk/python/site/)
	cd sdk/python && mkdocs build --config-file docs/mkdocs.yml

.PHONY: sdk-docs-serve
sdk-docs-serve: ## Serve SDK docs with live reload
	cd sdk/python && mkdocs serve --config-file docs/mkdocs.yml

# -----------------------------------------------------------------------------
# Dashboard (web/dashboard)
# -----------------------------------------------------------------------------
DASHBOARD_DIR := web/dashboard

.PHONY: dashboard-install
dashboard-install: ## Install dashboard npm dependencies
	cd $(DASHBOARD_DIR) && npm install

.PHONY: dashboard-dev
dashboard-dev: ## Start dashboard Vite dev server (port 5173, proxies /v1 to :8080)
	cd $(DASHBOARD_DIR) && npm run dev

.PHONY: dashboard-build
dashboard-build: ## Production build → web/dashboard/dist/
	cd $(DASHBOARD_DIR) && npm run build

.PHONY: dashboard-test
dashboard-test: ## Run Vitest unit tests
	cd $(DASHBOARD_DIR) && npm test

.PHONY: dashboard-test-e2e
dashboard-test-e2e: ## Run Playwright E2E tests (requires PLAYWRIGHT_BASE_URL or preview server)
	cd $(DASHBOARD_DIR) && npm run test:e2e

.PHONY: dashboard-codegen
dashboard-codegen: ## Regenerate src/api/types.generated.ts from live API
	cd $(DASHBOARD_DIR) && npm run codegen

.PHONY: dashboard-codegen-check
dashboard-codegen-check: ## CI gate — fail if types.generated.ts is stale
	cd $(DASHBOARD_DIR) && npm run codegen:check

.PHONY: dashboard-lint
dashboard-lint: ## ESLint + typecheck for the dashboard
	cd $(DASHBOARD_DIR) && npm run typecheck && npm run lint

.PHONY: helm-lint-dashboard
helm-lint-dashboard: ## Lint the dashboard Helm chart
	@helm lint helm/dashboard

.PHONY: lint
lint: ## Run ruff lint
	uv run ruff check src/ tests/

.PHONY: format
format: ## Run ruff format
	uv run ruff format src/ tests/

.PHONY: typecheck
typecheck: ## Run mypy strict type checking
	uv run mypy src/trendstorm

.PHONY: test
test: ## Run unit tests only (no infrastructure required)
	uv run pytest tests/unit -m unit

.PHONY: test-integration
test-integration: ## Run integration tests (requires `make up`)
	uv run pytest tests/integration -m integration

.PHONY: test-all
test-all: ## Run all tests
	uv run pytest

.PHONY: check-all
check-all: lint typecheck test ## Run all quality gates locally before pushing

# -----------------------------------------------------------------------------
# Evaluation harness
# -----------------------------------------------------------------------------
.PHONY: eval-fast
eval-fast: ## Run deterministic evaluators over golden dataset (no LLM keys required)
	uv run python scripts/run_eval.py --suite fast

.PHONY: eval-full
eval-full: ## Run all evaluators including LLM panel judges (requires API keys)
	uv run python scripts/run_eval.py --suite full

.PHONY: eval-check
eval-check: ## Fail if latest eval artifact has threshold violations (CI gate)
	@python3 -c " \
import glob, json, sys; \
files = sorted(glob.glob('artifacts/eval-*.json')); \
[sys.exit('No eval artifacts found — run make eval-fast first') if not files else None]; \
data = json.load(open(files[-1])); \
violations = data.get('threshold_violations', []); \
[print(f'  FAIL: {v}') for v in violations]; \
sys.exit(1) if violations else print(f'PASS — {files[-1]}') \
"

# -----------------------------------------------------------------------------
# Helm
# -----------------------------------------------------------------------------
.PHONY: helm-lint
helm-lint: ## Lint the Helm chart
	@helm lint helm/trendstorm

.PHONY: helm-template
helm-template: ## Render Helm templates (preview first 200 lines)
	@helm template trendstorm helm/trendstorm --debug | head -200

# -----------------------------------------------------------------------------
# Cleanup
# -----------------------------------------------------------------------------
.PHONY: clean
clean: ## Remove Docker volumes (destructive — destroys all data)
	@echo "$(RED)This will delete all data volumes. Press Ctrl-C to abort.$(NC)"
	@sleep 3
	$(COMPOSE_FULL) down -v --remove-orphans
	@echo "$(GREEN)Volumes removed.$(NC)"

.PHONY: prune
prune: ## Remove stopped containers and dangling images
	docker container prune -f
	docker image prune -f
