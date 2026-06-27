"""
main.py
-------
CLI entry point for the negotiation training tool.

Usage:
    python main.py [options]

Options:
    --config    PATH    Path to config.json           (default: config.json)
    --norms     PATH    Path to company_norms.json    (default: inputs/company_norms.json)
    --scenario  PATH    Path to scenario.json         (default: inputs/scenario.json)
    --persona   PATH    Path to persona.json          (default: inputs/persona.json)
    --rounds    INT     Maximum number of rounds      (default: from config.json)
    --output    PATH    Directory for summary JSON    (default: current directory)

In-session commands (type at the prompt):
    stop        — terminate immediately and generate summary
    help        — show in-session commands
"""

import argparse
import json
import os
import sys
import textwrap

from engine import NegotiationEngine


# ── Helpers ───────────────────────────────────────────────────────────────────

DIVIDER = "─" * 70
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def load_json(path: str, label: str) -> dict:
    if not os.path.exists(path):
        print(f"{RED}Error: {label} not found at '{path}'{RESET}")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def print_ai(name: str, text: str, round_num: int, max_rounds: int) -> None:
    print(f"\n{DIVIDER}")
    print(f"{CYAN}{BOLD}[Round {round_num}/{max_rounds}] {name}:{RESET}")
    # Wrap long lines for readability
    for para in text.split("\n"):
        if para.strip():
            print(textwrap.fill(para, width=70))
        else:
            print()
    print(f"{DIVIDER}")


def print_opening(name: str, text: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"{CYAN}{BOLD}[Opening] {name}:{RESET}")
    for para in text.split("\n"):
        if para.strip():
            print(textwrap.fill(para, width=70))
        else:
            print()
    print(f"{DIVIDER}")


def print_system(msg: str, colour: str = YELLOW) -> None:
    print(f"\n{colour}{BOLD}[SYSTEM]{RESET} {colour}{msg}{RESET}\n")


