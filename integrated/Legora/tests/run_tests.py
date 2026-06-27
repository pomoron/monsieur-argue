#!/usr/bin/env python3
"""Zero-dependency test runner for the assessor.

Run from the project root:  python3 tests/run_tests.py
Exits non-zero if any check fails.
"""

from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from assessor import difficulty, llm
from assessor.scoring import assess
from assessor.schemas import load_inputs

RUBRIC_IDS = [
    "anchoring", "legal_grounding", "concession_discipline", "information_probing",
    "creativity_tradecraft", "composure_professionalism", "coverage_outcome",
]

_checks = 0
_fails = 0


def check(cond, msg):
    global _checks, _fails
    _checks += 1
    if cond:
        print(f"  ok   {msg}")
    else:
        _fails += 1
        print(f"  FAIL {msg}")


def fake_assessment(score, *, beat=None, weak_ids=(), untouched=()):
    dims = [
        {"id": i, "name": i, "score_5": (2.0 if i in weak_ids else 4.0)}
        for i in RUBRIC_IDS
    ]
    return {
        "overall_score": score,
        "grade": "X",
        "beat_target": (score >= 70 if beat is None else beat),
        "dimensions": dims,
        "coverage": {"engaged": 5 - len(untouched), "total": 5, "untouched": list(untouched)},
    }


# --------------------------------------------------------------------------- #
def test_json_extraction():
    print("\n[json extraction]")
    check(llm.extract_json('{"a": 1}')["a"] == 1, "plain JSON parses")
    check(llm.extract_json('```json\n{"a": 2}\n```')["a"] == 2, "fenced JSON parses")
    check(llm.extract_json('blah blah {"a": 3} trailing')["a"] == 3, "embedded JSON parses")
    try:
        llm.extract_json("no json here")
        check(False, "non-JSON raises")
    except ValueError:
        check(True, "non-JSON raises")


def test_offline_assessment():
    print("\n[offline assessment on sample files]")
    t, s, p = load_inputs(
        os.path.join(ROOT, "negotiation_summary_20260627_115604.json"),
        os.path.join(ROOT, "scenario.json"),
        os.path.join(ROOT, "playbook.json"),
    )
    a = assess(t, s, p, offline=True)
    check(0 <= a["overall_score"] <= 100, f"overall score in range ({a['overall_score']})")
    check(len(a["dimensions"]) == len(RUBRIC_IDS), "one score per rubric dimension")
    check(abs(sum(d["weight"] for d in a["dimensions"]) - 1.0) < 1e-6, "weights normalise to 1.0")
    # Cap + limitation engaged; the other three not raised.
    check(a["coverage"]["engaged"] == 2, f"2/5 issues engaged ({a['coverage']['engaged']})")
    untouched = set(a["coverage"]["untouched"])
    check("Warranty cap" not in untouched, "warranty cap correctly detected as engaged")
    check("Earn-out mechanism" in untouched, "earn-out correctly flagged untabled")
    check(a["mode"] == "offline", "mode is offline")
    check(bool(a["improvements"]), "produced improvements")


def test_difficulty_ramp_up():
    print("\n[difficulty ramps up on strong play]")
    prog = difficulty.load_progress("", "u_up")
    last = None
    for _ in range(3):
        last = difficulty.update_difficulty(prog, fake_assessment(82), "S", "u_up")
    check(last["new_difficulty"] > 3, f"difficulty rose above start ({last['new_difficulty']})")
    check(last["streak"] == 3, f"streak counts consecutive wins ({last['streak']})")
    check(last["tier"] in ("Hardball", "Shark", "Ruthless"), f"tier escalated ({last['tier']})")


def test_difficulty_ramp_down():
    print("\n[difficulty eases on weak play]")
    prog = difficulty.load_progress("", "u_dn")
    prog["users"]["u_dn"]["current_difficulty"] = 6
    last = None
    for _ in range(3):
        last = difficulty.update_difficulty(prog, fake_assessment(40), "S", "u_dn")
    check(last["new_difficulty"] < 6, f"difficulty fell from 6 ({last['new_difficulty']})")
    check(last["streak"] == 0, "streak resets after a loss")


