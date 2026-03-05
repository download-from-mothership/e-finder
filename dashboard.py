#!/usr/bin/env python3
"""
E-FINDER — Live Investigation Dashboard
=========================================
Flask app serving the full React dashboard UI with live MongoDB data.
Designed to run on VPS and be shared via Cloudflare Tunnel.

Usage:
  cd ~/efinder
  source .venv/bin/activate
  export $(cat .env | xargs)
  python3 _pipeline_output/dashboard.py

  # Then in another tmux pane:
  ~/efinder/cloudflared tunnel --url http://localhost:5000
"""

import json
import os
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime

try:
    from flask import Flask, jsonify, request, Response
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "flask", "--break-system-packages", "-q"])
    from flask import Flask, jsonify, request, Response

try:
    from pymongo import MongoClient
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "pymongo", "--break-system-packages", "-q"])
    from pymongo import MongoClient

# ─── Config ───────────────────────────────────────────────────────────
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = "doj_investigation"
PORT = int(os.environ.get("DASHBOARD_PORT", 5000))

MAX_NODES = 300
MAX_EDGES = 2000
MIN_EDGE_WEIGHT = 2

# Path for the persistent on-disk network snapshot (survives restarts)
# Stored next to dashboard.py so it persists across container restarts
_NETWORK_SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "network_snapshot.json"
)

# ─── App Setup ────────────────────────────────────────────────────────
app = Flask(__name__)

_db = None

# ─── Simple TTL cache ────────────────────────────────────────────────
_cache = {}  # key -> (value, expires_at)

def _cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None

def _cache_set(key, value, ttl=600):
    _cache[key] = (value, time.time() + ttl)


# ─── Disk-persistent network snapshot ───────────────────────────────
def _load_network_snapshot():
    """Load the pre-built network graph from disk. Returns None if absent."""
    try:
        if os.path.exists(_NETWORK_SNAPSHOT_PATH):
            with open(_NETWORK_SNAPSHOT_PATH, "r") as f:
                data = json.load(f)
            import logging
            log = logging.getLogger(__name__)
            log.info(
                "Loaded network snapshot from disk (%d nodes, %d edges, built %s)",
                len(data.get("nodes", [])),
                len(data.get("edges", [])),
                data.get("built_at", "unknown"),
            )
            return data
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to load network snapshot: %s", exc)
    return None


def _save_network_snapshot(payload):
    """Persist the network graph to disk with a timestamp."""
    try:
        payload["built_at"] = datetime.utcnow().isoformat() + "Z"
        with open(_NETWORK_SNAPSHOT_PATH, "w") as f:
            json.dump(payload, f, separators=(",", ":"))
        import logging
        logging.getLogger(__name__).info(
            "Network snapshot saved to disk (%d nodes, %d edges)",
            len(payload.get("nodes", [])),
            len(payload.get("edges", [])),
        )
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Failed to save network snapshot: %s", exc)


