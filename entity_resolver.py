#!/usr/bin/env python3
"""
E-FINDER — Entity Resolver
===========================
Phase 1 foundation: Deduplicate and normalize the entities collection.

Takes raw entity records (where "Jeffrey Epstein", "EPSTEIN, JEFFREY",
"J. Epstein" are separate entries) and merges them into canonical entities
with variant tracking.

This runs as a batch job after extraction completes, producing a
`canonical_entities` collection that all other agents depend on.

Usage:
  export MONGODB_URI="mongodb+srv://user:PASSWORD@your-cluster.mongodb.net/..."
  export ANTHROPIC_API_KEY="sk-ant-..."

  # Dry run — show what would be merged
  python3 _pipeline_output/entity_resolver.py --dry-run

  # Full resolution (people only — most important)
  python3 _pipeline_output/entity_resolver.py --type person

  # All entity types
  python3 _pipeline_output/entity_resolver.py

  # Just organizations
  python3 _pipeline_output/entity_resolver.py --type organization
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

try:
    from pymongo import MongoClient, UpdateOne
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "pymongo", "--break-system-packages", "-q"])
    from pymongo import MongoClient, UpdateOne

try:
    import anthropic
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "anthropic", "--break-system-packages", "-q"])
    import anthropic

try:
    from rapidfuzz import fuzz, process
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          "rapidfuzz", "--break-system-packages", "-q"])
    from rapidfuzz import fuzz, process

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

MONGODB_URI = os.environ.get(
    "MONGODB_URI",
    "mongodb+srv://user:password@your-cluster.mongodb.net/?retryWrites=true&w=majority",
)
DATABASE_NAME = "doj_investigation"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

MODEL = "claude-sonnet-4-5-20250929"

# ═══════════════════════════════════════════
# Name Normalization
# ═══════════════════════════════════════════

def normalize_name(name):
    """Basic name normalization: strip titles, reorder LAST, FIRST, clean whitespace."""
    if not name:
        return ""

    name = name.strip()

    # Remove common titles/suffixes
    titles = r'\b(Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?|Jr\.?|Sr\.?|III|II|IV|Esq\.?|Hon\.?|Rev\.?)\b'
    name = re.sub(titles, '', name, flags=re.IGNORECASE).strip()

    # Handle "LAST, FIRST" or "LAST, FIRST MIDDLE" format
    if ',' in name:
        parts = [p.strip() for p in name.split(',', 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            # Only flip if the first part looks like a last name (all caps or single word)
            if parts[0].isupper() or ' ' not in parts[0]:
                name = f"{parts[1]} {parts[0]}"

    # Title case
    name = name.title()

    # Collapse whitespace
    name = re.sub(r'\s+', ' ', name).strip()

    # Remove trailing punctuation
    name = name.rstrip('.,;:')

    return name


def name_key(name):
    """Generate a simplified key for initial grouping."""
    n = normalize_name(name).lower()
    # Remove all non-alphanumeric
    n = re.sub(r'[^a-z0-9\s]', '', n)
    # Split into parts, sort alphabetically (so "John Smith" == "Smith John")
    parts = sorted(n.split())
    return ' '.join(parts)


# ═══════════════════════════════════════════
# Clustering: Group similar names
# ═══════════════════════════════════════════

def cluster_by_exact_key(entities_by_name):
    """First pass: group names that normalize to the same key."""
    clusters = defaultdict(list)
    for raw_name, records in entities_by_name.items():
        key = name_key(raw_name)
        if key:
            clusters[key].append({
                "raw_name": raw_name,
                "normalized": normalize_name(raw_name),
                "doc_count": len(records),
                "records": records,
            })
    return dict(clusters)


def cluster_by_fuzzy(clusters, threshold=85):
    """Second pass: merge clusters whose canonical names are fuzzy-similar."""
    keys = list(clusters.keys())
    canonical_names = {k: clusters[k][0]["normalized"] for k in keys}

    merged = {}
    used = set()

    for i, key_a in enumerate(keys):
        if key_a in used:
            continue

        group = list(clusters[key_a])
        used.add(key_a)

        for j in range(i + 1, len(keys)):
            key_b = keys[j]
            if key_b in used:
                continue

            # Compare canonical names with fuzzy matching
            score = fuzz.token_sort_ratio(canonical_names[key_a], canonical_names[key_b])
            if score >= threshold:
                group.extend(clusters[key_b])
                used.add(key_b)

        # Pick the most common variant as canonical
        best = max(group, key=lambda x: x["doc_count"])
        merged_key = name_key(best["normalized"])
        merged[merged_key] = group

    return merged


# ═══════════════════════════════════════════
# Claude disambiguation for ambiguous clusters
# ═══════════════════════════════════════════

DISAMBIGUATION_PROMPT = """You are resolving entity names from a corpus of DOJ documents related to the Jeffrey Epstein investigation.

