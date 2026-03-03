# ── E-FINDER — Makefile ───────────────────────────────────────────────────────
# Convenience wrappers around docker compose.
# Usage: make <target>
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help up down build logs shell \
        migrate migrate-test \
        gliner gliner-test \
        extract extract-test \
        swarm network-map \
        ps clean

# ── Default ───────────────────────────────────────────────────────────────────
help:
	@echo ""
	@echo "  E-FINDER — Docker commands"
	@echo "  ──────────────────────────────────────────────────────"
	@echo "  make up              Start Weaviate + dashboard (detached)"
	@echo "  make down            Stop all services"
	@echo "  make build           Rebuild the app image"
	@echo "  make logs            Tail all service logs"
	@echo "  make ps              Show running containers"
	@echo ""
	@echo "  make migrate-test    Migrate 100 docs → Weaviate (test)"
	@echo "  make migrate         Migrate full corpus → Weaviate"
	@echo ""
	@echo "  make gliner-test     GLiNER NER pass on 500 docs (test)"
	@echo "  make gliner          GLiNER NER pass on full corpus"
	@echo ""
	@echo "  make extract-test    Extract entities from 50 docs (test)"
	@echo "  make extract         Extract entities (resume from last position)"
	@echo ""
	@echo "  make network-map     Build co-occurrence network in MongoDB"
	@echo "  make swarm Q=\"...\"   Run a swarm investigation question"
	@echo "  make shell           Open a bash shell inside the app container"
	@echo "  make clean           Remove volumes and containers"
	@echo ""

# ── Core lifecycle ────────────────────────────────────────────────────────────
up:
	docker compose up -d
	@echo ""
	@echo "  ✓ Dashboard: http://localhost:$${DASHBOARD_PORT:-5000}"
	@echo "  ✓ Weaviate:  http://localhost:8080"
	@echo ""

down:
	docker compose down

build:
	docker compose build --no-cache

logs:
	docker compose logs -f

ps:
	docker compose ps

# ── Migration ─────────────────────────────────────────────────────────────────
migrate-test:
	docker compose run --rm migrate --setup
	docker compose run --rm migrate --migrate --limit 100
	docker compose run --rm migrate --stats

migrate:
	docker compose run --rm migrate --setup
	docker compose run --rm migrate --migrate
	docker compose run --rm migrate --stats

# ── GLiNER ────────────────────────────────────────────────────────────────────
gliner-test:
	docker compose run --rm gliner --limit 500

gliner:
	docker compose run --rm gliner --update-weaviate
	docker compose run --rm gliner --stats

# ── Entity extraction ─────────────────────────────────────────────────────────
extract-test:
	docker compose run --rm extract --limit 50

extract:
	docker compose run --rm extract --resume

# ── Swarm ─────────────────────────────────────────────────────────────────────
network-map:
	docker compose run --rm swarm --agent network_mapper

swarm:
ifndef Q
	$(error Q is not set. Usage: make swarm Q="your question here")
endif
	docker compose run --rm swarm -q "$(Q)"

# ── Debug ─────────────────────────────────────────────────────────────────────
shell:
	docker compose run --rm --entrypoint bash dashboard

# ── Cleanup ───────────────────────────────────────────────────────────────────
clean:
	docker compose down -v --remove-orphans
	docker image rm -f efinder-app 2>/dev/null || true
	@echo "  ✓ Volumes, containers, and image removed"
