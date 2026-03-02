#!/bin/bash
# E-FINDER — Share Relationship Map
# ====================================
# Generates the relationship map HTML from live MongoDB data,
# then serves it via Cloudflare Tunnel for public sharing.
#
# Usage:
#   cd ~/efinder
#   bash _pipeline_output/start_dashboard.sh

set -e

echo ""
echo "============================================================"
echo "  E-FINDER — Relationship Map Sharing"
echo "============================================================"
echo ""

# ─── Load env ───
if [ -f .env ]; then
    export $(cat .env | grep -v '^#' | xargs)
    echo "  ✓ Environment loaded"
else
    echo "  ✗ No .env file found. Run from ~/efinder/"
    exit 1
fi

# ─── Activate venv ───
if [ -d .venv ]; then
    source .venv/bin/activate
    echo "  ✓ Virtual environment activated"
else
    echo "  ✗ No .venv found. Run: python3 -m venv .venv"
    exit 1
fi

# ─── Generate fresh map from MongoDB ───
echo "  Generating relationship map from live data..."
python3 _pipeline_output/generate_network_map.py
echo ""

# ─── Install cloudflared if needed ───
if ! command -v cloudflared &> /dev/null; then
    echo "  Installing cloudflared..."
    ARCH=$(dpkg --print-architecture 2>/dev/null || echo "amd64")
    curl -sL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${ARCH}" -o /tmp/cloudflared
    chmod +x /tmp/cloudflared
    sudo mv /tmp/cloudflared /usr/local/bin/cloudflared
    echo "  ✓ cloudflared installed"
else
    echo "  ✓ cloudflared already installed"
fi

echo ""
echo "============================================================"
echo "  Serving the relationship map..."
echo "  Press Ctrl+C to stop."
echo "============================================================"
echo ""

# ─── Serve from the serve/ directory (contains index.html) ───
python3 -m http.server 5000 --directory serve &
HTTP_PID=$!
sleep 2

# ─── Start Cloudflare Tunnel ───
echo ""
echo "  ──────────────────────────────────────────────────────"
echo "  LOOK FOR THE PUBLIC URL BELOW"
echo "  (https://xxx.trycloudflare.com)"
echo ""
echo "  Send that URL to your business partner!"
echo "  They open it → full-screen relationship map."
echo "  ──────────────────────────────────────────────────────"
echo ""

cloudflared tunnel --url http://localhost:5000

# Cleanup on exit
kill $HTTP_PID 2>/dev/null
