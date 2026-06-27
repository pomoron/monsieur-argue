"""
evaluator.py
------------
Standalone evaluation agent that grades a trainee's negotiation performance.

Inputs:
  1. negotiation_summary_*.json  — produced by the negotiation engine at session end
  2. contract_extraction_*.json  — produced by contract_parser when --contract is used
     (optional but strongly recommended; falls back to scenario contested_points)

What it evaluates:
  - TERM COVERAGE: which key contested terms were raised vs ignored
  - NEGOTIATION QUALITY: per term, how well the trainee argued given who the term favours
  - PATTERN DETECTION: mistakes, weaknesses, and strengths observed
  - OVERALL SCORE: a simple rubric score with breakdown

Output (evaluation_*.json):
  {
    "evaluation_id": str,
    "session_id": str,
    "timestamp": str,
    "term_coverage": [...],          # per-term breakdown
    "missed_terms": [...],           # contested terms never raised
    "scores": {...},                 # numeric summary
    "patterns": { "mistakes": [...], "strengths": [...] },
    "recommended_focus": [...],      # top 3 things to work on next
    "past_learnings_entry": {...}    # ready to append to past_learnings.json
  }

Standalone usage:
    python evaluator.py --summary negotiation_summary_20260627_120000.json \\
                        --contract-extraction outputs/contract_extraction_*.json \\
                        --config config.json \\
                        [--append-learnings inputs/past_learnings.json]

Module usage:
    from evaluator import run_evaluation
    evaluation = run_evaluation(config, summary, contract_data)
"""

import argparse
import json
import os
import sys
import uuid
from datetime import datetime

from llm_client import call_llm


# ── LLM prompts ───────────────────────────────────────────────────────────────

_COVERAGE_SYSTEM = """\
You are an expert legal negotiation coach evaluating a trainee lawyer's performance.

You will be given:
1. A list of KEY TERMS from a contract, each flagged as favouring the BUYER (USER),
   the SELLER (AI), or NEUTRAL.
2. A full NEGOTIATION TRANSCRIPT where the trainee played the BUYER side.

Your job:
For each key term, determine:
  - was_addressed: did the trainee raise or respond to this issue? (true/false)
  - rounds_mentioned: list of round numbers where it was discussed ([] if never)
  - trainee_opening: trainee's first stated position on this term (null if never raised)
  - trainee_final: trainee's last stated position (null if never raised)
  - ai_final: the AI opponent's last stated position
  - outcome: one of:
      "AGREED"      — both sides reached an explicit agreement
      "OUTSTANDING" — discussed but not resolved
      "AVOIDED"     — trainee never raised it (missed opportunity or tactical choice)
      "CONCEDED"    — trainee gave up the point without adequate resistance
  - outcome_quality: one of:
      "STRONG"  — outcome was good for the trainee relative to what the term favoured
      "FAIR"    — outcome was roughly at market norm
      "WEAK"    — trainee accepted a poor outcome or missed leverage
      "N/A"     — term was avoided entirely
  - notes: one sentence on what happened or what the trainee should have done

Return ONLY valid JSON:
{
  "term_coverage": [
    {
      "issue": str,
      "favours": "USER" | "AI" | "NEUTRAL",
      "was_addressed": bool,
      "rounds_mentioned": [int],
      "trainee_opening": str | null,
      "trainee_final": str | null,
      "ai_final": str | null,
      "outcome": "AGREED" | "OUTSTANDING" | "AVOIDED" | "CONCEDED",
      "outcome_quality": "STRONG" | "FAIR" | "WEAK" | "N/A",
      "notes": str
    }
  ]
}
"""

