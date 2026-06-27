#!/usr/bin/env python3
"""Legora — one CLI for the negotiation training ground.

The full loop in one tool:  negotiate against the AI  →  get scored & coached
→  difficulty adapts and the opponent is sharpened against your weak spots  →
play again.

Subcommands
-----------
  play       Run a live negotiation round, then auto-score it and adapt difficulty.
  assess     Score an existing transcript (the standalone assessor).
  status     Show your progress: difficulty, streak, best score, history.
  simulate   Demo the adaptive-difficulty curve over a scripted series of rounds.

Examples
--------
  # Try the whole loop with no API key (mock opponent, offline scoring):
  python3 legora.py play --mock

  # Live round (needs ANTHROPIC_API_KEY):
  export ANTHROPIC_API_KEY=sk-ant-...
  python3 legora.py play

  # Where am I?
  python3 legora.py status

  # Score a transcript the negotiator already produced:
  python3 legora.py assess -t negotiation_summary_XXXX.json -s scenario.json
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from assessor import difficulty as diff_mod
from assessor import integration, llm
from assessor.integration import NEGOTIATOR_INPUTS

DEFAULT_PLAYBOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbook.json")


def _supports_color(no_color: bool) -> bool:
    return (not no_color) and sys.stdout.isatty()


# --------------------------------------------------------------------------- #
# play
# --------------------------------------------------------------------------- #
def cmd_play(args) -> int:
    if not args.mock and not llm.have_api_key():
        print("note: ANTHROPIC_API_KEY not set — running with the MOCK opponent "
              "(offline). Set the key or pass --mock to silence this.\n", file=sys.stderr)

    script = None
    if args.script:
        with open(args.script, encoding="utf-8") as fh:
            script = [ln for ln in fh.read().splitlines() if ln.strip()]

    color = _supports_color(args.no_color)
    try:
        assessment, transcript_path = integration.play_once(
            scenario_path=args.scenario,
            persona_path=args.persona,
            norms_path=args.norms,
            playbook_path=args.playbook,
            progress_path=args.progress,
            learnings_path=args.learnings,
            user=args.user,
            model=args.model,
            mock=args.mock,
            rounds=args.rounds,
            script=script,
            output_dir=args.output,
            color=color,
        )
    except llm.LLMUnavailable as exc:
        print(f"\nerror: the live opponent could not reach the model ({exc}).\n"
              f"Check ANTHROPIC_API_KEY and --model, or run with --mock to play offline.",
              file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    block = assessment["adaptive_difficulty"]
    nxt = block["new_difficulty"]
    print(f"\n  Next round: difficulty {nxt}/10 ({block['tier']}). "
          f"Run `python3 legora.py play` again to try to beat {assessment['overall_score']}.\n")
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def cmd_status(args) -> int:
    if not os.path.exists(args.progress):
        print("No progress yet. Run `python3 legora.py play --mock` to get started.")
        return 0
    progress = diff_mod.load_progress(args.progress, args.user)
    u = progress["users"][args.user]
    sessions = u.get("sessions", [])
    cur = u.get("current_difficulty", diff_mod.DEFAULT_START)

    print(f"\n  LEGORA — progress for '{args.user}'")
    print(f"  {'─'*52}")
    print(f"  Current difficulty : {cur}/10 ({diff_mod.tier_for(cur)})")
    print(f"  Streak             : {u.get('streak', 0)}")
    print(f"  Best score         : {u.get('best_score', 0)}")
    print(f"  Rounds played      : {len(sessions)}")
    if sessions:
        print(f"\n  {'#':>2}  {'score':>5}  {'grade':>5}  {'difficulty':<12} scenario")
        for i, s in enumerate(sessions[-10:], 1):
            d = f"{s.get('difficulty_in')}→{s.get('difficulty_out')}"
            print(f"  {i:>2}  {s.get('overall_score',0):>5}  {str(s.get('grade','')):>5}  "
                  f"{d:<12} {s.get('scenario','')[:30]}")

    if args.learnings and os.path.exists(args.learnings):
        import json
        with open(args.learnings, encoding="utf-8") as fh:
            data = json.load(fh)
        weaknesses = data.get("aggregate_patterns", {}).get("recurring_weaknesses", [])
        if weaknesses:
            print(f"\n  Opponent is targeting {len(weaknesses)} recurring weakness(es):")
            for w in sorted(weaknesses, key=lambda x: -x.get("count", 1)):
                print(f"    [{w.get('severity','?')}] {w.get('pattern','?')} "
                      f"(seen {w.get('count',1)}x)")
    print()
    return 0


# --------------------------------------------------------------------------- #
# simulate
# --------------------------------------------------------------------------- #
def cmd_simulate(args) -> int:
    import simulate_progress
    simulate_progress.main()
    return 0


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="legora", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("play", help="Run a negotiation round, then score + adapt.")
    pp.add_argument("--scenario", default=os.path.join(NEGOTIATOR_INPUTS, "scenario.json"))
    pp.add_argument("--persona", default=os.path.join(NEGOTIATOR_INPUTS, "persona.json"))
    pp.add_argument("--norms", default=os.path.join(NEGOTIATOR_INPUTS, "company_norms.json"))
    pp.add_argument("--playbook", default=DEFAULT_PLAYBOOK)
    pp.add_argument("--progress", default="progress.json")
    pp.add_argument("--learnings", default="learnings.json",
                    help="Past-learnings file; written each round and fed back to the opponent.")
    pp.add_argument("--user", default="default")
    pp.add_argument("--model", default=llm.DEFAULT_MODEL)
    pp.add_argument("--rounds", type=int, default=None, help="Override the round budget.")
    pp.add_argument("--mock", action="store_true", help="Use the canned opponent (no API key).")
    pp.add_argument("--script", default=None,
                    help="Text file of your turns (one per line) for a non-interactive run.")
    pp.add_argument("--output", default=".", help="Directory for the transcript JSON.")
    pp.add_argument("--no-color", action="store_true")
    pp.set_defaults(func=cmd_play)

    ps = sub.add_parser("status", help="Show progress, streak and targeted weaknesses.")
    ps.add_argument("--progress", default="progress.json")
    ps.add_argument("--learnings", default="learnings.json")
    ps.add_argument("--user", default="default")
    ps.set_defaults(func=cmd_status)

    sim = sub.add_parser("simulate", help="Demo the adaptive-difficulty curve.")
    sim.set_defaults(func=cmd_simulate)

    # 'assess' forwards everything after it to the standalone assessor CLI.
    sub.add_parser("assess", add_help=False,
                   help="Score an existing transcript (assessor CLI; use -h after it for options).")
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "assess":
        from assessor.cli import main as assess_main
        return assess_main(argv[1:])
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
