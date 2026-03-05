#!/bin/bash
# ── E-FINDER — Docker entrypoint ─────────────────────────────────────────────
# Routes the CMD argument to the correct Python script / mode.
#
# CMD values:
#   dashboard          Start the Flask dashboard (default)
#   swarm [args...]    Run the swarm coordinator CLI
#   extract [args...]  Run the entity extraction pipeline
#   gliner [args...]   Run the GLiNER secondary NER pass
#   migrate [args...]  Run the MongoDB → Weaviate migration
#   shell              Drop into bash (for debugging)
# ─────────────────────────────────────────────────────────────────────────────
set -e

MODE="${1:-dashboard}"
shift || true   # remaining args passed through to the Python script

echo ""
echo "╔══════════════════════════════════════════════════════════╗"
echo "║            E-FINDER  ·  $(date -u '+%Y-%m-%d %H:%M:%S UTC')             ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo "  Mode: $MODE"
echo ""

# ── Validate required env vars ────────────────────────────────────────────────
check_env() {
    local var="$1"
    if [ -z "${!var}" ]; then
        echo "  ERROR: $var is not set. Add it to your .env file."
        exit 1
    fi
}

case "$MODE" in
    dashboard|swarm|extract|gliner|migrate)
        check_env MONGODB_URI
        ;;
esac

# ── Wait for Weaviate (if WEAVIATE_URL is set and mode needs it) ──────────────
wait_for_weaviate() {
    if [ -z "$WEAVIATE_URL" ]; then
        return
    fi
    echo "  Waiting for Weaviate at $WEAVIATE_URL ..."
    local max_attempts=30
    local attempt=0
    until curl -sf "${WEAVIATE_URL}/v1/.well-known/ready" > /dev/null 2>&1; do
        attempt=$((attempt + 1))
        if [ $attempt -ge $max_attempts ]; then
            echo "  WARNING: Weaviate not ready after ${max_attempts}s — continuing anyway"
            return
        fi
        sleep 2
    done
    echo "  ✓ Weaviate ready"
}

# ── Route to service ──────────────────────────────────────────────────────────
case "$MODE" in

    dashboard)
        echo "  Starting Flask dashboard on port ${DASHBOARD_PORT:-5000} ..."
        echo ""
        exec gunicorn \
            --bind "0.0.0.0:${DASHBOARD_PORT:-5000}" \
            --workers 1 \
            --threads 8 \
            --timeout 300 \
            --log-level info \
            --access-logfile - \
            --error-logfile - \
            "dashboard:app"
        ;;

    swarm)
        wait_for_weaviate
        echo "  Running swarm with args: $*"
        echo ""
        exec python3 swarm.py "$@"
        ;;

    extract)
        echo "  Running entity extraction pipeline with args: $*"
        echo ""
        exec python3 extract_entities.py "$@"
        ;;

    gliner)
        wait_for_weaviate
        echo "  Running GLiNER extraction pass with args: $*"
        echo ""
        exec python3 gliner_reextract.py "$@"
        ;;

    migrate)
        wait_for_weaviate
        echo "  Running MongoDB → Weaviate migration with args: $*"
        echo ""
        exec python3 weaviate_setup.py "$@"
        ;;

    shell)
        echo "  Dropping into bash shell ..."
        exec bash
        ;;

    *)
        echo "  ERROR: Unknown mode '$MODE'"
        echo ""
        echo "  Valid modes: dashboard | swarm | extract | gliner | migrate | shell"
        exit 1
        ;;
esac
