"""Scoring: turns a transcript + scenario + playbook into a structured assessment.

Two paths produce the *same* assessment shape:
  * ``score_with_llm`` — asks Claude to grade each rubric dimension and write
    specific, grounded coaching. Preferred when an API key is available.
  * ``score_offline``  — deterministic heuristic over the computed signals.
    Always available; used as a fallback so the CLI never hard-fails.

The weighted overall score, grade and pass/fail are computed in Python in
*both* paths so the maths is reliable and consistent rather than hallucinated.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from . import llm
from .schemas import Scenario, Transcript, resolved_weights
from .signals import extract_signals, signals_summary


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _grade(playbook: dict, score: int) -> tuple[str, str]:
    bands = sorted(
        playbook.get("scoring", {}).get("grade_bands", []),
        key=lambda b: b["min"],
        reverse=True,
    )
    for b in bands:
        if score >= b["min"]:
            return b["grade"], b.get("label", "")
    return "E", "Needs work"


def _pass_target(playbook: dict) -> int:
    return int(playbook.get("scoring", {}).get("pass_target", 70))


def _finalize(assessment: dict, playbook: dict, scenario: Scenario, signals: dict) -> dict:
    """Compute overall score, grade, coverage and attach signals — shared by both paths."""
    weights = resolved_weights(playbook, scenario)
    dim_meta = {d["id"]: d for d in playbook["rubric"]["dimensions"]}

    weighted = 0.0
    for dim in assessment["dimensions"]:
        did = dim["id"]
        w = weights.get(did, 0.0)
        score5 = max(0.0, min(5.0, float(dim.get("score_5", 0))))
        dim["score_5"] = round(score5, 1)
        dim["weight"] = round(w, 3)
        dim["name"] = dim.get("name") or dim_meta.get(did, {}).get("name", did)
        dim["weighted_points"] = round((score5 / 5.0) * w * 100, 1)
        weighted += dim["weighted_points"]

    overall = int(round(weighted))
    grade, label = _grade(playbook, overall)
    target = _pass_target(playbook)

    assessment["overall_score"] = overall
    assessment["grade"] = grade
    assessment["grade_label"] = label
    assessment["pass_target"] = target
    assessment["beat_target"] = overall >= target
    assessment["coverage"] = {
        "engaged": signals["issues_engaged"],
        "total": signals["issues_total"],
        "untouched": signals["issues_untouched"],
    }
    assessment["signals"] = signals
    return assessment


# --------------------------------------------------------------------------- #
# LLM path
# --------------------------------------------------------------------------- #
def _build_prompt(transcript: Transcript, scenario: Scenario, playbook: dict, signals: dict):
    scen_pb = playbook.get("scenarios", {}).get(scenario.title, {})
    dims = playbook["rubric"]["dimensions"]

    rubric_block = "\n".join(
        f"  - {d['id']} ({d['name']}): {d['what_good_looks_like']}\n"
        f"      5 = {d['bands'].get('5','')}\n"
        f"      3 = {d['bands'].get('3','')}\n"
        f"      1 = {d['bands'].get('1','')}"
        for d in dims
    )

    contested = "\n".join(
        f"  - {c.issue}: BUYER wants [{c.buyer_position}]; SELLER wants [{c.seller_position}]. Note: {c.notes}"
        for c in scenario.contested_points
    )

    model_answers = json.dumps(scen_pb.get("target_outcomes", {}), indent=2) if scen_pb else "(none provided)"
    model_summary = scen_pb.get("model_answer_summary", "(none provided)")
    key_traps = "\n".join(f"  - {t}" for t in scen_pb.get("key_traps", []))

    system = (
        "You are an elite negotiation coach and assessor for M&A lawyers — think a "
        "demanding magic-circle partner debriefing a junior after a live negotiation. "
        "You are fair but exacting: you reward genuine skill and you name weak play "
        "precisely, with the round number and a quote. You NEVER flatter. You judge the "
        "USER's performance only (not the AI opponent). Ground every judgement in the "
        "rubric, the scenario's model answers, and the verifiable signals provided. "
        "Return ONLY a single JSON object, no prose, no code fences."
    )

    user = f"""Assess the USER's performance in this negotiation training round.

## SCENARIO
Title: {scenario.title}
Background: {scenario.background}
The USER acts for: {scenario.user_side}
The AI opponent acts for: {scenario.your_side}

Contested issues (the USER should fight for the buyer column):
{contested}

## THE STANDARD ("what good looks like")
Model-answer summary:
{model_summary}

Per-issue target outcomes (strong / acceptable / weak, model moves, traps):
{model_answers}

