"""Bridge between the negotiator (``monsieur-argue-main``) and the assessor.

This is what makes the two halves one tool:

* It drives the negotiator's ``NegotiationEngine`` to run a live (or mock)
  session and produce the transcript.
* It routes the negotiator's LLM calls through the assessor's dependency-free
  client, so the whole app runs on **one** ``ANTHROPIC_API_KEY`` and one model
  config (no separate ``anthropic`` install, no ``config.json`` to hand-write).
* After scoring, it converts the assessment into the exact ``past_learnings.json``
  shape the negotiator's ``--learnings`` hook consumes, and injects the current
  difficulty into the persona — closing the adaptive-difficulty loop so the next
  session is both harder and aimed at the trainee's demonstrated weak spots.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from datetime import date

from . import debrief, difficulty, llm, scoring
from .difficulty import WEAKNESS_EXPLOITS, tier_for
from .schemas import Scenario, Transcript, load_json

ASSESSOR_DIR = os.path.dirname(os.path.abspath(__file__))
LEGORA_ROOT = os.path.dirname(ASSESSOR_DIR)
# Two levels up from integrated/Legora/ lands at the negotiation root
NEGOTIATOR_DIR = os.path.normpath(os.path.join(LEGORA_ROOT, "..", ".."))
NEGOTIATOR_INPUTS = os.path.join(NEGOTIATOR_DIR, "inputs")

_SEV = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


# --------------------------------------------------------------------------- #
# Negotiator module loading + LLM routing
# --------------------------------------------------------------------------- #
def load_negotiator():
    """Import the negotiator's top-level modules (they use flat imports)."""
    if not os.path.isdir(NEGOTIATOR_DIR):
        raise FileNotFoundError(
            f"Negotiator code not found at {NEGOTIATOR_DIR}. "
            "Expected the 'monsieur-argue-main' folder beside the assessor."
        )
    if NEGOTIATOR_DIR not in sys.path:
        sys.path.insert(0, NEGOTIATOR_DIR)
    import engine, llm_client, monitor, summariser, learnings_loader  # noqa: E402
    return engine, llm_client, monitor, summariser, learnings_loader


def patch_llm(adapter) -> None:
    """Point every negotiator module's ``call_llm`` at our adapter.

    The negotiator modules did ``from llm_client import call_llm``, so each holds
    its own reference — we must overwrite the name in each module, not just in
    ``llm_client``.
    """
    engine, llm_client, monitor, summariser, _ = load_negotiator()
    for mod in (llm_client, engine, monitor, summariser):
        setattr(mod, "call_llm", adapter)


def make_real_adapter(model: str):
    """A ``call_llm``-compatible function backed by the assessor's urllib client."""
    def adapter(config, system_prompt, messages, temperature=None, max_tokens=None):
        claude = config.get("claude", {})
        neg = config.get("negotiation", {})
        return llm.complete_messages(
            system_prompt,
            messages,
            model=claude.get("model", model),
            max_tokens=max_tokens or claude.get("max_tokens", 1500),
            temperature=temperature if temperature is not None else neg.get("temperature", 0.7),
        )
    return adapter


# --------------------------------------------------------------------------- #
# Mock opponent (no API key needed — lets the whole loop run for demos/tests)
# --------------------------------------------------------------------------- #
_MOCK_OPENING = (
    "Victoria Hale, Fielding Rowe. Let's be efficient. Heads of Terms are signed and "
    "my client wants a clean completion in August. On the open points: warranty cap at "
    "20% of EV, no retention, a modest earn-out preserved, an 18-month limitation period, "
    "and a reasonable non-compete. Those are sensible market terms. Where would you like to begin?"
)
_MOCK_TURNS = [
    "I hear you, but 100% is a guarantee, not a cap. The market for a clean deal of this "
    "size sits at 30-50%. Give me a reason rooted in this transaction and I'll consider moving "
    "off 20% — but not to anything resembling the full value.",
    "That's an operational-risk argument dressed up as a warranty point. Diligence priced that. "
    "I can discuss asset-specific warranties on the conversion kit, but the cap stays modest. "
    "What are you actually worried about — be specific.",
    "A tiered structure I can work with in principle. But 50% in year one is £22.5m hanging over "
    "my client while they repay investors. I'll go to 35% stepping to 22%, and that's a real move. "
    "Is there a deal in that territory, or do we park it?",
    "Fine — but if I give ground on the cap I want it back on limitation. 18 months general, and "
    "I'm not agreeing to six years on technical warranties. What's your priority: the cap or the period?",
    "We're going in circles and we still haven't touched retention, the earn-out or the non-compete. "
    "I'd rather not run the clock down. Pick the issue that matters most to your client and let's close it.",
    "My client's instruction is firm here. I can offer a specific indemnity on the planning dispute, "
    "but a general escrow is off the table — they need the proceeds at completion. Take the indemnity; "
    "it's the cleaner protection and you know it.",
]


