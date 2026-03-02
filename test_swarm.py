#!/usr/bin/env python3
"""
Quick swarm test — runs each agent on the currently extracted docs.
Run this on the VPS after the extraction has some data.

Usage:
  cd ~/efinder  # adjust to your workspace path
  source .venv/bin/activate
  export $(cat .env | xargs)
  python3 _pipeline_output/test_swarm.py
"""

import json
import os
import sys
import time

try:
    from pymongo import MongoClient
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "pymongo", "--break-system-packages", "-q"])
    from pymongo import MongoClient

try:
    import anthropic
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "anthropic", "--break-system-packages", "-q"])
    import anthropic

try:
    import networkx
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "networkx", "--break-system-packages", "-q"])

try:
    from rapidfuzz import fuzz
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "rapidfuzz", "--break-system-packages", "-q"])

# Now import the swarm
sys.path.insert(0, os.path.dirname(__file__))
from swarm import (
    NetworkMapperAgent, DocumentQueryAgent, TimelineBuilderAgent,
    RedactionAnalystAgent, Coordinator, AGENT_REGISTRY
)

MONGODB_URI = os.environ.get("MONGODB_URI", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

def main():
    print(f"\n{'='*60}")
    print(f"  E-FINDER SWARM — TEST SUITE")
    print(f"{'='*60}\n")

    # Connect
    client_mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    client_mongo.admin.command("ping")
    db = client_mongo["doj_investigation"]
    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    print("  Connected to MongoDB + Claude.\n")

    # Stats
    total = db["documents"].count_documents({})
    extracted = db["documents"].count_documents({"processing_stage": "entities_extracted"})
    entities = db["entities"].count_documents({})
    print(f"  Corpus: {total:,} docs, {extracted:,} extracted, {entities:,} entities\n")

    if extracted < 10:
        print("  Not enough extracted docs to test. Wait for more extraction.")
        return

    errors = []

    # ─── Test 1: Network Mapper ───
    print(f"  {'─'*50}")
    print(f"  TEST 1: Network Mapper (no API calls)")
    print(f"  {'─'*50}")
    try:
        agent = NetworkMapperAgent(db, claude)
        result = agent.run({"min_weight": 1})
        print(f"  ✓ Completed in {result.duration_seconds:.1f}s")
        if result.findings:
            stats = result.findings[0]
            print(f"    Graph: {stats.get('nodes', 0)} nodes, {stats.get('edges', 0)} edges")
            if len(result.findings) > 1:
                top = result.findings[1].get("entities", [])[:5]
                print(f"    Top connected:")
                for e in top:
                    print(f"      {e['name']}: {e['centrality']:.4f}")
        if result.error:
            print(f"  ⚠ Error: {result.error}")
            errors.append(("network_mapper", result.error))
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        errors.append(("network_mapper", str(e)))

    # ─── Test 2: Document Query ───
    print(f"\n  {'─'*50}")
    print(f"  TEST 2: Document Query")
    print(f"  {'─'*50}")
    try:
        agent = DocumentQueryAgent(db, claude)
        result = agent.run({"question": "What document types are most common in the corpus?"})
        print(f"  ✓ Completed in {result.duration_seconds:.1f}s ({result.api_calls} API calls)")
        if result.findings:
            answer = result.findings[0] if result.findings else {}
            if answer.get("type") == "answer":
                print(f"    Answer: {answer.get('content', '')[:200]}")
        if result.error:
            print(f"  ⚠ Error: {result.error}")
            errors.append(("document_query", result.error))
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        errors.append(("document_query", str(e)))

    # ─── Test 3: Timeline Builder ───
    print(f"\n  {'─'*50}")
    print(f"  TEST 3: Timeline Builder")
    print(f"  {'─'*50}")
    try:
        # Find the most common person to build a timeline for
        top_person = db["entities"].aggregate([
            {"$match": {"entity_type": "person"}},
            {"$group": {"_id": "$name", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
            {"$limit": 1},
        ])
        top_list = list(top_person)
        subject = top_list[0]["_id"] if top_list else "Jeffrey Epstein"
        print(f"    Subject: {subject}")

        agent = TimelineBuilderAgent(db, claude)
        result = agent.run({"subject": subject})
        print(f"  ✓ Completed in {result.duration_seconds:.1f}s ({result.api_calls} API calls)")
        if result.findings:
            timeline = result.findings[0].get("data", {})
            events = timeline.get("timeline", [])
            print(f"    Events found: {len(events)}")
            for e in events[:3]:
                print(f"      {e.get('date', '?')}: {e.get('event', '')[:80]}")
        if result.error:
            print(f"  ⚠ Error: {result.error}")
            errors.append(("timeline_builder", result.error))
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        errors.append(("timeline_builder", str(e)))

    # ─── Test 4: Redaction Analyst ───
    print(f"\n  {'─'*50}")
    print(f"  TEST 4: Redaction Analyst")
    print(f"  {'─'*50}")
    try:
        agent = RedactionAnalystAgent(db, claude)
        result = agent.run({})
        print(f"  ✓ Completed in {result.duration_seconds:.1f}s ({result.api_calls} API calls)")
        if result.findings:
            analysis = result.findings[0].get("data", {})
            print(f"    Assessment: {analysis.get('overall_assessment', '')[:200]}")
        if result.error:
            print(f"  ⚠ Error: {result.error}")
            errors.append(("redaction_analyst", result.error))
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        errors.append(("redaction_analyst", str(e)))

    # ─── Test 5: Full Coordinator ───
    print(f"\n  {'─'*50}")
    print(f"  TEST 5: Coordinator (full investigation)")
    print(f"  {'─'*50}")
    try:
        coordinator = Coordinator(db, claude)
        report = coordinator.investigate("Who are the most frequently mentioned people and what connects them?")
        print(f"  ✓ Completed in {report.get('meta', {}).get('total_duration_seconds', 0):.1f}s")
        print(f"    Cost: ~${report.get('meta', {}).get('estimated_cost', 0):.3f}")
        summary = report.get("executive_summary", "")
        print(f"    Summary: {summary[:250]}")
        findings = report.get("key_findings", [])
        print(f"    Findings: {len(findings)}")
    except Exception as e:
        print(f"  ✗ FAILED: {e}")
        errors.append(("coordinator", str(e)))

    # ─── Summary ───
    print(f"\n{'='*60}")
    if not errors:
        print(f"  ALL TESTS PASSED ✓")
    else:
        print(f"  {len(errors)} ERRORS:")
        for name, err in errors:
            print(f"    {name}: {err}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