Key traps to check whether the user fell into:
{key_traps}

## RUBRIC (score each dimension 0-5 against these descriptors)
{rubric_block}

## COMPUTED SIGNALS (verifiable facts about the transcript — use these to stay grounded)
{signals_summary(signals)}

## TRANSCRIPT
{transcript.transcript_text()}

## OUTPUT — return exactly this JSON shape:
{{
  "headline": "<=25 word verdict on how the user did",
  "dimensions": [
    {{"id": "<rubric id>", "score_5": <0-5, .5 ok>, "comment": "<1-2 sentences>",
      "evidence": ["<short quote or round ref>", "..."]}}
    // one object PER rubric dimension above, same ids
  ],
  "strengths": ["<specific thing the user did well>", "..."],
  "improvements": [
    {{"title": "<short label>", "round": <int or null>,
      "what_happened": "<what the user actually did, with a quote>",
      "better_move": "<the stronger move a great lawyer would have made>"}}
  ],
  "turning_points": [
    {{"round": <int>, "label": "<short>", "what_happened": "<...>", "should_have": "<the better line at that exact moment>"}}
  ],
  "issue_outcomes": [
    {{"issue": "<contested issue>", "engaged": <true|false>,
      "vs_target": "strong|acceptable|weak|not addressed",
      "result": "<where it landed>", "note": "<coaching>"}}
    // one PER contested issue
  ]
}}

