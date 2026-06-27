"""
summariser.py
-------------
Generates the end-of-session JSON summary via an LLM extraction pass
over the full dialogue history.

Output schema (for the downstream evaluation agent):
{
  "session_id": str,
  "termination": {"reason": str, "detail": str | null},
  "rounds_completed": int,
  "dialogue": [{"round": int, "role": "USER"|"AI", "content": str}],
  "kill_switch_events": [...],
  "agreements": [{"issue": str, "agreed_term": str}],
  "outstanding_issues": [{"issue": str, "user_position": str, "ai_position": str, "notes": str|null}],
  "metadata": {
    "scenario_title": str, "ai_persona": str,
    "user_side": str, "ai_side": str, "max_rounds": int,
    "contract_used": str|null, "learnings_used": bool
  }
}
"""

import json
import uuid
from llm_client import call_llm


_SUMMARISER_SYSTEM = """\
You are a legal analyst reviewing a completed negotiation transcript.
Extract structured information from the dialogue.

Return ONLY valid JSON matching this schema:
{
  "agreements": [
    {"issue": "<issue name>", "agreed_term": "<what was explicitly agreed by both sides>"}
  ],
  "outstanding_issues": [
    {
      "issue": "<issue name>",
      "user_position": "<buyer/user's last stated position>",
      "ai_position": "<seller/AI's last stated position>",
      "notes": "<context on why it remains contested, or null>"
    }
  ]
}

Rules:
- Only include in "agreements" if BOTH parties explicitly accepted a term.
- Include in "outstanding_issues" every contested point NOT fully resolved.
- Be specific — quote numbers, percentages, timeframes.
- If a point was never discussed, include it with the original scenario positions.
"""


def generate_summary(
    config: dict,
    scenario: dict,
    persona: dict,
    dialogue: list,
    kill_switch_events: list,
    termination_reason: str,
    termination_detail,
    max_rounds: int,
    contract_title=None,
    learnings_used: bool = False,
) -> dict:
    """
    Run the LLM extraction and assemble the full summary dict.
    """
    transcript_lines = []
    for entry in dialogue:
        label = "USER (Buyer)" if entry["role"] == "USER" else "AI (Seller)"
        transcript_lines.append(f"[Round {entry['round']}] {label}:\n{entry['content']}")
    transcript = "\n\n".join(transcript_lines)

    contested_summary = json.dumps(scenario.get("contested_points", []), indent=2)

    raw = call_llm(
        config=config,
        system_prompt=_SUMMARISER_SYSTEM,
        messages=[{"role": "user", "content": (
            f"Original contested issues:\n{contested_summary}\n\n"
            f"Full negotiation transcript:\n{transcript}\n\n"
            f"Extract agreements and outstanding issues. Return JSON only."
        )}],
        temperature=0.1,
        max_tokens=2000,
    )

    try:
        extracted = json.loads(raw.strip())
    except json.JSONDecodeError:
        extracted = {
            "agreements": [],
            "outstanding_issues": [{
                "issue": "PARSE_ERROR",
                "user_position": "N/A",
                "ai_position": "N/A",
                "notes": f"Summariser returned unparseable output: {raw[:300]}",
            }],
        }

    rounds_completed = max((e["round"] for e in dialogue), default=0)

    return {
        "session_id": str(uuid.uuid4()),
        "termination": {"reason": termination_reason, "detail": termination_detail},
        "rounds_completed": rounds_completed,
        "dialogue": dialogue,
        "kill_switch_events": kill_switch_events,
        "agreements": extracted.get("agreements", []),
        "outstanding_issues": extracted.get("outstanding_issues", []),
        "metadata": {
            "scenario_title": scenario.get("title", ""),
            "ai_persona":     persona.get("name", ""),
            "user_side":      scenario.get("user_side", ""),
            "ai_side":        scenario.get("your_side", ""),
            "max_rounds":     max_rounds,
            "contract_used":  contract_title,
            "learnings_used": learnings_used,
        },
    }
