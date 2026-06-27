"""Deterministic signal extraction from a transcript.

These signals are computed without any LLM. They serve two purposes:
  1. They are passed to the LLM as *grounding* so its scoring is anchored to
     concrete, verifiable facts about the transcript (issue coverage,
     concession trajectory, etc.) rather than vibes.
  2. They drive the fully-offline heuristic scorer (the fallback when no API
     key is available), so the CLI always produces a useful assessment.
"""

from __future__ import annotations

import re
from typing import Any

from .schemas import Scenario, Transcript

# Keyword sets used to detect which contested issue a user turn engages with.
# Keys are matched against scenario issue names by substring (case-insensitive).
ISSUE_KEYWORDS: dict[str, list[str]] = {
    # The cap is almost always argued through percentage figures, so "%" / "percent"
    # are strong signals even when the literal word "cap" is never used.
    "warranty cap": ["cap", "warranty cap", "ceiling", "%", "percent", "enterprise value", "liability cap"],
    "limitation period": ["limitation", "limitation period", "how long", "warranty period",
                            "sunset", "6 year", "six year", "18 month", "years for", "statutory period"],
    "earn-out": ["earn-out", "earnout", "earn out", "deferred", "ebitda", "deferred consideration"],
    "non-compete": ["non-compete", "non compete", "noncompete", "restraint", "compete", "restrictive covenant"],
    "retention": ["retention", "escrow", "holdback", "hold back", "indemnity claim", "ring-fence", "ring fence"],
}

# Structural / creative mechanisms worth credit.
CREATIVITY_TERMS = [
    "tiered", "tier", "ramp down", "ramp-down", "step down", "step-down", "sunset",
    "w&i", "warranty and indemnity", "insurance", "escrow", "specific indemnity",
    "carve out", "carve-out", "lock-in", "lock in", "retention of", "independent engineer",
    "deterioration model", "first 12 months", "first twelve months", "generation cycle",
]

# Signals of legal / commercial grounding.
GROUNDING_TERMS = [
    "statutory", "statute", "market norm", "market standard", "market sits", "case law",
    "hmrc", "limitation act", "due diligence", "disclosure", "data room", "precedent",
    "industrial standard", "industry standard", "reasonable", "enforceable", "6 year", "six year",
]

# Probing / diagnostic phrasing.
PROBE_TERMS = [
    "what", "why", "how", "where", "would you", "can you", "could you",
    "what's driving", "what is driving", "does it", "do you",
]

# Professionalism red flags: insults, ad hominem, slang, profanity.
UNPROFESSIONAL_TERMS = [
    "trainspotter", "wind turbine spotter", "second hand car", "second-hand car",
    "dirty", "lowball", "low ball", "ain't", "stuff that you hide", "screw",
    "ripping us off", "rip us off", "bullshit", "crap", "stupid", "ridiculous",
]

PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")
MONTHS_RE = re.compile(r"(\d{1,3})\s*month")
YEARS_RE = re.compile(r"(\d{1,2})\s*year")


def _contains_any(text: str, terms: list[str]) -> list[str]:
    low = text.lower()
    return [t for t in terms if t in low]


def _match_issue(issue_name: str, text: str) -> bool:
    """Does this text engage the given scenario issue?"""
    low_issue = issue_name.lower()
    low_text = text.lower()
    for key, kws in ISSUE_KEYWORDS.items():
        if key in low_issue:
            return any(kw in low_text for kw in kws)
    # Fallback: match on words from the issue name itself.
    words = [w for w in re.findall(r"[a-z]+", low_issue) if len(w) > 3]
    return any(w in low_text for w in words)


