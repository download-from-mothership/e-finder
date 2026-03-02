#!/usr/bin/env python3
"""
E-FINDER — Live Investigation Dashboard
=========================================
Flask app serving the entity relationship map + investigation reports
with live MongoDB data. Designed to run on VPS and be shared via
Cloudflare Tunnel.

Usage:
  cd ~/efinder
  source .venv/bin/activate
  export $(cat .env | xargs)
  python3 _pipeline_output/dashboard.py

  # Then in another tmux pane:
  cloudflared tunnel --url http://localhost:5000
  # → gives you a public https://xxx.trycloudflare.com URL to share
"""

import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime

try:
    from flask import Flask, render_template_string, jsonify, request
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "flask", "--break-system-packages", "-q"])
    from flask import Flask, render_template_string, jsonify, request

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
SECRET_KEY = os.environ.get("DASHBOARD_SECRET", "efinder-dashboard-2026")

MAX_NODES = 300
MAX_EDGES = 2000
MIN_EDGE_WEIGHT = 2

# ─── App Setup ────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = SECRET_KEY

# MongoDB connection (lazy)
_db = None

def get_db():
    global _db
    if _db is None:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
        client.admin.command("ping")
        _db = client[DATABASE_NAME]
    return _db


# ─── API Routes ───────────────────────────────────────────────────────

@app.route("/api/stats")
def api_stats():
    """Corpus statistics for the dashboard header."""
    db = get_db()
    return jsonify({
        "total_docs": db["documents"].count_documents({}),
        "extracted_docs": db["documents"].count_documents({"processing_stage": "entities_extracted"}),
        "total_entities": db["entities"].count_documents({}),
        "network_edges": db["network"].count_documents({}),
        "reports": db["reports"].count_documents({}),
        "generated_at": datetime.utcnow().isoformat() + "Z",
    })


@app.route("/api/network")
def api_network():
    """Live network data for the relationship map."""
    db = get_db()
    min_weight = int(request.args.get("min_weight", MIN_EDGE_WEIGHT))
    max_nodes = int(request.args.get("max_nodes", MAX_NODES))

    # Load edges
    edges_raw = list(db["network"].find(
        {"weight": {"$gte": min_weight}},
        {"person1": 1, "person2": 1, "weight": 1, "shared_doc_ids": 1, "_id": 0}
    ).sort("weight", -1))

    # Build node degrees
    node_degree = defaultdict(int)
    node_weighted_degree = defaultdict(int)
    for e in edges_raw:
        node_degree[e["person1"]] += 1
        node_degree[e["person2"]] += 1
        node_weighted_degree[e["person1"]] += e["weight"]
        node_weighted_degree[e["person2"]] += e["weight"]

    # Top nodes
    top_nodes = sorted(node_weighted_degree.items(), key=lambda x: x[1], reverse=True)[:max_nodes]
    node_set = set(n for n, _ in top_nodes)

    # Filter edges
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

    # Build node metadata
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

        roles = list(db["entities"].find(
            {"name": name, "entity_type": "person", "context": {"$ne": ""}},
            {"context": 1, "_id": 0}
        ).limit(3))
        role_str = "; ".join(r.get("context", "")[:80] for r in roles if r.get("context"))

        role_lower = role_str.lower()
        if any(w in role_lower for w in ["attorney", "counsel", "lawyer", "judge", "magistrate"]):
            node_type = "legal"
        elif any(w in role_lower for w in ["victim", "plaintiff", "doe", "minor", "accuser"]):
            node_type = "victim"
        elif any(w in role_lower for w in ["defendant", "accused", "perpetrator", "co-conspirator"]):
            node_type = "defendant"
        elif any(w in role_lower for w in ["agent", "detective", "officer", "fbi", "investigator"]):
            node_type = "law_enforcement"
        elif any(w in role_lower for w in ["witness", "deponent"]):
            node_type = "witness"
        else:
            node_type = "other"

        nodes.append({
            "id": name,
            "doc_count": doc_count,
            "degree": node_degree[name],
            "weighted_degree": node_weighted_degree[name],
            "sections": sections,
            "role": role_str[:150],
            "type": node_type,
        })

    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/api/reports")
def api_reports():
    """List stored investigation reports."""
    db = get_db()
    reports = list(db["reports"].find(
        {},
        {"_id": 0, "question": 1, "executive_summary": 1, "key_findings": 1,
         "meta": 1, "timestamp": 1}
    ).sort("timestamp", -1).limit(20))
    return jsonify(reports)