Rules:
- Provide one dimensions entry for every rubric id, using the exact ids.
- Be specific: cite round numbers and short quotes. No generic advice.
- Flag any contested issue the user never raised as "not addressed" — that is a silent concession and should hurt the coverage_outcome score.
- 3-5 improvements, ordered by impact. 1-3 turning points."""
    return system, user


def score_with_llm(
    transcript: Transcript, scenario: Scenario, playbook: dict, signals: dict, model: str
) -> dict:
    system, user = _build_prompt(transcript, scenario, playbook, signals)
    raw = llm.complete(system, user, model=model)
    data = llm.extract_json(raw)

    # Ensure one dimension entry per rubric id (fill any the model omitted).
    by_id = {d.get("id"): d for d in data.get("dimensions", [])}
    dims = []
    for meta in playbook["rubric"]["dimensions"]:
        did = meta["id"]
        d = by_id.get(did, {"id": did, "score_5": 2.5, "comment": "(model omitted this dimension)", "evidence": []})
        d["id"] = did
        dims.append(d)
    data["dimensions"] = dims
    data.setdefault("strengths", [])
    data.setdefault("improvements", [])
    data.setdefault("turning_points", [])
    data.setdefault("issue_outcomes", [])
    data["mode"] = "llm"
    data["model"] = model
    return _finalize(data, playbook, scenario, signals)


# --------------------------------------------------------------------------- #
# Offline heuristic path
# --------------------------------------------------------------------------- #
def _clamp(x: float) -> float:
    return round(max(0.0, min(5.0, x)) * 2) / 2  # snap to nearest 0.5


def score_offline(transcript: Transcript, scenario: Scenario, playbook: dict, signals: dict) -> dict:
    s = signals
    dim_scores: dict[str, float] = {}
    dim_comments: dict[str, str] = {}
    dim_evidence: dict[str, list] = {}

    # anchoring
    if s["cap_anchor"] is None:
        dim_scores["anchoring"] = 3.0
        dim_comments["anchoring"] = "No explicit numeric anchor detected; opened without a hard number to defend."
    else:
        base = 5.0 if s["strong_anchor"] else (3.0 if (40 <= s["cap_anchor"] <= 80 or not s["user_wants_high"]) else 2.5)
        dim_scores["anchoring"] = _clamp(base)
        dim_comments["anchoring"] = f"Anchored at {s['cap_anchor']}% — {'ambitious and defensible' if s['strong_anchor'] else 'on the soft side for an opening'}."
        dim_evidence["anchoring"] = [f"Opening cap demand ~{s['cap_anchor']}%"]

    # legal_grounding
    g = len(s["grounding_hits"])
    dim_scores["legal_grounding"] = _clamp(2.0 + 0.7 * g)
    dim_comments["legal_grounding"] = (
        f"Used {g} grounding cue(s) ({', '.join(s['grounding_hits']) or 'none'}). "
        + ("Solid use of law/market." if g >= 3 else "Leans on assertion; cite statute and market data more.")
    )
    dim_evidence["legal_grounding"] = s["grounding_hits"][:4]

    # concession_discipline
    drop = s["biggest_single_pct_drop"]
    if drop == 0:
        cd = 3.5
    elif drop <= 15:
        cd = 4.5
    elif drop <= 30:
        cd = 3.5
    elif drop <= 50:
        cd = 2.5
    else:
        cd = 1.5
    dim_scores["concession_discipline"] = _clamp(cd)
    dim_comments["concession_discipline"] = (
        f"Largest single adverse move was {drop} pts."
        + (" Disciplined, incremental moves." if drop <= 15 else " Watch oversized single-step concessions; trade every give for a get.")
    )
    if s["cap_anchor"] is not None and s["cap_final"] is not None:
        dim_evidence["concession_discipline"] = [f"Moved from {s['cap_anchor']}% toward {s['cap_final']}%"]

    # information_probing
    q = s["questions_asked"]
    ratio = q / max(1, s["n_user_turns"])
    ip = 2.0 + min(3.0, ratio * 3.0)
    dim_scores["information_probing"] = _clamp(ip)
    dim_comments["information_probing"] = f"Asked {q} question(s) across {s['n_user_turns']} turns. " + (
        "Good diagnostic pressure." if ratio >= 0.6 else "Probe the other side's interests/BATNA more."
    )

    # creativity_tradecraft
    c = len(s["creativity_hits"])
    dim_scores["creativity_tradecraft"] = _clamp(2.0 + 0.8 * c)
    dim_comments["creativity_tradecraft"] = (
        f"Deployed {c} structural mechanism(s): {', '.join(s['creativity_hits']) or 'none'}. "
        + ("Strong pie-expanding play." if c >= 2 else "Reach for structures (tiered caps, W&I insurance, specific indemnities) to bridge gaps.")
    )
    dim_evidence["creativity_tradecraft"] = s["creativity_hits"][:5]

    # composure_professionalism (score on incidents, not raw term hits)
    incidents = s.get("professionalism_incidents", len(s["unprofessional_hits"]))
    dim_scores["composure_professionalism"] = max(1.0, _clamp(4.8 - 0.6 * incidents))
    dim_comments["composure_professionalism"] = (
        "Professional register held throughout." if incidents == 0
        else f"{incidents} turn(s) slipped into informal/ad-hominem register ({', '.join(s['unprofessional_hits'])}). Keep it precise and professional under pressure."
    )
    dim_evidence["composure_professionalism"] = s["unprofessional_hits"][:4]

    # coverage_outcome
    cov = s["coverage_ratio"]
    co = 1.0 + cov * 4.0
    dim_scores["coverage_outcome"] = _clamp(co)
    dim_comments["coverage_outcome"] = (
        f"Engaged {s['issues_engaged']}/{s['issues_total']} contested issues."
        + (f" Never raised: {', '.join(s['issues_untouched'])} — silent concessions." if s["issues_untouched"] else " Full coverage.")
    )
    dim_evidence["coverage_outcome"] = [f"{int(cov*100)}% issue coverage"]

    dimensions = []
    for meta in playbook["rubric"]["dimensions"]:
        did = meta["id"]
        dimensions.append({
            "id": did,
            "name": meta["name"],
            "score_5": dim_scores.get(did, 3.0),
            "comment": dim_comments.get(did, ""),
            "evidence": dim_evidence.get(did, []),
        })

    # --- narrative -----------------------------------------------------------
    strengths = []
    if s["strong_anchor"]:
        strengths.append(f"Anchored ambitiously (~{s['cap_anchor']}%) and made the other side argue down from your number.")
    if len(s["creativity_hits"]) >= 1:
        strengths.append(f"Brought structure to break deadlock: {', '.join(s['creativity_hits'][:3])}.")
    if len(s["grounding_hits"]) >= 3:
        strengths.append("Grounded arguments in law/market rather than bare assertion.")
    if not strengths:
        strengths.append("Engaged the opponent's arguments directly and held a position under pressure.")

    improvements = []
    if s["issues_untouched"]:
        improvements.append({
            "title": "You left value on the table — whole issues untabled",
            "round": s["rounds_completed"],
            "what_happened": f"{len(s['issues_untouched'])} of {s['issues_total']} contested issues were never raised: {', '.join(s['issues_untouched'])}.",
            "better_move": "Pace the clock across all issues; park the one you're stuck on and table the rest. Every issue you never raise is conceded on the opponent's terms by default.",
        })
    if drop > 30:
        improvements.append({
            "title": "Oversized single concession",
            "round": None,
            "what_happened": f"Your biggest single move was {drop} percentage points.",
            "better_move": "Move in smaller increments and make each concession reciprocal — 'I can come down on the cap if you give me the longer limitation period.'",
        })
    if len(s["creativity_hits"]) == 0:
        improvements.append({
            "title": "Positional haggling where structure would win",
            "round": None,
            "what_happened": "The exchange stayed on single numbers with no bridging structure.",
            "better_move": "Introduce W&I insurance, a tiered/sunset cap, or a specific indemnity to bridge gaps without either side losing face.",
        })
    if s["unprofessional_hits"]:
        improvements.append({
            "title": "Mind the register",
            "round": None,
            "what_happened": f"Informal / ad-hominem language detected: {', '.join(s['unprofessional_hits'])}.",
            "better_move": "Stay precise and professional — jabs leak that you're rattled and cost you credibility at the table.",
        })
    if s["questions_asked"] / max(1, s["n_user_turns"]) < 0.4:
        improvements.append({
            "title": "Probe more for interests",
            "round": None,
            "what_happened": f"Only {s['questions_asked']} questions across {s['n_user_turns']} turns.",
            "better_move": "Ask why the other side wants something. Their stated interests (cash at completion, key-employee retention, founder's new venture) are levers for cheap wins.",
        })
    improvements = improvements[:5] or [{
        "title": "Push for above-target outcomes",
        "round": None,
        "what_happened": "Solid all-round play.",
        "better_move": "Now press for the strong-target outcome on the highest-value issues, not just the acceptable line.",
    }]

    turning_points = []
    drop_round = None
    for a, b in zip(s["pct_trajectory"], s["pct_trajectory"][1:]):
        step = (a["pct"] - b["pct"]) if s["user_wants_high"] else (b["pct"] - a["pct"])
        if step == drop and drop > 0:
            drop_round = b["round"]
            turning_points.append({
                "round": b["round"],
                "label": "Big concession",
                "what_happened": f"Moved from {a['pct']}% to {b['pct']}% in one step.",
                "should_have": "Hold, and attach a condition: concede only in exchange for a reciprocal give on another issue.",
            })
            break
    if s["issues_untouched"]:
        turning_points.append({
            "round": s["rounds_completed"],
            "label": "Clock ran out",
            "what_happened": f"Session ended with {', '.join(s['issues_untouched'])} never tabled.",
            "should_have": "By this point all five issues should have been opened; the unraised ones default to the seller's position.",
        })

    issue_outcomes = []
    for cp in scenario.contested_points:
        cov_entry = s["issue_coverage"].get(cp.issue, {})
        engaged = bool(cov_entry.get("engaged"))
        issue_outcomes.append({
            "issue": cp.issue,
            "engaged": engaged,
            "vs_target": "engaged" if engaged else "not addressed",
            "result": f"discussed in rounds {cov_entry.get('rounds')}" if engaged else "never raised",
            "note": "" if engaged else "Silent concession — defaults to the seller's position.",
        })

    headline = (
        f"Grade pending — engaged {s['issues_engaged']}/{s['issues_total']} issues"
        f"{', strong anchoring and creative structure' if (s['strong_anchor'] and s['creativity_hits']) else ''}."
    )

    data = {
        "mode": "offline",
        "model": None,
        "headline": headline,
        "dimensions": dimensions,
        "strengths": strengths,
        "improvements": improvements,
        "turning_points": turning_points,
        "issue_outcomes": issue_outcomes,
    }
    return _finalize(data, playbook, scenario, signals)


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def assess(
    transcript: Transcript,
    scenario: Scenario,
    playbook: dict,
    *,
    offline: bool = False,
    model: str = llm.DEFAULT_MODEL,
) -> dict:
    """Produce an assessment, preferring the LLM but always returning something."""
    signals = extract_signals(transcript, scenario)
    if offline or not llm.have_api_key():
        result = score_offline(transcript, scenario, playbook, signals)
        if offline:
            result["mode_note"] = "offline (forced with --offline)"
        else:
            result["mode_note"] = "offline (no ANTHROPIC_API_KEY found)"
        return result
    print(f"  … scoring with {model} — waiting on the Claude API "
          f"(can take 30-90s; offline fallback at 90s) …", file=sys.stderr, flush=True)
    t0 = time.time()
    try:
        result = score_with_llm(transcript, scenario, playbook, signals, model=model)
        print(f"  ✓ Claude responded in {time.time() - t0:.0f}s.", file=sys.stderr, flush=True)
        return result
    except (llm.LLMUnavailable, ValueError, KeyError) as exc:
        print(f"  ! LLM scoring failed after {time.time() - t0:.0f}s — using offline scorer.",
              file=sys.stderr, flush=True)
        result = score_offline(transcript, scenario, playbook, signals)
        result["mode_note"] = f"offline fallback (LLM error: {exc})"
        return result