def _build_network_data(db):
    """Run the full MongoDB queries and return {nodes, edges}. Shared by
    the pre-warm thread, the /api/network endpoint, and the refresh endpoint."""
    min_weight = MIN_EDGE_WEIGHT
    max_nodes = MAX_NODES

    edges_raw = list(db["network"].find(
        {"weight": {"$gte": min_weight}},
        {"person1": 1, "person2": 1, "weight": 1, "shared_doc_ids": 1, "_id": 0}
    ).sort("weight", -1))

    node_degree = defaultdict(int)
    node_weighted_degree = defaultdict(int)
    for e in edges_raw:
        node_degree[e["person1"]] += 1
        node_degree[e["person2"]] += 1
        node_weighted_degree[e["person1"]] += e["weight"]
        node_weighted_degree[e["person2"]] += e["weight"]

    top_nodes = sorted(node_weighted_degree.items(), key=lambda x: x[1], reverse=True)[:max_nodes]
    node_set = set(n for n, _ in top_nodes)

    edges = []
    for e in edges_raw:
        if e["person1"] in node_set and e["person2"] in node_set:
            edges.append({
                "source": e["person1"],
                "target": e["person2"],
                "weight": e["weight"],
                "docs": len(e.get("shared_doc_ids", [])),
            })
            if len(edges) >= MAX_EDGES:
                break

    nodes = []
    for name in node_set:
        doc_count = db["entities"].count_documents({"name": name, "entity_type": "person"})
        section_pipeline = [
            {"$match": {"name": name, "entity_type": "person"}},
            {"$group": {"_id": "$section", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 3},
        ]
        sections = [r["_id"] for r in db["entities"].aggregate(section_pipeline)]
        node_type, role_str = _classify_person_role(db, name)
        nodes.append({
            "id": name,
            "doc_count": doc_count,
            "degree": node_degree[name],
            "weighted_degree": node_weighted_degree[name],
            "sections": sections,
            "role": role_str[:150],
            "type": node_type,
        })

    return {"nodes": nodes, "edges": edges}


def _prewarm_cache():
    """Pre-warm expensive caches in a background thread on startup."""
    import logging
    log = logging.getLogger(__name__)
    try:
        time.sleep(3)  # let gunicorn fully start first
        db = get_db()

        # Pre-warm stats (fast)
        result = {
            "total_docs": db["documents"].count_documents({}),
            "extracted_docs": db["documents"].count_documents({"processing_stage": "entities_extracted"}),
            "total_entities": db["entities"].count_documents({}),
            "network_edges": db["network"].count_documents({}),
            "reports": db["reports"].count_documents({}),
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }
        _cache_set("stats", result, ttl=120)
        log.info("Cache pre-warm: stats done")

        # Pre-warm network graph — use disk snapshot if available, otherwise build & save
        cache_key = f"network_{MIN_EDGE_WEIGHT}_{MAX_NODES}"
        snapshot = _load_network_snapshot()
        if snapshot:
            # Disk snapshot exists — load into memory cache instantly (no MongoDB queries)
            _cache_set(cache_key, snapshot, ttl=86400)  # 24 h TTL; refreshed on demand
            log.info("Cache pre-warm: network loaded from disk snapshot")
        else:
            # No snapshot yet — build from MongoDB and persist to disk
            log.info("Cache pre-warm: no network snapshot found, building from MongoDB...")
            network_data = _build_network_data(db)
            _cache_set(cache_key, network_data, ttl=86400)
            _save_network_snapshot(network_data)
            log.info("Cache pre-warm: network graph built and saved (%d nodes, %d edges)",
                     len(network_data["nodes"]), len(network_data["edges"]))
        log.info("Cache pre-warm: network graph done")

        # Pre-warm entity breakdown (fast aggregation)
        eb_pipeline = [
            {"$group": {"_id": "$entity_type", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        eb_result = [{"type": r["_id"], "count": r["count"]}
                     for r in db["entities"].aggregate(eb_pipeline)]
        _cache_set("entity_breakdown", eb_result, ttl=300)
        log.info("Cache pre-warm: entity breakdown done")

        # Pre-warm top-entities (person, limit=12 — the dashboard default)
        te_pipeline = [
            {"$match": {"entity_type": "person"}},
            {"$group": {"_id": "$name", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 12},
        ]
        te_results = list(db["entities"].aggregate(te_pipeline))
        te_enriched = []
        for r in te_results:
            role_type, role_desc = _classify_person_role(db, r["_id"])
            te_enriched.append({"name": r["_id"], "count": r["count"],
                                 "type": role_type, "role": role_desc})
        _cache_set("top_entities_person_12", te_enriched, ttl=300)
        log.info("Cache pre-warm: top-entities done")

        # Pre-warm reports list
        reports = list(db["reports"].find(
            {},
            {"_id": 0, "question": 1, "executive_summary": 1,
             "key_findings": 1, "meta": 1, "timestamp": 1}
        ).sort("timestamp", -1).limit(20))
        _cache_set("reports", reports, ttl=60)
        log.info("Cache pre-warm: reports done")

    except Exception as exc:
        log.warning("Cache pre-warm failed: %s", exc)


# Start cache pre-warming in background when module loads (works with gunicorn)
_prewarm_thread = threading.Thread(target=_prewarm_cache, daemon=True)
_prewarm_thread.start()


def get_db():
    global _db
    if _db is None:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
        _db = client[DATABASE_NAME]
    return _db


# ─── Helpers ─────────────────────────────────────────────────────────

def _classify_person_role(db, name):
    """Classify a person's role based on their context fields in the entities collection."""
    roles = list(db["entities"].find(
        {"name": name, "entity_type": "person", "context": {"$ne": ""}},
        {"context": 1, "_id": 0}
    ).limit(5))
    role_str = "; ".join(r.get("context", "")[:80] for r in roles if r.get("context"))
    role_lower = role_str.lower()
    if any(w in role_lower for w in ["attorney", "counsel", "lawyer", "judge", "magistrate"]):
        return "legal", role_str[:150]
    elif any(w in role_lower for w in ["victim", "plaintiff", "doe", "minor", "accuser"]):
        return "victim", role_str[:150]
    elif any(w in role_lower for w in ["defendant", "accused", "perpetrator", "co-conspirator"]):
        return "defendant", role_str[:150]
    elif any(w in role_lower for w in ["agent", "detective", "officer", "fbi", "investigator"]):
        return "law_enforcement", role_str[:150]
    elif any(w in role_lower for w in ["witness", "deponent"]):
        return "witness", role_str[:150]
    return "other", role_str[:150]


# ─── API Routes ───────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    cached = _cache_get("stats")
    if cached:
        return jsonify(cached)
    db = get_db()
    result = {
        "total_docs": db["documents"].count_documents({}),
        "extracted_docs": db["documents"].count_documents({"processing_stage": "entities_extracted"}),
        "total_entities": db["entities"].count_documents({}),
        "network_edges": db["network"].count_documents({}),
        "reports": db["reports"].count_documents({}),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }
    _cache_set("stats", result, ttl=120)  # cache for 2 minutes
    return jsonify(result)


@app.route("/api/network")
def api_network():
    """Serve the relationship network graph.

    Priority order:
      1. In-memory TTL cache (fastest — microseconds)
      2. Disk snapshot (fast — milliseconds, survives restarts)
      3. Build from MongoDB (slow — ~30-60 s, only on first-ever run)
    """
    # Only the default parameters are covered by the persistent snapshot.
    # Custom min_weight / max_nodes still fall through to a live build.
    min_weight = int(request.args.get("min_weight", MIN_EDGE_WEIGHT))
    max_nodes = int(request.args.get("max_nodes", MAX_NODES))
    cache_key = f"network_{min_weight}_{max_nodes}"

    # 1. In-memory cache hit
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)

    # 2. Disk snapshot (default params only)
    if min_weight == MIN_EDGE_WEIGHT and max_nodes == MAX_NODES:
        snapshot = _load_network_snapshot()
        if snapshot:
            _cache_set(cache_key, snapshot, ttl=86400)
            return jsonify(snapshot)

    # 3. Build from MongoDB (first run or custom params)
    db = get_db()
    result = _build_network_data(db)
    _cache_set(cache_key, result, ttl=86400)
    # Persist to disk if this is the default view
    if min_weight == MIN_EDGE_WEIGHT and max_nodes == MAX_NODES:
        _save_network_snapshot(result)
    return jsonify(result)


@app.route("/api/network/refresh", methods=["POST"])
def api_network_refresh():
    """Force-rebuild the network snapshot from MongoDB and update the disk cache.
    Call this after ingesting new documents into the corpus.
    """
    import logging
    log = logging.getLogger(__name__)

    def _do_refresh():
        try:
            db = get_db()
            log.info("Network refresh: rebuilding from MongoDB...")
            data = _build_network_data(db)
            cache_key = f"network_{MIN_EDGE_WEIGHT}_{MAX_NODES}"
            _cache_set(cache_key, data, ttl=86400)
            _save_network_snapshot(data)
            log.info("Network refresh: complete (%d nodes, %d edges)",
                     len(data["nodes"]), len(data["edges"]))
        except Exception as exc:
            log.error("Network refresh failed: %s", exc)

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()
    return jsonify({"status": "rebuilding", "message": "Network snapshot rebuild started in background. Check logs for completion."})


@app.route("/api/reports")
def api_reports():
    cached = _cache_get("reports")
    if cached:
        return jsonify(cached)
    db = get_db()
    reports = list(db["reports"].find(
        {},
        {"_id": 0, "question": 1, "executive_summary": 1, "key_findings": 1,
         "meta": 1, "timestamp": 1}
    ).sort("timestamp", -1).limit(20))
    _cache_set("reports", reports, ttl=60)  # cache for 1 minute
    return jsonify(reports)


@app.route("/api/top-entities")
def api_top_entities():
    entity_type = request.args.get("type", "person")
    limit = int(request.args.get("limit", 25))
    cache_key = f"top_entities_{entity_type}_{limit}"
    cached = _cache_get(cache_key)
    if cached:
        return jsonify(cached)
    db = get_db()
    pipeline = [
        {"$match": {"entity_type": entity_type}},
        {"$group": {"_id": "$name", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    results = list(db["entities"].aggregate(pipeline))
    enriched = []
    for r in results:
        entry = {"name": r["_id"], "count": r["count"]}
        if entity_type == "person":
            role_type, role_desc = _classify_person_role(db, r["_id"])
            entry["type"] = role_type
            entry["role"] = role_desc
        else:
            entry["type"] = entity_type
        enriched.append(entry)
    _cache_set(cache_key, enriched, ttl=300)  # cache for 5 minutes
    return jsonify(enriched)


@app.route("/api/entity-breakdown")
def api_entity_breakdown():
    cached = _cache_get("entity_breakdown")
    if cached:
        return jsonify(cached)
    db = get_db()
    pipeline = [
        {"$group": {"_id": "$entity_type", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
    ]
    results = list(db["entities"].aggregate(pipeline))
    result = [{"type": r["_id"], "count": r["count"]} for r in results]
    _cache_set("entity_breakdown", result, ttl=300)  # cache for 5 minutes
    return jsonify(result)


# ─── Cache invalidation ─────────────────────────────────────────────
def _invalidate_report_cache():
    """Called after a new investigation completes to bust the reports cache."""
    _cache.pop("reports", None)
    _cache.pop("stats", None)


# ─── Investigation state (in-memory) ─────────────────────────────────
_investigations = {}  # job_id -> {status, result, error}

@app.route("/api/investigate", methods=["POST"])
def api_investigate():
    """Start an investigation via the swarm coordinator."""
    data = request.get_json()
    question = data.get("question", "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    job_id = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    _investigations[job_id] = {"status": "running", "result": None, "error": None}

    def run_investigation(jid, q):
        try:
            # Import swarm coordinator + anthropic
            swarm_dir = os.path.dirname(os.path.abspath(__file__))
            if swarm_dir not in sys.path:
                sys.path.insert(0, swarm_dir)
            import anthropic
            from swarm import Coordinator

            # Get db handle and create Claude client
            db = get_db()
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
            claude_client = anthropic.Anthropic(api_key=api_key)

            coord = Coordinator(db=db, claude_client=claude_client)
            result = coord.investigate(q)
            _investigations[jid]["status"] = "complete"
            _investigations[jid]["result"] = result
            _invalidate_report_cache()  # bust cache so new report appears immediately
        except Exception as e:
            _investigations[jid]["status"] = "error"
            _investigations[jid]["error"] = str(e)

    t = threading.Thread(target=run_investigation, args=(job_id, question))
    t.daemon = True
    t.start()

    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/investigate/<job_id>")
def api_investigate_status(job_id):
    """Poll investigation status."""
    job = _investigations.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404

    resp = {"job_id": job_id, "status": job["status"]}
    if job["status"] == "complete" and job["result"]:
        r = job["result"]
        resp["result"] = {
            "question": r.get("question", ""),
            "executive_summary": r.get("executive_summary", ""),
            "key_findings": r.get("key_findings", []),
            "meta": r.get("meta", {}),
        }
    elif job["status"] == "error":
        resp["error"] = job["error"]
    return jsonify(resp)


# ─── Timeline state (in-memory) ─────────────────────────────────────
_timelines = {}  # job_id -> {status, result, error}


@app.route("/api/timeline", methods=["POST"])
def api_timeline():
    """Start a timeline build for a subject via TimelineBuilderAgent."""
    data = request.get_json()
    subject = (data.get("subject") or "").strip()
    if not subject:
        return jsonify({"error": "No subject provided"}), 400

    # Return cached result if available (keyed by lower-case subject)
    cache_key = f"timeline_{subject.lower()}"
    cached = _cache_get(cache_key)
    if cached:
        job_id = "cached_" + cache_key
        _timelines[job_id] = {"status": "complete", "result": cached, "error": None}
        return jsonify({"job_id": job_id, "status": "complete"})

    job_id = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")
    _timelines[job_id] = {"status": "running", "result": None, "error": None}

    def run_timeline(jid, subj):
        try:
            swarm_dir = os.path.dirname(os.path.abspath(__file__))
            if swarm_dir not in sys.path:
                sys.path.insert(0, swarm_dir)
            import anthropic
            from swarm import TimelineBuilderAgent

            db = get_db()
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY not set in environment")
            claude_client = anthropic.Anthropic(api_key=api_key)

            agent = TimelineBuilderAgent(db=db, claude_client=claude_client)
            agent_result = agent.run({"subject": subj})

            # Extract the timeline finding
            timeline_data = None
            for finding in (agent_result.findings or []):
                if isinstance(finding, dict) and finding.get("type") == "timeline":
                    timeline_data = finding.get("data", {})
                    break

            if timeline_data is None:
                # Fallback: wrap raw findings
                timeline_data = {
                    "subject": subj,
                    "timeline": [],
                    "date_range": "unknown",
                    "gaps": [],
                    "patterns": [],
                    "total_documents": 0,
                    "error": agent_result.error or "No timeline data returned",
                    "raw_findings": agent_result.findings,
                }

            timeline_data["_agent_duration"] = round(agent_result.duration_seconds or 0, 1)
            _timelines[jid]["status"] = "complete"
            _timelines[jid]["result"] = timeline_data
            # Cache for 10 minutes
            _cache_set(f"timeline_{subj.lower()}", timeline_data, ttl=600)
        except Exception as exc:
            _timelines[jid]["status"] = "error"
            _timelines[jid]["error"] = str(exc)

    t = threading.Thread(target=run_timeline, args=(job_id, subject))
    t.daemon = True
    t.start()
    return jsonify({"job_id": job_id, "status": "running"})


@app.route("/api/timeline/<job_id>")
def api_timeline_status(job_id):
    """Poll timeline build status."""
    job = _timelines.get(job_id)
    if not job:
        return jsonify({"error": "Unknown job"}), 404
    resp = {"job_id": job_id, "status": job["status"]}
    if job["status"] == "complete" and job["result"]:
        resp["result"] = job["result"]
    elif job["status"] == "error":
        resp["error"] = job["error"]
    return jsonify(resp)


# ─── Main Page ────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(DASHBOARD_HTML, mimetype="text/html")


# ─── Full React Dashboard HTML ───────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>E-FINDER — Investigation Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react/18.2.0/umd/react.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.2.0/umd/react-dom.production.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.23.9/babel.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0f; color: #e0e0e0; }
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: #0a0a0f; }
  ::-webkit-scrollbar-thumb { background: #333; border-radius: 3px; }
  .stat-number { font-size: 28px; font-weight: 700; color: #8b5cf6; }
  .brand { font-size: 15px; font-weight: 700; color: #8b5cf6; letter-spacing: 1px; }
  .tab-active { background: rgba(255,255,255,0.08); color: #e0e0e0; }
  .tab-inactive { color: #666; }
  .tab-inactive:hover { color: #aaa; }
  .card { background: rgba(15,15,25,0.8); border: 1px solid #1a1a2e; border-radius: 10px; }
  .card:hover { border-color: #2a2a4e; }
  .legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .bar-fill { transition: width 1s ease; }
  .finding-card { background: rgba(139,92,246,0.06); border-left: 3px solid #8b5cf6; }
  #network-svg { width: 100%; height: 100%; }

  /* D3 tooltip */
  #d3-tooltip {
    position: fixed; display: none; background: rgba(15,15,25,0.97);
    border: 1px solid #8b5cf6; border-radius: 8px; padding: 12px 16px;
    font-size: 13px; max-width: 350px; z-index: 9999; pointer-events: none;
  }
  #d3-tooltip .tt-name { font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 6px; }
  #d3-tooltip .tt-meta { color: #999; margin-bottom: 3px; }
  #d3-tooltip .tt-role { color: #8b5cf6; font-style: italic; margin-top: 6px; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
</style>
</head>
<body>

<div id="d3-tooltip">
  <div class="tt-name"></div>
  <div class="tt-meta tt-docs"></div>
  <div class="tt-meta tt-conns"></div>
  <div class="tt-meta tt-sections"></div>
  <div class="tt-role"></div>
</div>

<div id="root"></div>

<script type="text/babel">
const { useState, useEffect, useRef, useCallback } = React;

const TYPE_COLORS = {
  defendant: "#ef4444", legal: "#3b82f6", victim: "#f59e0b",
  law_enforcement: "#10b981", witness: "#a855f7", other: "#6b7280",
};
const ENTITY_COLORS = {
  person: "#8b5cf6", organization: "#3b82f6", location: "#10b981",
  date_event: "#f59e0b", financial: "#ef4444",
};

// ─── Hooks ───
function useFetch(url) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    setLoading(true);
    fetch(url).then(r => r.json()).then(d => { setData(d); setLoading(false); })
      .catch(() => setLoading(false));
  }, [url]);
  return { data, loading };
}

// ─── Components ───

function StatCard({ label, value, icon }) {
  const fmt = typeof value === 'number'
    ? (value >= 1000 ? (value/1000).toFixed(1) + 'k' : value.toString()) : value || '—';
  return (
    <div className="card p-5 text-center">
      <div style={{fontSize: '28px'}} className="mb-1">{icon}</div>
      <div className="stat-number">{fmt}</div>
      <div style={{fontSize: '12px', color: '#666', marginTop: '4px'}}>{label}</div>
    </div>
  );
}

function EntityBar({ name, count, maxCount, type }) {
  const pct = (count / maxCount * 100).toFixed(1);
  const color = TYPE_COLORS[type] || TYPE_COLORS.other;
  return (
    <div style={{display: 'flex', alignItems: 'center', marginBottom: '8px'}}>
      <div className="legend-dot" style={{background: color, marginRight: '8px'}}></div>
      <div style={{width: '140px', fontSize: '12px', color: '#ccc', overflow: 'hidden',
        textOverflow: 'ellipsis', whiteSpace: 'nowrap'}}>{name}</div>
      <div style={{flex: 1, height: '16px', background: '#111', borderRadius: '4px',
        overflow: 'hidden', margin: '0 8px'}}>
        <div className="bar-fill" style={{height: '100%', width: pct+'%',
          background: `linear-gradient(90deg, ${color}88, ${color})`, borderRadius: '4px'}}></div>
      </div>
      <div style={{width: '55px', fontSize: '11px', color: '#666', textAlign: 'right'}}>
        {count.toLocaleString()}
      </div>
    </div>
  );
}

// ─── Network Map (full D3) ───
function NetworkMap() {
  const svgRef = useRef(null);
  const [loading, setLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');
  const [minDegree, setMinDegree] = useState(2);
  const [minWeight, setMinWeight] = useState(2);
  const [snapshotBuiltAt, setSnapshotBuiltAt] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  // All types active by default
  const [activeTypes, setActiveTypes] = useState(new Set(Object.keys(TYPE_COLORS)));
  const simRef = useRef(null);
  const dataRef = useRef(null);

  function toggleType(type) {
    setActiveTypes(prev => {
      const next = new Set(prev);
      if (next.has(type)) {
        // Don't allow deselecting all
        if (next.size > 1) next.delete(type);
      } else {
        next.add(type);
      }
      return next;
    });
  }

  useEffect(() => {
    fetch('/api/network').then(r => r.json()).then(data => {
      setLoading(false);
      if (data.built_at) setSnapshotBuiltAt(data.built_at);
      buildGraph(data);
    });
    return () => { if (simRef.current) simRef.current.stop(); };
  }, []);

  function handleRefresh() {
    if (refreshing) return;
    setRefreshing(true);
    fetch('/api/network/refresh', {method: 'POST'})
      .then(r => r.json())
      .then(() => {
        // Poll until the in-memory cache is busted (snapshot rebuilt)
        const poll = setInterval(() => {
          fetch('/api/network?_t=' + Date.now())
            .then(r => r.json())
            .then(data => {
              if (data.built_at && data.built_at !== snapshotBuiltAt) {
                clearInterval(poll);
                setRefreshing(false);
                setSnapshotBuiltAt(data.built_at);
                if (simRef.current) simRef.current.stop();
                buildGraph(data);
              }
            });
        }, 5000);
        // Safety timeout after 10 minutes
        setTimeout(() => { clearInterval(poll); setRefreshing(false); }, 600000);
      })
      .catch(() => setRefreshing(false));
  }

  function buildGraph(data) {
    const svg = d3.select(svgRef.current);
    svg.selectAll("*").remove();
    const container = svgRef.current.parentElement;
    const width = container.clientWidth;
    const height = container.clientHeight;
    svg.attr("width", width).attr("height", height);

    const g = svg.append("g");
    const zoom = d3.zoom().scaleExtent([0.1, 8])
      .on("zoom", (e) => g.attr("transform", e.transform));
    svg.call(zoom);

    const nodes = data.nodes.map(d => ({...d}));
    const edges = data.edges.map(d => ({...d}));

    const sizeScale = d3.scaleSqrt()
      .domain([1, d3.max(nodes, d => d.weighted_degree) || 1]).range([3, 28]);
    const edgeWidthScale = d3.scaleLinear()
      .domain([1, d3.max(edges, d => d.weight) || 1]).range([0.3, 3]);

    // ── Pre-settle the simulation off-screen before first paint ──────────
    // Run the physics headlessly (no DOM ticks) so the graph appears already
    // in a stable layout rather than animating from a random starting state.
    const presettleWidth = 1600;
    const presettleHeight = 1200;
    const presim = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(edges).id(d => d.id)
        .distance(d => 60 + (d.weight || 1) * 4)   // longer links for heavy edges = spread out
        .strength(d => Math.min(d.weight / 30, 0.3)))
      .force("charge", d3.forceManyBody()
        .strength(d => -200 - sizeScale(d.weighted_degree) * 15)  // bigger nodes repel more
        .distanceMax(400))
      .force("center", d3.forceCenter(presettleWidth / 2, presettleHeight / 2))
      .force("collision", d3.forceCollide()
        .radius(d => sizeScale(d.weighted_degree) + 18)  // generous padding to prevent overlap
        .strength(0.8))
      .alphaDecay(0.025)  // slower decay = more thorough settling
      .stop();
    // Tick until stable (max 500 iterations for thorough layout)
    const maxTicks = 500;
    for (let i = 0; i < maxTicks && presim.alpha() > 0.005; i++) presim.tick();
    // Translate pre-settled positions to actual canvas centre
    const xExtent = d3.extent(nodes, d => d.x);
    const yExtent = d3.extent(nodes, d => d.y);
    const xOffset = width / 2 - (xExtent[0] + xExtent[1]) / 2;
    const yOffset = height / 2 - (yExtent[0] + yExtent[1]) / 2;
    nodes.forEach(d => { d.x += xOffset; d.y += yOffset; });

    // ── Live simulation for drag/interaction (starts near-stable) ────────
    const simulation = d3.forceSimulation(nodes)
      .force("link", d3.forceLink(edges).id(d => d.id)
        .distance(d => 60 + (d.weight || 1) * 4)
        .strength(d => Math.min(d.weight / 30, 0.3)))
      .force("charge", d3.forceManyBody()
        .strength(d => -200 - sizeScale(d.weighted_degree) * 15)
        .distanceMax(400))
      .force("center", d3.forceCenter(width / 2, height / 2))
      .force("collision", d3.forceCollide()
        .radius(d => sizeScale(d.weighted_degree) + 18)
        .strength(0.8))
      .alpha(0.05)
      .alphaDecay(0.05);
    simRef.current = simulation;

    const link = g.append("g").selectAll("line").data(edges).join("line")
      .attr("stroke", "#1a1a3e").attr("stroke-width", d => edgeWidthScale(d.weight))
      .attr("stroke-opacity", 0.4);

    const node = g.append("g").selectAll("circle").data(nodes).join("circle")
      .attr("r", d => sizeScale(d.weighted_degree))
      .attr("fill", d => TYPE_COLORS[d.type] || TYPE_COLORS.other)
      .attr("fill-opacity", 0.85).attr("stroke", "#000").attr("stroke-width", 0.5)
      .style("cursor", "pointer")
      .call(d3.drag()
        .on("start", (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
        .on("drag", (e, d) => { d.fx=e.x; d.fy=e.y; })
        .on("end", (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx=null; d.fy=null; }));

    // Label group: background rect + text for legibility
    // Only show labels for high-degree nodes at default zoom; more appear as you zoom in
    const LABEL_DEGREE_THRESHOLD = 20;  // show at default zoom
    const labelG = g.append("g").attr("class", "labels");
    const labelNodes = nodes.filter(d => d.degree >= 8);  // render all >=8, visibility via opacity
    // Background rects (rendered first, sized after text)
    const labelBg = labelG.selectAll("rect").data(labelNodes).join("rect")
      .attr("fill", "rgba(5,5,8,0.82)")
      .attr("rx", 2).attr("ry", 2)
      .style("pointer-events", "none");
    const label = labelG.selectAll("text").data(labelNodes).join("text")
      .text(d => d.id)
      .attr("font-size", "11")
      .attr("fill", d => d.degree >= LABEL_DEGREE_THRESHOLD ? "#ddd" : "#888")
      .attr("text-anchor", "middle")
      .attr("dy", d => -sizeScale(d.weighted_degree) - 5)
      .style("pointer-events", "none");
    // Size background rects to match text after render
    label.each(function(d) {
      try {
        const bbox = this.getBBox();
        const pad = 2;
        d3.select(this.parentNode).selectAll("rect").filter((r) => r === d)
          .attr("x", bbox.x - pad).attr("y", bbox.y - pad)
          .attr("width", bbox.width + pad * 2).attr("height", bbox.height + pad * 2);
      } catch(e) {}
    });
    // Zoom-aware label opacity: fade in lower-degree labels as user zooms in
    zoom.on("zoom", (e) => {
      g.attr("transform", e.transform);
      const k = e.transform.k;
      label.attr("fill-opacity", d => {
        if (d.degree >= LABEL_DEGREE_THRESHOLD) return 1;
        if (d.degree >= 12) return Math.min(1, (k - 0.8) / 0.7);
        return Math.min(1, (k - 1.5) / 0.5);
      });
      labelBg.attr("fill-opacity", d => {
        if (d.degree >= LABEL_DEGREE_THRESHOLD) return 0.82;
        if (d.degree >= 12) return Math.min(0.82, (k - 0.8) / 0.7 * 0.82);
        return Math.min(0.82, (k - 1.5) / 0.5 * 0.82);
      });
    });

    const tooltip = d3.select("#d3-tooltip");

    node.on("mouseover", (event, d) => {
      tooltip.style("display", "block")
        .style("left", (event.clientX + 16) + "px")
        .style("top", (event.clientY - 10) + "px");
      tooltip.select(".tt-name").text(d.id);
      tooltip.select(".tt-docs").text("Documents: " + d.doc_count);
      tooltip.select(".tt-conns").text("Connections: " + d.degree + " (" + d.weighted_degree + " weighted)");
      tooltip.select(".tt-sections").text("Sections: " + (d.sections || []).join(", "));
      tooltip.select(".tt-role").text(d.role || "");
      const connected = new Set();
      edges.forEach(e => {
        const s = typeof e.source === "object" ? e.source.id : e.source;
        const t = typeof e.target === "object" ? e.target.id : e.target;
        if (s === d.id) connected.add(t);
        if (t === d.id) connected.add(s);
      });
      node.attr("fill-opacity", n => n.id === d.id || connected.has(n.id) ? 1 : 0.08);
      link.attr("stroke-opacity", e => {
        const s = typeof e.source === "object" ? e.source.id : e.source;
        const t = typeof e.target === "object" ? e.target.id : e.target;
        return s === d.id || t === d.id ? 0.7 : 0.03;
      }).attr("stroke", e => {
        const s = typeof e.source === "object" ? e.source.id : e.source;
        const t = typeof e.target === "object" ? e.target.id : e.target;
        return s === d.id || t === d.id ? "#8b5cf6" : "#1a1a3e";
      });
      label.attr("fill-opacity", n => n.id === d.id || connected.has(n.id) ? 1 : 0.05);
      labelBg.attr("fill-opacity", n => n.id === d.id || connected.has(n.id) ? 0.82 : 0);
    }).on("mouseout", () => {
      tooltip.style("display", "none");
      node.attr("fill-opacity", 0.85);
      link.attr("stroke-opacity", 0.4).attr("stroke", "#1a1a3e");
      label.attr("fill-opacity", 1);
      labelBg.attr("fill-opacity", 0.82);
    });

    // Render initial positions immediately (from pre-settle), then let live sim
    // make tiny adjustments as it decays to rest
    const applyPositions = () => {
      link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
          .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      node.attr("cx", d => d.x).attr("cy", d => d.y);
      // Move label text and its background rect together
      label.attr("x", d => d.x).attr("y", d => d.y);
      label.each(function(d) {
        try {
          const bbox = this.getBBox();
          const pad = 2;
          labelBg.filter(r => r === d)
            .attr("x", bbox.x - pad).attr("y", bbox.y - pad)
            .attr("width", bbox.width + pad * 2).attr("height", bbox.height + pad * 2);
        } catch(e) {}
      });
    };
    applyPositions();  // paint immediately with pre-settled positions
    simulation.on("tick", applyPositions);

    // Center on Epstein immediately (no delay needed — positions are already set)
    const ep = nodes.find(n => n.id === "Jeffrey Epstein");
    if (ep) {
      const t = d3.zoomIdentity.translate(width/2, height/2).scale(1.5).translate(-ep.x, -ep.y);
      svg.call(zoom.transform, t);
    }

    // Store refs for search/filter
    dataRef.current = { nodes, edges, node, link, label, labelBg, sizeScale };
  }

  // Apply type-filter whenever activeTypes changes
  useEffect(() => {
    if (!dataRef.current || !dataRef.current.node) return;
    const { node, link, label, labelBg } = dataRef.current;
    node.attr('display', d => activeTypes.has(d.type) ? null : 'none');
    link.attr('display', e => {
      const s = typeof e.source === 'object' ? e.source : {type: ''};
      const t = typeof e.target === 'object' ? e.target : {type: ''};
      return activeTypes.has(s.type) && activeTypes.has(t.type) ? null : 'none';
    });
    label.attr('display', d => activeTypes.has(d.type) ? null : 'none');
    if (labelBg) labelBg.attr('display', d => activeTypes.has(d.type) ? null : 'none');
  }, [activeTypes]);

  useEffect(() => {
    if (!dataRef.current || !dataRef.current.node) return;
    const { nodes, edges, node, link, label, labelBg } = dataRef.current;
    const q = searchQuery.toLowerCase();
    if (!q) {
      node.attr("fill-opacity", 0.85);
      link.attr("stroke-opacity", 0.4).attr("stroke", "#1a1a3e");
      label.attr("fill-opacity", 1);
      if (labelBg) labelBg.attr("fill-opacity", 0.82);
      return;
    }
    const matches = new Set();
    const connected = new Set();
    nodes.forEach(n => { if (n.id.toLowerCase().includes(q)) matches.add(n.id); });
    edges.forEach(e => {
      const s = typeof e.source === "object" ? e.source.id : e.source;
      const t = typeof e.target === "object" ? e.target.id : e.target;
      if (matches.has(s)) connected.add(t);
      if (matches.has(t)) connected.add(s);
    });
    node.attr("fill-opacity", n => matches.has(n.id) ? 1 : connected.has(n.id) ? 0.5 : 0.05);
    link.attr("stroke-opacity", e => {
      const s = typeof e.source === "object" ? e.source.id : e.source;
      const t = typeof e.target === "object" ? e.target.id : e.target;
      return matches.has(s) || matches.has(t) ? 0.6 : 0.02;
    });
    label.attr("fill-opacity", n => matches.has(n.id) ? 1 : connected.has(n.id) ? 0.6 : 0.05);
    if (labelBg) labelBg.attr("fill-opacity",
      n => matches.has(n.id) ? 0.82 : connected.has(n.id) ? 0.6 : 0);
  }, [searchQuery]);

  return (
    <div style={{position: 'relative', width: '100%', height: 'calc(100vh - 48px)', background: '#050508'}}>
      {loading && <div style={{position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
        color: '#8b5cf6', fontSize: '16px', zIndex: 100, textAlign: 'center'}}>
        <div style={{marginBottom: '8px'}}>Loading relationship map…</div>
        <div style={{fontSize: '12px', color: '#555'}}>Calculating layout — will appear instantly</div>
      </div>}
      {/* Controls overlay */}
      <div style={{position: 'absolute', top: '16px', left: '16px', zIndex: 100, background: 'rgba(15,15,25,0.95)',
        border: '1px solid #1a1a2e', borderRadius: '8px', padding: '16px', width: '260px', fontSize: '13px'}}>
        <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px', color: '#8b5cf6',
          marginBottom: '12px', fontWeight: 600}}>Filters</div>
        <div style={{color: '#999', marginBottom: '4px'}}>Search:</div>
        <input type="text" value={searchQuery} onChange={e => setSearchQuery(e.target.value)}
          placeholder="Type a name..."
          style={{width: '100%', padding: '6px 10px', background: '#111', border: '1px solid #333',
            borderRadius: '4px', color: '#e0e0e0', fontSize: '13px', marginBottom: '12px', outline: 'none'}} />
        <div style={{color: '#999', marginBottom: '4px'}}>Min connections: {minDegree}</div>
        <input type="range" min="1" max="50" value={minDegree}
          onChange={e => setMinDegree(+e.target.value)}
          style={{width: '100%', marginBottom: '12px', accentColor: '#8b5cf6'}} />
        <div style={{color: '#999', marginBottom: '4px'}}>Min edge weight: {minWeight}</div>
        <input type="range" min="1" max="30" value={minWeight}
          onChange={e => setMinWeight(+e.target.value)}
          style={{width: '100%', marginBottom: '16px', accentColor: '#8b5cf6'}} />
        <div style={{borderTop: '1px solid #1a1a2e', paddingTop: '12px'}}>
          <div style={{fontSize: '11px', color: '#555', marginBottom: '8px', textTransform: 'uppercase',
            letterSpacing: '0.5px'}}>Filter by type</div>
          {Object.entries(TYPE_COLORS).map(([type, color]) => {
            const active = activeTypes.has(type);
            return (
              <div key={type} onClick={() => toggleType(type)}
                style={{display: 'flex', alignItems: 'center', marginBottom: '5px', fontSize: '12px',
                  cursor: 'pointer', padding: '4px 6px', borderRadius: '4px', userSelect: 'none',
                  background: active ? 'rgba(255,255,255,0.04)' : 'transparent',
                  opacity: active ? 1 : 0.35,
                  transition: 'opacity 0.15s, background 0.15s'}}>
                <div style={{width: '10px', height: '10px', borderRadius: '50%',
                  background: active ? color : '#333',
                  marginRight: '8px', flexShrink: 0,
                  boxShadow: active ? '0 0 5px ' + color + '88' : 'none',
                  transition: 'background 0.15s, box-shadow 0.15s'}}></div>
                <span style={{textTransform: 'capitalize', color: active ? '#ccc' : '#555'}}>
                  {type.replace('_', ' ')}
                </span>
                {active && <span style={{marginLeft: 'auto', fontSize: '10px', color: color, fontWeight: 600}}>✓</span>}
              </div>
            );
          })}
          <div style={{marginTop: '8px', display: 'flex', gap: '6px'}}>
            <button onClick={() => setActiveTypes(new Set(Object.keys(TYPE_COLORS)))}
              style={{flex: 1, padding: '4px 0', fontSize: '10px', background: 'transparent',
                color: '#555', border: '1px solid #222', borderRadius: '4px', cursor: 'pointer'}}>
              All
            </button>
            <button onClick={() => setActiveTypes(new Set([Object.keys(TYPE_COLORS)[0]]))}
              style={{flex: 1, padding: '4px 0', fontSize: '10px', background: 'transparent',
                color: '#555', border: '1px solid #222', borderRadius: '4px', cursor: 'pointer'}}>
              None
            </button>
          </div>
        </div>
        {/* Snapshot info + refresh */}
        <div style={{borderTop: '1px solid #1a1a2e', paddingTop: '12px', marginTop: '4px'}}>
          {snapshotBuiltAt && (
            <div style={{fontSize: '11px', color: '#444', marginBottom: '8px', lineHeight: '1.4'}}>
              Snapshot: {new Date(snapshotBuiltAt).toLocaleDateString(undefined,
                {month: 'short', day: 'numeric', year: 'numeric'})}
            </div>
          )}
          <button onClick={handleRefresh} disabled={refreshing}
            style={{width: '100%', padding: '6px 0', fontSize: '11px', fontWeight: 500,
              background: refreshing ? '#111' : 'rgba(139,92,246,0.12)',
              color: refreshing ? '#555' : '#8b5cf6',
              border: '1px solid ' + (refreshing ? '#222' : 'rgba(139,92,246,0.3)'),
              borderRadius: '4px', cursor: refreshing ? 'default' : 'pointer'}}>
            {refreshing ? 'Rebuilding…' : 'Rebuild Snapshot'}
          </button>
        </div>
      </div>
      <svg ref={svgRef} id="network-svg"></svg>
    </div>
  );
}

// ─── Investigation Panel ───
function InvestigationPanel() {
  const [query, setQuery] = useState('');
  const [running, setRunning] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [elapsed, setElapsed] = useState(0);

  const presets = [
    "Who are the most connected people outside Epstein's inner circle?",
    "What financial entities appear across multiple document sections?",
    "Which documents have the heaviest redactions and why?",
    "Map the timeline of the plea deal negotiations",
    "What travel patterns emerge from the flight logs?",
  ];

  function handleRun(q) {
    if (!q || running) return;
    setRunning(true);
    setResult(null);
    setError(null);
    setElapsed(0);
    const startTime = Date.now();
    const timer = setInterval(() => setElapsed(((Date.now() - startTime) / 1000).toFixed(1)), 500);

    fetch('/api/investigate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q}),
    })
    .then(r => r.json())
    .then(data => {
      if (data.error) { setError(data.error); setRunning(false); clearInterval(timer); return; }
      setJobId(data.job_id);
      // Poll for results
      const poll = setInterval(() => {
        fetch('/api/investigate/' + data.job_id)
          .then(r => r.json())
          .then(status => {
            if (status.status === 'complete') {
              clearInterval(poll);
              clearInterval(timer);
              setResult(status.result);
              setRunning(false);
            } else if (status.status === 'error') {
              clearInterval(poll);
              clearInterval(timer);
              setError(status.error);
              setRunning(false);
            }
          });
      }, 2000);
    })
    .catch(e => { setError(e.message); setRunning(false); clearInterval(timer); });
  }

  return (
    <div className="card" style={{padding: '20px'}}>
      <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px'}}>
        <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px',
          color: '#8b5cf6', fontWeight: 600}}>Run Investigation</div>
        <span style={{fontSize: '11px', color: '#555'}}>Agent Swarm</span>
      </div>

      <div style={{display: 'flex', gap: '8px', marginBottom: '12px'}}>
        <input type="text" value={query} onChange={e => setQuery(e.target.value)}
          placeholder="Ask a question about the corpus..."
          onKeyDown={e => e.key === 'Enter' && handleRun(query)}
          style={{flex: 1, background: '#0a0a12', border: '1px solid #333', borderRadius: '8px',
            padding: '8px 12px', fontSize: '13px', color: '#e0e0e0', outline: 'none'}} />
        <button onClick={() => handleRun(query)} disabled={!query || running}
          style={{padding: '8px 16px', background: running ? '#333' : '#8b5cf6',
            color: running ? '#888' : '#fff', border: 'none', borderRadius: '8px',
            fontSize: '13px', fontWeight: 500, cursor: running ? 'default' : 'pointer'}}>
          {running ? 'Running...' : 'Investigate'}
        </button>
      </div>

      <div style={{marginBottom: '12px'}}>
        {presets.map((p, i) => (
          <button key={i} onClick={() => { setQuery(p); handleRun(p); }}
            disabled={running}
            style={{display: 'block', width: '100%', textAlign: 'left', fontSize: '12px',
              color: '#666', background: 'none', border: 'none', padding: '5px 8px',
              borderRadius: '4px', cursor: running ? 'default' : 'pointer',
              opacity: running ? 0.4 : 1}}>
            {p}
          </button>
        ))}
      </div>

      {running && (
        <div style={{border: '1px solid rgba(139,92,246,0.3)', background: 'rgba(139,92,246,0.05)',
          borderRadius: '8px', padding: '12px'}}>
          <div style={{display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px'}}>
            <div style={{width: '8px', height: '8px', background: '#8b5cf6', borderRadius: '50%',
              animation: 'pulse 1.5s infinite'}}></div>
            <span style={{fontSize: '12px', color: '#a78bfa'}}>
              Swarm active — agents working ({elapsed}s)
            </span>
          </div>
          <div style={{fontSize: '11px', color: '#666', lineHeight: '1.6'}}>
            <div>NetworkMapper: building co-occurrence graph...</div>
            <div>DocumentQuery: searching corpus...</div>
            <div>TimelineBuilder: extracting events...</div>
            <div>Coordinator: synthesizing findings...</div>
          </div>
        </div>
      )}

      {error && (
        <div style={{border: '1px solid #ef4444', background: 'rgba(239,68,68,0.05)',
          borderRadius: '8px', padding: '12px', fontSize: '13px', color: '#f87171'}}>
          Error: {error}
        </div>
      )}

      {result && (
        <div style={{border: '1px solid #1a1a2e', borderRadius: '8px', padding: '16px', marginTop: '8px'}}>
          <div style={{fontSize: '14px', fontWeight: 600, color: '#fff', marginBottom: '8px'}}>
            {result.question}
          </div>
          <div style={{fontSize: '13px', color: '#aaa', lineHeight: '1.6', marginBottom: '12px'}}>
            {result.executive_summary}
          </div>
          {result.key_findings?.length > 0 && (
            <div style={{borderTop: '1px solid #1a1a2e', paddingTop: '12px'}}>
              <div style={{fontSize: '11px', textTransform: 'uppercase', color: '#8b5cf6',
                marginBottom: '8px', fontWeight: 600}}>Key Findings</div>
              {result.key_findings.map((f, i) => (
                <div key={i} className="finding-card"
                  style={{padding: '10px 14px', marginBottom: '6px', borderRadius: '0 6px 6px 0',
                    fontSize: '12px', color: '#ccc', lineHeight: '1.5'}}>
                  {typeof f === 'string' ? f : f.finding || JSON.stringify(f)}
                </div>
              ))}
            </div>
          )}
          {result.meta && (
            <div style={{marginTop: '8px', fontSize: '11px', color: '#555'}}>
              {result.meta.total_duration_seconds ? result.meta.total_duration_seconds.toFixed(1) + 's' : ''}
              {result.meta.estimated_cost ? ' | $' + result.meta.estimated_cost.toFixed(3) : ''}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ─── Overview Page ───
function OverviewPage({ onNavigate }) {
  const { data: stats } = useFetch('/api/stats');
  const { data: topPeople } = useFetch('/api/top-entities?type=person&limit=12');
  const { data: breakdown } = useFetch('/api/entity-breakdown');
  const { data: reports } = useFetch('/api/reports');

  const maxCount = topPeople?.[0]?.count || 1;

  // API now returns type classification for each person
  const latestReport = reports?.[0];

  return (
    <div style={{maxWidth: '1200px', margin: '0 auto', padding: '32px 24px'}}>
      {/* Stats row */}
      <div style={{display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '16px', marginBottom: '32px'}}>
        <StatCard label="Documents Analyzed" value={stats?.extracted_docs} icon="&#128196;" />
        <StatCard label="Entities Extracted" value={stats?.total_entities} icon="&#127991;" />
        <StatCard label="Network Connections" value={stats?.network_edges} icon="&#128279;" />
        <StatCard label="Investigations Run" value={stats?.reports} icon="&#128270;" />
      </div>

      <div style={{display: 'grid', gridTemplateColumns: '3fr 2fr', gap: '24px'}}>
        {/* Left column */}
        <div>
          {/* Network preview card */}
          <div className="card" style={{padding: '20px', marginBottom: '24px'}}>
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px'}}>
              <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px',
                color: '#8b5cf6', fontWeight: 600}}>Relationship Map</div>
              <button onClick={() => onNavigate('network')}
                style={{fontSize: '12px', color: '#666', background: 'none', border: 'none',
                  cursor: 'pointer'}}>Open Full Map →</button>
            </div>
            <div style={{background: '#050508', borderRadius: '8px', padding: '24px', textAlign: 'center'}}>
              <div style={{fontSize: '48px', marginBottom: '8px', opacity: 0.5}}>&#128376;</div>
              <div style={{color: '#888', fontSize: '14px'}}>
                {stats ? `${(stats.network_edges || 0).toLocaleString()} connections between ${(stats.total_entities || 0).toLocaleString()} entities` : 'Loading...'}
              </div>
              <button onClick={() => onNavigate('network')}
                style={{marginTop: '12px', padding: '8px 20px', background: '#8b5cf6', color: '#fff',
                  border: 'none', borderRadius: '6px', fontSize: '13px', fontWeight: 500, cursor: 'pointer'}}>
                Explore Network
              </button>
            </div>
            <div style={{display: 'flex', gap: '16px', marginTop: '12px', flexWrap: 'wrap'}}>
              {Object.entries(TYPE_COLORS).map(([type, color]) => (
                <div key={type} style={{display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11px', color: '#666'}}>
                  <div style={{width: '8px', height: '8px', borderRadius: '50%', background: color}}></div>
                  <span style={{textTransform: 'capitalize'}}>{type.replace('_', ' ')}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Investigation panel */}
          <InvestigationPanel />
        </div>

        {/* Right column */}
        <div>
          {/* Top people */}
          <div className="card" style={{padding: '20px', marginBottom: '24px'}}>
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px'}}>
              <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px',
                color: '#8b5cf6', fontWeight: 600}}>Most Referenced People</div>
              <button onClick={() => onNavigate('entities')}
                style={{fontSize: '12px', color: '#666', background: 'none', border: 'none',
                  cursor: 'pointer'}}>View All →</button>
            </div>
            {topPeople?.slice(0, 10).map((p, i) => (
              <EntityBar key={i} name={p.name} count={p.count} maxCount={maxCount} type={p.type || 'other'} />
            ))}
          </div>

          {/* Entity breakdown */}
          <div className="card" style={{padding: '20px', marginBottom: '24px'}}>
            <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px',
              color: '#8b5cf6', fontWeight: 600, marginBottom: '16px'}}>Entity Breakdown</div>
            {breakdown?.map((e, i) => (
              <div key={i} style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px'}}>
                <div style={{display: 'flex', alignItems: 'center', gap: '8px'}}>
                  <div style={{width: '10px', height: '10px', borderRadius: '4px',
                    background: ENTITY_COLORS[e.type] || '#6b7280'}}></div>
                  <span style={{fontSize: '12px', color: '#aaa', textTransform: 'capitalize'}}>
                    {(e.type || '').replace('_', ' ')}
                  </span>
                </div>
                <span style={{fontSize: '12px', color: '#666'}}>{e.count.toLocaleString()}</span>
              </div>
            ))}
          </div>

          {/* Latest report */}
          {latestReport && (
            <div className="card" style={{padding: '20px'}}>
              <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px'}}>
                <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px',
                  color: '#8b5cf6', fontWeight: 600}}>Latest Report</div>
                <button onClick={() => onNavigate('reports')}
                  style={{fontSize: '12px', color: '#666', background: 'none', border: 'none',
                    cursor: 'pointer'}}>All Reports →</button>
              </div>
              <div style={{fontSize: '13px', fontWeight: 500, color: '#ddd', marginBottom: '6px'}}>
                {latestReport.question}
              </div>
              <div style={{fontSize: '12px', color: '#777', lineHeight: '1.5'}}>
                {(latestReport.executive_summary || '').slice(0, 150)}...
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Reports Page ───
function ReportsPage() {
  const { data: reports, loading } = useFetch('/api/reports');
  const [expanded, setExpanded] = useState(null);

  return (
    <div style={{maxWidth: '800px', margin: '0 auto', padding: '32px 24px'}}>
      <h2 style={{fontSize: '20px', fontWeight: 600, color: '#fff', marginBottom: '8px'}}>
        Investigation Reports</h2>
      <p style={{fontSize: '14px', color: '#666', marginBottom: '24px'}}>
        AI-generated analysis from the agent swarm</p>

      {loading && <div style={{color: '#666', textAlign: 'center', padding: '40px'}}>Loading...</div>}
      {reports && !reports.length && (
        <div style={{color: '#555', textAlign: 'center', padding: '48px', fontSize: '14px'}}>
          No investigation reports yet. Run the swarm to generate reports.</div>
      )}
      {reports?.map((r, i) => {
        const meta = r.meta || {};
        const date = r.timestamp ? new Date(r.timestamp).toLocaleDateString() : '';
        const cost = meta.estimated_cost ? '$' + meta.estimated_cost.toFixed(3) : '';
        const duration = meta.total_duration_seconds ? meta.total_duration_seconds.toFixed(1) + 's' : '';
        const isExpanded = expanded === i;
        const findings = r.key_findings || [];

        return (
          <div key={i} className="card" style={{padding: '20px', marginBottom: '12px', cursor: 'pointer'}}
            onClick={() => setExpanded(isExpanded ? null : i)}>
            <div style={{fontSize: '14px', fontWeight: 500, color: '#e0e0e0', marginBottom: '8px'}}>
              {r.question || 'Investigation'}</div>
            <div style={{fontSize: '13px', color: '#888', lineHeight: '1.6', marginBottom: '10px'}}>
              {r.executive_summary || ''}</div>
            <div style={{display: 'flex', gap: '16px', fontSize: '12px', color: '#555'}}>
              <span>{findings.length} findings</span>
              {duration && <span>{duration}</span>}
              {cost && <span>{cost}</span>}
              {date && <span>{date}</span>}
            </div>
            {isExpanded && findings.length > 0 && (
              <div style={{marginTop: '12px', paddingTop: '12px', borderTop: '1px solid #1a1a2e'}}>
                {findings.map((f, j) => (
                  <div key={j} className="finding-card"
                    style={{padding: '10px 14px', marginBottom: '8px', borderRadius: '0 6px 6px 0',
                      fontSize: '13px', color: '#ccc', lineHeight: '1.5'}}>
                    {typeof f === 'string' ? f : f.finding || JSON.stringify(f)}
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── Entities Page ───
function EntitiesPage() {
  const { data: people } = useFetch('/api/top-entities?type=person&limit=50');
  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState('All');

  const filtered = people?.filter(p => {
    const matchesSearch = !search || p.name.toLowerCase().includes(search.toLowerCase());
    const matchesType = typeFilter === 'All' || (p.type || 'other') === typeFilter.toLowerCase().replace(' ', '_');
    return matchesSearch && matchesType;
  }) || [];

  return (
    <div style={{maxWidth: '1000px', margin: '0 auto', padding: '32px 24px'}}>
      <h2 style={{fontSize: '20px', fontWeight: 600, color: '#fff', marginBottom: '8px'}}>Entity Explorer</h2>
      <p style={{fontSize: '14px', color: '#666', marginBottom: '24px'}}>
        Browse extracted entities across the corpus</p>

      <div style={{display: 'flex', gap: '8px', marginBottom: '24px', alignItems: 'center'}}>
        {['All', 'Defendant', 'Legal', 'Victim', 'Law Enforcement', 'Witness', 'Other'].map(f => (
          <button key={f} onClick={() => setTypeFilter(f)}
            style={{padding: '6px 12px', fontSize: '12px', fontWeight: 500, borderRadius: '6px',
              background: typeFilter === f ? 'rgba(139,92,246,0.2)' : '#111',
              color: typeFilter === f ? '#a78bfa' : '#888',
              border: `1px solid ${typeFilter === f ? '#8b5cf6' : '#222'}`, cursor: 'pointer'}}>
            {f}
          </button>
        ))}
        <input type="text" placeholder="Search entities..." value={search}
          onChange={e => setSearch(e.target.value)}
          style={{marginLeft: 'auto', background: '#111', border: '1px solid #333', borderRadius: '6px',
            padding: '6px 12px', fontSize: '12px', color: '#e0e0e0', width: '250px', outline: 'none'}} />
      </div>

      <div className="card" style={{overflow: 'hidden'}}>
        <table style={{width: '100%', fontSize: '12px', borderCollapse: 'collapse'}}>
          <thead>
            <tr style={{borderBottom: '1px solid #1a1a2e'}}>
              <th style={{textAlign: 'left', padding: '12px', color: '#666', fontWeight: 500}}>Name</th>
              <th style={{textAlign: 'left', padding: '12px', color: '#666', fontWeight: 500}}>Role</th>
              <th style={{textAlign: 'right', padding: '12px', color: '#666', fontWeight: 500}}>Documents</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((p, i) => (
              <tr key={i} style={{borderBottom: '1px solid #0f0f1a'}}>
                <td style={{padding: '10px 12px', color: '#ddd', fontWeight: 500}}>
                  <div style={{display: 'flex', alignItems: 'center', gap: '8px'}}>
                    <div style={{width: '8px', height: '8px', borderRadius: '50%',
                      background: TYPE_COLORS[p.type] || TYPE_COLORS.other, flexShrink: 0}}></div>
                    {p.name}
                  </div>
                </td>
                <td style={{padding: '10px 12px', color: '#888', textTransform: 'capitalize', fontSize: '11px'}}>
                  {(p.type || 'other').replace('_', ' ')}
                </td>
                <td style={{padding: '10px 12px', color: '#888', textAlign: 'right'}}>{p.count.toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Timeline Page ───
const SIG_COLORS = { high: '#ef4444', medium: '#f59e0b', low: '#6b7280' };
const SIG_LABELS = { high: 'HIGH', medium: 'MED', low: 'LOW' };

function TimelineEventCard({ event, index, isLast }) {
  const [expanded, setExpanded] = useState(false);
  const sig = (event.significance || 'low').toLowerCase();
  const sigColor = SIG_COLORS[sig] || SIG_COLORS.low;
  const sigLabel = SIG_LABELS[sig] || 'LOW';
  const docCount = (event.doc_ids || []).length;
  return (
    <div style={{display: 'flex', gap: '0'}}>
      {/* Spine */}
      <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', width: '40px', flexShrink: 0}}>
        <div style={{width: '12px', height: '12px', borderRadius: '50%',
          background: sigColor, border: `2px solid ${sigColor}44`,
          boxShadow: `0 0 8px ${sigColor}66`, flexShrink: 0, marginTop: '4px'}}></div>
        {!isLast && <div style={{width: '2px', flex: 1, background: 'linear-gradient(to bottom, #2a2a4e, #1a1a2e)', minHeight: '24px', marginTop: '4px'}}></div>}
      </div>
      {/* Card */}
      <div className="card" onClick={() => setExpanded(!expanded)}
        style={{flex: 1, padding: '14px 16px', marginBottom: '12px', cursor: 'pointer',
          borderLeft: `3px solid ${sigColor}55`, transition: 'border-color 0.2s',
          borderColor: expanded ? sigColor : undefined}}>
        <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '12px'}}>
          <div style={{flex: 1}}>
            <div style={{display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '6px', flexWrap: 'wrap'}}>
              <span style={{fontSize: '12px', fontWeight: 700, color: '#a78bfa',
                fontFamily: 'monospace', letterSpacing: '0.5px'}}>{event.date || 'Unknown date'}</span>
              <span style={{fontSize: '10px', fontWeight: 700, color: sigColor,
                background: `${sigColor}18`, border: `1px solid ${sigColor}44`,
                borderRadius: '4px', padding: '1px 6px', letterSpacing: '0.5px'}}>{sigLabel}</span>
              {docCount > 0 && (
                <span style={{fontSize: '10px', color: '#555',
                  background: '#111', border: '1px solid #222',
                  borderRadius: '4px', padding: '1px 6px'}}>
                  {docCount} doc{docCount !== 1 ? 's' : ''}
                </span>
              )}
            </div>
            <div style={{fontSize: '13px', color: '#ddd', lineHeight: '1.5'}}>
              {event.event || 'No description'}
            </div>
          </div>
          <div style={{fontSize: '16px', color: '#444', flexShrink: 0, marginTop: '2px'}}>
            {expanded ? '▲' : '▼'}
          </div>
        </div>
        {expanded && docCount > 0 && (
          <div style={{marginTop: '10px', paddingTop: '10px', borderTop: '1px solid #1a1a2e'}}>
            <div style={{fontSize: '11px', color: '#555', marginBottom: '6px', textTransform: 'uppercase', letterSpacing: '0.5px'}}>Source Documents</div>
            <div style={{display: 'flex', flexWrap: 'wrap', gap: '6px'}}>
              {(event.doc_ids || []).map((id, i) => (
                <span key={i} style={{fontSize: '10px', color: '#8b5cf6',
                  background: 'rgba(139,92,246,0.08)', border: '1px solid rgba(139,92,246,0.2)',
                  borderRadius: '4px', padding: '2px 8px', fontFamily: 'monospace'}}>{id}</span>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function GapCard({ gap }) {
  return (
    <div style={{display: 'flex', gap: '0', opacity: 0.7}}>
      <div style={{display: 'flex', flexDirection: 'column', alignItems: 'center', width: '40px', flexShrink: 0}}>
        <div style={{width: '2px', flex: 1, background: 'repeating-linear-gradient(to bottom, #333 0px, #333 6px, transparent 6px, transparent 12px)', minHeight: '40px'}}></div>
      </div>
      <div style={{flex: 1, marginBottom: '12px', padding: '10px 14px',
        background: 'rgba(251,191,36,0.04)', border: '1px dashed #3a3a1e',
        borderRadius: '8px', display: 'flex', alignItems: 'center', gap: '10px'}}>
        <span style={{fontSize: '16px', opacity: 0.6}}>⚠️</span>
        <div>
          <div style={{fontSize: '11px', color: '#f59e0b', fontWeight: 600, marginBottom: '2px', textTransform: 'uppercase', letterSpacing: '0.5px'}}>Gap: {gap.from} → {gap.to}</div>
          <div style={{fontSize: '12px', color: '#888'}}>{gap.note || 'Unexplained gap in records'}</div>
        </div>
      </div>
    </div>
  );
}

function TimelinePage() {
  const { data: topPeople } = useFetch('/api/top-entities?type=person&limit=50');
  const [subject, setSubject] = useState('');
  const [inputValue, setInputValue] = useState('');
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [jobId, setJobId] = useState(null);
  const [status, setStatus] = useState(null); // null | 'running' | 'complete' | 'error'
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [sigFilter, setSigFilter] = useState('all');
  const inputRef = useRef(null);

  const PRESETS = [
    'Jeffrey Epstein', 'Ghislaine Maxwell', 'Prince Andrew',
    'Alan Dershowitz', 'Leslie Wexner', 'Jean-Luc Brunel',
  ];

  // Autocomplete from top-entities
  useEffect(() => {
    if (!inputValue || !topPeople) { setSuggestions([]); return; }
    const q = inputValue.toLowerCase();
    const matches = topPeople.filter(p => p.name.toLowerCase().includes(q)).slice(0, 8);
    setSuggestions(matches);
  }, [inputValue, topPeople]);

  function handleRun(subj) {
    const s = (subj || inputValue).trim();
    if (!s) return;
    setSubject(s);
    setInputValue(s);
    setShowSuggestions(false);
    setStatus('running');
    setResult(null);
    setError(null);
    setElapsed(0);
    const startTime = Date.now();
    const timer = setInterval(() => setElapsed(((Date.now() - startTime) / 1000).toFixed(1)), 500);

    fetch('/api/timeline', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subject: s}),
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'complete') {
        // Cached result returned immediately — fetch the full result
        clearInterval(timer);
        fetch(`/api/timeline/${data.job_id}`).then(r => r.json()).then(d => {
          setStatus('complete');
          setResult(d.result || {});
        }).catch(() => { setStatus('complete'); setResult({}); });
        return;
      }
      const jid = data.job_id;
      setJobId(jid);
      const poll = setInterval(() => {
        fetch(`/api/timeline/${jid}`).then(r => r.json()).then(d => {
          if (d.status === 'complete') {
            clearInterval(poll); clearInterval(timer);
            setStatus('complete');
            setResult(d.result || {});
          } else if (d.status === 'error') {
            clearInterval(poll); clearInterval(timer);
            setStatus('error');
            setError(d.error || 'Unknown error');
          }
        });
      }, 2000);
    })
    .catch(e => { setStatus('error'); setError(e.message); clearInterval(timer); });
  }

  // Merge gaps into timeline for display
  function buildDisplayItems(tl, gaps) {
    if (!tl || tl.length === 0) return [];
    const items = tl.map(e => ({...e, _type: 'event'}));
    // Insert gaps between events where applicable
    if (gaps && gaps.length > 0) {
      const result = [];
      for (let i = 0; i < items.length; i++) {
        result.push(items[i]);
        if (i < items.length - 1) {
          const gap = gaps.find(g => g.from && g.to &&
            items[i].date && items[i+1].date &&
            g.from >= items[i].date && g.to <= items[i+1].date);
          if (gap) result.push({...gap, _type: 'gap'});
        }
      }
      return result;
    }
    return items;
  }

  const timeline = result?.timeline || [];
  const gaps = result?.gaps || [];
  const patterns = result?.patterns || [];
  const displayItems = buildDisplayItems(timeline, gaps);
  const filteredItems = sigFilter === 'all'
    ? displayItems
    : displayItems.filter(item => item._type === 'gap' || (item.significance || 'low').toLowerCase() === sigFilter);

  const highCount = timeline.filter(e => (e.significance || '').toLowerCase() === 'high').length;
  const medCount  = timeline.filter(e => (e.significance || '').toLowerCase() === 'medium').length;
  const lowCount  = timeline.filter(e => (e.significance || '').toLowerCase() === 'low').length;

  return (
    <div style={{maxWidth: '900px', margin: '0 auto', padding: '32px 24px'}}>
      <h2 style={{fontSize: '20px', fontWeight: 600, color: '#fff', marginBottom: '4px'}}>Timeline View</h2>
      <p style={{fontSize: '14px', color: '#666', marginBottom: '24px'}}>
        Reconstruct a chronological narrative for any person or topic from the corpus
      </p>

      {/* Search bar */}
      <div style={{position: 'relative', marginBottom: '16px'}}>
        <div style={{display: 'flex', gap: '8px'}}>
          <div style={{flex: 1, position: 'relative'}}>
            <input ref={inputRef} type="text" value={inputValue}
              onChange={e => { setInputValue(e.target.value); setShowSuggestions(true); }}
              onKeyDown={e => {
                if (e.key === 'Enter') { handleRun(inputValue); }
                if (e.key === 'Escape') setShowSuggestions(false);
              }}
              onFocus={() => setShowSuggestions(true)}
              onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
              placeholder="Search for a person or topic..."
              style={{width: '100%', background: '#0a0a12', border: '1px solid #333',
                borderRadius: '8px', padding: '10px 14px', fontSize: '14px',
                color: '#e0e0e0', outline: 'none'}} />
            {showSuggestions && suggestions.length > 0 && (
              <div style={{position: 'absolute', top: '100%', left: 0, right: 0, zIndex: 100,
                background: '#0f0f1a', border: '1px solid #2a2a4e', borderRadius: '8px',
                marginTop: '4px', overflow: 'hidden', boxShadow: '0 8px 24px rgba(0,0,0,0.6)'}}>
                {suggestions.map((s, i) => (
                  <div key={i}
                    onMouseDown={() => { setInputValue(s.name); setShowSuggestions(false); }}
                    style={{padding: '9px 14px', fontSize: '13px', cursor: 'pointer',
                      display: 'flex', alignItems: 'center', gap: '10px',
                      borderBottom: i < suggestions.length - 1 ? '1px solid #1a1a2e' : 'none'}}
                    onMouseEnter={e => e.currentTarget.style.background = '#1a1a2e'}
                    onMouseLeave={e => e.currentTarget.style.background = 'transparent'}>
                    <div style={{width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0,
                      background: TYPE_COLORS[s.type] || TYPE_COLORS.other}}></div>
                    <span style={{color: '#ddd', flex: 1}}>{s.name}</span>
                    <span style={{fontSize: '11px', color: '#555'}}>{s.count.toLocaleString()} docs</span>
                  </div>
                ))}
              </div>
            )}
          </div>
          <button onClick={() => handleRun(inputValue)} disabled={!inputValue.trim() || status === 'running'}
            style={{padding: '10px 20px', background: status === 'running' ? '#333' : '#8b5cf6',
              color: status === 'running' ? '#888' : '#fff', border: 'none', borderRadius: '8px',
              fontSize: '13px', fontWeight: 600, cursor: status === 'running' ? 'default' : 'pointer',
              whiteSpace: 'nowrap'}}>
            {status === 'running' ? `Building... (${elapsed}s)` : 'Build Timeline'}
          </button>
        </div>
      </div>

      {/* Preset chips */}
      {!result && status !== 'running' && (
        <div style={{display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '24px'}}>
          <span style={{fontSize: '12px', color: '#555', alignSelf: 'center', marginRight: '4px'}}>Quick select:</span>
          {PRESETS.map(p => (
            <button key={p} onClick={() => handleRun(p)}
              style={{padding: '5px 12px', fontSize: '12px', background: '#111',
                border: '1px solid #2a2a4e', borderRadius: '20px', color: '#a78bfa',
                cursor: 'pointer', fontWeight: 500}}>
              {p}
            </button>
          ))}
        </div>
      )}

      {/* Running state */}
      {status === 'running' && (
        <div className="card" style={{padding: '24px', textAlign: 'center', marginBottom: '24px'}}>
          <div style={{display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '10px', marginBottom: '12px'}}>
            <div style={{width: '10px', height: '10px', background: '#8b5cf6', borderRadius: '50%',
              animation: 'pulse 1.5s infinite'}}></div>
            <span style={{fontSize: '14px', color: '#a78bfa', fontWeight: 500}}>
              Building timeline for "{subject}"...
            </span>
          </div>
          <div style={{fontSize: '12px', color: '#555', lineHeight: '1.8'}}>
            <div>Searching entity index for matching documents...</div>
            <div>Extracting date events from document metadata...</div>
            <div>Synthesizing chronological narrative with Claude...</div>
          </div>
          <div style={{marginTop: '12px', fontSize: '12px', color: '#444'}}>{elapsed}s elapsed</div>
        </div>
      )}

      {/* Error state */}
      {status === 'error' && (
        <div style={{border: '1px solid #ef4444', background: 'rgba(239,68,68,0.05)',
          borderRadius: '8px', padding: '16px', fontSize: '13px', color: '#f87171', marginBottom: '24px'}}>
          Error: {error}
        </div>
      )}

      {/* Results */}
      {status === 'complete' && result && (
        <div>
          {/* Header */}
          <div className="card" style={{padding: '20px', marginBottom: '24px'}}>
            <div style={{display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '12px'}}>
              <div>
                <div style={{fontSize: '18px', fontWeight: 700, color: '#fff', marginBottom: '4px'}}>
                  {result.subject || subject}
                </div>
                <div style={{fontSize: '13px', color: '#666'}}>
                  {result.date_range || 'Date range unknown'}
                  {result.total_documents ? ` · ${result.total_documents} source documents` : ''}
                  {result._agent_duration ? ` · ${result._agent_duration}s` : ''}
                </div>
              </div>
              <div style={{display: 'flex', gap: '8px', flexWrap: 'wrap'}}>
                {[['all', '#8b5cf6', `All (${timeline.length})`],
                  ['high', '#ef4444', `High (${highCount})`],
                  ['medium', '#f59e0b', `Med (${medCount})`],
                  ['low', '#6b7280', `Low (${lowCount})`]].map(([val, col, lbl]) => (
                  <button key={val} onClick={() => setSigFilter(val)}
                    style={{padding: '5px 12px', fontSize: '11px', fontWeight: 600,
                      borderRadius: '6px', border: `1px solid ${sigFilter === val ? col : '#222'}`,
                      background: sigFilter === val ? `${col}18` : '#111',
                      color: sigFilter === val ? col : '#555', cursor: 'pointer'}}>
                    {lbl}
                  </button>
                ))}
              </div>
            </div>
            {result.error && (
              <div style={{marginTop: '12px', fontSize: '12px', color: '#f59e0b',
                background: 'rgba(245,158,11,0.06)', border: '1px solid rgba(245,158,11,0.2)',
                borderRadius: '6px', padding: '8px 12px'}}>
                Note: {result.error}
              </div>
            )}
          </div>

          {/* Timeline */}
          {filteredItems.length === 0 ? (
            <div className="card" style={{padding: '40px', textAlign: 'center', color: '#555'}}>
              No events found{sigFilter !== 'all' ? ` with ${sigFilter} significance` : ''}.
            </div>
          ) : (
            <div style={{paddingLeft: '8px'}}>
              {filteredItems.map((item, i) =>
                item._type === 'gap'
                  ? <GapCard key={`gap-${i}`} gap={item} />
                  : <TimelineEventCard key={`evt-${i}`} event={item}
                      index={i} isLast={i === filteredItems.length - 1} />
              )}
            </div>
          )}

          {/* Gaps section (standalone if not interleaved) */}
          {gaps.length > 0 && filteredItems.every(i => i._type !== 'gap') && (
            <div className="card" style={{padding: '20px', marginTop: '24px'}}>
              <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px',
                color: '#f59e0b', fontWeight: 600, marginBottom: '12px'}}>Identified Gaps</div>
              {gaps.map((g, i) => (
                <div key={i} style={{display: 'flex', gap: '10px', alignItems: 'flex-start',
                  padding: '10px 0', borderBottom: i < gaps.length - 1 ? '1px solid #1a1a2e' : 'none'}}>
                  <span style={{fontSize: '12px', color: '#f59e0b', fontWeight: 600, minWidth: '160px', flexShrink: 0}}>
                    {g.from} → {g.to}
                  </span>
                  <span style={{fontSize: '12px', color: '#888'}}>{g.note}</span>
                </div>
              ))}
            </div>
          )}

          {/* Patterns */}
          {patterns.length > 0 && (
            <div className="card" style={{padding: '20px', marginTop: '16px'}}>
              <div style={{fontSize: '12px', textTransform: 'uppercase', letterSpacing: '1px',
                color: '#8b5cf6', fontWeight: 600, marginBottom: '12px'}}>Temporal Patterns</div>
              {patterns.map((p, i) => (
                <div key={i} className="finding-card"
                  style={{padding: '10px 14px', marginBottom: '8px', borderRadius: '0 6px 6px 0',
                    fontSize: '13px', color: '#ccc', lineHeight: '1.5'}}>
                  {typeof p === 'string' ? p : JSON.stringify(p)}
                </div>
              ))}
            </div>
          )}

          {/* Re-run button */}
          <div style={{textAlign: 'center', marginTop: '24px'}}>
            <button onClick={() => { setResult(null); setStatus(null); setInputValue(subject); }}
              style={{padding: '8px 20px', background: 'none', border: '1px solid #333',
                borderRadius: '8px', color: '#666', fontSize: '13px', cursor: 'pointer'}}>
              Search another subject
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── App Shell ───
function App() {
  const [tab, setTab] = useState('overview');
  const tabs = [
    {id: 'overview', label: 'Overview'},
    {id: 'network', label: 'Relationship Map'},
    {id: 'timeline', label: 'Timeline'},
    {id: 'reports', label: 'Reports'},
    {id: 'entities', label: 'Entities'},
  ];

  return (
    <div style={{minHeight: '100vh'}}>
      {/* Nav */}
      <nav style={{position: 'fixed', top: 0, left: 0, right: 0, zIndex: 1000,
        background: 'rgba(10,10,15,0.95)', backdropFilter: 'blur(12px)',
        borderBottom: '1px solid #1a1a2e', padding: '0 24px',
        display: 'flex', alignItems: 'center', height: '48px'}}>
        <span className="brand" style={{marginRight: '32px'}}>E-FINDER</span>
        <div style={{display: 'flex', gap: '4px'}}>
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={tab === t.id ? 'tab-active' : 'tab-inactive'}
              style={{padding: '6px 12px', fontSize: '12px', fontWeight: 500,
                borderRadius: '6px', border: 'none', cursor: 'pointer', background: tab === t.id ? 'rgba(255,255,255,0.08)' : 'transparent'}}>
              {t.label}
            </button>
          ))}
        </div>
        <div style={{marginLeft: 'auto', fontSize: '12px', color: '#555'}}>
          DOJ Epstein Document Corpus
        </div>
      </nav>

      <div style={{paddingTop: '48px'}}>
        {tab === 'overview' && <OverviewPage onNavigate={setTab} />}
        {tab === 'network' && <NetworkMap />}
        {tab === 'timeline' && <TimelinePage />}
        {tab === 'reports' && <ReportsPage />}
        {tab === 'entities' && <EntitiesPage />}
      </div>
    </div>
  );
}

ReactDOM.render(<App />, document.getElementById('root'));
</script>
</body>
</html>"""


# ─── Main ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{'='*60}")
    print(f"  E-FINDER — Investigation Dashboard")
    print(f"{'='*60}")
    print(f"  Testing MongoDB connection...")

    try:
        db = get_db()
        docs = db["documents"].count_documents({})
        print(f"  Connected. {docs:,} documents in corpus.\n")
    except Exception as e:
        print(f"  Warning: MongoDB connection failed: {e}")
        print(f"  Dashboard will retry on each request.\n")
        _db = None

    print(f"  Starting on port {PORT}...")
    print(f"  Local:  http://localhost:{PORT}")
    print(f"")
    print(f"  To share publicly, run in another tmux pane:")
    print(f"  ~/efinder/cloudflared tunnel --url http://localhost:{PORT}")
    print(f"{'='*60}\n")

    app.run(host="0.0.0.0", port=PORT, debug=False)