@app.route("/api/top-entities")
def api_top_entities():
    """Top entities by document frequency."""
    db = get_db()
    entity_type = request.args.get("type", "person")
    limit = int(request.args.get("limit", 25))

    pipeline = [
        {"$match": {"entity_type": entity_type}},
        {"$group": {"_id": "$name", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": limit},
    ]
    results = list(db["entities"].aggregate(pipeline))
    return jsonify([{"name": r["_id"], "count": r["count"]} for r in results])


# ─── Page Routes ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/network")
def network_page():
    return render_template_string(NETWORK_HTML)


@app.route("/reports")
def reports_page():
    return render_template_string(REPORTS_HTML)


# ─── HTML Templates ──────────────────────────────────────────────────

COMMON_CSS = """
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0a0a0f;
    color: #e0e0e0;
  }
  a { color: #8b5cf6; text-decoration: none; }
  a:hover { color: #a78bfa; text-decoration: underline; }

  nav {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 1000;
    background: rgba(10, 10, 15, 0.95);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid #1a1a2e;
    padding: 0 24px;
    display: flex;
    align-items: center;
    height: 52px;
  }
  nav .brand {
    font-size: 15px;
    font-weight: 700;
    color: #8b5cf6;
    letter-spacing: 1px;
    margin-right: 32px;
  }
  nav .links a {
    color: #888;
    font-size: 13px;
    font-weight: 500;
    margin-right: 24px;
    transition: color 0.2s;
  }
  nav .links a:hover, nav .links a.active {
    color: #e0e0e0;
    text-decoration: none;
  }
  nav .badge {
    font-size: 11px;
    color: #666;
    margin-left: auto;
  }

  .page { padding-top: 52px; }
"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>E-FINDER — Investigation Dashboard</title>
<style>
""" + COMMON_CSS + """
  .hero {
    padding: 48px 32px 32px;
    text-align: center;
  }
  .hero h1 {
    font-size: 28px;
    font-weight: 700;
    color: #fff;
    margin-bottom: 8px;
  }
  .hero p {
    color: #888;
    font-size: 15px;
    max-width: 600px;
    margin: 0 auto;
  }

  .stats-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    padding: 0 32px 32px;
    max-width: 900px;
    margin: 0 auto;
  }
  .stat-card {
    background: rgba(15, 15, 25, 0.8);
    border: 1px solid #1a1a2e;
    border-radius: 10px;
    padding: 20px;
    text-align: center;
  }
  .stat-card .number {
    font-size: 32px;
    font-weight: 700;
    color: #8b5cf6;
  }
  .stat-card .label {
    font-size: 13px;
    color: #888;
    margin-top: 4px;
  }

  .nav-cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
    gap: 16px;
    padding: 0 32px 48px;
    max-width: 900px;
    margin: 0 auto;
  }
  .nav-card {
    display: block;
    background: rgba(15, 15, 25, 0.8);
    border: 1px solid #1a1a2e;
    border-radius: 10px;
    padding: 24px;
    transition: all 0.2s;
  }
  .nav-card:hover {
    border-color: #8b5cf6;
    transform: translateY(-2px);
    text-decoration: none;
  }
  .nav-card h3 {
    font-size: 16px;
    color: #fff;
    margin-bottom: 8px;
  }
  .nav-card p {
    font-size: 13px;
    color: #888;
    line-height: 1.5;
  }
  .nav-card .icon {
    font-size: 28px;
    margin-bottom: 12px;
  }

  .top-entities {
    max-width: 900px;
    margin: 0 auto;
    padding: 0 32px 48px;
  }
  .top-entities h2 {
    font-size: 18px;
    color: #fff;
    margin-bottom: 16px;
  }
  .entity-bar {
    display: flex;
    align-items: center;
    margin-bottom: 8px;
  }
  .entity-bar .name {
    width: 200px;
    font-size: 13px;
    color: #ccc;
    text-align: right;
    padding-right: 12px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .entity-bar .bar-bg {
    flex: 1;
    height: 20px;
    background: #111;
    border-radius: 4px;
    overflow: hidden;
  }
  .entity-bar .bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #8b5cf6, #6d28d9);
    border-radius: 4px;
    transition: width 0.6s ease;
  }
  .entity-bar .count {
    width: 60px;
    font-size: 12px;
    color: #666;
    padding-left: 8px;
  }

  .footer {
    text-align: center;
    padding: 24px;
    color: #444;
    font-size: 12px;
    border-top: 1px solid #111;
  }
</style>
</head>
<body>

<nav>
  <span class="brand">E-FINDER</span>
  <div class="links">
    <a href="/" class="active">Dashboard</a>
    <a href="/network">Relationship Map</a>
    <a href="/reports">Reports</a>
  </div>
  <span class="badge" id="last-update"></span>
</nav>

<div class="page">
  <div class="hero">
    <h1>DOJ Epstein Document Investigation</h1>
    <p>AI-powered analysis of 26,138 documents from the Department of Justice Epstein document release</p>
  </div>

  <div class="stats-grid">
    <div class="stat-card">
      <div class="number" id="s-docs">—</div>
      <div class="label">Documents Analyzed</div>
    </div>
    <div class="stat-card">
      <div class="number" id="s-entities">—</div>
      <div class="label">Entities Extracted</div>
    </div>
    <div class="stat-card">
      <div class="number" id="s-connections">—</div>
      <div class="label">Network Connections</div>
    </div>
    <div class="stat-card">
      <div class="number" id="s-reports">—</div>
      <div class="label">Investigation Reports</div>
    </div>
  </div>

  <div class="nav-cards">
    <a href="/network" class="nav-card">
      <div class="icon">🕸️</div>
      <h3>Entity Relationship Map</h3>
      <p>Interactive force-directed graph showing connections between people across the document corpus. Search, filter, and explore the network.</p>
    </a>
    <a href="/reports" class="nav-card">
      <div class="icon">📋</div>
      <h3>Investigation Reports</h3>
      <p>AI-generated investigation reports from the agent swarm — financial connections, timelines, redaction analysis, and more.</p>
    </a>
  </div>

  <div class="top-entities">
    <h2>Most Referenced People</h2>
    <div id="entity-bars"></div>
  </div>

  <div class="footer">
    E-FINDER Investigation Dashboard &middot; Data from DOJ document releases &middot; Analysis powered by Claude
  </div>
</div>

<script>
function fmt(n) {
  return n >= 1000 ? (n/1000).toFixed(1) + 'k' : n.toString();
}

fetch('/api/stats')
  .then(r => r.json())
  .then(d => {
    document.getElementById('s-docs').textContent = fmt(d.extracted_docs);
    document.getElementById('s-entities').textContent = fmt(d.total_entities);
    document.getElementById('s-connections').textContent = fmt(d.network_edges);
    document.getElementById('s-reports').textContent = d.reports;
    document.getElementById('last-update').textContent =
      'Data as of ' + new Date(d.generated_at).toLocaleDateString();
  });

fetch('/api/top-entities?type=person&limit=15')
  .then(r => r.json())
  .then(data => {
    const maxCount = data[0]?.count || 1;
    const container = document.getElementById('entity-bars');
    data.forEach(d => {
      const pct = (d.count / maxCount * 100).toFixed(1);
      container.innerHTML += `
        <div class="entity-bar">
          <div class="name">${d.name}</div>
          <div class="bar-bg"><div class="bar-fill" style="width:${pct}%"></div></div>
          <div class="count">${d.count} docs</div>
        </div>`;
    });
  });
</script>
</body>
</html>"""


# ─── Network Map Page ─────────────────────────────────────────────────

NETWORK_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>E-FINDER — Relationship Map</title>
<style>
""" + COMMON_CSS + """
  body { overflow: hidden; }

  #controls {
    position: fixed;
    top: 64px;
    left: 16px;
    z-index: 100;
    background: rgba(15, 15, 25, 0.95);
    border: 1px solid #1a1a2e;
    border-radius: 8px;
    padding: 16px;
    width: 260px;
    font-size: 13px;
  }
  #controls h3 {
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #8b5cf6;
    margin-bottom: 12px;
  }
  #controls label {
    display: block;
    margin-bottom: 8px;
    color: #999;
  }
  #controls input[type=range] {
    width: 100%;
    margin: 4px 0 12px;
    accent-color: #8b5cf6;
  }
  #controls input[type=text] {
    width: 100%;
    padding: 6px 10px;
    background: #111;
    border: 1px solid #333;
    border-radius: 4px;
    color: #e0e0e0;
    font-size: 13px;
    margin-bottom: 12px;
  }
  .legend {
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid #1a1a2e;
  }
  .legend-item {
    display: flex;
    align-items: center;
    margin-bottom: 6px;
    font-size: 12px;
  }
  .legend-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 8px;
    flex-shrink: 0;
  }
  #loading {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    color: #8b5cf6;
    font-size: 16px;
    z-index: 200;
  }
  #tooltip {
    position: fixed;
    display: none;
    background: rgba(15, 15, 25, 0.97);
    border: 1px solid #8b5cf6;
    border-radius: 8px;
    padding: 12px 16px;
    font-size: 13px;
    max-width: 350px;
    z-index: 200;
    pointer-events: none;
  }
  #tooltip .name { font-size: 15px; font-weight: 600; color: #fff; margin-bottom: 6px; }
  #tooltip .meta { color: #999; margin-bottom: 3px; }
  #tooltip .role { color: #8b5cf6; font-style: italic; margin-top: 6px; }
  svg { width: 100vw; height: 100vh; }
