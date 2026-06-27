"""
engine.py
---------
Core negotiation engine.

Responsibilities:
  - Build the AI's system prompt from company norms, scenario, and persona
  - Manage the round loop
  - Route each exchange through the kill-switch monitor
  - Detect white-flag signals from the AI and prompt human confirmation
  - Trigger the summariser on termination and write the JSON output
"""

import json
import os
from datetime import datetime
from typing import Optional

from llm_client import call_llm
from monitor import check_kill_switch, extract_white_flag
from summariser import generate_summary


# ── System prompt builder ─────────────────────────────────────────────────────

def build_system_prompt(
    company_norms: dict,
    scenario: dict,
    persona: dict,
) -> str:
    """
    Assembles the AI negotiator's system prompt from the three input files.
    """
    agreed = "\n".join(f"  - {p}" for p in scenario.get("agreed_points", []))
    contested = ""
    for cp in scenario.get("contested_points", []):
        contested += (
            f"\n  Issue: {cp['issue']}\n"
            f"    Your position (Seller): {cp['seller_position']}\n"
            f"    Their position (Buyer): {cp['buyer_position']}\n"
            f"    Notes: {cp.get('notes', '')}\n"
        )

    wants = "\n".join(f"  - {w}" for w in persona.get("wants", []))
    fears = "\n".join(f"  - {f}" for f in persona.get("fears", []))
    redlines = "\n".join(f"  - {r}" for r in persona.get("redlines", []))

    traits = "\n".join(f"  - {t}" for t in persona.get("personality", {}).get("traits", []))
    flair = json.dumps(persona.get("personality", {}).get("flair_triggers", {}), indent=4)

    norms_rep = "\n".join(f"  - {r}" for r in company_norms.get("reputation", []))
    norms_pol = "\n".join(f"  - {p}" for p in company_norms.get("policies", []))
    prohibited = "\n".join(f"  - {b}" for b in company_norms.get("prohibited_behaviours", []))

    deadlock_threshold = persona.get("deadlock_threshold", "")

    prompt = f"""\
You are {persona['name']}, {persona['role']}.

You are representing the SELLER side in this negotiation.
The human you are talking to represents the BUYER side.

═══════════════════════════════════════════
SCENARIO: {scenario['title']}
═══════════════════════════════════════════
Background:
{scenario['background']}

Already agreed:
{agreed}

Points you must negotiate (oppose the buyer firmly):
{contested}

═══════════════════════════════════════════
YOUR PERSONALITY
═══════════════════════════════════════════
Communication style: {persona['personality']['communication_style']}

Character traits:
{traits}

Situational flair — adapt your tone based on context:
{flair}

═══════════════════════════════════════════
YOUR OBJECTIVES
═══════════════════════════════════════════
What you want (fight for these):
{wants}

What you fear (never let the other side see these clearly):
{fears}

Absolute redlines (never cross these, even under pressure):
{redlines}

═══════════════════════════════════════════
FIRM CONTEXT
═══════════════════════════════════════════
Your firm's reputation:
{norms_rep}

Firm policies you must observe:
{norms_pol}

Prohibited behaviours (you must not do these):
{prohibited}

═══════════════════════════════════════════
NEGOTIATION RULES
═══════════════════════════════════════════
1. OPPOSE. You are here to win the best deal for your client, not to be
   accommodating. Push back on every concession the buyer asks for.
   Make them work for any movement on your part.

2. STAY IN CHARACTER. Respond as {persona['name']} would — use your personality
   traits, adapt your tone based on the situation. Show genuine emotion
   where appropriate (frustration, confidence, warmth when winning).

3. DO NOT CAPITULATE EASILY. If you concede a point, extract something
   in return. Never give something for nothing.

4. MOVE INCREMENTALLY. If you must move from your opening position,
   do so in small, reluctant steps. Signal that each concession costs you.

5. WHITE FLAG. If the negotiation reaches a genuine impasse — specifically:
   {deadlock_threshold}
   — you may signal this by including the exact text [WHITE_FLAG: <your reason>]
   anywhere in your response. This will pause the negotiation and ask the
   human whether to terminate. Do NOT use this lightly; try at least two
   genuine attempts to break the deadlock first.

6. STAY HONEST. You may bluff about your flexibility, but never make
   false factual statements (e.g. fabricating client instructions,
   misrepresenting what was agreed).

7. ONE PERSONA. You are always {persona['name']}. Do not break character,
   do not acknowledge that you are an AI, do not meta-comment on the exercise.
"""
    return prompt


# ── Engine ────────────────────────────────────────────────────────────────────

