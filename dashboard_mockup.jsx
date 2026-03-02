import { useState, useEffect, useRef } from "react";

// Mock data based on real corpus stats
const STATS = {
  docs: 26138,
  entities: 40749,
  connections: 9852,
  reports: 3,
};

const TOP_PEOPLE = [
  { name: "Jeffrey Epstein", count: 4821, type: "defendant" },
  { name: "Ghislaine Maxwell", count: 2104, type: "defendant" },
  { name: "Alan Dershowitz", count: 891, type: "legal" },
  { name: "Virginia Giuffre", count: 764, type: "victim" },
  { name: "Sarah Kellen", count: 612, type: "other" },
  { name: "Nadia Marcinkova", count: 498, type: "other" },
  { name: "Jean-Luc Brunel", count: 445, type: "defendant" },
  { name: "Les Wexner", count: 387, type: "other" },
  { name: "Alexander Acosta", count: 341, type: "legal" },
  { name: "Lesley Groff", count: 298, type: "other" },
  { name: "Eduardo Saverin", count: 156, type: "other" },
  { name: "Bill Richardson", count: 142, type: "other" },
];

const MOCK_REPORTS = [
  {
    question: "What financial connections appear in the corpus?",
    summary: "Analysis reveals a complex financial network centered on NES LLC and multiple shell entities. Estate valued at $634M with structured disbursements through JP Morgan Chase and Deutsche Bank accounts.",
    findings: 5,
    cost: "$0.024",
    date: "2026-02-28",
    duration: "14.2s",
  },
  {
    question: "Timeline of key events for Jeffrey Epstein",
    summary: "50 chronological events spanning 2005-2023, from initial Palm Beach investigation through federal indictment, detention, death, and subsequent document releases.",
    findings: 8,
    cost: "$0.031",
    date: "2026-02-28",
    duration: "18.7s",
  },
  {
    question: "What redaction patterns exist across the corpus?",
    summary: "Two-tier redaction system identified: law enforcement redactions (FOIA b7) concentrated in investigation files, privacy redactions (b6/b7C) in witness statements. 34% of documents contain significant redactions.",
    findings: 4,
    cost: "$0.019",
    date: "2026-02-28",
    duration: "11.3s",
  },
];

const ENTITY_TYPES = [
  { type: "person", count: 22298, color: "#8b5cf6" },
  { type: "organization", count: 8941, color: "#3b82f6" },
  { type: "location", count: 5204, color: "#10b981" },
  { type: "date_event", count: 3102, color: "#f59e0b" },
  { type: "financial", count: 1204, color: "#ef4444" },
];

const NETWORK_NODES = [];
const NETWORK_LINKS = [];

// Generate mock network for the mini map
const nodeNames = TOP_PEOPLE.map((p) => p.name);
for (let i = 0; i < nodeNames.length; i++) {
  NETWORK_NODES.push({
    id: nodeNames[i],
    type: TOP_PEOPLE[i].type,
    size: TOP_PEOPLE[i].count,
  });
}
for (let i = 0; i < nodeNames.length; i++) {
  for (let j = i + 1; j < nodeNames.length; j++) {
    if (Math.random() < 0.4) {
      NETWORK_LINKS.push({
        source: i,
        target: j,
        weight: Math.floor(Math.random() * 20) + 1,
      });
    }
  }
}

const typeColors = {
  defendant: "#ef4444",
  legal: "#3b82f6",
  victim: "#f59e0b",
  law_enforcement: "#10b981",
  witness: "#a855f7",
  other: "#6b7280",
};