</style>
</head>
<body>

<nav>
  <span class="brand">E-FINDER</span>
  <div class="links">
    <a href="/">Dashboard</a>
    <a href="/network" class="active">Relationship Map</a>
    <a href="/reports">Reports</a>
  </div>
  <span class="badge">
    <span id="node-count">—</span> people &middot;
    <span id="edge-count">—</span> connections
  </span>
</nav>

<div id="loading">Loading network data...</div>

<div id="controls" style="display:none">
  <h3>Filters</h3>
  <label>Search:
    <input type="text" id="search" placeholder="Type a name...">
  </label>
  <label>Min connections: <span id="min-deg-val">2</span>
    <input type="range" id="min-degree" min="1" max="50" value="2">
  </label>
  <label>Min edge weight: <span id="min-wt-val">2</span>
    <input type="range" id="min-weight" min="1" max="30" value="2">
  </label>
  <div class="legend">
    <div class="legend-item"><div class="legend-dot" style="background:#ef4444"></div> Defendant</div>
    <div class="legend-item"><div class="legend-dot" style="background:#3b82f6"></div> Legal</div>
    <div class="legend-item"><div class="legend-dot" style="background:#f59e0b"></div> Victim</div>
    <div class="legend-item"><div class="legend-dot" style="background:#10b981"></div> Law Enforcement</div>
    <div class="legend-item"><div class="legend-dot" style="background:#a855f7"></div> Witness</div>
    <div class="legend-item"><div class="legend-dot" style="background:#6b7280"></div> Other</div>
  </div>