def extract_signals(transcript: Transcript, scenario: Scenario) -> dict[str, Any]:
    user_turns = transcript.user_turns()
    user_text = transcript.user_text()
    n_user = len(user_turns)
    total_user_words = sum(len(t.content.split()) for t in user_turns)

    # --- Issue coverage -----------------------------------------------------
    coverage = {}
    for cp in scenario.contested_points:
        rounds_touched = [t.round for t in user_turns if _match_issue(cp.issue, t.content)]
        coverage[cp.issue] = {
            "engaged": bool(rounds_touched),
            "rounds": rounds_touched,
            "mentions": len(rounds_touched),
        }
    engaged_count = sum(1 for v in coverage.values() if v["engaged"])
    total_issues = len(scenario.contested_points)
    coverage_ratio = round(engaged_count / total_issues, 2) if total_issues else 1.0

    # --- Concession trajectory on percentage figures (e.g. the cap) ---------
    # Direction matters: a buyer pushes the cap UP, a seller pushes it DOWN, so
    # the "anchor" and the "adverse concession" are mirror images depending on side.
    user_wants_high = "buyer" in (scenario.user_side or "").lower()
    pct_trajectory: list[dict] = []
    for t in user_turns:
        for m in PERCENT_RE.finditer(t.content):
            pct_trajectory.append({"round": t.round, "pct": float(m.group(1))})
    pct_values = [p["pct"] for p in pct_trajectory]
    cap_anchor = (max(pct_values) if user_wants_high else min(pct_values)) if pct_values else None
    cap_final = pct_trajectory[-1]["pct"] if pct_trajectory else None
    # Largest single adverse step (a buyer moving down / a seller moving up).
    biggest_single_drop = 0.0
    for a, b in zip(pct_trajectory, pct_trajectory[1:]):
        step = (a["pct"] - b["pct"]) if user_wants_high else (b["pct"] - a["pct"])
        biggest_single_drop = max(biggest_single_drop, step)

    # --- Counts of qualitative signals -------------------------------------
    questions_asked = user_text.count("?")
    creativity_hits = sorted(set(_contains_any(user_text, CREATIVITY_TERMS)))
    grounding_hits = sorted(set(_contains_any(user_text, GROUNDING_TERMS)))
    unprofessional_hits = sorted(set(_contains_any(user_text, UNPROFESSIONAL_TERMS)))
    # Count *incidents* (turns containing a flag) rather than raw term hits, so
    # near-duplicate phrases in one sentence don't over-penalise.
    professionalism_incidents = sum(1 for t in user_turns if _contains_any(t.content, UNPROFESSIONAL_TERMS))

    # Did the user open with an ambitious anchor on the cap?
    if cap_anchor is None:
        strong_anchor = False
    elif user_wants_high:
        strong_anchor = cap_anchor >= 80
    else:
        strong_anchor = cap_anchor <= 25

    return {
        "n_user_turns": n_user,
        "rounds_completed": transcript.rounds_completed,
        "avg_user_words": round(total_user_words / n_user, 1) if n_user else 0,
        "questions_asked": questions_asked,
        "issue_coverage": coverage,
        "issues_engaged": engaged_count,
        "issues_total": total_issues,
        "coverage_ratio": coverage_ratio,
        "issues_untouched": [k for k, v in coverage.items() if not v["engaged"]],
        "pct_trajectory": pct_trajectory,
        "user_wants_high": user_wants_high,
        "cap_anchor": cap_anchor,
        "cap_final": cap_final,
        "biggest_single_pct_drop": biggest_single_drop,
        "strong_anchor": strong_anchor,
        "creativity_hits": creativity_hits,
        "grounding_hits": grounding_hits,
        "unprofessional_hits": unprofessional_hits,
        "professionalism_incidents": professionalism_incidents,
        "termination_reason": (transcript.termination or {}).get("reason", "unknown"),
        "agreements_reached": len(transcript.agreements),
    }


def signals_summary(signals: dict[str, Any]) -> str:
    """A compact human/LLM-readable digest of the computed signals."""
    lines = [
        f"- User turns: {signals['n_user_turns']} (avg {signals['avg_user_words']} words); "
        f"questions asked: {signals['questions_asked']}.",
        f"- Issue coverage: {signals['issues_engaged']}/{signals['issues_total']} contested issues engaged "
        f"({int(signals['coverage_ratio'] * 100)}%).",
    ]
    if signals["issues_untouched"]:
        lines.append(f"- Contested issues NEVER raised by the user: {', '.join(signals['issues_untouched'])}.")
    if signals["cap_anchor"] is not None:
        lines.append(
            f"- Percentage trajectory (e.g. cap): opened at {signals['cap_anchor']}%, "
            f"ended at {signals['cap_final']}%, biggest single drop {signals['biggest_single_pct_drop']} pts."
        )
    if signals["creativity_hits"]:
        lines.append(f"- Creative/structural mechanisms used: {', '.join(signals['creativity_hits'])}.")
    if signals["grounding_hits"]:
        lines.append(f"- Legal/market grounding cues: {', '.join(signals['grounding_hits'])}.")
    if signals["unprofessional_hits"]:
        lines.append(f"- Professionalism flags: {', '.join(signals['unprofessional_hits'])}.")
    return "\n".join(lines)