// Simple force simulation for mini network
function useMiniNetwork(canvasRef, width, height) {
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    canvas.width = width * 2;
    canvas.height = height * 2;
    ctx.scale(2, 2);

    const nodes = NETWORK_NODES.map((n, i) => ({
      ...n,
      x: width / 2 + (Math.random() - 0.5) * width * 0.6,
      y: height / 2 + (Math.random() - 0.5) * height * 0.6,
      vx: 0,
      vy: 0,
      r: Math.max(4, Math.sqrt(n.size) * 0.3),
    }));

    const links = NETWORK_LINKS.map((l) => ({ ...l }));

    let frame;
    let tick = 0;

    function simulate() {
      tick++;
      // Center gravity
      nodes.forEach((n) => {
        n.vx += (width / 2 - n.x) * 0.001;
        n.vy += (height / 2 - n.y) * 0.001;
      });
      // Repulsion
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const dx = nodes[j].x - nodes[i].x;
          const dy = nodes[j].y - nodes[i].y;
          const dist = Math.sqrt(dx * dx + dy * dy) || 1;
          const force = -200 / (dist * dist);
          nodes[i].vx += (dx / dist) * force;
          nodes[i].vy += (dy / dist) * force;
          nodes[j].vx -= (dx / dist) * force;
          nodes[j].vy -= (dy / dist) * force;
        }
      }
      // Link attraction
      links.forEach((l) => {
        const s = nodes[l.source];
        const t = nodes[l.target];
        const dx = t.x - s.x;
        const dy = t.y - s.y;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = (dist - 60) * 0.003;
        s.vx += (dx / dist) * force;
        s.vy += (dy / dist) * force;
        t.vx -= (dx / dist) * force;
        t.vy -= (dy / dist) * force;
      });
      // Apply velocity
      nodes.forEach((n) => {
        n.vx *= 0.9;
        n.vy *= 0.9;
        n.x += n.vx;
        n.y += n.vy;
        n.x = Math.max(n.r, Math.min(width - n.r, n.x));
        n.y = Math.max(n.r, Math.min(height - n.r, n.y));
      });

      // Draw
      ctx.clearRect(0, 0, width, height);

      // Links
      links.forEach((l) => {
        const s = nodes[l.source];
        const t = nodes[l.target];
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(t.x, t.y);
        ctx.strokeStyle = "rgba(139, 92, 246, 0.12)";
        ctx.lineWidth = Math.max(0.5, l.weight / 10);
        ctx.stroke();
      });

      // Nodes
      nodes.forEach((n) => {
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2);
        ctx.fillStyle = typeColors[n.type] || typeColors.other;
        ctx.globalAlpha = 0.85;
        ctx.fill();
        ctx.globalAlpha = 1;
      });

      // Labels for big nodes
      nodes.forEach((n) => {
        if (n.r > 8) {
          ctx.font = "9px -apple-system, sans-serif";
          ctx.fillStyle = "#ccc";
          ctx.textAlign = "center";
          ctx.fillText(
            n.id.split(" ").pop(),
            n.x,
            n.y - n.r - 3
          );
        }
      });

      if (tick < 200) frame = requestAnimationFrame(simulate);
    }

    frame = requestAnimationFrame(simulate);
    return () => cancelAnimationFrame(frame);
  }, [canvasRef, width, height]);
}

// Components
function StatCard({ label, value, icon }) {
  const [display, setDisplay] = useState(0);
  useEffect(() => {
    const target = typeof value === "number" ? value : 0;
    if (target === 0) return;
    let start = 0;
    const step = Math.ceil(target / 40);
    const timer = setInterval(() => {
      start += step;
      if (start >= target) {
        setDisplay(target);
        clearInterval(timer);
      } else {
        setDisplay(start);
      }
    }, 30);
    return () => clearInterval(timer);
  }, [value]);

  const fmt =
    typeof value === "number"
      ? display >= 1000
        ? (display / 1000).toFixed(1) + "k"
        : display.toString()
      : value;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 text-center">
      <div className="text-3xl mb-1">{icon}</div>
      <div className="text-2xl font-bold text-purple-400">{fmt}</div>
      <div className="text-xs text-gray-500 mt-1">{label}</div>
    </div>
  );
}

