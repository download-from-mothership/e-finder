# ── E-FINDER — Python service image ──────────────────────────────────────────
# Runs: dashboard (Flask), swarm CLI, extract_entities, weaviate_setup,
#       gliner_reextract, geospatial_agent, intelligence_orchestrator
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim-bookworm

# System deps: poppler (PDF text), curl (healthchecks), git
RUN apt-get update && apt-get install -y --no-install-recommends \
        poppler-utils \
        curl \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY . .

# ── Entrypoint ────────────────────────────────────────────────────────────────
# Default: start the dashboard. Override CMD to run other services.
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["dashboard"]