def confirm(prompt: str) -> bool:
    while True:
        ans = input(f"{YELLOW}{prompt} [y/n]: {RESET}").strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Negotiation training chatbot — AI plays the opposing counsel."
    )
    parser.add_argument("--config",   default="config.json",                help="Path to config.json")
    parser.add_argument("--norms",    default="inputs/company_norms.json",  help="Path to company_norms.json")
    parser.add_argument("--scenario", default="inputs/scenario.json",       help="Path to scenario.json")
    parser.add_argument("--persona",  default="inputs/persona.json",        help="Path to persona.json")
    parser.add_argument("--rounds",   type=int, default=None,               help="Max rounds (overrides config.json)")
    parser.add_argument("--output",   default=".",                          help="Directory for summary JSON output")
    args = parser.parse_args()

    # ── Load inputs ──────────────────────────────────────────────────────────
    config       = load_json(args.config,   "config.json")
    company_norms = load_json(args.norms,   "company_norms.json")
    scenario     = load_json(args.scenario, "scenario.json")
    persona      = load_json(args.persona,  "persona.json")

    max_rounds = args.rounds or config["negotiation"]["default_max_rounds"]

    os.makedirs(args.output, exist_ok=True)

    # ── Intro banner ─────────────────────────────────────────────────────────
    print(f"\n{'═' * 70}")
    print(f"  {BOLD}NEGOTIATION TRAINING — {scenario['title']}{RESET}")
    print(f"{'═' * 70}")
    print(f"  You are:    {scenario['user_side']}")
    print(f"  Opposing:   {persona['name']} ({scenario['your_side']})")
    print(f"  Max rounds: {max_rounds}")
    print(f"  Provider:   {config['api_provider'].upper()}")
    print(f"\n  Type '{BOLD}stop{RESET}' to end the session at any time.")
    print(f"  Type '{BOLD}help{RESET}' for in-session commands.")
    print(f"{'═' * 70}\n")

    input("Press ENTER to begin...")

    # ── Initialise engine ────────────────────────────────────────────────────
    engine = NegotiationEngine(
        config=config,
        company_norms=company_norms,
        scenario=scenario,
        persona=persona,
        max_rounds=max_rounds,
        output_dir=args.output,
    )

    # ── Opening statement ────────────────────────────────────────────────────
    print_system("Generating opening statement...", CYAN)
    opening = engine.opening_statement()
    print_opening(persona["name"], opening)

    # ── Round loop ────────────────────────────────────────────────────────────
    termination_reason = None
    termination_detail = None

    while True:
        # User input
        try:
            user_input = input(f"\n{BOLD}You:{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            print_system("Session interrupted.", RED)
            termination_reason = "USER_STOP"
            termination_detail = "KeyboardInterrupt"
            break

        if not user_input:
            continue

        # In-session commands
        if user_input.lower() == "stop":
            print_system("You have chosen to end the session.")
            termination_reason = "USER_STOP"
            termination_detail = "User typed 'stop'"
            break

        if user_input.lower() == "help":
            print(f"""
  {BOLD}In-session commands:{RESET}
    stop    — terminate and generate summary
    help    — show this message
  Round {engine.current_round + 1}/{max_rounds}
""")
            continue

        # Process the turn
        print_system("Thinking...", CYAN)
        try:
            result = engine.process_turn(user_input)
        except Exception as e:
            print_system(f"API error: {e}", RED)
            if confirm("Retry this turn?"):
                continue
            else:
                termination_reason = "USER_STOP"
                termination_detail = f"Session ended after API error: {e}"
                break

        # Display AI response
        print_ai(persona["name"], result["ai_response"], result["round"], max_rounds)

        # ── Status handling ──────────────────────────────────────────────────

        if result["status"] == "kill_switch":
            ev = result["kill_switch_event"]
            print_system(
                f"KILL SWITCH TRIGGERED\n"
                f"  Condition : {ev['condition']}\n"
                f"  Party     : {ev['party']}\n"
                f"  Reason    : {ev['reason']}\n"
                f"  Quote     : \"{ev.get('verbatim_quote', '')}\"\n\n"
                f"  The session is being terminated.",
                RED,
            )
            termination_reason = "KILL_SWITCH"
            termination_detail = f"{ev['condition']} by {ev['party']}: {ev['reason']}"
            break

        elif result["status"] == "white_flag":
            print_system(
                f"⚑  {persona['name']} has raised a WHITE FLAG.\n"
                f"   Reason: {result['white_flag_reason']}\n\n"
                f"   The opposing counsel believes the negotiation cannot proceed.",
                YELLOW,
            )
            if confirm("Do you wish to terminate the session?"):
                termination_reason = "WHITE_FLAG"
                termination_detail = result["white_flag_reason"]
                break
            else:
                print_system(
                    "You have chosen to continue. The opposing counsel will proceed.",
                    GREEN,
                )
                # Continue — the engine carries on normally

        elif result["status"] == "max_rounds":
            print_system(
                f"Maximum rounds ({max_rounds}) reached. Session ending.",
                YELLOW,
            )
            termination_reason = "MAX_ROUNDS"
            termination_detail = f"Completed {max_rounds} rounds"
            break

        # else: "continue" — loop

    # ── Termination & summary ─────────────────────────────────────────────────
    print_system("Generating session summary — please wait...", CYAN)

    try:
        filepath = engine.finalise(termination_reason, termination_detail)
        print(f"\n{GREEN}{BOLD}Summary saved to:{RESET} {filepath}")
        print(
            f"\nThis JSON file contains the full dialogue, agreements reached,\n"
            f"and outstanding issues — ready for the evaluation agent.\n"
        )
    except Exception as e:
        print_system(f"Failed to generate summary: {e}", RED)
        print("Raw dialogue saved to stdout as fallback:\n")
        print(json.dumps(engine.dialogue, indent=2))


if __name__ == "__main__":
    main()