function MiniNetworkMap() {
  const canvasRef = useRef(null);
  useMiniNetwork(canvasRef, 400, 250);
  return (
    <canvas
      ref={canvasRef}
      style={{ width: 400, height: 250 }}
      className="rounded-lg"
    />
  );
}

function EntityBar({ name, count, maxCount, type }) {
  const pct = (count / maxCount) * 100;
  return (
    <div className="flex items-center mb-2">
      <div
        className="w-3 h-3 rounded-full mr-2 flex-shrink-0"
        style={{ background: typeColors[type] }}
      />
      <div className="w-36 text-xs text-gray-400 truncate">{name}</div>
      <div className="flex-1 h-4 bg-gray-900 rounded overflow-hidden mx-2">
        <div
          className="h-full rounded"
          style={{
            width: `${pct}%`,
            background: `linear-gradient(90deg, ${typeColors[type]}88, ${typeColors[type]})`,
            transition: "width 1s ease",
          }}
        />
      </div>
      <div className="w-14 text-xs text-gray-600 text-right">
        {count.toLocaleString()}
      </div>
    </div>
  );
}

function InvestigationPanel({ onRun }) {
  const [query, setQuery] = useState("");
  const [running, setRunning] = useState(false);

  const presets = [
    "Who are the most connected people outside Epstein's inner circle?",
    "What financial entities appear across multiple document sections?",
    "Which documents have the heaviest redactions and why?",
    "Map the timeline of the plea deal negotiations",
    "What travel patterns emerge from the flight logs?",
  ];

  const handleRun = (q) => {
    setRunning(true);
    setTimeout(() => setRunning(false), 3000);
    onRun && onRun(q);
  };

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-purple-400 uppercase tracking-wider">
          Run Investigation
        </h3>
        <span className="text-xs text-gray-600">Agent Swarm</span>
      </div>

      <div className="flex gap-2 mb-4">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ask a question about the corpus..."
          className="flex-1 bg-gray-950 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 placeholder-gray-600 focus:outline-none focus:border-purple-500"
          onKeyDown={(e) => e.key === "Enter" && query && handleRun(query)}
        />
        <button
          onClick={() => query && handleRun(query)}
          disabled={!query || running}
          className="px-4 py-2 bg-purple-600 hover:bg-purple-500 disabled:bg-gray-700 disabled:text-gray-500 text-white text-sm font-medium rounded-lg transition-colors"
        >
          {running ? "Running..." : "Investigate"}
        </button>
      </div>

      <div className="space-y-1.5">
        {presets.map((p, i) => (
          <button
            key={i}
            onClick={() => {
              setQuery(p);
              handleRun(p);
            }}
            className="block w-full text-left text-xs text-gray-500 hover:text-purple-400 hover:bg-gray-800 rounded px-2 py-1.5 transition-colors"
          >
            {p}
          </button>
        ))}
      </div>

      {running && (
        <div className="mt-4 border border-purple-900 bg-purple-950 rounded-lg p-3">
          <div className="flex items-center gap-2 mb-2">
            <div className="w-2 h-2 bg-purple-400 rounded-full animate-pulse" />
            <span className="text-xs text-purple-300">
              Swarm active — 4 agents working
            </span>
          </div>
          <div className="space-y-1 text-xs text-gray-500">
            <div>
              → NetworkMapper: building co-occurrence graph...
            </div>
            <div>→ DocumentQuery: searching corpus...</div>
            <div>→ TimelineBuilder: extracting events...</div>
            <div>→ Coordinator: planning investigation...</div>
          </div>
        </div>
      )}
    </div>
  );
}