_PATTERN_SYSTEM = """\
You are an expert legal negotiation coach. Based on a term-by-term coverage analysis
and the full negotiation transcript, identify:

1. MISTAKES — specific errors the trainee made, each with:
   - issue: which term or "General"
   - mistake: what specifically went wrong (be concrete, quote if possible)
   - pattern: short camel_case label e.g. Premature_concession, Silence_aversion,
               Failure_to_trade, Unused_leverage, Anchoring_error, Bundling_error,
               Poor_preparation, Missed_redline, Emotional_reaction
   - severity: "HIGH" | "MEDIUM" | "LOW"
   - exploit_instruction: how an AI opponent should exploit this next time

2. STRENGTHS — things the trainee did well (be specific)

3. RECOMMENDED_FOCUS — top 3 things the trainee should prioritise in the next session,
   ordered by importance, each as a short actionable sentence.

Return ONLY valid JSON:
{
  "mistakes": [
    {
      "issue": str,
      "mistake": str,
      "pattern": str,
      "severity": "HIGH" | "MEDIUM" | "LOW",
      "exploit_instruction": str
    }
  ],
  "strengths": [str],
  "recommended_focus": [str]
}
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_transcript(dialogue: list) -> str:
    lines = []
    for entry in dialogue:
        label = "TRAINEE (Buyer)" if entry["role"] == "USER" else "AI (Seller)"
        lines.append(f"[Round {entry['round']}] {label}:\n{entry['content']}")
    return "\n\n".join(lines)


def _extract_key_terms(contract_data: dict, summary: dict) -> list:
    """
    Build a flat list of key terms to evaluate against.
    Prefers contract_data contested_terms (with favours flags).
    Falls back to summary outstanding_issues + agreements if no contract data.
    """
    terms = []

    if contract_data and contract_data.get("contested_terms"):
        for t in contract_data["contested_terms"]:
            terms.append({
                "issue":   t.get("issue", "Unknown"),
                "favours": t.get("favours", "NEUTRAL"),   # PARTY_A / PARTY_B / NEUTRAL
                "notes":   t.get("notes", ""),
                "current_drafting": t.get("current_drafting", ""),
            })
        # Also include agreed_terms that might still be worth evaluating
        for t in contract_data.get("agreed_terms", []):
            if t.get("favours") in ("PARTY_A", "PARTY_B"):  # skip neutral settled terms
                terms.append({
                    "issue":   t.get("description", t.get("clause", "Unknown")),
                    "favours": t.get("favours", "NEUTRAL"),
                    "notes":   f"Already agreed in contract per clause {t.get('clause','')}",
                    "current_drafting": t.get("description", ""),
                })
    else:
        # Fallback: derive from session summary
        for cp in summary.get("outstanding_issues", []):
            terms.append({
                "issue":   cp.get("issue", "Unknown"),
                "favours": "NEUTRAL",
                "notes":   cp.get("notes", ""),
                "current_drafting": "",
            })
        for ag in summary.get("agreements", []):
            terms.append({
                "issue":   ag.get("issue", "Unknown"),
                "favours": "NEUTRAL",
                "notes":   f"Agreed: {ag.get('agreed_term','')}",
                "current_drafting": "",
            })

    return terms


def _score(term_coverage: list) -> dict:
    """Compute numeric scores from term coverage list."""
    total     = len(term_coverage)
    if total == 0:
        return {"term_coverage_pct": 0, "quality_score": 0, "overall": 0}

    addressed = sum(1 for t in term_coverage if t.get("was_addressed"))
    quality_map = {"STRONG": 1.0, "FAIR": 0.5, "WEAK": 0.0, "N/A": 0.0}
    quality_sum = sum(quality_map.get(t.get("outcome_quality", "N/A"), 0.0)
                      for t in term_coverage)

    coverage_pct  = round(addressed / total * 100)
    quality_score = round(quality_sum / total * 100)
    overall       = round((coverage_pct * 0.4 + quality_score * 0.6))

    return {
        "term_coverage_pct": coverage_pct,
        "quality_score":     quality_score,
        "overall":           overall,
        "terms_total":       total,
        "terms_addressed":   addressed,
        "terms_missed":      total - addressed,
    }


# ── Core evaluation ───────────────────────────────────────────────────────────

def run_evaluation(
    config: dict,
    summary: dict,
    contract_data: dict = None,
) -> dict:
    """
    Run the full evaluation pipeline.

    Args:
        config:        loaded config.json
        summary:       loaded negotiation_summary_*.json
        contract_data: loaded contract_extraction_*.json (optional)

    Returns:
        evaluation dict (save to disk with save_evaluation())
    """
    transcript  = _build_transcript(summary.get("dialogue", []))
    key_terms   = _extract_key_terms(contract_data, summary)
    terms_json  = json.dumps(key_terms, indent=2)

    # ── Step 1: term coverage ─────────────────────────────────────────────────
    print("[evaluator] Evaluating term coverage...")
    coverage_raw = call_llm(
        config=config,
        system_prompt=_COVERAGE_SYSTEM,
        messages=[{"role": "user", "content": (
            f"KEY TERMS:\n{terms_json}\n\n"
            f"NEGOTIATION TRANSCRIPT:\n{transcript}\n\n"
            "Evaluate each term. Return JSON only."
        )}],
        temperature=0.1,
        max_tokens=4096,
    )

    coverage_raw = coverage_raw.strip()
    if coverage_raw.startswith("```"):
        lines = coverage_raw.split("\n")
        coverage_raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        coverage = json.loads(coverage_raw)
    except json.JSONDecodeError as e:
        print(f"[evaluator] WARNING: Coverage parse error ({e}). Using empty coverage.")
        coverage = {"term_coverage": []}

    term_coverage = coverage.get("term_coverage", [])
    missed_terms  = [t["issue"] for t in term_coverage if not t.get("was_addressed")]

    # ── Step 2: pattern detection ─────────────────────────────────────────────
    print("[evaluator] Detecting patterns and mistakes...")
    pattern_raw = call_llm(
        config=config,
        system_prompt=_PATTERN_SYSTEM,
        messages=[{"role": "user", "content": (
            f"TERM COVERAGE ANALYSIS:\n{json.dumps(term_coverage, indent=2)}\n\n"
            f"FULL TRANSCRIPT:\n{transcript}\n\n"
            "Identify mistakes, strengths, and recommended focus. Return JSON only."
        )}],
        temperature=0.1,
        max_tokens=2048,
    )

    pattern_raw = pattern_raw.strip()
    if pattern_raw.startswith("```"):
        lines = pattern_raw.split("\n")
        pattern_raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        patterns = json.loads(pattern_raw)
    except json.JSONDecodeError as e:
        print(f"[evaluator] WARNING: Pattern parse error ({e}). Using empty patterns.")
        patterns = {"mistakes": [], "strengths": [], "recommended_focus": []}

    # ── Step 3: assemble output ───────────────────────────────────────────────
    scores = _score(term_coverage)

    # Build a past_learnings_entry ready to drop into past_learnings.json
    past_learnings_entry = {
        "session_id":         summary.get("session_id", str(uuid.uuid4())),
        "date":               datetime.now().strftime("%Y-%m-%d"),
        "scenario_title":     summary.get("metadata", {}).get("scenario_title", ""),
        "rounds_completed":   summary.get("rounds_completed", 0),
        "termination_reason": summary.get("termination", {}).get("reason", ""),
        "agreements_reached": summary.get("agreements", []),
        "outstanding_issues": [
            {
                "issue": oi.get("issue"),
                "trainee_final_position": oi.get("user_position"),
                "ai_final_position":      oi.get("ai_position"),
            }
            for oi in summary.get("outstanding_issues", [])
        ],
        "analyst_notes": {
            "overall_assessment": (
                f"Term coverage: {scores['term_coverage_pct']}% "
                f"({scores['terms_addressed']}/{scores['terms_total']} terms addressed). "
                f"Quality score: {scores['quality_score']}%. "
                f"Overall: {scores['overall']}%."
            ),
            "mistakes":  patterns.get("mistakes", []),
            "strengths": patterns.get("strengths", []),
        },
    }

    evaluation = {
        "evaluation_id":    str(uuid.uuid4()),
        "session_id":       summary.get("session_id", ""),
        "timestamp":        datetime.now().isoformat(),
        "scenario_title":   summary.get("metadata", {}).get("scenario_title", ""),
        "trainee_side":     summary.get("metadata", {}).get("user_side", ""),
        "ai_side":          summary.get("metadata", {}).get("ai_side", ""),
        "contract_used":    summary.get("metadata", {}).get("contract_used"),
        "term_coverage":    term_coverage,
        "missed_terms":     missed_terms,
        "scores":           scores,
        "patterns": {
            "mistakes":  patterns.get("mistakes", []),
            "strengths": patterns.get("strengths", []),
        },
        "recommended_focus":    patterns.get("recommended_focus", []),
        "past_learnings_entry": past_learnings_entry,
    }

    return evaluation


def save_evaluation(evaluation: dict, output_dir: str = ".") -> str:
    """Write evaluation dict to disk. Returns file path."""
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath  = os.path.join(output_dir, f"evaluation_{timestamp}.json")
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(evaluation, f, indent=2, ensure_ascii=False)
    return filepath


def append_to_learnings(evaluation: dict, learnings_path: str) -> None:
    """
    Append the past_learnings_entry from an evaluation into an existing
    past_learnings.json, and regenerate aggregate_patterns from all sessions.
    """
    if not os.path.exists(learnings_path):
        print(f"[evaluator] past_learnings.json not found at '{learnings_path}'. "
              "Creating new file.")
        learnings = {
            "_schema_version": "1.0",
            "trainee_id":      "unknown",
            "sessions":        [],
            "aggregate_patterns": {"recurring_weaknesses": [], "recurring_strengths": []},
        }
    else:
        with open(learnings_path, encoding="utf-8") as f:
            learnings = json.load(f)

    entry = evaluation.get("past_learnings_entry", {})

    # Avoid duplicate session IDs
    existing_ids = {s.get("session_id") for s in learnings.get("sessions", [])}
    if entry.get("session_id") in existing_ids:
        print(f"[evaluator] Session {entry['session_id']} already in past_learnings. Skipping append.")
        return

    learnings.setdefault("sessions", []).append(entry)

    # Regenerate aggregate_patterns from all sessions
    pattern_counts = {}
    for session in learnings["sessions"]:
        for mistake in session.get("analyst_notes", {}).get("mistakes", []):
            pat = mistake.get("pattern", "")
            if not pat:
                continue
            if pat not in pattern_counts:
                pattern_counts[pat] = {
                    "pattern":     pat,
                    "description": mistake.get("mistake", ""),
                    "exploit_instruction": mistake.get("exploit_instruction", ""),
                    "severity":    mistake.get("severity", "MEDIUM"),
                    "observed_in_sessions": [],
                }
            sid = session.get("session_id", "")
            if sid not in pattern_counts[pat]["observed_in_sessions"]:
                pattern_counts[pat]["observed_in_sessions"].append(sid)

    # Sort by frequency descending, then severity
    sev_order = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    sorted_patterns = sorted(
        pattern_counts.values(),
        key=lambda p: (
            -len(p["observed_in_sessions"]),
            sev_order.get(p["severity"], 9),
        ),
    )
    learnings["aggregate_patterns"]["recurring_weaknesses"] = sorted_patterns

    # Aggregate strengths (deduplicated)
    seen_strengths = set()
    all_strengths  = []
    for session in learnings["sessions"]:
        for s in session.get("analyst_notes", {}).get("strengths", []):
            if s not in seen_strengths:
                seen_strengths.add(s)
                all_strengths.append(s)
    learnings["aggregate_patterns"]["recurring_strengths"] = all_strengths

    with open(learnings_path, "w", encoding="utf-8") as f:
        json.dump(learnings, f, indent=2, ensure_ascii=False)

    print(f"[evaluator] Appended session to: {learnings_path}")
    print(f"[evaluator] {len(sorted_patterns)} recurring pattern(s) in aggregate.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Evaluate a negotiation session against contract key terms."
    )
    parser.add_argument(
        "--summary", required=True,
        help="Path to negotiation_summary_*.json (output from main.py)"
    )
    parser.add_argument(
        "--contract-extraction", default=None,
        help="Path to contract_extraction_*.json (output from contract_parser). "
             "Optional but recommended — without it evaluation uses summary data only."
    )
    parser.add_argument(
        "--config", default="config.json",
        help="Path to config.json"
    )
    parser.add_argument(
        "--output", default=".",
        help="Directory to write evaluation_*.json (default: current directory)"
    )
    parser.add_argument(
        "--append-learnings", default=None, metavar="PATH",
        help="Path to past_learnings.json — if provided, appends this session's "
             "findings and regenerates aggregate_patterns automatically."
    )
    args = parser.parse_args()

    def load(path, label):
        if not os.path.exists(path):
            print(f"Error: {label} not found at '{path}'"); sys.exit(1)
        with open(path, encoding="utf-8") as f: return json.load(f)

    config  = load(args.config,  "config.json")
    summary = load(args.summary, "negotiation summary")

    contract_data = None
    if args.contract_extraction:
        contract_data = load(args.contract_extraction, "contract extraction")
        print(f"[evaluator] Loaded contract extraction: {args.contract_extraction}")
    else:
        print("[evaluator] No contract extraction provided — using session summary data only.")

    print(f"[evaluator] Evaluating session: {summary.get('session_id', '?')}")
    evaluation = run_evaluation(config, summary, contract_data)

    # Print summary to console
    scores = evaluation["scores"]
    print(f"\n{'═' * 60}")
    print(f"  EVALUATION RESULTS")
    print(f"{'═' * 60}")
    print(f"  Term coverage : {scores['term_coverage_pct']}%  "
          f"({scores['terms_addressed']}/{scores['terms_total']} terms addressed)")
    print(f"  Quality score : {scores['quality_score']}%")
    print(f"  Overall       : {scores['overall']}%")
    if evaluation["missed_terms"]:
        print(f"\n  Missed terms: {', '.join(evaluation['missed_terms'])}")
    mistakes = evaluation["patterns"]["mistakes"]
    if mistakes:
        print(f"\n  Mistakes ({len(mistakes)}):")
        for m in mistakes:
            print(f"    [{m.get('severity','?')}] {m.get('pattern','?')}: {m.get('mistake','')[:80]}...")
    focus = evaluation.get("recommended_focus", [])
    if focus:
        print(f"\n  Recommended focus for next session:")
        for i, f_item in enumerate(focus, 1):
            print(f"    {i}. {f_item}")
    print(f"{'═' * 60}\n")

    filepath = save_evaluation(evaluation, args.output)
    print(f"[evaluator] Full evaluation saved: {filepath}")

    if args.append_learnings:
        append_to_learnings(evaluation, args.append_learnings)


if __name__ == "__main__":
    main()
