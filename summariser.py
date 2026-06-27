"""
summariser.py
-------------
Generates the end-of-session JSON summary by running an LLM extraction
pass over the full dialogue history.

Output schema (intended for the downstream evaluation agent):
{
  "session_id": str,
  "termination": {
    "reason": "MAX_ROUNDS" | "USER_STOP" | "KILL_SWITCH" | "WHITE_FLAG",
    "detail": str | null
  },
  "rounds_completed": int,
  "dialogue": [
    {"round": int, "role": "USER" | "AI", "content": str}
  ],
  "kill_switch_events": [
    {
      "round": int,
      "condition": str,
      "party": str,
      "reason": str,
      "verbatim_quote": str | null
    }
  ],
  "agreements": [
    {"issue": str, "agreed_term": str}
  ],
  "outstanding_issues": [
    {"issue": str, "user_position": str, "ai_position": str, "notes": str | null}
  ],
  "metadata": {
    "scenario_title": str,
    "ai_persona": str,
    "user_side": str,
    "ai_side": str,
    "max_rounds": int
  }
}
"""

import json
import uuid
from llm_client import call_llm


_SUMMARISER_SYSTEM = """\
You are a legal analyst reviewing a completed negotiation transcript.
Your job is to extract structured information from the dialogue.

Return ONLY valid JSON matching this schema exactly:
{
  "agreements": [
    {"issue": "<issue name>", "agreed_term": "<what was agreed>"}
  ],
  "outstanding_issues": [
    {
      "issue": "<issue name>",
      "user_position": "<buyer/user's last stated position>",
      "ai_position": "<seller/AI's last stated position>",
      "notes": "<any relevant context or why it remains contested, or null>"
    }
  ]
}

Rules:
- Only include an item in "agreements" if BOTH parties explicitly accepted a term.
  A position stated by one side is NOT an agreement.
- "outstanding_issues" should capture every contested point that was NOT resolved.
- Be specific — quote numbers, percentages, timeframes where mentioned.
- If a point was never discussed, include it in outstanding_issues with
  user_position and ai_position reflecting the original scenario positions.
"""


def generate_summary(
    config: dict,
    scenario: dict,
    persona: dict,
    dialogue: list[dict],
    kill_switch_events: list[dict],
    termination_reason: str,
    termination_detail: str | None,
    max_rounds: int,
) -> dict:
    """
    Run the LLM extraction and assemble the full summary dict.

    Args:
        config: full config dict
        scenario: loaded scenario.json
        persona: loaded persona.json
        dialogue: list of {"round": int, "role": str, "content": str}
        kill_switch_events: list of kill-switch records accumulated during session
        termination_reason: one of MAX_ROUNDS | USER_STOP | KILL_SWITCH | WHITE_FLAG
        termination_detail: optional string explaining termination
        max_rounds: the round limit that was set

    Returns:
        Full summary dict ready to be serialised to JSON
    """
    # Build a readable transcript for the LLM
    transcript_lines = []
    for entry in dialogue:
        label = "USER (Buyer)" if entry["role"] == "USER" else "AI (Seller)"
        transcript_lines.append(f"[Round {entry['round']}] {label}:\n{entry['content']}")
    transcript = "\n\n".join(transcript_lines)

    # Include original contested points so the LLM knows what to look for
    contested_summary = json.dumps(scenario.get("contested_points", []), indent=2)

    user_content = f"""\
Original contested issues:
{contested_summary}

Full negotiation transcript:
{transcript}

Extract agreements and outstanding issues. Return JSON only.
"""

    raw = call_llm(
        config=config,
        system_prompt=_SUMMARISER_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
        temperature=0.1,
        max_tokens=2000,
    )

    try:
        extracted = json.loads(raw.strip())
    except json.JSONDecodeError:
        extracted = {
            "agreements": [],
            "outstanding_issues": [
                {
                    "issue": "PARSE_ERROR",
                    "user_position": "N/A",
                    "ai_position": "N/A",
                    "notes": f"Summariser returned unparseable output: {raw[:300]}",
                }
            ],
        }

    rounds_completed = max(
        (e["round"] for e in dialogue), default=0
    )

    summary = {
        "session_id": str(uuid.uuid4()),
        "termination": {
            "reason": termination_reason,
            "detail": termination_detail,
        },
        "rounds_completed": rounds_completed,
        "dialogue": dialogue,
        "kill_switch_events": kill_switch_events,
        "agreements": extracted.get("agreements", []),
        "outstanding_issues": extracted.get("outstanding_issues", []),
        "metadata": {
            "scenario_title": scenario.get("title", ""),
            "ai_persona": persona.get("name", ""),
            "user_side": scenario.get("user_side", ""),
            "ai_side": scenario.get("your_side", ""),
            "max_rounds": max_rounds,
        },
    }

    return summary
