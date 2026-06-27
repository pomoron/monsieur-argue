#!/usr/bin/env python3
"""Demo: show how adaptive difficulty ramps across a sequence of rounds.

This drives the real difficulty engine with a scripted series of round scores
(simulating a lawyer who improves, plateaus, then has an off day) and prints the
resulting difficulty curve, tiers, personas and streaks.

    python3 simulate_progress.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assessor import difficulty

RUBRIC_IDS = [
    "anchoring", "legal_grounding", "concession_discipline", "information_probing",
    "creativity_tradecraft", "composure_professionalism", "coverage_outcome",
]

# (score, list-of-untouched-issues) for each simulated round.
SCRIPT = [
    (61, ["Earn-out mechanism", "Non-compete period", "Retention for indemnity claims"]),
    (69, ["Non-compete period"]),
    (78, []),
    (84, []),
    (82, []),
    (47, ["Retention for indemnity claims", "Earn-out mechanism"]),
    (75, []),
]


def fake_assessment(score, untouched):
    weak = {"coverage_outcome"} if untouched else set()
    if score < 55:
        weak |= {"concession_discipline"}
    dims = [{"id": i, "name": i, "score_5": (2.0 if i in weak else 4.0)} for i in RUBRIC_IDS]
    return {
        "overall_score": score,
        "grade": "X",
        "beat_target": score >= 70,
        "dimensions": dims,
        "coverage": {"engaged": 5 - len(untouched), "total": 5, "untouched": untouched},
    }


def bar(d):
    return "▮" * d + "·" * (10 - d)


def main():
    prog = difficulty.load_progress("", "demo")
    print("\nADAPTIVE DIFFICULTY — simulated progression\n")
    print(f"{'Rnd':>3}  {'Score':>5}  {'Beat':>4}  {'Difficulty':<12} {'Tier':<11} {'Streak':>6}  Next opponent")
    print("-" * 92)
    for i, (score, untouched) in enumerate(SCRIPT, 1):
        a = fake_assessment(score, untouched)
        blk = difficulty.update_difficulty(prog, a, "Greenvale", "demo")
        beat = "✓" if a["beat_target"] else "✗"
        diff_str = f"{blk['previous_difficulty']}→{blk['new_difficulty']} {bar(blk['new_difficulty'])}"
        streak = f"🔥{blk['streak']}" if blk["streak"] else "—"
        print(f"{i:>3}  {score:>5}  {beat:>4}  {diff_str:<12} {blk['tier']:<11} {streak:>6}  {blk['recommended_persona']['name']}")
    u = prog["users"]["demo"]
    print("-" * 92)
    print(f"Final difficulty: {u['current_difficulty']} ({difficulty.tier_for(u['current_difficulty'])})  "
          f"| best score: {u['best_score']}  | rounds played: {len(u['sessions'])}")
    print("\nNote how difficulty climbs as scores stay high, eases after the off day (round 6),\n"
          "and the recommended opponent switches to punish the specific weakness shown.\n")


if __name__ == "__main__":
    main()
