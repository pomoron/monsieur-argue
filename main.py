"""
main.py
-------
CLI entry point for the negotiation training tool.

Usage:
    python main.py [options]

Core options:
    --config    PATH    Path to config.json              (default: config.json)
    --norms     PATH    Path to company_norms.json       (default: inputs/company_norms.json)
    --scenario  PATH    Path to scenario.json            (default: inputs/scenario.json)
    --persona   PATH    Path to persona.json             (default: inputs/persona.json)
    --rounds    INT     Maximum number of rounds         (default: from config.json)
    --output    PATH    Directory for summary JSON       (default: current directory)

Optional enrichment:
    --contract  PATH    Contract PDF — parsed and merged into scenario/persona
    --ai-side   STR     Which contract party the AI plays: PARTY_A or PARTY_B
                        (default: PARTY_B = Seller)
    --learnings PATH    past_learnings.json — sharpens AI tactics against trainee weaknesses

Voice mode:
    --voice             Enable voice I/O (mic input + spoken AI responses via Gemini)
                        Requires:  pip install sounddevice soundfile numpy
                        Config:    config.json → "voice" → tts_model, voice_name
                        Press Enter to stop recording each turn.
                        Type 'stop' as normal to end the session,
                        or speak nothing and press Enter — the tool will re-prompt.

In-session commands (type at the prompt):
    stop        — terminate and generate summary
    help        — show in-session commands
"""

import argparse
import json
import os
import sys
import textwrap

from engine import NegotiationEngine


# ── Console colours ────────────────────────────────────────────────────────────
BOLD   = "\033[1m"
RESET  = "\033[0m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
CYAN   = "\033[36m"
DIM    = "\033[2m"


def _wrap(text: str, width: int = 70) -> str:
    lines = []
    for para in text.split("\n"):
        if para.strip():
            lines.append(textwrap.fill(para, width=width))
        else:
            lines.append("")
    return "\n".join(lines)


def print_ai(name, text, round_num, max_rounds):
    print(f"\n{'─' * 70}")
    print(f"{CYAN}{BOLD}[Round {round_num}/{max_rounds}] {name}:{RESET}")
    print(_wrap(text))
    print(f"{'─' * 70}")


def print_opening(name, text):
    print(f"\n{'─' * 70}")
    print(f"{CYAN}{BOLD}[Opening Statement] {name}:{RESET}")
    print(_wrap(text))
    print(f"{'─' * 70}")


def print_system(msg, colour=YELLOW):
    print(f"\n{colour}{BOLD}[SYSTEM]{RESET} {colour}{msg}{RESET}\n")


def confirm(prompt: str) -> bool:
    while True:
        ans = input(f"{YELLOW}{prompt} [y/n]: {RESET}").strip().lower()
        if ans in ("y", "yes"): return True
        if ans in ("n", "no"):  return False


