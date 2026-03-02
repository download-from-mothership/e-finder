#!/usr/bin/env python3
"""
E-FINDER — Generate Interactive Relationship Map
==================================================
Exports the network collection from MongoDB into a self-contained HTML
file with D3.js force-directed graph visualization.

Usage:
  cd ~/efinder
  source .venv/bin/activate
  export $(cat .env | xargs)
  python3 _pipeline_output/generate_network_map.py

  # Then copy to Mac:
  # scp openclaw@100.107.109.53:~/efinder/entity_relationship_map.html ~/Desktop/
"""

import json
import os
import sys
from collections import defaultdict

try:
    from pymongo import MongoClient
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "pymongo", "--break-system-packages", "-q"])
    from pymongo import MongoClient

MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = "doj_investigation"

# Cap for browser performance
MAX_NODES = 300
MAX_EDGES = 2000
MIN_EDGE_WEIGHT = 2


def main():
    print("Connecting to MongoDB...")
    client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    client.admin.command("ping")
    db = client[DATABASE_NAME]
    print("Connected.\n")

    # Load network edges
    edges_raw = list(db["network"].find(
        {"weight": {"$gte": MIN_EDGE_WEIGHT}},
        {"person1": 1, "person2": 1, "weight": 1, "shared_doc_ids": 1, "_id": 0}
    ).sort("weight", -1))
    print(f"Loaded {len(edges_raw)} edges (weight >= {MIN_EDGE_WEIGHT})")

    # Build node set with degree counts
    node_degree = defaultdict(int)
    node_weighted_degree = defaultdict(int)
    for e in edges_raw:
        node_degree[e["person1"]] += 1
        node_degree[e["person2"]] += 1
        node_weighted_degree[e["person1"]] += e["weight"]
        node_weighted_degree[e["person2"]] += e["weight"]

    # Take top N nodes by weighted degree
    top_nodes = sorted(node_weighted_degree.items(), key=lambda x: x[1], reverse=True)[:MAX_NODES]
    node_set = set(n for n, _ in top_nodes)
    print(f"Top {len(node_set)} nodes selected")

    # Filter edges to only include selected nodes
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
    print(f"Filtered to {len(edges)} edges")

    # Get entity metadata from documents collection for coloring
    # Compute community labels via simple degree-based clustering
    node_data = {}
    for name in node_set:
        # Look up how many docs this person appears in
        doc_count = db["entities"].count_documents({"name": name, "entity_type": "person"})

        # Get most common sections
        section_pipeline = [
            {"$match": {"name": name, "entity_type": "person"}},
            {"$group": {"_id": "$section", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 3},
        ]
        sections = [r["_id"] for r in db["entities"].aggregate(section_pipeline)]

        # Get roles/context
        roles = list(db["entities"].find(
            {"name": name, "entity_type": "person", "context": {"$ne": ""}},
            {"context": 1, "_id": 0}
        ).limit(3))
        role_str = "; ".join(r.get("context", "")[:80] for r in roles if r.get("context"))

        # Classify node type based on roles
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

        node_data[name] = {
            "id": name,
            "doc_count": doc_count,
            "degree": node_degree[name],
            "weighted_degree": node_weighted_degree[name],
            "sections": sections,
            "role": role_str[:150],
            "type": node_type,
        }

    nodes = list(node_data.values())
    print(f"Built {len(nodes)} node records")

    # Count types
    type_counts = defaultdict(int)
    for n in nodes:
        type_counts[n["type"]] += 1
    print(f"Node types: {dict(type_counts)}")

    # Generate HTML
    html = generate_html(nodes, edges)

    # Write both versions — named file for reference, index.html for serving
    output_path = "entity_relationship_map.html"
    with open(output_path, "w") as f:
        f.write(html)

    # Also write as index.html in a serve/ directory for easy hosting
    import os
    serve_dir = os.path.join(os.path.dirname(output_path) or ".", "serve")
    os.makedirs(serve_dir, exist_ok=True)
    index_path = os.path.join(serve_dir, "index.html")
    with open(index_path, "w") as f:
        f.write(html)

    print(f"\nSaved to {output_path}")
    print(f"Saved to {index_path} (for web serving)")
    print(f"  Nodes: {len(nodes)}")
    print(f"  Edges: {len(edges)}")
    print(f"  File size: {len(html) / 1024:.0f} KB")


def generate_html(nodes, edges):
    nodes_json = json.dumps(nodes)
    edges_json = json.dumps(edges)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>E-FINDER — Entity Relationship Map</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0a0a0f;
    color: #e0e0e0;
    overflow: hidden;
  }}
  #header {{
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 100;
    background: rgba(10, 10, 15, 0.92);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid #1a1a2e;
    padding: 12px 24px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }}
  #header h1 {{
    font-size: 16px;
    font-weight: 600;
    color: #8b5cf6;
    letter-spacing: 0.5px;
  }}
  #header .stats {{
    font-size: 13px;
    color: #666;
  }}
  #controls {{
    position: fixed;
    top: 56px;
    left: 16px;
    z-index: 100;
    background: rgba(15, 15, 25, 0.95);
    border: 1px solid #1a1a2e;
    border-radius: 8px;
    padding: 16px;
    width: 260px;
    font-size: 13px;
  }}
  #controls h3 {{
    font-size: 12px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: #8b5cf6;
    margin-bottom: 12px;
  }}
  #controls label {{
    display: block;
    margin-bottom: 8px;
    color: #999;
  }}
  #controls input[type=range] {{
    width: 100%;
    margin: 4px 0 12px;
    accent-color: #8b5cf6;
  }}
  #controls input[type=text] {{
    width: 100%;
    padding: 6px 10px;
    background: #111;
    border: 1px solid #333;
    border-radius: 4px;
    color: #e0e0e0;
    font-size: 13px;
    margin-bottom: 12px;
  }}
  .legend {{
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid #1a1a2e;
  }}
  .legend-item {{
    display: flex;
    align-items: center;
    margin-bottom: 6px;
    font-size: 12px;
  }}
  .legend-dot {{
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 8px;
    flex-shrink: 0;
  }}
  #tooltip {{
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
  }}
  #tooltip .name {{
    font-size: 15px;
    font-weight: 600;
    color: #fff;
    margin-bottom: 6px;
  }}
  #tooltip .meta {{
    color: #999;
    margin-bottom: 3px;
  }}
  #tooltip .role {{
    color: #8b5cf6;
    font-style: italic;
    margin-top: 6px;
  }}
  svg {{
    width: 100vw;
    height: 100vh;
  }}
