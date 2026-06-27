"""Command-line entry point for the Legora negotiation assessor."""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__, llm
from .debrief import render_card, supports_color
from .difficulty import load_progress, save_progress, update_difficulty
from .schemas import load_inputs
from .scoring import assess


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="assess",
        description="Legora negotiation assessor — grade a training round, coach the lawyer, "
                    "and compute adaptive difficulty for the next round.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-t", "--transcript", required=True, help="Path to the session transcript JSON.")
    p.add_argument("-s", "--scenario", required=True, help="Path to the scenario JSON.")
    p.add_argument("-p", "--playbook", default="playbook.json", help="Path to the playbook JSON.")
    p.add_argument("--progress", default="progress.json",
                   help="Path to the persisted progress file (created if absent). Use '' to disable.")
    p.add_argument("--user", default="default", help="User id key within the progress file.")
    p.add_argument("--offline", action="store_true",
                   help="Force the deterministic heuristic scorer (no API call).")
    p.add_argument("--model", default=llm.DEFAULT_MODEL, help="Claude model for LLM scoring.")
    p.add_argument("-o", "--json-out", default=None, help="Write the full assessment JSON to this path.")
    p.add_argument("--print-json", action="store_true", help="Print the assessment JSON to stdout.")
    p.add_argument("--no-card", action="store_true", help="Suppress the human-readable debrief card.")
    p.add_argument("--no-color", action="store_true", help="Disable ANSI colour in the card.")
    p.add_argument("--start-difficulty", type=int, default=None,
                   help="Override the starting difficulty for a brand-new user (1-10).")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def run(args: argparse.Namespace) -> dict:
    transcript, scenario, playbook = load_inputs(args.transcript, args.scenario, args.playbook)

    assessment = assess(
        transcript, scenario, playbook,
        offline=args.offline, model=args.model,
    )

    progress = load_progress(args.progress, args.user)
    if args.start_difficulty is not None and not progress["users"][args.user]["sessions"]:
        progress["users"][args.user]["current_difficulty"] = max(1, min(10, args.start_difficulty))

    difficulty = update_difficulty(
        progress, assessment, scenario.title, args.user,
        prev_rounds=int(transcript.metadata.get("max_rounds", transcript.rounds_completed) or 10),
    )
    assessment["adaptive_difficulty"] = difficulty
    assessment["session_id"] = transcript.session_id
    assessment["scenario_title"] = scenario.title
    assessment["scenario_user_side"] = scenario.user_side

    if args.progress:
        save_progress(args.progress, progress)

    return assessment


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        assessment = run(args)
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(assessment, fh, indent=2)

    if not args.no_card:
        color = (not args.no_color) and supports_color()
        print(render_card(assessment, assessment["adaptive_difficulty"], _scenario_stub(assessment), color=color))

    if args.print_json or (args.no_card and not args.json_out):
        print(json.dumps(assessment, indent=2))

    return 0


def _scenario_stub(assessment: dict):
    """Lightweight object carrying just what the card needs (title, user_side)."""
    from .schemas import Scenario
    return Scenario(
        title=assessment.get("scenario_title", "Negotiation"),
        background="",
        agreed_points=[],
        contested_points=[],
        your_side="",
        user_side=assessment.get("scenario_user_side", ""),
    )


if __name__ == "__main__":
    raise SystemExit(main())