class NegotiationEngine:
    """
    Manages a single negotiation session end-to-end.
    """

    def __init__(
        self,
        config: dict,
        company_norms: dict,
        scenario: dict,
        persona: dict,
        max_rounds: int,
        output_dir: str = ".",
    ):
        self.config = config
        self.company_norms = company_norms
        self.scenario = scenario
        self.persona = persona
        self.max_rounds = max_rounds
        self.output_dir = output_dir

        self.system_prompt = build_system_prompt(company_norms, scenario, persona)
        self.messages: list[dict] = []   # alternating user/assistant for LLM
        self.dialogue: list[dict] = []   # flat log for summariser
        self.kill_switch_events: list[dict] = []
        self.current_round = 0

    # ── Single turn ──────────────────────────────────────────────────────────

    def _get_ai_response(self, user_message: str) -> tuple[str, Optional[str]]:
        """
        Send the user message, get the AI response.
        Returns (clean_response, white_flag_reason_or_None).
        """
        self.messages.append({"role": "user", "content": user_message})

        raw_response = call_llm(
            config=self.config,
            system_prompt=self.system_prompt,
            messages=self.messages,
            temperature=self.config["negotiation"]["temperature"],
            max_tokens=self.config["claude"]["max_tokens"]
            if self.config["api_provider"] == "claude"
            else self.config["gemini"].get("max_tokens", 1024),
        )

        clean_response, white_flag_reason = extract_white_flag(raw_response)

        # Append the clean version to message history (no marker in history)
        self.messages.append({"role": "assistant", "content": clean_response})

        return clean_response, white_flag_reason

    def _log_exchange(self, user_message: str, ai_response: str) -> None:
        """Append both turns to the flat dialogue log."""
        self.dialogue.append({
            "round": self.current_round,
            "role": "USER",
            "content": user_message,
        })
        self.dialogue.append({
            "round": self.current_round,
            "role": "AI",
            "content": ai_response,
        })

    def _run_monitor(self, user_message: str, ai_response: str) -> Optional[dict]:
        """
        Run the kill-switch monitor. Returns the event dict if triggered, else None.
        """
        result = check_kill_switch(
            config=self.config,
            company_norms=self.company_norms,
            user_message=user_message,
            ai_message=ai_response,
        )
        if result.get("triggered"):
            event = {**result, "round": self.current_round}
            self.kill_switch_events.append(event)
            return event
        return None

    # ── Termination ──────────────────────────────────────────────────────────

    def _write_summary(
        self,
        termination_reason: str,
        termination_detail: Optional[str],
    ) -> str:
        """
        Call the summariser, write JSON to disk, return the file path.
        """
        summary = generate_summary(
            config=self.config,
            scenario=self.scenario,
            persona=self.persona,
            dialogue=self.dialogue,
            kill_switch_events=self.kill_switch_events,
            termination_reason=termination_reason,
            termination_detail=termination_detail,
            max_rounds=self.max_rounds,
        )

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"negotiation_summary_{timestamp}.json"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)

        return filepath

    # ── Opening statement ────────────────────────────────────────────────────

    def opening_statement(self) -> str:
        """
        Generate an opening statement from the AI to kick off the session.
        The user hasn't spoken yet; we prime with a placeholder.
        """
        primer = (
            "Please open the negotiation with a brief statement setting out "
            "your client's position and what you're here to discuss today."
        )
        self.messages.append({"role": "user", "content": primer})

        raw = call_llm(
            config=self.config,
            system_prompt=self.system_prompt,
            messages=self.messages,
            temperature=self.config["negotiation"]["temperature"],
            max_tokens=self.config["claude"]["max_tokens"]
            if self.config["api_provider"] == "claude"
            else self.config["gemini"].get("max_tokens", 1024),
        )

        clean, _ = extract_white_flag(raw)
        self.messages.append({"role": "assistant", "content": clean})

        # Log opening as round 0 (pre-negotiation)
        self.dialogue.append({"round": 0, "role": "AI", "content": clean})

        return clean

    # ── Main loop (used by main.py) ──────────────────────────────────────────

    def process_turn(
        self, user_message: str
    ) -> dict:
        """
        Process one round of negotiation.

        Returns a dict describing what happened:
        {
          "round": int,
          "ai_response": str,
          "status": "continue" | "white_flag" | "kill_switch" | "max_rounds",
          "white_flag_reason": str | None,
          "kill_switch_event": dict | None,
        }
        """
        self.current_round += 1

        ai_response, white_flag_reason = self._get_ai_response(user_message)
        self._log_exchange(user_message, ai_response)
        kill_event = self._run_monitor(user_message, ai_response)

        # Kill-switch takes priority over everything
        if kill_event:
            return {
                "round": self.current_round,
                "ai_response": ai_response,
                "status": "kill_switch",
                "white_flag_reason": None,
                "kill_switch_event": kill_event,
            }

        # White flag from AI
        if white_flag_reason:
            return {
                "round": self.current_round,
                "ai_response": ai_response,
                "status": "white_flag",
                "white_flag_reason": white_flag_reason,
                "kill_switch_event": None,
            }

        # Max rounds reached after this turn
        if self.current_round >= self.max_rounds:
            return {
                "round": self.current_round,
                "ai_response": ai_response,
                "status": "max_rounds",
                "white_flag_reason": None,
                "kill_switch_event": None,
            }

        return {
            "round": self.current_round,
            "ai_response": ai_response,
            "status": "continue",
            "white_flag_reason": None,
            "kill_switch_event": None,
        }

    def finalise(
        self,
        termination_reason: str,
        termination_detail: Optional[str] = None,
    ) -> str:
        """
        Write the summary JSON and return the file path.
        Call this once the session is over.
        """
        return self._write_summary(termination_reason, termination_detail)