def load_json(path: str, label: str) -> dict:
    if not os.path.exists(path):
        print(f"{RED}Error: {label} not found at '{path}'{RESET}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Negotiation training chatbot — AI plays the opposing counsel."
    )
    # Core
    parser.add_argument("--config",    default="config.json",               help="Path to config.json")
    parser.add_argument("--norms",     default="inputs/company_norms.json", help="Path to company_norms.json")
    parser.add_argument("--scenario",  default="inputs/scenario.json",      help="Path to scenario.json")
    parser.add_argument("--persona",   default="inputs/persona.json",       help="Path to persona.json")
    parser.add_argument("--rounds",    type=int, default=None,              help="Max rounds")
    parser.add_argument("--output",    default=".",                         help="Directory for summary JSON")
    # Optional enrichment
    parser.add_argument("--voice",              action="store_true", help="Enable voice I/O (mic + Gemini TTS)")
    parser.add_argument("--list-audio-devices", action="store_true", help="Print available PortAudio devices and exit")
    parser.add_argument("--contract",  default=None,                        help="Contract PDF to parse")
    parser.add_argument(
        "--ai-side", default=None, choices=["PARTY_A", "PARTY_B"],
        help=(
            "Override which contract party the AI plays. "
            "If omitted, inferred automatically from persona['represents'] "
            "and scenario['your_side']."
        ),
    )
    parser.add_argument("--learnings", default=None,                        help="Path to past_learnings.json")
    args = parser.parse_args()

    # ── --list-audio-devices: diagnose PortAudio and exit ─────────────────────
    if args.list_audio_devices:
        from voice_io import list_audio_devices
        list_audio_devices()
        sys.exit(0)

    # ── Voice mode: check dependencies early ──────────────────────────────────
    if args.voice:
        try:
            from voice_io import check_voice_deps
            check_voice_deps()
        except RuntimeError as e:
            print(f"{RED}Voice mode unavailable: {e}{RESET}")
            sys.exit(1)

    # ── Load core inputs ───────────────────────────────────────────────────────
    config        = load_json(args.config,   "config.json")
    company_norms = load_json(args.norms,    "company_norms.json")
    scenario      = load_json(args.scenario, "scenario.json")
    persona       = load_json(args.persona,  "persona.json")

    max_rounds = args.rounds or config["negotiation"]["default_max_rounds"]
    os.makedirs(args.output, exist_ok=True)

    contract_title = None
    learnings_used = False

    # ── Optional: contract parser ──────────────────────────────────────────────
    if args.contract:
        print_system(f"Contract PDF detected: {args.contract}", CYAN)
        try:
            from contract_parser import parse_contract, augment_inputs
            contract_data     = parse_contract(config, args.contract, output_dir=args.output)
            scenario, persona = augment_inputs(
                contract_data, scenario, persona, ai_side=args.ai_side
            )  # ai_side=None triggers auto-inference from persona["represents"]
            contract_title    = contract_data.get("contract_title")
            print_system(
                f"Contract parsed: {len(contract_data.get('agreed_terms', []))} agreed terms, "
                f"{len(contract_data.get('contested_terms', []))} contested terms added.",
                GREEN,
            )
        except Exception as e:
            print_system(f"Contract parsing failed: {e}\nProceeding without contract data.", YELLOW)

    # ── Optional: past learnings ───────────────────────────────────────────────
    if args.learnings:
        print_system(f"Past learnings detected: {args.learnings}", CYAN)
        try:
            from learnings_loader import augment_persona_with_learnings_from_path, print_learnings_summary
            persona, learnings = augment_persona_with_learnings_from_path(args.learnings, persona)
            print_learnings_summary(learnings)
            learnings_used = True
            n_weak = len(learnings.get("aggregate_patterns", {}).get("recurring_weaknesses", []))
            print_system(
                f"Loaded {len(learnings.get('sessions', []))} past session(s). "
                f"{n_weak} weakness pattern(s) injected into persona.",
                GREEN,
            )
        except Exception as e:
            print_system(f"Learnings loading failed: {e}\nProceeding without past learnings.", YELLOW)

    # ── Banner ─────────────────────────────────────────────────────────────────
    print(f"\n{'═' * 70}")
    print(f"  {BOLD}NEGOTIATION TRAINING — {scenario['title']}{RESET}")
    print(f"{'═' * 70}")
    print(f"  You are:      {scenario['user_side']}")
    print(f"  Opposing:     {persona['name']} ({scenario['your_side']})")
    print(f"  Max rounds:   {max_rounds}")
    print(f"  Provider:     {config['api_provider'].upper()}")
    print(f"  Contract:     {contract_title or 'None'}")
    print(f"  Learnings:    {'Yes — AI tactics sharpened' if learnings_used else 'None'}")
    if args.voice:
        voice_name = config.get("voice", {}).get("voice_name", "Charon")
        print(f"  Voice mode:   ON  (voice: {voice_name} — press Enter to stop recording)")
    print(f"\n  Type 'stop' at any time to end the session.")
    print(f"{'═' * 70}\n")
    input("Press ENTER to begin...")

    # ── Initialise engine ──────────────────────────────────────────────────────
    engine = NegotiationEngine(
        config=config,
        company_norms=company_norms,
        scenario=scenario,
        persona=persona,
        max_rounds=max_rounds,
        output_dir=args.output,
        contract_title=contract_title,
        learnings_used=learnings_used,
    )

    # ── Opening statement ──────────────────────────────────────────────────────
    print_system("Generating opening statement...", CYAN)
    opening = engine.opening_statement()
    print_opening(persona["name"], opening)
    if args.voice:
        from voice_io import speak_ai_response
        speak_ai_response(config, opening)

    # ── Round loop ─────────────────────────────────────────────────────────────
    termination_reason = None
    termination_detail = None

    while True:
        try:
            if args.voice:
                from voice_io import record_user_input, speak_ai_response
                print(f"\n{BOLD}You:{RESET} ", end="", flush=True)
                user_input = record_user_input(config)
                if not user_input:
                    print_system("No speech detected — please try again.", YELLOW)
                    continue
                # Echo the transcript so the user can confirm what was heard
                print(f"{DIM}  [Heard: \"{user_input}\"]{RESET}")
            else:
                user_input = input(f"\n{BOLD}You:{RESET} ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            termination_reason = "USER_STOP"
            termination_detail = "KeyboardInterrupt"
            break

        if not user_input:
            continue

        if user_input.lower() == "stop":
            print_system("You have chosen to end the session.")
            termination_reason = "USER_STOP"
            termination_detail = "User typed 'stop'"
            break

        if user_input.lower() == "help":
            print(f"\n  Commands: stop | help    Round {engine.current_round + 1}/{max_rounds}\n")
            continue

        print_system("Thinking...", DIM)
        try:
            result = engine.process_turn(user_input)
        except Exception as e:
            print_system(f"API error: {e}", RED)
            if confirm("Retry this turn?"):
                engine.current_round -= 1
                continue
            termination_reason = "USER_STOP"
            termination_detail = f"Ended after API error: {e}"
            break

        print_ai(persona["name"], result["ai_response"], result["round"], max_rounds)
        if args.voice:
            from voice_io import speak_ai_response
            speak_ai_response(config, result["ai_response"])

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
                print_system("Continuing at your request.", GREEN)

        elif result["status"] == "max_rounds":
            print_system(f"Maximum rounds ({max_rounds}) reached. Session ending.", YELLOW)
            termination_reason = "MAX_ROUNDS"
            termination_detail = f"Completed {max_rounds} rounds"
            break

    # ── Summary ────────────────────────────────────────────────────────────────
    print_system("Generating session summary — please wait...", CYAN)
    try:
        filepath = engine.finalise(termination_reason, termination_detail)
        print(f"\n{GREEN}{BOLD}Summary saved:{RESET} {filepath}\n")
    except Exception as e:
        print_system(f"Failed to generate summary: {e}", RED)
        print(json.dumps(engine.dialogue, indent=2))


if __name__ == "__main__":
    main()