</div>

<div id="tooltip">
  <div class="name"></div>
  <div class="meta docs"></div>
  <div class="meta connections"></div>
  <div class="meta sections"></div>
  <div class="role"></div>
</div>

<svg></svg>

<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.8.5/d3.min.js"></script>
<script>
const typeColors = {
  defendant: "#ef4444",
  legal: "#3b82f6",
  victim: "#f59e0b",
  law_enforcement: "#10b981",
  witness: "#a855f7",
  other: "#6b7280",
};

fetch('/api/network')
  .then(r => r.json())
  .then(data => {
    document.getElementById('loading').style.display = 'none';
    document.getElementById('controls').style.display = 'block';
    buildGraph(data.nodes, data.edges);
  })
  .catch(err => {
    document.getElementById('loading').textContent = 'Error loading data: ' + err.message;
  });

function buildGraph(rawNodes, rawEdges) {
  const width = window.innerWidth;
  const height = window.innerHeight;

  const svg = d3.select("svg").attr("width", width).attr("height", height);
  const g = svg.append("g");

  const zoom = d3.zoom()
    .scaleExtent([0.1, 8])
    .on("zoom", (e) => g.attr("transform", e.transform));
  svg.call(zoom);

  let nodes = rawNodes.map(d => ({...d}));
  let edges = rawEdges.map(d => ({...d}));

  const sizeScale = d3.scaleSqrt()
    .domain([1, d3.max(nodes, d => d.weighted_degree)])
    .range([3, 28]);

  const edgeWidthScale = d3.scaleLinear()
    .domain([1, d3.max(edges, d => d.weight)])
    .range([0.3, 3]);

  const simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(edges).id(d => d.id).distance(80).strength(d => Math.min(d.weight / 20, 0.5)))
    .force("charge", d3.forceManyBody().strength(-120))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collision", d3.forceCollide().radius(d => sizeScale(d.weighted_degree) + 2))
    .alphaDecay(0.02);

  let linkGroup = g.append("g").attr("class", "links");
  let link = linkGroup.selectAll("line")
    .data(edges).join("line")
    .attr("stroke", "#1a1a3e")
    .attr("stroke-width", d => edgeWidthScale(d.weight))
    .attr("stroke-opacity", 0.4);

  let nodeGroup = g.append("g").attr("class", "nodes");
  let node = nodeGroup.selectAll("circle")
    .data(nodes).join("circle")
    .attr("r", d => sizeScale(d.weighted_degree))
    .attr("fill", d => typeColors[d.type] || typeColors.other)
    .attr("fill-opacity", 0.85)
    .attr("stroke", "#000")
    .attr("stroke-width", 0.5)
    .style("cursor", "pointer")
    .call(d3.drag()
      .on("start", (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag", (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end", (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));

  let labelGroup = g.append("g").attr("class", "labels");
  let label = labelGroup.selectAll("text")
    .data(nodes.filter(d => d.degree >= 15))
    .join("text")
    .text(d => d.id)
    .attr("font-size", d => Math.max(9, Math.min(14, d.degree / 3)))
    .attr("fill", "#ccc")
    .attr("text-anchor", "middle")
    .attr("dy", d => -sizeScale(d.weighted_degree) - 4)
    .style("pointer-events", "none")
    .style("text-shadow", "0 0 4px #000, 0 0 8px #000");

  const tooltip = d3.select("#tooltip");

  node.on("mouseover", (event, d) => {
    tooltip.style("display", "block")
      .style("left", (event.clientX + 16) + "px")
      .style("top", (event.clientY - 10) + "px");
    tooltip.select(".name").text(d.id);
    tooltip.select(".docs").text("Documents: " + d.doc_count);
    tooltip.select(".connections").text("Connections: " + d.degree + " (" + d.weighted_degree + " weighted)");
    tooltip.select(".sections").text("Sections: " + (d.sections || []).join(", "));
    tooltip.select(".role").text(d.role || "");

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
    label.attr("fill-opacity", n => n.id === d.id || connected.has(n.id) ? 1 : 0.1);
  })
  .on("mouseout", () => {
    tooltip.style("display", "none");
    node.attr("fill-opacity", 0.85);
    link.attr("stroke-opacity", 0.4).attr("stroke", "#1a1a3e");
    label.attr("fill-opacity", 1);
  });

  simulation.on("tick", () => {
    link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
    node.attr("cx", d => d.x).attr("cy", d => d.y);
    label.attr("x", d => d.x).attr("y", d => d.y);
  });

  document.getElementById("node-count").textContent = nodes.length;
  document.getElementById("edge-count").textContent = edges.length;

  // Search
  document.getElementById("search").addEventListener("input", function() {
    const q = this.value.toLowerCase();
    if (!q) {
      node.attr("fill-opacity", 0.85);
      link.attr("stroke-opacity", 0.4).attr("stroke", "#1a1a3e");
      label.attr("fill-opacity", 1);
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
  });

  // Degree filter
  document.getElementById("min-degree").addEventListener("input", function() {
    document.getElementById("min-deg-val").textContent = this.value;
    updateVisibility();
  });
  document.getElementById("min-weight").addEventListener("input", function() {
    document.getElementById("min-wt-val").textContent = this.value;
    updateVisibility();
  });

  function updateVisibility() {
    const minDeg = +document.getElementById("min-degree").value;
    const minWt = +document.getElementById("min-weight").value;
    const visibleNodes = new Set();
    nodes.forEach(n => { if (n.degree >= minDeg) visibleNodes.add(n.id); });
    node.attr("display", n => visibleNodes.has(n.id) ? null : "none");
    label.attr("display", n => visibleNodes.has(n.id) && n.degree >= 15 ? null : "none");
    link.attr("display", e => {
      const s = typeof e.source === "object" ? e.source.id : e.source;
      const t = typeof e.target === "object" ? e.target.id : e.target;
      return visibleNodes.has(s) && visibleNodes.has(t) && e.weight >= minWt ? null : "none";
    });
    const vn = nodes.filter(n => visibleNodes.has(n.id)).length;
    const ve = edges.filter(e => {
      const s = typeof e.source === "object" ? e.source.id : e.source;
      const t = typeof e.target === "object" ? e.target.id : e.target;
      return visibleNodes.has(s) && visibleNodes.has(t) && e.weight >= minWt;
    }).length;
    document.getElementById("node-count").textContent = vn;
    document.getElementById("edge-count").textContent = ve;
  }

  // Center on Epstein
  setTimeout(() => {
    const epstein = nodes.find(n => n.id === "Jeffrey Epstein");
    if (epstein) {
      const transform = d3.zoomIdentity
        .translate(width / 2, height / 2)
        .scale(1.5)
        .translate(-epstein.x, -epstein.y);
      svg.transition().duration(1000).call(zoom.transform, transform);
    }
  }, 3000);
}
</script>
</body>
</html>"""


# ─── Reports Page ─────────────────────────────────────────────────────

REPORTS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>E-FINDER — Investigation Reports</title>
<style>
""" + COMMON_CSS + """
  .content {
    max-width: 900px;
    margin: 0 auto;
    padding: 72px 32px 48px;
  }
  .content h1 {
    font-size: 24px;
    color: #fff;
    margin-bottom: 8px;
  }
  .content .subtitle {
    color: #888;
    font-size: 14px;
    margin-bottom: 32px;
  }

  .report-card {
    background: rgba(15, 15, 25, 0.8);
    border: 1px solid #1a1a2e;
    border-radius: 10px;
    padding: 24px;
    margin-bottom: 16px;
    transition: border-color 0.2s;
  }
  .report-card:hover { border-color: #333; }
  .report-card h3 {
    font-size: 16px;
    color: #e0e0e0;
    margin-bottom: 8px;
  }
  .report-card .summary {
    color: #999;
    font-size: 14px;
    line-height: 1.6;
    margin-bottom: 12px;
  }
  .report-card .meta {
    font-size: 12px;
    color: #555;
  }
  .report-card .findings {
    margin-top: 12px;
    padding-top: 12px;
    border-top: 1px solid #1a1a2e;
  }
  .finding {
    background: rgba(139, 92, 246, 0.06);
    border-left: 3px solid #8b5cf6;
    padding: 10px 14px;
    margin-bottom: 8px;
    border-radius: 0 6px 6px 0;
    font-size: 13px;
    color: #ccc;
    line-height: 1.5;
  }
  .empty {
    text-align: center;
    color: #555;
    padding: 48px;
    font-size: 15px;
  }
</style>
</head>
<body>

<nav>
  <span class="brand">E-FINDER</span>
  <div class="links">
    <a href="/">Dashboard</a>
    <a href="/network">Relationship Map</a>
    <a href="/reports" class="active">Reports</a>
  </div>
</nav>

<div class="page">
  <div class="content">
    <h1>Investigation Reports</h1>
    <p class="subtitle">AI-generated analysis from the OSINT agent swarm</p>
    <div id="reports-list"><div class="empty">Loading reports...</div></div>
  </div>
</div>

<script>
fetch('/api/reports')
  .then(r => r.json())
  .then(reports => {
    const container = document.getElementById('reports-list');
    if (!reports.length) {
      container.innerHTML = '<div class="empty">No investigation reports yet. Run the swarm to generate reports.</div>';
      return;
    }
    container.innerHTML = '';
    reports.forEach(r => {
      const findings = (r.key_findings || []).map(f =>
        '<div class="finding">' + escapeHtml(typeof f === 'string' ? f : f.finding || JSON.stringify(f)) + '</div>'
      ).join('');

      const meta = r.meta || {};
      const date = r.timestamp ? new Date(r.timestamp).toLocaleDateString() : 'Unknown date';
      const cost = meta.estimated_cost ? '$' + meta.estimated_cost.toFixed(3) : '';
      const duration = meta.total_duration_seconds ? meta.total_duration_seconds.toFixed(1) + 's' : '';

      container.innerHTML += `
        <div class="report-card">
          <h3>${escapeHtml(r.question || 'Investigation')}</h3>
          <div class="summary">${escapeHtml(r.executive_summary || '')}</div>
          <div class="meta">${date} ${duration ? '&middot; ' + duration : ''} ${cost ? '&middot; ' + cost : ''}</div>
          ${findings ? '<div class="findings">' + findings + '</div>' : ''}
        </div>`;
    });
  });

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}
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
        print(f"  ⚠ MongoDB connection failed: {e}")
        print(f"  Dashboard will retry on each request.\n")
        _db = None

    print(f"  Starting Flask on port {PORT}...")
    print(f"  Local:  http://localhost:{PORT}")
    print(f"  ")
    print(f"  To share publicly, run in another tmux pane:")
    print(f"  cloudflared tunnel --url http://localhost:{PORT}")
    print(f"{'='*60}\n")

    app.run(host="0.0.0.0", port=PORT, debug=False)
