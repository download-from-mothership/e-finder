#!/usr/bin/env python3
"""
E-FINDER — Geospatial Agent
=============================
Adapted from IntellYWeave's Geospatial Analyst agent.

Analyzes location intelligence from the Epstein corpus:
  - Frequency and co-occurrence of locations with key entities
  - Geographic clustering (which locations appear together)
  - Timeline of locations (when did activity occur where)
  - Geocoding of location names to lat/lon coordinates
  - GeoJSON export for Mapbox GL visualization

The Epstein corpus contains rich location data:
  - Islands (Little St. James, Great St. James, Palm Beach)
  - Properties (Manhattan townhouse, New Mexico ranch, Paris apartment)
  - Flight destinations (from flight logs)
  - Meeting locations (hotels, offices, residences)

Output:
  - MongoDB `location_intelligence` collection
  - GeoJSON file for Mapbox GL / web dashboard
  - Summary statistics per location

Usage:
  export MONGODB_URI="mongodb+srv://..."
  export ANTHROPIC_API_KEY="sk-ant-..."
  export GEOCODING_API_KEY=""   # Optional: OpenCage or Nominatim (free)

  python3 geospatial_agent.py --analyze              # Full analysis
  python3 geospatial_agent.py --analyze --subject "Jeffrey Epstein"
  python3 geospatial_agent.py --export-geojson       # Export for Mapbox
  python3 geospatial_agent.py --stats                # Show stats

Integration with swarm.py:
  from geospatial_agent import GeospatialAnalystAgent
  AGENT_REGISTRY["geospatial_analyst"] = GeospatialAnalystAgent
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
MONGODB_URI = os.environ.get("MONGODB_URI", "")
DATABASE_NAME = "doj_investigation"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GEOCODING_API_KEY = os.environ.get("GEOCODING_API_KEY", "")
MODEL = "claude-sonnet-4-5-20250929"

# Known Epstein-related locations with pre-seeded coordinates
# (avoids geocoding API calls for the most important locations)
KNOWN_LOCATIONS = {
    "little st. james": {"lat": 18.2997, "lon": -64.8250, "type": "island", "alias": "Epstein Island"},
    "little saint james": {"lat": 18.2997, "lon": -64.8250, "type": "island", "alias": "Epstein Island"},
    "great st. james": {"lat": 18.3100, "lon": -64.8100, "type": "island"},
    "palm beach": {"lat": 26.7056, "lon": -80.0364, "type": "city"},
    "palm beach, florida": {"lat": 26.7056, "lon": -80.0364, "type": "city"},
    "new york": {"lat": 40.7128, "lon": -74.0060, "type": "city"},
    "new york city": {"lat": 40.7128, "lon": -74.0060, "type": "city"},
    "manhattan": {"lat": 40.7831, "lon": -73.9712, "type": "borough"},
    "new mexico": {"lat": 34.5199, "lon": -105.8701, "type": "state"},
    "zorro ranch": {"lat": 33.8734, "lon": -106.6562, "type": "property", "alias": "Epstein Ranch NM"},
    "paris": {"lat": 48.8566, "lon": 2.3522, "type": "city"},
    "london": {"lat": 51.5074, "lon": -0.1278, "type": "city"},
    "florida": {"lat": 27.9944, "lon": -81.7603, "type": "state"},
    "virgin islands": {"lat": 18.3358, "lon": -64.8963, "type": "territory"},
    "us virgin islands": {"lat": 18.3358, "lon": -64.8963, "type": "territory"},
    "cambridge": {"lat": 42.3736, "lon": -71.1097, "type": "city"},
    "cambridge, massachusetts": {"lat": 42.3736, "lon": -71.1097, "type": "city"},
    "washington": {"lat": 38.9072, "lon": -77.0369, "type": "city"},
    "washington, d.c.": {"lat": 38.9072, "lon": -77.0369, "type": "city"},
    "washington dc": {"lat": 38.9072, "lon": -77.0369, "type": "city"},
    "miami": {"lat": 25.7617, "lon": -80.1918, "type": "city"},
    "los angeles": {"lat": 34.0522, "lon": -118.2437, "type": "city"},
    "santa fe": {"lat": 35.6870, "lon": -105.9378, "type": "city"},
    "stanley, new mexico": {"lat": 35.1481, "lon": -105.9783, "type": "town"},
}


# ── Dependency bootstrap ──────────────────────────────────────────────────────
def _install(pkg):
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          pkg, "--break-system-packages", "-q"])


try:
    from pymongo import MongoClient, UpdateOne
    from pymongo.database import Database
except ImportError:
    _install("pymongo")
    from pymongo import MongoClient, UpdateOne
    from pymongo.database import Database

try:
    import anthropic
except ImportError:
    _install("anthropic")
    import anthropic

try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# ── AgentResult (mirrors swarm.py) ───────────────────────────────────────────
@dataclass
class AgentResult:
    agent_name: str
    task: dict
    findings: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    confidence: float = 0.0
    open_questions: list = field(default_factory=list)
    duration_seconds: float = 0.0
    api_calls: int = 0
    estimated_cost: float = 0.0
    error: Optional[str] = None


# ── Geocoding ─────────────────────────────────────────────────────────────────
def geocode_location(name: str) -> Optional[dict]:
    """
    Geocode a location name to lat/lon.
    Uses pre-seeded known locations first, then Nominatim (free, no key needed).
    """
    # Check known locations first
    name_lower = name.lower().strip()
    for known_name, coords in KNOWN_LOCATIONS.items():
        if known_name in name_lower or name_lower in known_name:
            return {
                "lat": coords["lat"],
                "lon": coords["lon"],
                "type": coords.get("type", "place"),
                "display_name": coords.get("alias", name),
                "source": "known_locations",
            }

    # Fall back to Nominatim (OpenStreetMap, free, no API key)
    if not REQUESTS_AVAILABLE:
        return None

    try:
        url = "https://nominatim.openstreetmap.org/search"
        params = {
            "q": name,
            "format": "json",
            "limit": 1,
            "addressdetails": 1,
        }
        headers = {"User-Agent": "e-finder-osint/1.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=5)
        resp.raise_for_status()
        results = resp.json()
        if results:
            r = results[0]
            return {
                "lat": float(r["lat"]),
                "lon": float(r["lon"]),
                "type": r.get("type", "place"),
                "display_name": r.get("display_name", name),
                "source": "nominatim",
            }
    except Exception as e:
        log.debug("Geocoding failed for '%s': %s", name, e)

    return None


# ── Location analysis ─────────────────────────────────────────────────────────
def analyze_locations(db: Database, subject: Optional[str] = None,
                      doc_ids: Optional[list] = None) -> list[dict]:
    """
    Aggregate location data from MongoDB entities collection.
    Returns a list of location records with frequency, co-occurring entities,
    and geocoordinates.
    """
    match_stage = {"entity_type": "location"}
    if doc_ids:
        match_stage["source_doc_id"] = {"$in": doc_ids}
    if subject:
        # Find doc_ids that mention the subject
        subject_doc_ids = [
            r["_id"] for r in db["entities"].aggregate([
                {"$match": {"name_lower": {"$regex": subject.lower()}}},
                {"$group": {"_id": "$source_doc_id"}},
            ])
        ]
        if subject_doc_ids:
            match_stage["source_doc_id"] = {"$in": subject_doc_ids}

    pipeline = [
        {"$match": match_stage},
        {"$group": {
            "_id": "$name",
            "frequency": {"$sum": 1},
            "doc_ids": {"$addToSet": "$source_doc_id"},
            "contexts": {"$push": "$context"},
            "sections": {"$addToSet": "$section"},
        }},
        {"$sort": {"frequency": -1}},
        {"$limit": 100},
    ]

    locations = list(db["entities"].aggregate(pipeline, allowDiskUse=True))

    # Also include GLiNER-extracted locations
    gliner_locs = {}
    gliner_match = {}
    if doc_ids:
        gliner_match["doc_id"] = {"$in": doc_ids}
    for rec in db["gliner_entities"].find(gliner_match, {"doc_id": 1, "locations": 1}):
        for loc in rec.get("locations", []):
            if loc not in gliner_locs:
                gliner_locs[loc] = {"count": 0, "doc_ids": set()}
            gliner_locs[loc]["count"] += 1
            gliner_locs[loc]["doc_ids"].add(rec["doc_id"])

    # Merge GLiNER locations
    existing_names = {l["_id"].lower() for l in locations}
    for loc_name, data in gliner_locs.items():
        if loc_name.lower() not in existing_names:
            locations.append({
                "_id": loc_name,
                "frequency": data["count"],
                "doc_ids": list(data["doc_ids"]),
                "contexts": [],
                "sections": [],
                "source": "gliner",
            })

    # Geocode and build final records
    results = []
    for loc in locations:
        name = loc["_id"]
        if not name or len(name) < 2:
            continue

        coords = geocode_location(name)
        time.sleep(0.1)  # Rate limit for Nominatim

        # Get co-occurring persons for this location
        co_persons = []
        if loc.get("doc_ids"):
            person_pipeline = [
                {"$match": {
                    "entity_type": "person",
                    "source_doc_id": {"$in": list(loc["doc_ids"])[:50]},
                }},
                {"$group": {"_id": "$name", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
                {"$limit": 10},
            ]
            co_persons = [r["_id"] for r in db["entities"].aggregate(person_pipeline)]

        record = {
            "name": name,
            "frequency": loc["frequency"],
            "doc_count": len(set(loc.get("doc_ids", []))),
            "sections": list(set(loc.get("sections", []))),
            "co_occurring_persons": co_persons,
            "sample_context": (loc.get("contexts") or [""])[:1],
            "geocoded": coords is not None,
            "source": loc.get("source", "claude_extraction"),
        }

        if coords:
            record.update({
                "lat": coords["lat"],
                "lon": coords["lon"],
                "location_type": coords.get("type", "place"),
                "display_name": coords.get("display_name", name),
                "geocoding_source": coords.get("source", "unknown"),
            })

        results.append(record)

    return results


# ── GeoJSON export ────────────────────────────────────────────────────────────
def export_geojson(locations: list[dict], output_path: str):
    """
    Export location data as GeoJSON for Mapbox GL visualization.
    Only includes geocoded locations.
    """
    features = []
    for loc in locations:
        if not loc.get("geocoded") or not loc.get("lat"):
            continue

        feature = {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [loc["lon"], loc["lat"]],
            },
            "properties": {
                "name": loc["name"],
                "display_name": loc.get("display_name", loc["name"]),
                "frequency": loc["frequency"],
                "doc_count": loc["doc_count"],
                "location_type": loc.get("location_type", "place"),
                "co_occurring_persons": loc.get("co_occurring_persons", [])[:5],
                "sections": loc.get("sections", []),
                # Mapbox GL circle radius (log scale)
                "radius": min(30, max(5, 5 + loc["frequency"] // 5)),
            },
        }
        features.append(feature)

    geojson = {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_locations": len(features),
            "source": "e-finder geospatial agent",
        },
    }

    with open(output_path, "w") as f:
        json.dump(geojson, f, indent=2)

    log.info("GeoJSON exported: %s (%d features)", output_path, len(features))
    return geojson


# ── Claude synthesis ──────────────────────────────────────────────────────────
def synthesize_location_intelligence(claude_client, locations: list[dict],
                                     subject: Optional[str] = None) -> dict:
    """Ask Claude to identify significant geographic patterns."""
    top_locations = [
        {
            "name": l["name"],
            "frequency": l["frequency"],
            "doc_count": l["doc_count"],
            "co_persons": l.get("co_occurring_persons", [])[:5],
            "geocoded": l.get("geocoded", False),
        }
        for l in locations[:30]
    ]

    prompt = f"""Analyze these location patterns from the DOJ Epstein investigation corpus.
{f'Focus on: {subject}' if subject else ''}

