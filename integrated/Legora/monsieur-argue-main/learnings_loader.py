"""
learnings_loader.py
-------------------
Optional pre-session agent: reads a past_learnings.json file, extracts the
trainee's recurring weaknesses, then injects tactical awareness into the
persona so the AI targets those specific weak spots.

Standalone usage:
    python learnings_loader.py --learnings inputs/past_learnings.json [--save]

Module usage:
    from learnings_loader import augment_persona_with_learnings_from_path
    persona, learnings = augment_persona_with_learnings_from_path(path, persona)
"""

import argparse
import json
import os
import sys


# ── Loader ─────────────────────────────────────────────────────────────────────

def load_learnings(learnings_path: str) -> dict:
    """Load and validate a past_learnings.json file."""
    if not os.path.exists(learnings_path):
        raise FileNotFoundError(f"past_learnings.json not found: {learnings_path}")

    with open(learnings_path, encoding="utf-8") as f:
        data = json.load(f)

    if "sessions" not in data and "aggregate_patterns" not in data:
        raise ValueError(
            "past_learnings.json must contain 'sessions' and/or 'aggregate_patterns'."
        )
    return data


# ── Augmentation ───────────────────────────────────────────────────────────────

def _summarise_session_mistakes(sessions: list) -> list:
    """
    Derive weakness patterns bottom-up from session data when
    aggregate_patterns is absent.
    """
    seen = set()
    result = []
    for session in sessions:
        for mistake in session.get("analyst_notes", {}).get("mistakes", []):
            pattern = mistake.get("pattern", "")
            if pattern in seen:
                continue
            seen.add(pattern)
            result.append({
                "pattern":     pattern,
                "description": mistake.get("mistake", ""),
                "exploit_instruction": (
                    f"The trainee has repeatedly shown this pattern: {mistake.get('mistake', '')}. "
                    f"Apply the same stimulus that triggered it before."
                ),
                "severity": mistake.get("severity", "MEDIUM"),
            })
    return result


def augment_persona_with_learnings(learnings: dict, persona: dict) -> dict:
    """
    Inject tactical awareness from past learnings into the persona.

    Adds a "tactical_awareness" block containing trainee weaknesses
    (with exploit instructions) and strengths.
    Also surfaces HIGH/MEDIUM severity exploit instructions into
    persona["wants"] so they appear in the primary system prompt.

    Returns augmented copy; does not mutate the original.
    """
    persona  = json.loads(json.dumps(persona))

    sessions             = learnings.get("sessions", [])
    aggregate            = learnings.get("aggregate_patterns", {})
    recurring_weaknesses = aggregate.get("recurring_weaknesses", [])
    recurring_strengths  = aggregate.get("recurring_strengths", [])

    if not recurring_weaknesses and sessions:
        recurring_weaknesses = _summarise_session_mistakes(sessions)

    persona["tactical_awareness"] = {
        "session_count":      len(sessions),
        "trainee_id":         learnings.get("trainee_id", "unknown"),
        "trainee_weaknesses": recurring_weaknesses,
        "trainee_strengths":  recurring_strengths,
        "usage_note": (
            "Use trainee_weaknesses to guide your tactics. "
            "Do not telegraph that you are targeting these patterns. "
            "Trainee_strengths are areas where they are prepared — "
            "spend less time there and pivot to weaker ground."
        ),
    }

    # Surface high/medium severity exploits into wants for the system prompt
    for weakness in recurring_weaknesses:
        if weakness.get("severity") in ("HIGH", "MEDIUM"):
            entry = (
                f"[Learned tactic — {weakness['pattern']}] "
                f"{weakness.get('exploit_instruction', '')}"
            )
            if entry not in persona.get("wants", []):
                persona.setdefault("wants", []).append(entry)

    return persona


def augment_persona_with_learnings_from_path(learnings_path: str, persona: dict):
    """
    Convenience: load file and augment in one call.
    Returns (augmented_persona, learnings_dict).
    """
    learnings = load_learnings(learnings_path)
    return augment_persona_with_learnings(learnings, persona), learnings


# ── Summary printer ────────────────────────────────────────────────────────────

def print_learnings_summary(learnings: dict) -> None:
    sessions   = learnings.get("sessions", [])
    weaknesses = learnings.get("aggregate_patterns", {}).get("recurring_weaknesses", [])
    print(f"\n── Past Learnings Summary ──────────────────────────────")
    print(f"  Trainee:  {learnings.get('trainee_id', 'unknown')}")
    print(f"  Sessions: {len(sessions)}")
    if sessions:
        print(f"  Dates:    {', '.join(s.get('date', '?') for s in sessions)}")
    print(f"\n  Recurring weaknesses ({len(weaknesses)}):")
    for w in weaknesses:
        print(f"    [{w.get('severity','?')}] {w.get('pattern','?')}: {w.get('description','')[:80]}...")
    print(f"────────────────────────────────────────────────────────\n")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Load past learnings and augment persona with tactical awareness."
    )
    parser.add_argument("--learnings", required=True,                 help="Path to past_learnings.json")
    parser.add_argument("--persona",   default="inputs/persona.json", help="Path to persona.json")
    parser.add_argument("--save",      action="store_true",           help="Overwrite persona.json in place")
    args = parser.parse_args()

    if not os.path.exists(args.persona):
        print(f"Error: persona.json not found at '{args.persona}'"); sys.exit(1)

    with open(args.persona, encoding="utf-8") as f:
        persona = json.load(f)

    learnings = load_learnings(args.learnings)
    print_learnings_summary(learnings)
    new_persona = augment_persona_with_learnings(learnings, persona)

    if args.save:
        with open(args.persona, "w", encoding="utf-8") as f:
            json.dump(new_persona, f, indent=2)
        print(f"[learnings_loader] persona.json updated: {args.persona}")
    else:
        print("── Injected tactical_awareness ──")
        print(json.dumps(new_persona.get("tactical_awareness", {}), indent=2))
        learned = [w for w in new_persona.get("wants", []) if w.startswith("[Learned")]
        if learned:
            print("\n── Injected into wants ──")
            for w in learned:
                print(f"  {w[:100]}...")
        print("\nRun with --save to write back to persona.json.")

if __name__ == "__main__":
    main()