class MockLLM:
    """Deterministic stand-in for ``call_llm`` so the pipeline runs without a key."""

    def __init__(self):
        self.turn = 0

    def __call__(self, config, system_prompt, messages, temperature=None, max_tokens=None):
        s = (system_prompt or "").lower()
        last = (messages[-1]["content"] if messages else "").lower()
        if "compliance monitor" in s:
            return json.dumps({"triggered": False, "condition": None, "party": None,
                               "reason": None, "verbatim_quote": None})
        if "legal analyst" in s:
            return json.dumps({"agreements": [], "outstanding_issues": []})
        if "open the negotiation" in last:
            return _MOCK_OPENING
        resp = _MOCK_TURNS[self.turn % len(_MOCK_TURNS)]
        self.turn += 1
        return resp


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def build_config(*, model: str, temperature: float = 0.7, max_rounds: int = 10,
                 monitor_temperature: float = 0.0) -> dict:
    """A negotiator config dict; the API key comes from the environment (or 'MOCK')."""
    return {
        "api_provider": "claude",
        "negotiation": {
            "temperature": temperature,
            "monitor_temperature": monitor_temperature,
            "default_max_rounds": max_rounds,
        },
        "claude": {
            "api_key": os.environ.get("ANTHROPIC_API_KEY", "MOCK"),
            "model": model,
            "max_tokens": 1500,
        },
    }


# --------------------------------------------------------------------------- #
# Difficulty -> persona, and learnings export
# --------------------------------------------------------------------------- #
_INTENSITY_BY_TIER = {
    "Cooperative": "Negotiate constructively. Concede reasonably when given a fair argument; you want a deal.",
    "Seasoned": "Hold your positions. Concede only in small steps and ask for something back each time.",
    "Hardball": "Anchor hard and move grudgingly. Demand reciprocity for every concession and probe weak reasoning.",
    "Shark": "Open aggressively, concede almost nothing without a major give, bundle issues and apply time pressure.",
    "Ruthless": "Maximal pressure: extreme anchors, manufactured deadlines, credible walk-away threats; give nothing for free.",
}


def inject_difficulty_into_persona(persona: dict, difficulty_level: int) -> dict:
    """Add a difficulty directive to the persona so the level changes AI behaviour."""
    persona = json.loads(json.dumps(persona))
    tier = tier_for(difficulty_level)
    note = f"[Difficulty {tier} — level {difficulty_level}/10] {_INTENSITY_BY_TIER.get(tier, '')}"
    persona.setdefault("wants", []).insert(0, note)
    return persona


def weaknesses_from_assessment(assessment: dict) -> list[dict]:
    """Map a scored assessment onto the negotiator's recurring-weakness schema."""
    out: list[dict] = []
    for dim in assessment.get("dimensions", []):
        if dim["score_5"] <= 2.5 and dim["id"] in WEAKNESS_EXPLOITS:
            tag, instruction = WEAKNESS_EXPLOITS[dim["id"]]
            out.append({
                "pattern": tag,
                "description": dim.get("comment", ""),
                "exploit_instruction": instruction,
                "severity": "HIGH" if dim["score_5"] <= 1.5 else "MEDIUM",
            })
    untouched = assessment.get("coverage", {}).get("untouched") or []
    if untouched:
        tag, instruction = WEAKNESS_EXPLOITS["coverage_outcome"]
        if not any(w["pattern"] == tag for w in out):
            out.insert(0, {
                "pattern": tag,
                "description": "Left contested issues untabled: " + ", ".join(untouched),
                "exploit_instruction": instruction,
                "severity": "HIGH",
            })
    return out


def update_learnings_file(path: str, assessment: dict, *, trainee_id: str,
                          scenario_title: str, session_id: str) -> dict:
    """Append this session and re-aggregate, in the negotiator's past_learnings schema."""
    if path and os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    data.setdefault("trainee_id", trainee_id)
    data.setdefault("sessions", [])
    agg = data.setdefault("aggregate_patterns", {})
    agg.setdefault("recurring_weaknesses", [])
    agg.setdefault("recurring_strengths", [])

    current = weaknesses_from_assessment(assessment)
    data["sessions"].append({
        "date": date.today().isoformat(),
        "session_id": session_id,
        "scenario": scenario_title,
        "score": assessment.get("overall_score"),
        "grade": assessment.get("grade"),
        "analyst_notes": {
            "mistakes": [
                {"pattern": w["pattern"], "mistake": w["description"], "severity": w["severity"]}
                for w in current
            ]
        },
    })

    by_pattern = {w["pattern"]: w for w in agg["recurring_weaknesses"]}
    for w in current:
        existing = by_pattern.get(w["pattern"])
        if existing:
            existing["count"] = existing.get("count", 1) + 1
            existing["exploit_instruction"] = w["exploit_instruction"]
            existing["description"] = w["description"]
            if _SEV[w["severity"]] > _SEV.get(existing.get("severity", "LOW"), 1):
                existing["severity"] = w["severity"]
        else:
            entry = dict(w, count=1)
            agg["recurring_weaknesses"].append(entry)
            by_pattern[w["pattern"]] = entry

    for s in assessment.get("strengths", []):
        if s not in agg["recurring_strengths"]:
            agg["recurring_strengths"].append(s)
    agg["recurring_strengths"] = agg["recurring_strengths"][-6:]

    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
    return data


