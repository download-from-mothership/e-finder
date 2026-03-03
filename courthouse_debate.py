#!/usr/bin/env python3
"""
E-FINDER — Courthouse Debate (Adversarial Finding Validation)
==============================================================
Adapted from IntellYWeave's courthouse debate system.

Before any finding is written to the `reports` collection, it is routed
through an adversarial three-agent debate:

  Prosecution Agent  — argues that the finding is VALID and significant
  Defense Agent      — argues that the finding is INVALID, overstated, or
                       based on insufficient evidence
  Judge Agent        — weighs both arguments and produces a verdict:
                         CONFIRMED / CONTESTED / INSUFFICIENT_EVIDENCE

This prevents confirmation bias in the Coordinator's synthesis and ensures
that only well-supported findings reach the final report. It is especially
important for an investigation of this sensitivity.

Architecture (mirrors IntellYWeave):
  CourthouseDebate.adjudicate(finding, evidence) → Verdict
    ├── ProsecutionAgent.argue(finding, evidence) → prosecution_argument
    ├── DefenseAgent.argue(finding, evidence, prosecution_argument) → defense_argument
    └── JudgeAgent.verdict(finding, prosecution_argument, defense_argument) → Verdict

Usage (standalone):
  export ANTHROPIC_API_KEY="sk-ant-..."

  python3 courthouse_debate.py --finding "Jeffrey Epstein had financial ties to Deutsche Bank" \\
    --evidence "Documents show wire transfers totaling $X from account Y to Z"

Integration with swarm.py Coordinator:
  from courthouse_debate import CourthouseDebate
  debate = CourthouseDebate(claude_client)
  verdict = debate.adjudicate(finding_text, evidence_list)
  if verdict.status == "CONFIRMED":
      report["key_findings"].append(...)
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ── Config ────────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-sonnet-4-5-20250929"
# Use a faster/cheaper model for prosecution/defense; reserve full model for judge
DEBATE_MODEL = os.environ.get("DEBATE_MODEL", MODEL)
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", MODEL)


# ── Dependency bootstrap ──────────────────────────────────────────────────────
def _install(pkg):
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install",
                          pkg, "--break-system-packages", "-q"])


try:
    import anthropic
except ImportError:
    _install("anthropic")
    import anthropic


# ── Data structures ───────────────────────────────────────────────────────────
@dataclass
class DebateArgument:
    role: str           # "prosecution" or "defense"
    argument: str       # main argument text
    key_points: list    # list of specific points
    evidence_used: list # which evidence items were cited
    confidence: float   # 0.0–1.0


@dataclass
class Verdict:
    status: str                    # CONFIRMED / CONTESTED / INSUFFICIENT_EVIDENCE
    confidence: float              # 0.0–1.0
    reasoning: str                 # judge's reasoning
    prosecution_summary: str       # key prosecution points
    defense_summary: str           # key defense points
    conditions: list               # conditions under which finding holds
    recommended_caveats: list      # caveats to add to the finding
    additional_investigation: list # what else should be investigated
    debate_rounds: int = 1


# ── Prosecution Agent ─────────────────────────────────────────────────────────
class ProsecutionAgent:
    """
    Argues that the finding is valid, significant, and well-supported
    by the available evidence.
    """

    SYSTEM_PROMPT = """You are the Prosecution Agent in an adversarial evidence review.
Your role is to argue that the finding under review is VALID and SIGNIFICANT.

You must:
1. Identify the strongest evidence supporting the finding
2. Explain why the evidence is credible and sufficient
3. Address the most obvious counterarguments preemptively
4. Assess the significance of the finding to the overall investigation
5. Assign a confidence score based on evidence quality

You are analyzing documents from the DOJ Epstein investigation corpus.
Be rigorous but fair — do not overstate what the evidence shows."""

    def __init__(self, claude_client):
        self.claude = claude_client

    def argue(self, finding: str, evidence: list[dict]) -> DebateArgument:
        evidence_text = self._format_evidence(evidence)

        prompt = f"""Review this finding and argue for its validity.

FINDING: {finding}

AVAILABLE EVIDENCE:
{evidence_text}

