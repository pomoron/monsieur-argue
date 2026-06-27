"""Loading and light validation of the assessor's three inputs.

Schemas for the transcript and scenario are *fixed* (defined by the negotiator
agent / pipeline); this module reads them defensively and raises clear errors
rather than failing deep inside scoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def load_json(path: str) -> dict:
    """Load a JSON file with a friendly error if it is missing or malformed."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Input file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"File is not valid JSON ({path}): {exc}") from exc


@dataclass
class Turn:
    round: int
    role: str  # "AI" or "USER"
    content: str

    @property
    def is_user(self) -> bool:
        return self.role.upper() == "USER"

    @property
    def is_ai(self) -> bool:
        return self.role.upper() == "AI"


@dataclass
class Transcript:
    """A negotiation session transcript (fixed schema, see sample json)."""

    session_id: str
    rounds_completed: int
    dialogue: list[Turn]
    termination: dict = field(default_factory=dict)
    agreements: list = field(default_factory=list)
    outstanding_issues: list = field(default_factory=list)
    kill_switch_events: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Transcript":
        if "dialogue" not in d or not isinstance(d["dialogue"], list):
            raise ValueError("Transcript missing a 'dialogue' list.")
        dialogue = [
            Turn(
                round=int(t.get("round", i)),
                role=str(t.get("role", "")),
                content=str(t.get("content", "")),
            )
            for i, t in enumerate(d["dialogue"])
        ]
        if not any(t.is_user for t in dialogue):
            raise ValueError("Transcript has no USER turns to assess.")
        return cls(
            session_id=str(d.get("session_id", "unknown")),
            rounds_completed=int(d.get("rounds_completed", len(dialogue))),
            dialogue=dialogue,
            termination=d.get("termination", {}) or {},
            agreements=d.get("agreements", []) or [],
            outstanding_issues=d.get("outstanding_issues", []) or [],
            kill_switch_events=d.get("kill_switch_events", []) or [],
            metadata=d.get("metadata", {}) or {},
            raw=d,
        )

    def user_turns(self) -> list[Turn]:
        return [t for t in self.dialogue if t.is_user]

    def user_text(self) -> str:
        return "\n".join(t.content for t in self.user_turns())

    def transcript_text(self) -> str:
        """A readable single-string rendering for the LLM prompt."""
        lines = []
        for t in self.dialogue:
            speaker = "USER (you)" if t.is_user else "AI (opponent)"
            lines.append(f"[Round {t.round}] {speaker}:\n{t.content}\n")
        return "\n".join(lines)


@dataclass
class ContestedPoint:
    issue: str
    buyer_position: str = ""
    seller_position: str = ""
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ContestedPoint":
        return cls(
            issue=str(d.get("issue", "")),
            buyer_position=str(d.get("buyer_position", "")),
            seller_position=str(d.get("seller_position", "")),
            notes=str(d.get("notes", "")),
        )


@dataclass
class Scenario:
    """The scenario description (fixed schema, see sample json)."""

    title: str
    background: str
    agreed_points: list[str]
    contested_points: list[ContestedPoint]
    your_side: str  # the AI's side
    user_side: str  # the user's side
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Scenario":
        return cls(
            title=str(d.get("title", "Untitled scenario")),
            background=str(d.get("background", "")),
            agreed_points=list(d.get("agreed_points", []) or []),
            contested_points=[
                ContestedPoint.from_dict(c) for c in d.get("contested_points", []) or []
            ],
            your_side=str(d.get("your_side", "")),
            user_side=str(d.get("user_side", "")),
            raw=d,
        )

    def issue_names(self) -> list[str]:
        return [c.issue for c in self.contested_points]


def load_inputs(transcript_path: str, scenario_path: str, playbook_path: str):
    """Load and validate all three inputs. Returns (Transcript, Scenario, playbook dict)."""
    transcript = Transcript.from_dict(load_json(transcript_path))
    scenario = Scenario.from_dict(load_json(scenario_path))
    playbook = load_json(playbook_path)
    if "rubric" not in playbook or "dimensions" not in playbook.get("rubric", {}):
        raise ValueError("Playbook missing 'rubric.dimensions'.")
    return transcript, scenario, playbook


def resolved_weights(playbook: dict, scenario: Scenario) -> dict[str, float]:
    """Dimension weights, applying any per-scenario weight overrides, normalised to 1.0."""
    weights = {
        dim["id"]: float(dim.get("weight", 0.0))
        for dim in playbook["rubric"]["dimensions"]
    }
    scen = playbook.get("scenarios", {}).get(scenario.title, {})
    overrides = scen.get("weight_overrides") or {}
    for k, v in overrides.items():
        if k in weights:
            weights[k] = float(v)
    total = sum(weights.values()) or 1.0
    return {k: v / total for k, v in weights.items()}