# --------------------------------------------------------------------------- #
# Session runner (drives NegotiationEngine; interactive or scripted)
# --------------------------------------------------------------------------- #
class _Ansi:
    def __init__(self, on): self.on = on
    def w(self, code, s): return f"\033[{code}m{s}\033[0m" if self.on else s
    def bold(self, s): return self.w("1", s)
    def cyan(self, s): return self.w("36", s)
    def yellow(self, s): return self.w("33", s)
    def red(self, s): return self.w("31", s)
    def dim(self, s): return self.w("2", s)


def _wrap(text, width=74):
    return "\n".join(textwrap.fill(p, width) if p.strip() else "" for p in text.split("\n"))


def _voice_speak(voice_config: dict, text: str) -> None:
    """Speak text via voice_io if available; silently skip if not."""
    try:
        sys.path.insert(0, NEGOTIATOR_DIR)
        from voice_io import speak_ai_response
        speak_ai_response(voice_config, text)
    except Exception as exc:
        print(f"  [TTS skipped: {exc}]")


def _voice_input(voice_config: dict) -> str:
    """Record mic input via voice_io; falls back to text input on any error."""
    try:
        sys.path.insert(0, NEGOTIATOR_DIR)
        from voice_io import record_user_input
        return record_user_input(voice_config)
    except Exception as exc:
        print(f"  [Mic unavailable: {exc}]")
        return input("  Type your message: ").strip()