function ReportCard({ report }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      className="bg-gray-900 border border-gray-800 rounded-xl p-5 cursor-pointer hover:border-gray-700 transition-colors"
      onClick={() => setExpanded(!expanded)}
    >
      <h4 className="text-sm font-medium text-gray-200 mb-2">
        {report.question}
      </h4>
      <p className="text-xs text-gray-500 leading-relaxed mb-3">
        {report.summary}
      </p>
      <div className="flex gap-4 text-xs text-gray-600">
        <span>{report.findings} findings</span>
        <span>{report.duration}</span>
        <span>{report.cost}</span>
        <span>{report.date}</span>
      </div>
      {expanded && (
        <div className="mt-3 pt-3 border-t border-gray-800 text-xs text-gray-500">
          Click "View Full Report" to see complete findings, evidence, and
          source documents.
          <button className="block mt-2 text-purple-400 hover:text-purple-300">
            View Full Report →
          </button>
        </div>
      )}
    </div>
  );
}

// Main Dashboard
export default function Dashboard() {
  const [activeTab, setActiveTab] = useState("overview");
  const [hoveredEntity, setHoveredEntity] = useState(null);

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "network", label: "Relationship Map" },
    { id: "investigate", label: "Investigate" },
    { id: "reports", label: "Reports" },
    { id: "entities", label: "Entities" },
  ];

  return (
    <div
      className="min-h-screen text-gray-200"
      style={{ background: "#0a0a0f" }}
    >
      {/* Nav */}
      <nav
        className="fixed top-0 left-0 right-0 z-50 border-b border-gray-800 px-6 flex items-center h-12"
        style={{ background: "rgba(10,10,15,0.95)", backdropFilter: "blur(12px)" }}
      >
        <span className="text-sm font-bold text-purple-400 tracking-wider mr-8">
          E-FINDER
        </span>
        <div className="flex gap-1">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setActiveTab(t.id)}
              className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                activeTab === t.id
                  ? "bg-gray-800 text-gray-200"
                  : "text-gray-500 hover:text-gray-300"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
        <div className="ml-auto text-xs text-gray-600">
          DOJ Epstein Document Corpus — 26,138 docs
        </div>
      </nav>

      <div className="pt-12">
        {/* ─── OVERVIEW ─── */}
        {activeTab === "overview" && (
          <div className="max-w-6xl mx-auto px-6 py-8">
            {/* Stats */}
            <div className="grid grid-cols-4 gap-4 mb-8">
              <StatCard
                label="Documents Analyzed"
                value={STATS.docs}
                icon="📄"
              />
              <StatCard
                label="Entities Extracted"
                value={STATS.entities}
                icon="🏷️"
              />
              <StatCard
                label="Network Connections"
                value={STATS.connections}
                icon="🔗"
              />
              <StatCard
                label="Investigations Run"
                value={STATS.reports}
                icon="🔍"
              />
            </div>

            <div className="grid grid-cols-5 gap-6">
              {/* Left: Network preview + investigate */}
              <div className="col-span-3 space-y-6">
                {/* Mini network */}
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-semibold text-purple-400 uppercase tracking-wider">
                      Network Preview
                    </h3>
                    <button
                      onClick={() => setActiveTab("network")}
                      className="text-xs text-gray-500 hover:text-purple-400"
                    >
                      Open Full Map →
                    </button>
                  </div>
                  <div
                    className="rounded-lg overflow-hidden"
                    style={{ background: "#050508" }}
                  >
                    <MiniNetworkMap />
                  </div>
                  <div className="flex gap-4 mt-3">
                    {Object.entries(typeColors).map(([type, color]) => (
                      <div
                        key={type}
                        className="flex items-center gap-1.5 text-xs text-gray-500"
                      >
                        <div
                          className="w-2 h-2 rounded-full"
                          style={{ background: color }}
                        />
                        {type.replace("_", " ")}
                      </div>
                    ))}
                  </div>
                </div>

                {/* Investigation panel */}
                <InvestigationPanel />
              </div>

              {/* Right: Top people + entity breakdown */}
              <div className="col-span-2 space-y-6">
                {/* Top people */}
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <div className="flex items-center justify-between mb-4">
                    <h3 className="text-sm font-semibold text-purple-400 uppercase tracking-wider">
                      Most Referenced People
                    </h3>
                    <button
                      onClick={() => setActiveTab("entities")}
                      className="text-xs text-gray-500 hover:text-purple-400"
                    >
                      View All →
                    </button>
                  </div>
                  {TOP_PEOPLE.slice(0, 10).map((p, i) => (
                    <EntityBar
                      key={i}
                      name={p.name}
                      count={p.count}
                      maxCount={TOP_PEOPLE[0].count}
                      type={p.type}
                    />
                  ))}
                </div>

                {/* Entity type breakdown */}
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <h3 className="text-sm font-semibold text-purple-400 uppercase tracking-wider mb-4">
                    Entity Breakdown
                  </h3>
                  {ENTITY_TYPES.map((e, i) => (
                    <div key={i} className="flex items-center justify-between mb-2.5">
                      <div className="flex items-center gap-2">
                        <div
                          className="w-2.5 h-2.5 rounded"
                          style={{ background: e.color }}
                        />
                        <span className="text-xs text-gray-400 capitalize">
                          {e.type.replace("_", " ")}
                        </span>
                      </div>
                      <span className="text-xs text-gray-500">
                        {e.count.toLocaleString()}
                      </span>
                    </div>
                  ))}
                </div>

                {/* Recent report */}
                <div className="bg-gray-900 border border-gray-800 rounded-xl p-5">
                  <div className="flex items-center justify-between mb-3">
                    <h3 className="text-sm font-semibold text-purple-400 uppercase tracking-wider">
                      Latest Report
                    </h3>
                    <button
                      onClick={() => setActiveTab("reports")}
                      className="text-xs text-gray-500 hover:text-purple-400"
                    >
                      All Reports →
                    </button>
                  </div>
                  <p className="text-xs text-gray-300 font-medium mb-1">
                    {MOCK_REPORTS[0].question}
                  </p>
                  <p className="text-xs text-gray-600 leading-relaxed">
                    {MOCK_REPORTS[0].summary.slice(0, 120)}...
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* ─── NETWORK ─── */}
        {activeTab === "network" && (
          <div className="flex items-center justify-center" style={{ height: "calc(100vh - 48px)" }}>
            <div className="text-center">
              <div
                className="rounded-xl overflow-hidden border border-gray-800 mb-4"
                style={{ background: "#050508" }}
              >
                <MiniNetworkMap />
              </div>
              <p className="text-sm text-gray-400 mb-2">
                Full interactive D3.js force-directed graph
              </p>
              <p className="text-xs text-gray-600 max-w-md">
                The live version loads 300 nodes and 2,000+ edges from MongoDB
                with search, filtering, hover highlighting, and zoom. This is
                the same view you already have — served directly via the
                shareable URL.
              </p>
            </div>
          </div>
        )}

        {/* ─── INVESTIGATE ─── */}
        {activeTab === "investigate" && (
          <div className="max-w-3xl mx-auto px-6 py-8">
            <h2 className="text-lg font-semibold text-white mb-2">
              Run an Investigation
            </h2>
            <p className="text-sm text-gray-500 mb-6">
              Ask any question and the agent swarm will coordinate across the
              full corpus — querying documents, building timelines, mapping
              networks, and analyzing redactions.
            </p>
            <InvestigationPanel />

            <div className="mt-8">
              <h3 className="text-sm font-semibold text-gray-400 mb-4">
                Available Agents
              </h3>
              <div className="grid grid-cols-2 gap-3">
                {[
                  {
                    name: "Network Mapper",
                    desc: "Builds co-occurrence graphs, computes centrality, detects communities",
                    icon: "🕸️",
                  },
                  {
                    name: "Document Query",
                    desc: "Natural language search across all 26K documents",
                    icon: "📄",
                  },
                  {
                    name: "Timeline Builder",
                    desc: "Extracts and orders chronological events for any subject",
                    icon: "📅",
                  },
                  {
                    name: "Redaction Analyst",
                    desc: "Analyzes redaction patterns, FOIA codes, and information gaps",
                    icon: "🔍",
                  },
                ].map((a, i) => (
                  <div
                    key={i}
                    className="bg-gray-900 border border-gray-800 rounded-lg p-4"
                  >
                    <div className="text-lg mb-1">{a.icon}</div>
                    <div className="text-sm font-medium text-gray-300">
                      {a.name}
                    </div>
                    <div className="text-xs text-gray-600 mt-1">{a.desc}</div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ─── REPORTS ─── */}
        {activeTab === "reports" && (
          <div className="max-w-3xl mx-auto px-6 py-8">
            <h2 className="text-lg font-semibold text-white mb-2">
              Investigation Reports
            </h2>
            <p className="text-sm text-gray-500 mb-6">
              AI-generated analysis from the agent swarm
            </p>
            <div className="space-y-3">
              {MOCK_REPORTS.map((r, i) => (
                <ReportCard key={i} report={r} />
              ))}
            </div>
          </div>
        )}

        {/* ─── ENTITIES ─── */}
        {activeTab === "entities" && (
          <div className="max-w-4xl mx-auto px-6 py-8">
            <h2 className="text-lg font-semibold text-white mb-2">
              Entity Explorer
            </h2>
            <p className="text-sm text-gray-500 mb-6">
              Browse all {STATS.entities.toLocaleString()} extracted entities across the corpus
            </p>

            <div className="flex gap-2 mb-6">
              {["All", "Person", "Organization", "Location", "Financial"].map(
                (f) => (
                  <button
                    key={f}
                    className="px-3 py-1.5 text-xs font-medium rounded-md bg-gray-800 text-gray-400 hover:text-gray-200 transition-colors"
                  >
                    {f}
                  </button>
                )
              )}
              <input
                type="text"
                placeholder="Search entities..."
                className="ml-auto bg-gray-900 border border-gray-700 rounded-lg px-3 py-1.5 text-xs text-gray-200 placeholder-gray-600 w-64 focus:outline-none focus:border-purple-500"
              />
            </div>

            <div className="bg-gray-900 border border-gray-800 rounded-xl overflow-hidden">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-gray-800">
                    <th className="text-left p-3 text-gray-500 font-medium">
                      Name
                    </th>
                    <th className="text-left p-3 text-gray-500 font-medium">
                      Type
                    </th>
                    <th className="text-right p-3 text-gray-500 font-medium">
                      Documents
                    </th>
                    <th className="text-right p-3 text-gray-500 font-medium">
                      Connections
                    </th>
                    <th className="text-left p-3 text-gray-500 font-medium">
                      Context
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {TOP_PEOPLE.map((p, i) => (
                    <tr
                      key={i}
                      className="border-b border-gray-800 hover:bg-gray-800 cursor-pointer transition-colors"
                      onMouseEnter={() => setHoveredEntity(p.name)}
                      onMouseLeave={() => setHoveredEntity(null)}
                    >
                      <td className="p-3 font-medium text-gray-300">
                        {p.name}
                      </td>
                      <td className="p-3">
                        <span
                          className="inline-flex items-center gap-1"
                          style={{ color: typeColors[p.type] }}
                        >
                          <span
                            className="w-1.5 h-1.5 rounded-full"
                            style={{ background: typeColors[p.type] }}
                          />
                          {p.type}
                        </span>
                      </td>
                      <td className="p-3 text-right text-gray-400">
                        {p.count.toLocaleString()}
                      </td>
                      <td className="p-3 text-right text-gray-400">
                        {Math.floor(p.count * 0.3)}
                      </td>
                      <td className="p-3 text-gray-600 truncate max-w-xs">
                        {p.type === "defendant"
                          ? "Named in criminal proceedings"
                          : p.type === "legal"
                            ? "Attorney/counsel in proceedings"
                            : p.type === "victim"
                              ? "Identified in victim statements"
                              : "Referenced in multiple document sections"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