</style>
</head>
<body>

<div id="header">
  <h1>E-FINDER &mdash; Entity Relationship Map</h1>
  <div class="stats">
    <span id="node-count"></span> people &middot;
    <span id="edge-count"></span> connections &middot;
    DOJ Epstein Document Corpus (26,138 docs)
  </div>
</div>

<div id="controls">
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
const rawNodes = {nodes_json};
const rawEdges = {edges_json};

const typeColors = {{
  defendant: "#ef4444",
  legal: "#3b82f6",
  victim: "#f59e0b",
  law_enforcement: "#10b981",
  witness: "#a855f7",
  other: "#6b7280",
}};

const width = window.innerWidth;
const height = window.innerHeight;

const svg = d3.select("svg")
  .attr("width", width)
  .attr("height", height);

const g = svg.append("g");

// Zoom
const zoom = d3.zoom()
  .scaleExtent([0.1, 8])
  .on("zoom", (e) => g.attr("transform", e.transform));
svg.call(zoom);

// Initial data
let nodes = rawNodes.map(d => ({{...d}}));
let edges = rawEdges.map(d => ({{...d}}));

// Size scale
const sizeScale = d3.scaleSqrt()
  .domain([1, d3.max(nodes, d => d.weighted_degree)])
  .range([3, 28]);

const edgeWidthScale = d3.scaleLinear()
  .domain([1, d3.max(edges, d => d.weight)])
  .range([0.3, 3]);

// Force simulation
const simulation = d3.forceSimulation(nodes)
  .force("link", d3.forceLink(edges).id(d => d.id).distance(80).strength(d => Math.min(d.weight / 20, 0.5)))
  .force("charge", d3.forceManyBody().strength(-120))
  .force("center", d3.forceCenter(width / 2, height / 2))
  .force("collision", d3.forceCollide().radius(d => sizeScale(d.weighted_degree) + 2))
  .alphaDecay(0.02);

// Draw edges
let linkGroup = g.append("g").attr("class", "links");
let link = linkGroup.selectAll("line")
  .data(edges)
  .join("line")
  .attr("stroke", "#1a1a3e")
  .attr("stroke-width", d => edgeWidthScale(d.weight))
  .attr("stroke-opacity", 0.4);

// Draw nodes
let nodeGroup = g.append("g").attr("class", "nodes");
let node = nodeGroup.selectAll("circle")
  .data(nodes)
  .join("circle")
  .attr("r", d => sizeScale(d.weighted_degree))
  .attr("fill", d => typeColors[d.type] || typeColors.other)
  .attr("fill-opacity", 0.85)
  .attr("stroke", "#000")
  .attr("stroke-width", 0.5)
  .style("cursor", "pointer")
  .call(d3.drag()
    .on("start", dragstarted)
    .on("drag", dragged)
    .on("end", dragended));

// Labels for top nodes
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

// Tooltip
const tooltip = d3.select("#tooltip");