def test_difficulty_hold_band():
    print("\n[difficulty holds in the middle band]")
    prog = difficulty.load_progress("", "u_hold")
    blk = difficulty.update_difficulty(prog, fake_assessment(62), "S", "u_hold")
    check(blk["new_difficulty"] == 3, f"steady at start in hold band ({blk['new_difficulty']})")
    check(blk["delta"] == 0, "delta is zero in hold band")


def test_adaptive_persona_and_directives():
    print("\n[adaptive persona + directive monotonicity]")
    prog = difficulty.load_progress("", "u_p")
    blk = difficulty.update_difficulty(
        prog, fake_assessment(55, untouched=["Earn-out mechanism", "Non-compete period"]),
        "S", "u_p",
    )
    check(blk["recommended_persona"]["name"] == "The Filibusterer",
          f"poor coverage selects the Filibusterer ({blk['recommended_persona']['name']})")
    # Harder difficulty must mean a stingier opponent.
    d_easy = difficulty._directives(2, [], 10)
    d_hard = difficulty._directives(9, [], 10)
    check(d_hard["concession_rate"] < d_easy["concession_rate"], "higher difficulty concedes less")
    check(d_hard["aggression"] > d_easy["aggression"], "higher difficulty is more aggressive")
    check(d_hard["will_walk_away"] and not d_easy["will_walk_away"], "walk-away unlocks at high difficulty")


def test_progress_persistence():
    print("\n[progress persists to disk]")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "progress.json")
        prog = difficulty.load_progress(path, "u")
        difficulty.update_difficulty(prog, fake_assessment(80), "S", "u")
        difficulty.save_progress(path, prog)
        reloaded = difficulty.load_progress(path, "u")
        check(len(reloaded["users"]["u"]["sessions"]) == 1, "session was written and reloaded")
        check(reloaded["users"]["u"]["best_score"] == 80, "best score persisted")


def test_integration_bridge():
    print("\n[integration: assessor output loads into the negotiator]")
    from assessor import integration
    # Map a weak-coverage assessment to the negotiator's learnings schema.
    a = fake_assessment(58, weak_ids=["information_probing"],
                        untouched=["Earn-out mechanism", "Non-compete period"])
    weaknesses = integration.weaknesses_from_assessment(a)
    check(bool(weaknesses), "weaknesses extracted from assessment")
    check(all({"pattern", "exploit_instruction", "severity"} <= set(w) for w in weaknesses),
          "each weakness has pattern + exploit_instruction + severity")

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "learnings.json")
        integration.update_learnings_file(path, a, trainee_id="t1",
                                          scenario_title="S", session_id="sid")
        # The real negotiator loader must accept and use the file.
        integration.load_negotiator()  # puts the negotiator dir on sys.path
        import learnings_loader
        data = learnings_loader.load_learnings(path)          # raises if schema invalid
        persona = learnings_loader.augment_persona_with_learnings(data, {"name": "V", "wants": []})
        check("tactical_awareness" in persona, "negotiator injects tactical_awareness from our file")
        check(len(persona["tactical_awareness"]["trainee_weaknesses"]) >= 1,
              "trainee_weaknesses populated for the opponent to exploit")

    persona = integration.inject_difficulty_into_persona({"name": "V", "wants": []}, 8)
    check(any("Difficulty" in w for w in persona["wants"]),
          "difficulty directive injected into persona for the live opponent")


def main():
    test_json_extraction()
    test_offline_assessment()
    test_difficulty_ramp_up()
    test_difficulty_ramp_down()
    test_difficulty_hold_band()
    test_adaptive_persona_and_directives()
    test_progress_persistence()
    test_integration_bridge()
    print(f"\n{'='*50}")
    print(f"{_checks - _fails}/{_checks} checks passed.")
    print("=" * 50)
    return 1 if _fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