Return JSON:
{{
  "argument": "your main argument for why this finding is valid (2-3 paragraphs)",
  "key_points": [
    "specific point 1 with evidence reference",
    "specific point 2 with evidence reference"
  ],
  "evidence_used": ["doc_id or excerpt identifier"],
  "strongest_evidence": "the single most compelling piece of evidence",
  "preemptive_rebuttals": ["anticipated defense argument and why it fails"],
  "confidence": 0.0-1.0
}}"""

        try:
            message = self.claude.messages.create(
                model=DEBATE_MODEL,
                max_tokens=2048,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            data = json.loads(text)
        except Exception as e:
            log.error("Prosecution agent failed: %s", e)
            data = {
                "argument": f"Prosecution failed to generate argument: {e}",
                "key_points": [],
                "evidence_used": [],
                "confidence": 0.5,
            }

        return DebateArgument(
            role="prosecution",
            argument=data.get("argument", ""),
            key_points=data.get("key_points", []),
            evidence_used=data.get("evidence_used", []),
            confidence=float(data.get("confidence", 0.5)),
        )

    def _format_evidence(self, evidence: list[dict]) -> str:
        if not evidence:
            return "No direct evidence provided."
        lines = []
        for i, e in enumerate(evidence[:10], 1):
            doc_id = e.get("doc_id", f"evidence_{i}")
            relevance = e.get("relevance", e.get("finding", ""))
            excerpt = e.get("excerpt", e.get("text", ""))[:300]
            lines.append(f"[{i}] doc_id={doc_id}\n    Relevance: {relevance}\n    Excerpt: {excerpt}")
        return "\n\n".join(lines)


# ── Defense Agent ─────────────────────────────────────────────────────────────
class DefenseAgent:
    """
    Argues that the finding is invalid, overstated, based on insufficient
    evidence, or subject to alternative interpretations.
    """

    SYSTEM_PROMPT = """You are the Defense Agent in an adversarial evidence review.
Your role is to challenge the finding under review — argue it is INVALID,
OVERSTATED, or based on INSUFFICIENT EVIDENCE.

You must:
1. Identify weaknesses in the evidence (gaps, ambiguities, alternative explanations)
2. Challenge the logical chain from evidence to conclusion
3. Identify what evidence is MISSING that would be needed to confirm the finding
4. Propose alternative explanations for the evidence
5. Assess whether the finding overstates what the documents actually show

You are analyzing documents from the DOJ Epstein investigation corpus.
Be rigorous — your job is to prevent false or overstated findings from
reaching the final report. This is a matter of investigative integrity."""

    def __init__(self, claude_client):
        self.claude = claude_client

    def argue(self, finding: str, evidence: list[dict],
              prosecution_argument: DebateArgument) -> DebateArgument:
        evidence_text = self._format_evidence(evidence)

        prompt = f"""Challenge this finding and argue against its validity.

FINDING: {finding}

PROSECUTION ARGUMENT:
{prosecution_argument.argument}

PROSECUTION KEY POINTS:
{json.dumps(prosecution_argument.key_points, indent=2)}

AVAILABLE EVIDENCE:
{evidence_text}

Return JSON:
{{
  "argument": "your main argument against this finding (2-3 paragraphs)",
  "key_points": [
    "specific weakness or counterpoint 1",
    "specific weakness or counterpoint 2"
  ],
  "evidence_gaps": ["what evidence is missing to confirm this finding"],
  "alternative_explanations": ["other explanations for the same evidence"],
  "rebuttals_to_prosecution": ["specific rebuttals to prosecution's key points"],
  "confidence": 0.0-1.0
}}"""

        try:
            message = self.claude.messages.create(
                model=DEBATE_MODEL,
                max_tokens=2048,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            data = json.loads(text)
        except Exception as e:
            log.error("Defense agent failed: %s", e)
            data = {
                "argument": f"Defense failed to generate argument: {e}",
                "key_points": [],
                "evidence_used": [],
                "confidence": 0.5,
            }

        return DebateArgument(
            role="defense",
            argument=data.get("argument", ""),
            key_points=data.get("key_points", []),
            evidence_used=data.get("evidence_gaps", []),
            confidence=float(data.get("confidence", 0.5)),
        )

    def _format_evidence(self, evidence: list[dict]) -> str:
        if not evidence:
            return "No direct evidence provided."
        lines = []
        for i, e in enumerate(evidence[:10], 1):
            doc_id = e.get("doc_id", f"evidence_{i}")
            relevance = e.get("relevance", e.get("finding", ""))
            excerpt = e.get("excerpt", e.get("text", ""))[:300]
            lines.append(f"[{i}] doc_id={doc_id}\n    Relevance: {relevance}\n    Excerpt: {excerpt}")
        return "\n\n".join(lines)


# ── Judge Agent ───────────────────────────────────────────────────────────────
class JudgeAgent:
    """
    Weighs prosecution and defense arguments impartially and delivers
    a verdict with confidence score and recommended caveats.
    """

    SYSTEM_PROMPT = """You are the Judge in an adversarial evidence review for a
sensitive DOJ investigation. You have heard arguments from both prosecution
(arguing the finding is valid) and defense (arguing it is invalid or overstated).

Your role is to deliver an impartial verdict:
  CONFIRMED          — finding is well-supported and can be included in the report
  CONTESTED          — finding has merit but requires caveats or additional evidence
  INSUFFICIENT_EVIDENCE — finding cannot be supported by available evidence