def run_session(engine, persona_name, max_rounds, *, script=None, color=True,
                voice=False, voice_config=None):
    """Run the round loop. ``script`` (list of user turns) => non-interactive.

    Pass ``voice=True`` and ``voice_config`` (the full config.json dict) to
    enable mic input and spoken AI responses via voice_io.py.

    Returns (termination_reason, termination_detail).
    """
    c = _Ansi(color)
    print(f"\n{'─'*74}\n{c.cyan(c.bold(f'[Opening] {persona_name}:'))}")
    opening = engine.opening_statement()
    print(_wrap(opening))
    print("─" * 74)
    if voice and voice_config:
        _voice_speak(voice_config, opening)

    scripted = script is not None
    idx = 0
    while True:
        if scripted:
            if idx >= len(script):
                return "USER_STOP", "Scripted input exhausted"
            user_input = script[idx].strip()
            idx += 1
            print(f"\n{c.bold('You:')} {user_input}")
        elif voice and voice_config:
            print(f"\n{c.bold('You:')} ", end="", flush=True)
            user_input = _voice_input(voice_config)
            if user_input:
                print(c.dim(f"  [Heard: \"{user_input}\"]"))
        else:
            try:
                user_input = input(f"\n{c.bold('You:')} ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return "USER_STOP", "Interrupted"

        if not user_input:
            continue
        if user_input.lower() == "stop":
            return "USER_STOP", "User typed 'stop'"
        if user_input.lower() == "help":
            print(c.dim(f"  commands: stop | help   (round {engine.current_round + 1}/{max_rounds})"))
            continue

        if not scripted:
            print(c.dim("  ...thinking..."))
        try:
            result = engine.process_turn(user_input)
        except Exception as exc:  # noqa: BLE001
            print(c.red(f"  [API error] {exc}"))
            return "USER_STOP", f"API error: {exc}"

        header = c.cyan(c.bold(f"[Round {result['round']}/{max_rounds}] {persona_name}:"))
        print(f"\n{'─' * 74}\n{header}")
        print(_wrap(result["ai_response"]))
        print("─" * 74)
        if voice and voice_config:
            _voice_speak(voice_config, result["ai_response"])

        status = result["status"]
        if status == "kill_switch":
            ev = result["kill_switch_event"]
            print(c.red(f"\n[KILL SWITCH] {ev.get('condition')} by {ev.get('party')}: {ev.get('reason')}"))
            return "KILL_SWITCH", f"{ev.get('condition')} by {ev.get('party')}"
        if status == "white_flag":
            print(c.yellow(f"\n[WHITE FLAG] {persona_name}: {result['white_flag_reason']}"))
            if scripted:
                return "WHITE_FLAG", result["white_flag_reason"]
            if input(c.yellow("  Terminate the session? [y/n]: ")).strip().lower() in ("y", "yes"):
                return "WHITE_FLAG", result["white_flag_reason"]
        if status == "max_rounds":
            print(c.yellow(f"\n[SYSTEM] Maximum rounds ({max_rounds}) reached."))
            return "MAX_ROUNDS", f"Completed {max_rounds} rounds"


# --------------------------------------------------------------------------- #
# Full loop: negotiate -> assess -> adapt
# --------------------------------------------------------------------------- #
def _load_voice_config() -> dict:
    """Load config.json from the negotiator root for voice I/O settings."""
    cfg_path = os.path.join(NEGOTIATOR_DIR, "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def play_once(*, scenario_path, persona_path, norms_path, playbook_path,
              progress_path, learnings_path, user="default", model=llm.DEFAULT_MODEL,
              mock=False, rounds=None, script=None, output_dir=".", color=True,
              voice=False):
    """Run one full round of the training loop and return the assessment dict.

    Pass ``voice=True`` to enable mic input + spoken AI responses.  The voice
    settings are loaded from config.json in the negotiator root.
    """
    engine_mod, _, _, _, learnings_loader = load_negotiator()

    # Voice: check deps and load config early so we can warn before the session starts.
    voice_config = None
    if voice:
        voice_config = _load_voice_config()
        try:
            sys.path.insert(0, NEGOTIATOR_DIR)
            from voice_io import check_voice_deps
            check_voice_deps()
        except RuntimeError as exc:
            print(f"  [Voice unavailable: {exc}]\n  Falling back to text input.")
            voice = False
            voice_config = None

    neg_mock = mock or not llm.have_api_key()
    patch_llm(MockLLM() if neg_mock else make_real_adapter(model))

    scenario_d = load_json(scenario_path)
    persona = load_json(persona_path)
    norms = load_json(norms_path)
    playbook = load_json(playbook_path)

    # Difficulty earned so far drives this session's intensity + round budget.
    progress = difficulty.load_progress(progress_path, user)
    cur_diff = int(progress["users"][user].get("current_difficulty", difficulty.DEFAULT_START))
    max_rounds = rounds or difficulty._suggested_rounds(cur_diff, 10)

    # Sharpen the opponent: inject past weaknesses (if any) + this difficulty level.
    learnings_used = bool(learnings_path and os.path.exists(learnings_path))
    if learnings_used:
        persona, _ = learnings_loader.augment_persona_with_learnings_from_path(learnings_path, persona)
    persona = inject_difficulty_into_persona(persona, cur_diff)

    neg_config = build_config(model=model, max_rounds=max_rounds)
    engine = engine_mod.NegotiationEngine(
        config=neg_config, company_norms=norms, scenario=scenario_d, persona=persona,
        max_rounds=max_rounds, output_dir=output_dir, learnings_used=learnings_used,
    )

    c = _Ansi(color)
    voice_label = " [VOICE]" if voice else ""
    print(c.bold(c.cyan(
        f"\n== NEGOTIATION -- {scenario_d.get('title','')} -- "
        f"difficulty {cur_diff}/10 ({tier_for(cur_diff)}) -- {max_rounds} rounds "
        f"{'[MOCK opponent]' if neg_mock else ''}{voice_label} ==")))
    if learnings_used:
        print(c.dim("  (opponent sharpened against your past weaknesses)"))

    reason, detail = run_session(
        engine, persona["name"], max_rounds,
        script=script, color=color, voice=voice, voice_config=voice_config,
    )
    transcript_path = engine.finalise(reason, detail)

    # Score the round.
    transcript = Transcript.from_dict(load_json(transcript_path))
    scenario = Scenario.from_dict(scenario_d)
    assessment = scoring.assess(transcript, scenario, playbook,
                                offline=neg_mock, model=model)

    block = difficulty.update_difficulty(progress, assessment, scenario.title, user,
                                         prev_rounds=max_rounds)
    assessment["adaptive_difficulty"] = block
    assessment["session_id"] = transcript.session_id
    assessment["scenario_title"] = scenario.title
    assessment["scenario_user_side"] = scenario.user_side
    difficulty.save_progress(progress_path, progress)

    # Feed weaknesses forward for the next session's opponent.
    update_learnings_file(learnings_path, assessment, trainee_id=user,
                          scenario_title=scenario.title, session_id=transcript.session_id)

    print(render_session_banner(transcript_path, color))
    print(debrief.render_card(assessment, block, scenario, color=color))
    return assessment, transcript_path


def render_session_banner(transcript_path: str, color: bool) -> str:
    c = _Ansi(color)
    return c.dim(f"\n  transcript saved: {transcript_path}")