I have a cluster of name variants that might all refer to the same person, or might be different people. Analyze the variants and their document contexts to decide.

Return a JSON array of resolution groups. Each group is:
{
  "canonical_name": "The best full name to use",
  "variants": ["list of name strings that refer to this person"],
  "confidence": 0.0 to 1.0,
  "reasoning": "brief explanation"
}

If some variants are clearly different people, put them in separate groups.
If a variant is ambiguous and could be multiple people, note that in reasoning and assign lower confidence.

IMPORTANT:
- Only group names that CLEARLY refer to the same individual
- When in doubt, keep names separate (higher precision > higher recall)
- Use the document context clues (roles, sections) to disambiguate
- Return valid JSON only

NAME VARIANTS AND CONTEXT:
"""


def disambiguate_with_claude(client, cluster_variants):
    """Use Claude to resolve ambiguous name clusters."""
    context_parts = []
    for variant in cluster_variants:
        contexts = []
        for rec in variant["records"][:5]:  # Limit context per variant
            ctx = rec.get("context", "") or rec.get("role", "")
            section = rec.get("section", "")
            if ctx:
                contexts.append(f"{section}: {ctx}")
        context_str = "; ".join(contexts[:3]) if contexts else "no context"
        context_parts.append(
            f'  "{variant["raw_name"]}" (appears in {variant["doc_count"]} docs) — {context_str}'
        )

    prompt = DISAMBIGUATION_PROMPT + "\n".join(context_parts)

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
        )
        response_text = message.content[0].text.strip()
        if response_text.startswith("```"):
            response_text = re.sub(r'^```(?:json)?\s*', '', response_text)
            response_text = re.sub(r'\s*```$', '', response_text)
        return json.loads(response_text)
    except Exception as e:
        log.warning("Claude disambiguation failed: %s", e)
        return None


# ═══════════════════════════════════════════
# MongoDB Operations
# ═══════════════════════════════════════════

def load_entities(db, entity_type):
    """Load all raw entities of a given type, grouped by name."""
    entities_by_name = defaultdict(list)

    cursor = db["entities"].find(
        {"entity_type": entity_type},
        {"name": 1, "name_lower": 1, "source_doc_id": 1, "section": 1,
         "context": 1, "role": 1, "frequency": 1}
    )

    count = 0
    for record in cursor:
        raw_name = record.get("name", "").strip()
        if raw_name and len(raw_name) > 1:
            entities_by_name[raw_name].append(record)
            count += 1

    log.info("Loaded %d raw %s entities across %d unique names",
             count, entity_type, len(entities_by_name))
    return entities_by_name


def write_canonical_entities(db, resolutions, entity_type):
    """Write resolved entities to the canonical_entities collection."""
    coll = db["canonical_entities"]

    ops = []
    for resolution in resolutions:
        canonical = resolution["canonical_name"]
        variants = resolution["variants"]
        doc_ids = resolution["doc_ids"]
        sections = resolution["sections"]

        ops.append(UpdateOne(
            {"canonical_name": canonical, "entity_type": entity_type},
            {"$set": {
                "canonical_name": canonical,
                "canonical_name_lower": canonical.lower(),
                "entity_type": entity_type,
                "variants": variants,
                "variants_lower": [v.lower() for v in variants],
                "total_doc_count": len(doc_ids),
                "doc_ids": list(doc_ids)[:500],  # Cap at 500 for storage
                "sections": list(sections),
                "confidence": resolution.get("confidence", 0.9),
                "resolved_at": datetime.now(timezone.utc),
            }},
            upsert=True,
        ))

    if ops:
        result = coll.bulk_write(ops, ordered=False)
        log.info("Wrote %d canonical %s entities (upserted: %d, modified: %d)",
                 len(ops), entity_type, result.upserted_count, result.modified_count)


# ═══════════════════════════════════════════
# Main
# ═══════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Resolve and deduplicate entities")
    parser.add_argument("--type", default=None,
                       choices=["person", "organization", "location", "case_number"],
                       help="Entity type to resolve (default: all)")
    parser.add_argument("--dry-run", action="store_true",
                       help="Show clusters without writing to MongoDB")
    parser.add_argument("--use-claude", action="store_true",
                       help="Use Claude for ambiguous cluster disambiguation")
    parser.add_argument("--fuzzy-threshold", type=int, default=85,
                       help="Fuzzy matching threshold (0-100, default 85)")
    parser.add_argument("--min-docs", type=int, default=2,
                       help="Minimum document appearances to include (default 2)")
    args = parser.parse_args()

    if "<db_password>" in MONGODB_URI:
        print("ERROR: Set MONGODB_URI environment variable")
        sys.exit(1)

    types_to_resolve = [args.type] if args.type else ["person", "organization", "location"]

    print(f"\n{'='*60}")
    print(f"  E-FINDER — ENTITY RESOLVER")
    print(f"{'='*60}\n")

    client_mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=10000)
    client_mongo.admin.command("ping")
    db = client_mongo[DATABASE_NAME]
    print("  Connected to MongoDB.\n")

    # Ensure indexes on canonical_entities
    coll = db["canonical_entities"]
    coll.create_index("canonical_name_lower", name="idx_canonical_name")
    coll.create_index("entity_type", name="idx_canonical_type")
    coll.create_index([("entity_type", 1), ("canonical_name_lower", 1)],
                      name="idx_type_canonical", unique=True)
    coll.create_index("variants_lower", name="idx_variants")

    # Claude client (optional)
    claude_client = None
    if args.use_claude and ANTHROPIC_API_KEY:
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        print(f"  Claude enabled for disambiguation ({MODEL})")

    total_stats = {"raw": 0, "canonical": 0, "merged": 0, "claude_calls": 0}

    for entity_type in types_to_resolve:
        print(f"\n  {'─'*50}")
        print(f"  Resolving: {entity_type}")
        print(f"  {'─'*50}\n")

        # Load raw entities
        entities_by_name = load_entities(db, entity_type)
        if not entities_by_name:
            print(f"  No {entity_type} entities found. Skipping.")
            continue

        # Filter by min doc count
        if args.min_docs > 1:
            filtered = {k: v for k, v in entities_by_name.items() if len(v) >= args.min_docs}
            log.info("Filtered to %d names with >= %d doc appearances (from %d)",
                     len(filtered), args.min_docs, len(entities_by_name))
            # Keep single-appearance entities too — they just won't be merged
            single_appearance = {k: v for k, v in entities_by_name.items() if len(v) < args.min_docs}
        else:
            filtered = entities_by_name
            single_appearance = {}

        # Pass 1: Exact key clustering
        clusters = cluster_by_exact_key(filtered)
        log.info("Pass 1 (exact key): %d clusters from %d unique names",
                 len(clusters), len(filtered))

        # Pass 2: Fuzzy merge
        merged_clusters = cluster_by_fuzzy(clusters, threshold=args.fuzzy_threshold)
        log.info("Pass 2 (fuzzy merge): %d clusters (merged %d)",
                 len(merged_clusters), len(clusters) - len(merged_clusters))

        # Build resolutions
        resolutions = []
        ambiguous_clusters = []

        for cluster_key, variants in merged_clusters.items():
            if len(variants) == 1:
                # Single variant — straightforward
                v = variants[0]
                doc_ids = set(r["source_doc_id"] for r in v["records"])
                sections = set(r.get("section", "") for r in v["records"])
                resolutions.append({
                    "canonical_name": v["normalized"],
                    "variants": [v["raw_name"]],
                    "doc_ids": doc_ids,
                    "sections": sections,
                    "confidence": 0.95,
                })
            else:
                # Multiple variants — check if names are similar enough to auto-merge
                names = [v["normalized"] for v in variants]
                all_raw = [v["raw_name"] for v in variants]
                all_docs = set()
                all_sections = set()
                for v in variants:
                    for r in v["records"]:
                        all_docs.add(r["source_doc_id"])
                        all_sections.add(r.get("section", ""))

                # Auto-merge if all normalized names are very similar
                base = names[0]
                all_similar = all(fuzz.token_sort_ratio(base, n) >= 92 for n in names)

                if all_similar:
                    # Pick the most common variant as canonical
                    best = max(variants, key=lambda x: x["doc_count"])
                    resolutions.append({
                        "canonical_name": best["normalized"],
                        "variants": list(set(all_raw)),
                        "doc_ids": all_docs,
                        "sections": all_sections,
                        "confidence": 0.9,
                    })
                else:
                    # Ambiguous — needs Claude or manual review
                    ambiguous_clusters.append(variants)

        # Handle ambiguous clusters
        if ambiguous_clusters and claude_client:
            log.info("Disambiguating %d ambiguous clusters with Claude...", len(ambiguous_clusters))
            for cluster in ambiguous_clusters:
                total_stats["claude_calls"] += 1
                result = disambiguate_with_claude(claude_client, cluster)
                time.sleep(0.5)  # Rate limit

                if result:
                    for group in result:
                        matched_variants = []
                        matched_docs = set()
                        matched_sections = set()

                        for variant_name in group.get("variants", []):
                            for v in cluster:
                                if v["raw_name"] == variant_name or v["normalized"] == variant_name:
                                    matched_variants.append(v["raw_name"])
                                    for r in v["records"]:
                                        matched_docs.add(r["source_doc_id"])
                                        matched_sections.add(r.get("section", ""))

                        if matched_variants:
                            resolutions.append({
                                "canonical_name": group["canonical_name"],
                                "variants": list(set(matched_variants)),
                                "doc_ids": matched_docs,
                                "sections": matched_sections,
                                "confidence": group.get("confidence", 0.7),
                            })
                else:
                    # Claude failed — keep each variant separate
                    for v in cluster:
                        doc_ids = set(r["source_doc_id"] for r in v["records"])
                        sections = set(r.get("section", "") for r in v["records"])
                        resolutions.append({
                            "canonical_name": v["normalized"],
                            "variants": [v["raw_name"]],
                            "doc_ids": doc_ids,
                            "sections": sections,
                            "confidence": 0.6,
                        })
        elif ambiguous_clusters:
            # No Claude — keep variants separate
            log.info("%d ambiguous clusters left unresolved (use --use-claude to resolve)", len(ambiguous_clusters))
            for cluster in ambiguous_clusters:
                for v in cluster:
                    doc_ids = set(r["source_doc_id"] for r in v["records"])
                    sections = set(r.get("section", "") for r in v["records"])
                    resolutions.append({
                        "canonical_name": v["normalized"],
                        "variants": [v["raw_name"]],
                        "doc_ids": doc_ids,
                        "sections": sections,
                        "confidence": 0.6,
                    })

        # Add single-appearance entities (no merging needed)
        for raw_name, records in single_appearance.items():
            normalized = normalize_name(raw_name)
            if normalized:
                doc_ids = set(r["source_doc_id"] for r in records)
                sections = set(r.get("section", "") for r in records)
                resolutions.append({
                    "canonical_name": normalized,
                    "variants": [raw_name],
                    "doc_ids": doc_ids,
                    "sections": sections,
                    "confidence": 0.95,
                })

        # Stats
        raw_count = sum(len(v) for v in entities_by_name.values())
        total_stats["raw"] += raw_count
        total_stats["canonical"] += len(resolutions)
        total_stats["merged"] += raw_count - len(resolutions)

        print(f"\n  Results for {entity_type}:")
        print(f"    Raw entity records:     {raw_count:,}")
        print(f"    Canonical entities:     {len(resolutions):,}")
        print(f"    Merged/deduplicated:    {raw_count - len(resolutions):,}")
        print(f"    Ambiguous clusters:     {len(ambiguous_clusters)}")

        # Show top entities
        top = sorted(resolutions, key=lambda x: len(x["doc_ids"]), reverse=True)[:15]
        print(f"\n  Top 15 {entity_type} entities by document count:")
        for r in top:
            variant_str = ""
            if len(r["variants"]) > 1:
                variant_str = f" (also: {', '.join(r['variants'][:3])})"
            print(f"    {r['canonical_name']}: {len(r['doc_ids'])} docs{variant_str}")

        # Show interesting merges
        merges = [r for r in resolutions if len(r["variants"]) > 1]
        if merges:
            print(f"\n  Sample merges ({len(merges)} total):")
            for r in sorted(merges, key=lambda x: len(x["variants"]), reverse=True)[:10]:
                print(f"    {r['canonical_name']} ← {r['variants']}")

        # Write to MongoDB
        if not args.dry_run:
            write_canonical_entities(db, resolutions, entity_type)
        else:
            print(f"\n  [DRY RUN] Would write {len(resolutions)} canonical entities")

    # Final summary
    print(f"\n{'='*60}")
    print(f"  ENTITY RESOLUTION COMPLETE")
    print(f"{'='*60}")
    print(f"  Raw entity records:   {total_stats['raw']:,}")
    print(f"  Canonical entities:   {total_stats['canonical']:,}")
    print(f"  Merged:               {total_stats['merged']:,}")
    print(f"  Claude API calls:     {total_stats['claude_calls']}")
    print(f"{'='*60}")

    if not args.dry_run:
        total = db["canonical_entities"].count_documents({})
        print(f"\n  Total canonical entities in MongoDB: {total:,}")


if __name__ == "__main__":
    main()
