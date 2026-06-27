"""Adaptive difficulty engine.

Design
------
Difficulty is an integer 1-10 mapped to five named tiers. After each round the
assessor:

1. Appends the session score to a persisted *progress* file (per user).
2. Recomputes difficulty from a **rolling average** of the last few scores
   (hysteresis), so a single fluky round doesn't whipsaw the level:
       rolling_avg >= STEP_UP   -> difficulty + 1
       rolling_avg <= STEP_DOWN -> difficulty - 1
       otherwise                -> hold
3. Translates the new difficulty into concrete **negotiator_directives**
   (concession rate, aggression, anchor extremity, traps, walk-away appetite,
   suggested round cap) that the *negotiator* agent reads to actually behave
   harder or softer next round.
4. Adds **targeted** pressure: the specific weaknesses found THIS round are fed
   back as `exploit_weaknesses` and a `recommended_persona`, so the opponent
   doesn't just get globally tougher — it attacks exactly where the user was
   soft. That is what makes the difficulty *adaptive* rather than a volume knob.

The output block is part of the assessment JSON, ready for the negotiator to
consume when the user clicks "go again".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

MIN_DIFFICULTY = 1
MAX_DIFFICULTY = 10
DEFAULT_START = 3
STEP_UP = 75.0     # rolling avg at/above this -> harder
STEP_DOWN = 50.0   # rolling avg at/below this -> easier
ROLLING_WINDOW = 3

TIERS = [
    (1, 2, "Cooperative"),
    (3, 4, "Seasoned"),
    (5, 6, "Hardball"),
    (7, 8, "Shark"),
    (9, 10, "Ruthless"),
]

# Map a weak rubric dimension -> (weakness tag, how the opponent should exploit it).
WEAKNESS_EXPLOITS = {
    "anchoring": ("anchors_soft", "Open with an extreme anchor and hold it — this user starts soft, so frame your number as the reasonable centre of gravity."),
    "legal_grounding": ("weak_grounding", "Make confident but subtly flawed legal/market assertions; this user rarely checks the law, so an unchallenged claim will stick."),
    "concession_discipline": ("concedes_fast", "Apply steady pressure and manufactured deadlines; this user concedes when pressed, so push for a second concession right after the first."),
    "information_probing": ("doesnt_probe", "Volunteer nothing about your real interests; this user doesn't probe, so your constraints stay hidden and you keep the information advantage."),
    "creativity_tradecraft": ("no_structure", "Keep it to single numbers and refuse to bundle issues; this user negotiates positionally, so deny them the structures that would bridge the gap."),
    "composure_professionalism": ("gets_rattled", "Needle them with charm and mild provocation; this user gets rattled and makes emotional concessions when baited."),
    "coverage_outcome": ("poor_coverage", "Filibuster the issue they fixate on to burn the clock; this user leaves issues untabled, so run time down and bank the unraised issues on your terms."),
}

# Persona selection: weakness tag -> persona that best punishes it.
PERSONA_BY_WEAKNESS = {
    "poor_coverage": ("The Filibusterer", "Drags a single issue out to eat the clock — punishes weak time management and coverage."),
    "concedes_fast": ("The Stonewaller", "Refuses to move and makes you justify everything — punishes loose concession discipline."),
    "gets_rattled": ("The Charmer", "Warm and disarming, then slips unfavourable terms past you — punishes composure lapses."),
    "weak_grounding": ("The Trapsetter", "Plants confident but flawed legal claims and logical traps — punishes weak grounding."),
    "no_structure": ("The Positional Bruiser", "Haggles single numbers and refuses to bundle — punishes a lack of creative trade-craft."),
    "doesnt_probe": ("The Closed Book", "Gives nothing away about its interests — punishes a failure to probe."),
    "anchors_soft": ("The Extreme Anchor", "Opens aggressively and reframes your number as unreasonable — punishes soft anchoring."),
}


def tier_for(difficulty: int) -> str:
    for lo, hi, name in TIERS:
        if lo <= difficulty <= hi:
            return name
    return "Seasoned"


def load_progress(path: str, user: str) -> dict:
    if path and os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    users = data.setdefault("users", {})
    users.setdefault(user, {
        "current_difficulty": DEFAULT_START,
        "streak": 0,
        "best_score": 0,
        "sessions": [],
    })
    return data


def save_progress(path: str, data: dict) -> None:
    if not path:
        return
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)


def _weaknesses(assessment: dict) -> list[tuple[str, str]]:
    """Return (tag, exploit_instruction) for each weak dimension, worst first."""
    weak = []
    for dim in sorted(assessment["dimensions"], key=lambda d: d["score_5"]):
        if dim["score_5"] <= 2.5 and dim["id"] in WEAKNESS_EXPLOITS:
            weak.append(WEAKNESS_EXPLOITS[dim["id"]])
    # Coverage gap is the highest-signal weakness: if the user left contested
    # issues untabled, surface it first so it drives persona selection.
    if assessment.get("coverage", {}).get("untouched"):
        cov = WEAKNESS_EXPLOITS["coverage_outcome"]
        if cov in weak:
            weak.remove(cov)
        weak.insert(0, cov)
    return weak


def _directives(difficulty: int, weakness_tags: list[str], suggested_rounds: int) -> dict:
    t = (difficulty - MIN_DIFFICULTY) / (MAX_DIFFICULTY - MIN_DIFFICULTY)  # 0..1
    rnd = lambda x: round(x, 2)
    return {
        "persona_intensity": rnd(0.2 + 0.8 * t),
        "aggression": rnd(0.10 + 0.85 * t),
        "concession_rate": rnd(0.60 - 0.50 * t),   # lower = gives less ground
        "patience": rnd(0.30 + 0.55 * t),
        "anchor_extremity": rnd(0.40 + 0.55 * t),
        "concede_only_for_reciprocity": difficulty >= 3,
        "use_logical_traps": difficulty >= 4,
        "bundle_issues_under_pressure": difficulty >= 5,
        "will_walk_away": difficulty >= 6,
        "manufactured_deadlines": difficulty >= 7,
        "suggested_max_rounds": suggested_rounds,
        "targeted_weaknesses": weakness_tags,
    }


def _suggested_rounds(difficulty: int, prev_rounds: int) -> int:
    base = prev_rounds if prev_rounds else 10
    if difficulty >= 9:
        return max(6, base - 4)
    if difficulty >= 7:
        return max(8, base - 2)
    return base


def update_difficulty(
    progress: dict,
    assessment: dict,
    scenario_title: str,
    user: str,
    *,
    prev_rounds: int = 10,
) -> dict:
    """Update the user's progress in-place and return the adaptive_difficulty block."""
    u = progress["users"][user]
    prev = int(u.get("current_difficulty", DEFAULT_START))
    score = int(assessment["overall_score"])
    beat = bool(assessment["beat_target"])

    prior_scores = [s["overall_score"] for s in u["sessions"]]
    window = (prior_scores + [score])[-ROLLING_WINDOW:]
    rolling = sum(window) / len(window)

    if rolling >= STEP_UP:
        new = min(MAX_DIFFICULTY, prev + 1)
        reason = f"Rolling average {rolling:.0f} over last {len(window)} round(s) cleared the step-up line ({STEP_UP:.0f})."
    elif rolling <= STEP_DOWN:
        new = max(MIN_DIFFICULTY, prev - 1)
        reason = f"Rolling average {rolling:.0f} fell to/under the step-down line ({STEP_DOWN:.0f}); easing off to rebuild."
    else:
        new = prev
        reason = f"Rolling average {rolling:.0f} sits in the hold band ({STEP_DOWN:.0f}-{STEP_UP:.0f}); difficulty steady."

    streak = (u.get("streak", 0) + 1) if beat else 0
    best = max(u.get("best_score", 0), score)

    weaknesses = _weaknesses(assessment)
    weakness_tags = [w[0] for w in weaknesses]
    exploit_instructions = [w[1] for w in weaknesses][:3]

    # Persona: pick the one that punishes the worst weakness; else scale by tier.
    persona = None
    for tag in weakness_tags:
        if tag in PERSONA_BY_WEAKNESS:
            name, why = PERSONA_BY_WEAKNESS[tag]
            persona = {"name": name, "why": why}
            break
    if persona is None:
        if new <= 2:
            persona = {"name": "The Cooperative Counterpart", "why": "Collaborative and fair — lets the user build confidence and basic reps."}
        else:
            persona = {"name": "The Closer", "why": f"A composed, well-prepared {tier_for(new)} opponent that presses every advantage cleanly."}

    suggested_rounds = _suggested_rounds(new, prev_rounds)

    block = {
        "previous_difficulty": prev,
        "new_difficulty": new,
        "delta": new - prev,
        "tier": tier_for(new),
        "rationale": reason,
        "rolling_avg": round(rolling, 1),
        "streak": streak,
        "best_score": best,
        "session_count": len(u["sessions"]) + 1,
        "recommended_persona": persona,
        "exploit_weaknesses": exploit_instructions,
        "negotiator_directives": _directives(new, weakness_tags, suggested_rounds),
    }

    # Persist this session.
    u["sessions"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "scenario": scenario_title,
        "overall_score": score,
        "grade": assessment.get("grade"),
        "beat_target": beat,
        "difficulty_in": prev,
        "difficulty_out": new,
    })
    u["current_difficulty"] = new
    u["streak"] = streak
    u["best_score"] = best

    return block