node.on("mouseover", (event, d) => {{
  tooltip.style("display", "block")
    .style("left", (event.clientX + 16) + "px")
    .style("top", (event.clientY - 10) + "px");
  tooltip.select(".name").text(d.id);
  tooltip.select(".docs").text(`Documents: ${{d.doc_count}}`);
  tooltip.select(".connections").text(`Connections: ${{d.degree}} (${{d.weighted_degree}} weighted)`);
  tooltip.select(".sections").text(`Sections: ${{(d.sections || []).join(", ")}}`);
  tooltip.select(".role").text(d.role || "");

  // Highlight connected
  const connected = new Set();
  edges.forEach(e => {{
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    if (s === d.id) connected.add(t);
    if (t === d.id) connected.add(s);
  }});

  node.attr("fill-opacity", n => n.id === d.id || connected.has(n.id) ? 1 : 0.08);
  link.attr("stroke-opacity", e => {{
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    return s === d.id || t === d.id ? 0.7 : 0.03;
  }});
  link.attr("stroke", e => {{
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    return s === d.id || t === d.id ? "#8b5cf6" : "#1a1a3e";
  }});
  label.attr("fill-opacity", n => n.id === d.id || connected.has(n.id) ? 1 : 0.1);
}})
.on("mouseout", () => {{
  tooltip.style("display", "none");
  node.attr("fill-opacity", 0.85);
  link.attr("stroke-opacity", 0.4).attr("stroke", "#1a1a3e");
  label.attr("fill-opacity", 1);
}});

// Tick
simulation.on("tick", () => {{
  link
    .attr("x1", d => d.source.x)
    .attr("y1", d => d.source.y)
    .attr("x2", d => d.target.x)
    .attr("y2", d => d.target.y);
  node
    .attr("cx", d => d.x)
    .attr("cy", d => d.y);
  label
    .attr("x", d => d.x)
    .attr("y", d => d.y);
}});

function dragstarted(event, d) {{
  if (!event.active) simulation.alphaTarget(0.3).restart();
  d.fx = d.x;
  d.fy = d.y;
}}
function dragged(event, d) {{
  d.fx = event.x;
  d.fy = event.y;
}}
function dragended(event, d) {{
  if (!event.active) simulation.alphaTarget(0);
  d.fx = null;
  d.fy = null;
}}

// Controls
document.getElementById("node-count").textContent = nodes.length;
document.getElementById("edge-count").textContent = edges.length;

document.getElementById("search").addEventListener("input", function() {{
  const q = this.value.toLowerCase();
  if (!q) {{
    node.attr("fill-opacity", 0.85);
    link.attr("stroke-opacity", 0.4).attr("stroke", "#1a1a3e");
    label.attr("fill-opacity", 1);
    return;
  }}

  const matches = new Set();
  const connected = new Set();
  nodes.forEach(n => {{
    if (n.id.toLowerCase().includes(q)) matches.add(n.id);
  }});

  edges.forEach(e => {{
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    if (matches.has(s)) connected.add(t);
    if (matches.has(t)) connected.add(s);
  }});

  node.attr("fill-opacity", n =>
    matches.has(n.id) ? 1 : connected.has(n.id) ? 0.5 : 0.05);
  link.attr("stroke-opacity", e => {{
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    return matches.has(s) || matches.has(t) ? 0.6 : 0.02;
  }});
  label.attr("fill-opacity", n =>
    matches.has(n.id) ? 1 : connected.has(n.id) ? 0.6 : 0.05);
}});

document.getElementById("min-degree").addEventListener("input", function() {{
  document.getElementById("min-deg-val").textContent = this.value;
  updateVisibility();
}});

document.getElementById("min-weight").addEventListener("input", function() {{
  document.getElementById("min-wt-val").textContent = this.value;
  updateVisibility();
}});

function updateVisibility() {{
  const minDeg = +document.getElementById("min-degree").value;
  const minWt = +document.getElementById("min-weight").value;

  const visibleNodes = new Set();
  nodes.forEach(n => {{
    if (n.degree >= minDeg) visibleNodes.add(n.id);
  }});

  node.attr("display", n => visibleNodes.has(n.id) ? null : "none");
  label.attr("display", n => visibleNodes.has(n.id) && n.degree >= 15 ? null : "none");
  link.attr("display", e => {{
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    return visibleNodes.has(s) && visibleNodes.has(t) && e.weight >= minWt ? null : "none";
  }});

  const vn = nodes.filter(n => visibleNodes.has(n.id)).length;
  const ve = edges.filter(e => {{
    const s = typeof e.source === "object" ? e.source.id : e.source;
    const t = typeof e.target === "object" ? e.target.id : e.target;
    return visibleNodes.has(s) && visibleNodes.has(t) && e.weight >= minWt;
  }}).length;
  document.getElementById("node-count").textContent = vn;
  document.getElementById("edge-count").textContent = ve;
}}

// Center on Epstein initially
setTimeout(() => {{
  const epstein = nodes.find(n => n.id === "Jeffrey Epstein");
  if (epstein) {{
    const transform = d3.zoomIdentity
      .translate(width / 2, height / 2)
      .scale(1.5)
      .translate(-epstein.x, -epstein.y);
    svg.transition().duration(1000).call(zoom.transform, transform);
  }}
}}, 3000);
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