You must:
1. Weigh the strength of both arguments objectively
2. Identify which side made stronger points
3. Determine appropriate confidence level
4. Specify any caveats that must accompany the finding
5. Recommend what additional investigation would resolve remaining uncertainty

Your verdict will determine whether this finding appears in the final report.
Be rigorous, fair, and precise."""

    def __init__(self, claude_client):
        self.claude = claude_client

    def verdict(self, finding: str,
                prosecution: DebateArgument,
                defense: DebateArgument) -> Verdict:
        prompt = f"""Deliver your verdict on this finding after hearing both sides.

FINDING: {finding}

PROSECUTION ARGUMENT:
{prosecution.argument}
Key points: {json.dumps(prosecution.key_points)}
Prosecution confidence: {prosecution.confidence}

DEFENSE ARGUMENT:
{defense.argument}
Key points: {json.dumps(defense.key_points)}
Defense confidence: {defense.confidence}

Return JSON:
{{
  "status": "CONFIRMED" or "CONTESTED" or "INSUFFICIENT_EVIDENCE",
  "confidence": 0.0-1.0,
  "reasoning": "your full reasoning for this verdict (2-3 paragraphs)",
  "prosecution_summary": "what prosecution argued most effectively",
  "defense_summary": "what defense argued most effectively",
  "conditions": ["conditions under which the finding holds"],
  "recommended_caveats": [
    "caveat 1 to add to the finding",
    "caveat 2 to add to the finding"
  ],
  "additional_investigation": [
    "what else should be investigated to resolve this",
    "what documents or evidence are needed"
  ]
}}"""

        try:
            message = self.claude.messages.create(
                model=JUDGE_MODEL,
                max_tokens=3000,
                system=self.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text.strip()
            if text.startswith("```"):
                text = re.sub(r'^```(?:json)?\s*', '', text)
                text = re.sub(r'\s*```$', '', text)
            data = json.loads(text)
        except Exception as e:
            log.error("Judge agent failed: %s", e)
            data = {
                "status": "CONTESTED",
                "confidence": 0.5,
                "reasoning": f"Judge failed to deliver verdict: {e}",
                "prosecution_summary": "",
                "defense_summary": "",
                "conditions": [],
                "recommended_caveats": ["Verdict generation failed — manual review required"],
                "additional_investigation": [],
            }

        return Verdict(
            status=data.get("status", "CONTESTED"),
            confidence=float(data.get("confidence", 0.5)),
            reasoning=data.get("reasoning", ""),
            prosecution_summary=data.get("prosecution_summary", ""),
            defense_summary=data.get("defense_summary", ""),
            conditions=data.get("conditions", []),
            recommended_caveats=data.get("recommended_caveats", []),
            additional_investigation=data.get("additional_investigation", []),
        )


# ── Courthouse Debate orchestrator ────────────────────────────────────────────
class CourthouseDebate:
    """
    Orchestrates the full prosecution → defense → judge debate pipeline.

    Usage:
        debate = CourthouseDebate(claude_client)
        verdict = debate.adjudicate(
            finding="Jeffrey Epstein had financial ties to Deutsche Bank",
            evidence=[{"doc_id": "doc123", "relevance": "wire transfer records", "excerpt": "..."}]
        )
        print(verdict.status, verdict.confidence)
    """

    def __init__(self, claude_client):
        self.claude = claude_client
        self.prosecution = ProsecutionAgent(claude_client)
        self.defense = DefenseAgent(claude_client)
        self.judge = JudgeAgent(claude_client)

    def adjudicate(self, finding: str, evidence: list[dict],
                   verbose: bool = False) -> Verdict:
        """
        Run the full debate and return a Verdict.

        Args:
            finding:  The finding text to adjudicate (e.g. from Coordinator synthesis)
            evidence: List of evidence dicts [{doc_id, relevance, excerpt}]
            verbose:  Print debate progress

        Returns:
            Verdict with status, confidence, caveats, and additional investigation
        """
        start = time.time()

        if verbose:
            print(f"\n  ⚖  COURTHOUSE DEBATE")
            print(f"  Finding: {finding[:100]}...")
            print(f"  Evidence items: {len(evidence)}")

        # Round 1: Prosecution
        if verbose:
            print(f"\n  Prosecution arguing...")
        prosecution_arg = self.prosecution.argue(finding, evidence)

        # Round 2: Defense (sees prosecution argument)
        if verbose:
            print(f"  Defense arguing...")
        defense_arg = self.defense.argue(finding, evidence, prosecution_arg)

        # Round 3: Judge delivers verdict
        if verbose:
            print(f"  Judge deliberating...")
        verdict = self.judge.verdict(finding, prosecution_arg, defense_arg)
        verdict.debate_rounds = 1

        elapsed = time.time() - start

        if verbose:
            status_icon = {"CONFIRMED": "✓", "CONTESTED": "~", "INSUFFICIENT_EVIDENCE": "✗"}.get(
                verdict.status, "?"
            )
            print(f"\n  {status_icon} VERDICT: {verdict.status} (confidence: {verdict.confidence:.2f})")
            print(f"  Reasoning: {verdict.reasoning[:200]}...")
            if verdict.recommended_caveats:
                print(f"  Caveats:")
                for c in verdict.recommended_caveats:
                    print(f"    • {c}")
            print(f"  Debate duration: {elapsed:.1f}s")

        return verdict

    def adjudicate_report(self, report: dict, evidence_by_finding: dict = None) -> dict:
        """
        Adjudicate all key findings in a Coordinator report.

        Args:
            report: The full report dict from Coordinator._synthesize()
            evidence_by_finding: Optional dict mapping finding text to evidence list

        Returns:
            Updated report with verdict metadata on each finding
        """
        if evidence_by_finding is None:
            evidence_by_finding = {}

        validated_findings = []
        contested_findings = []
        rejected_findings = []

        for finding in report.get("key_findings", []):
            finding_text = finding.get("finding", "")
            if not finding_text:
                continue

            # Get evidence for this finding
            evidence = evidence_by_finding.get(finding_text, [])
            # Fall back to doc_ids from the finding
            if not evidence:
                for doc_id in finding.get("evidence_doc_ids", []):
                    evidence.append({"doc_id": doc_id, "relevance": finding_text})

            verdict = self.adjudicate(finding_text, evidence)

            # Annotate finding with verdict
            finding["courthouse_verdict"] = {
                "status": verdict.status,
                "confidence": verdict.confidence,
                "reasoning": verdict.reasoning,
                "caveats": verdict.recommended_caveats,
                "additional_investigation": verdict.additional_investigation,
            }

            # Adjust finding confidence based on verdict
            original_confidence = finding.get("confidence", 0.5)
            if verdict.status == "CONFIRMED":
                finding["confidence"] = min(1.0, original_confidence * 1.1)
                validated_findings.append(finding)
            elif verdict.status == "CONTESTED":
                finding["confidence"] = original_confidence * 0.8
                contested_findings.append(finding)
            else:  # INSUFFICIENT_EVIDENCE
                finding["confidence"] = original_confidence * 0.4
                rejected_findings.append(finding)

        # Rebuild report with verdict-sorted findings
        report["key_findings"] = (
            validated_findings + contested_findings + rejected_findings
        )
        report["courthouse_summary"] = {
            "confirmed": len(validated_findings),
            "contested": len(contested_findings),
            "insufficient_evidence": len(rejected_findings),
            "total_adjudicated": len(validated_findings) + len(contested_findings) + len(rejected_findings),
        }

        # Collect all additional investigation items
        all_additional = []
        for f in report["key_findings"]:
            all_additional.extend(
                f.get("courthouse_verdict", {}).get("additional_investigation", [])
            )
        if all_additional:
            existing = report.get("recommended_next_steps", [])
            report["recommended_next_steps"] = list(set(existing + all_additional))

        return report


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="E-FINDER Courthouse Debate — adversarial finding validation"
    )
    parser.add_argument("--finding", "-f", required=True,
                        help="Finding text to adjudicate")
    parser.add_argument("--evidence", "-e", type=str, default="[]",
                        help="JSON array of evidence dicts [{doc_id, relevance, excerpt}]")
    args = parser.parse_args()

    if not ANTHROPIC_API_KEY:
        print("ERROR: Set ANTHROPIC_API_KEY environment variable")
        sys.exit(1)

    claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    try:
        evidence = json.loads(args.evidence)
    except json.JSONDecodeError:
        evidence = [{"relevance": args.evidence}]

    debate = CourthouseDebate(claude)
    verdict = debate.adjudicate(args.finding, evidence, verbose=True)

    print(f"\n{'='*60}")
    print(f"  FULL VERDICT")
    print(f"{'='*60}")
    print(f"  Status:     {verdict.status}")
    print(f"  Confidence: {verdict.confidence:.2f}")
    print(f"\n  Reasoning:")
    print(f"  {verdict.reasoning}")
    if verdict.recommended_caveats:
        print(f"\n  Recommended caveats:")
        for c in verdict.recommended_caveats:
            print(f"    • {c}")
    if verdict.additional_investigation:
        print(f"\n  Additional investigation needed:")
        for a in verdict.additional_investigation:
            print(f"    → {a}")


if __name__ == "__main__":
    main()
