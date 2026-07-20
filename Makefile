PY ?= python3
PIP ?= $(PY) -m pip

.PHONY: help install dev test lint type proto up down migrate run-api run-runtime run-worker fmt

help:
	@echo "Targets:"
	@echo "  install    Install dev deps"
	@echo "  test       Run the test suite"
	@echo "  e2e        Run the end-to-end platform test"
	@echo "  chat       Interactive REPL with a real LLM-backed agent"
	@echo "  explain    Walk through what each system does"
	@echo "  lint       Run ruff/black/bandit"
	@echo "  type       Run mypy"
	@echo "  proto      Regenerate gRPC stubs"
	@echo "  up         Start dev stack (postgres, redis, minio, jaeger, prometheus, grafana)"
	@echo "  down       Stop the dev stack"
	@echo "  migrate    Apply DB migrations"
	@echo "  run-api    Start the control-plane REST API"
	@echo "  run-runtime Start a single Automaton runtime worker"
	@echo "  fmt        Format with black + ruff"

install:
	$(PIP) install -r requirements.txt

test:
	$(PY) -m pytest -q

# End-to-end test: exercises the full platform with a real
# LLM-driven loop, the audit chain, dashboard, treasury, and
# inbox all wired together. This is the proof that the 10
# systems work as one organism.
e2e:
	$(PY) -m pytest tests/integration/test_e2e_platform.py -v

# Interactive REPL chat with a real LLM-backed agent. Requires
# at least one of: OPENAI_API_KEY, ANTHROPIC_API_KEY,
# OPENROUTER_API_KEY, or a local Ollama daemon.
chat:
	$(PY) scripts/chat.py

# Walk through the platform's major systems and explain what
# each one does. Useful for first-time readers.
explain:
	@echo "Helix platform — what each system does:"
	@echo ""
	@echo "  services/treasury/    — HelixTreasury: real on-chain money, credit ledger, auto-topup"
	@echo "  services/payments/    — x402: HTTP-native payment protocol (402 + payment-required)"
	@echo "  services/messaging/   — Inbox: agent-to-agent async message queue with state machine"
	@echo "  services/conversation/— Conversation history: token-budgeted, summarizable, format-agnostic"
	@echo "  services/bootstrap/   — Self-bootstrap: first-run wizard, default skills + memory"
	@echo "  services/planning/    — Plan mode: file-backed TODO.md plans, step transitions"
	@echo "  services/heartbeat/   — Heartbeat: long-running health monitor, stuck-message recovery"
	@echo "  services/soul/        — SOUL.md: agent's self-authored identity document"
	@echo "  services/self_mod/    — Self-modification: agent changes its own code, with safety rails"
	@echo "  services/dashboard/   — Dashboard: in-process event bus, WebSocket stream"
	@echo "  services/state/       — SqliteStore: hash-chained audit log, in-memory CRUD"
	@echo "  runtime/loop/         — AutomatonLoop: 14-stage tick, observation → reason → plan → act"
	@echo "  services/control_plane/— FastAPI control plane: REST + WebSocket routes"
	@echo "  core/policy/          — Constitution: immutable policy, Law 1-8"
	@echo "  core/security/        — Injection defense, signing, vault"
	@echo "  core/types/           — Money (micro-USDC), identifiers, Automaton types"
	@echo ""
	@echo "Tests:        477 across 47 files (~30s)"
	@echo "Docs:         22 architecture docs in docs/architecture/"
	@echo "Demos:        scripts/smoke.py, x402_demo.py, inbox_demo.py, dashboard_demo.py"
	@echo "              (chat.py requires a real LLM API key)"
	@echo ""
	@echo "Quick start:  make test       # run the test suite"
	@echo "              make e2e        # run the e2e test"
	@echo "              make chat       # chat with a real agent"

lint:
	$(PY) -m ruff check core runtime services api
	$(PY) -m bandit -r core runtime services api -lll

type:
	$(PY) -m mypy --strict core runtime services api

fmt:
	$(PY) -m black core runtime services api
	$(PY) -m ruff check --fix core runtime services api

proto:
	$(PY) -m grpc_tools.protoc -I schemas/proto \
		--python_out=api/grpc \
		--grpc_python_out=api/grpc \
		schemas/proto/automata.proto

up:
	docker compose -f infra/docker/docker-compose.yml up -d

down:
	docker compose -f infra/docker/docker-compose.yml down

migrate:
	@echo "Apply migrations in storage/postgres/migrations to the configured DB."

run-api:
	$(PY) -m services.control_plane.api

run-runtime:
	$(PY) -m services.control_plane.worker --automaton-id atm_demo
