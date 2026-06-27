"""
monitor.py
----------
Secondary LLM evaluation pass that runs after every exchange.
Checks for:
  1. Kill-switch conditions: breach of conduct or dishonesty by either party
  2. White-flag signal: the AI has embedded [WHITE_FLAG] in its response,
     indicating it judges the negotiation to be at an irreconcilable impasse.

Returns structured dicts so the engine can act without parsing free text.
"""

import json
import re
from typing import Optional
from llm_client import call_llm


# ── White-flag marker ────────────────────────────────────────────────────────

WHITE_FLAG_PATTERN = re.compile(
    r"\[WHITE_FLAG(?::\s*([^\]]*))?\]", re.IGNORECASE
)


def extract_white_flag(text: str) -> tuple[str, Optional[str]]:
    """
    Strip any [WHITE_FLAG: <reason>] marker from the AI's message.
    Returns (clean_text, reason_or_None).
    """
    match = WHITE_FLAG_PATTERN.search(text)
    if match:
        reason = (match.group(1) or "").strip() or "Irreconcilable impasse."
        clean = WHITE_FLAG_PATTERN.sub("", text).strip()
        return clean, reason
    return text, None


# ── Kill-switch monitor ──────────────────────────────────────────────────────

_MONITOR_SYSTEM = """\
You are a neutral compliance monitor for a legal negotiation training exercise.
Your job is to evaluate the latest exchange and decide whether a kill-switch
condition has been met.

Kill-switch conditions:
1. BREACH_OF_CONDUCT — a party has engaged in prohibited behaviour (personal
   attacks, threats of litigation used as a tactic, sharing privileged
   information, discriminatory remarks, misrepresentation of authority).
2. DISHONESTY — a party has made a factually false statement of fact
   (not opinion, not negotiating position, not bluff) in a way that
   constitutes material misrepresentation.

Do NOT flag:
- Hard bargaining, aggressive positions, or tough language
- Bluffing or positional statements ("my client won't accept less than X")
- Legitimate pressure tactics

Respond ONLY with valid JSON in this exact schema:
{
  "triggered": true | false,
  "condition": "BREACH_OF_CONDUCT" | "DISHONESTY" | null,
  "party": "AI" | "USER" | null,
  "reason": "<one sentence explanation>" | null,
  "verbatim_quote": "<the exact phrase that triggered this>" | null
}
"""


def check_kill_switch(
    config: dict,
    company_norms: dict,
    user_message: str,
    ai_message: str,
) -> dict:
    """
    Run the kill-switch monitor against the latest exchange.

    Args:
        config: full config dict (for API settings)
        company_norms: loaded company_norms.json (for prohibited behaviours)
        user_message: the human's last message
        ai_message: the AI's last response (already stripped of WHITE_FLAG)

    Returns:
        dict with keys: triggered, condition, party, reason, verbatim_quote
    """
    prohibited = json.dumps(company_norms.get("prohibited_behaviours", []), indent=2)

    user_content = f"""\
Prohibited behaviours for this session:
{prohibited}

Latest exchange:
USER: {user_message}
AI: {ai_message}

Evaluate and return JSON only.
"""

    raw = call_llm(
        config=config,
        system_prompt=_MONITOR_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
        temperature=config["negotiation"]["monitor_temperature"],
        max_tokens=300,
    )

    try:
        result = json.loads(raw.strip())
    except json.JSONDecodeError:
        # Fail safe: if monitor returns garbage, don't block the session
        result = {
            "triggered": False,
            "condition": None,
            "party": None,
            "reason": f"Monitor parse error. Raw: {raw[:200]}",
            "verbatim_quote": None,
        }

    return result