TOP LOCATIONS:
{json.dumps(top_locations, indent=2)}

Identify significant geographic patterns. Return JSON:
{{
  "geographic_summary": "2-3 sentence summary of the geographic footprint",
  "key_locations": [
    {{
      "name": "location name",
      "significance": "why this location matters",
      "activity_type": "what happened here",
      "confidence": 0.9
    }}
  ],
  "geographic_clusters": [
    {{
      "cluster": "region name",
      "locations": ["loc1", "loc2"],
      "significance": "what this cluster represents"
    }}
  ],
  "movement_patterns": ["any notable patterns in geographic movement"],
  "anomalies": ["any unusual geographic patterns"],
  "open_questions": ["what location data is missing or unclear"]
}}"""

    try:
        message = claude_client.messages.create(
            model=MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        return json.loads(text)
    except Exception as e:
        log.error("Location synthesis failed: %s", e)
        return {"geographic_summary": f"Synthesis failed: {e}"}


# ── Main agent class ──────────────────────────────────────────────────────────
class GeospatialAnalystAgent:
    """
    Geospatial analyst agent — integrates with swarm.py Coordinator.
    Implements the same Agent interface as other swarm agents.
    """

    name = "geospatial_analyst"
    description = ("Analyze location patterns across the corpus — frequency, "
                   "co-occurrence with entities, geocoding, and GeoJSON export "
                   "for map visualization.")
    requires_claude = True

    def __init__(self, db: Database, claude_client):
        self.db = db
        self.claude = claude_client

    def run(self, task: dict) -> AgentResult:
        start = time.time()
        result = AgentResult(agent_name=self.name, task=task)

        subject = task.get("subject")
        doc_ids = task.get("doc_ids")
        export_path = task.get("export_geojson")

        log.info("GeospatialAnalystAgent: subject=%s, doc_ids=%s",
                 subject, f"{len(doc_ids)} docs" if doc_ids else "all")

        # Analyze locations
        locations = analyze_locations(self.db, subject=subject, doc_ids=doc_ids)
        log.info("Found %d locations (%d geocoded)",
                 len(locations), sum(1 for l in locations if l.get("geocoded")))

        # Store in MongoDB
        if locations:
            ops = [
                UpdateOne(
                    {"name": l["name"]},
                    {"$set": {**l, "analyzed_at": datetime.now(timezone.utc)}},
                    upsert=True,
                )
                for l in locations
            ]
            self.db["location_intelligence"].bulk_write(ops, ordered=False)
            self.db["location_intelligence"].create_index("name", unique=True)
            self.db["location_intelligence"].create_index("frequency")
            log.info("Stored %d locations in location_intelligence collection", len(locations))

        # Export GeoJSON if requested
        geojson_path = export_path or "location_intelligence.geojson"
        export_geojson(locations, geojson_path)

        # Claude synthesis
        synthesis = synthesize_location_intelligence(self.claude, locations, subject)
        result.api_calls += 1

        # Build findings
        result.findings = [
            {
                "type": "geographic_summary",
                "content": synthesis.get("geographic_summary", ""),
                "key_locations": synthesis.get("key_locations", []),
                "clusters": synthesis.get("geographic_clusters", []),
                "movement_patterns": synthesis.get("movement_patterns", []),
                "anomalies": synthesis.get("anomalies", []),
                "geojson_path": geojson_path,
            },
            {
                "type": "location_stats",
                "total_locations": len(locations),
                "geocoded_count": sum(1 for l in locations if l.get("geocoded")),
                "top_locations": [
                    {
                        "name": l["name"],
                        "frequency": l["frequency"],
                        "doc_count": l["doc_count"],
                        "geocoded": l.get("geocoded", False),
                        "co_persons": l.get("co_occurring_persons", [])[:3],
                    }
                    for l in locations[:20]
                ],
            },
        ]

        result.open_questions = synthesis.get("open_questions", [])
        result.confidence = 0.8
        result.duration_seconds = time.time() - start
        return result


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="E-FINDER Geospatial Agent — location intelligence analysis"
    )
    parser.add_argument("--analyze",        action="store_true",
                        help="Run full location analysis")
    parser.add_argument("--subject",        type=str,
                        help="Focus analysis on a specific person/topic")
    parser.add_argument("--export-geojson", type=str,
                        default="location_intelligence.geojson",
                        help="Export GeoJSON to this path")
    parser.add_argument("--stats",          action="store_true",
                        help="Show location statistics from MongoDB")
    parser.add_argument("--geocode",        type=str,
                        help="Test geocoding for a location name")
    args = parser.parse_args()

    if args.geocode:
        result = geocode_location(args.geocode)
        print(f"Geocoding '{args.geocode}': {result}")
        return

    if not MONGODB_URI:
        print("ERROR: Set MONGODB_URI environment variable")
        sys.exit(1)

    mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    db = mongo[DATABASE_NAME]

    if args.stats:
        total = db["location_intelligence"].count_documents({})
        geocoded = db["location_intelligence"].count_documents({"geocoded": True})
        print(f"\n  location_intelligence: {total:,} locations ({geocoded:,} geocoded)")
        top = list(db["location_intelligence"].find(
            {}, {"name": 1, "frequency": 1, "geocoded": 1}
        ).sort("frequency", -1).limit(20))
        print("\n  Top locations:")
        for l in top:
            geo = "✓" if l.get("geocoded") else "✗"
            print(f"    [{geo}] {l['name']}: {l['frequency']} mentions")
        return

    if args.analyze:
        if not ANTHROPIC_API_KEY:
            print("ERROR: Set ANTHROPIC_API_KEY environment variable")
            sys.exit(1)

        print(f"\n{'='*60}")
        print(f"  E-FINDER — Geospatial Analysis")
        print(f"{'='*60}\n")

        claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        agent = GeospatialAnalystAgent(db, claude)
        result = agent.run({
            "subject": args.subject,
            "export_geojson": args.export_geojson,
        })

        print(f"\n  GEOGRAPHIC SUMMARY:")
        if result.findings:
            summary = result.findings[0]
            print(f"  {summary.get('content', 'N/A')}")
            print(f"\n  Key locations:")
            for loc in summary.get("key_locations", [])[:10]:
                print(f"    • {loc.get('name', '')}: {loc.get('significance', '')}")
            print(f"\n  GeoJSON exported to: {summary.get('geojson_path', 'N/A')}")
        print(f"\n  Duration: {result.duration_seconds:.1f}s")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
