"""Render the assessment + difficulty block as a terminal 'debrief card'."""

from __future__ import annotations

import sys

from .schemas import Scenario


class _C:
    """ANSI colours; blanked out when colour is disabled."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def _w(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.enabled else s

    def bold(self, s):   return self._w("1", s)
    def dim(self, s):    return self._w("2", s)
    def green(self, s):  return self._w("32", s)
    def red(self, s):    return self._w("31", s)
    def yellow(self, s): return self._w("33", s)
    def cyan(self, s):   return self._w("36", s)
    def grey(self, s):   return self._w("90", s)


WIDTH = 64


def _rule(ch="─") -> str:
    return ch * WIDTH


def _bar(score5: float, c: _C) -> str:
    filled = int(round(score5 * 2))  # 0..10
    bar = "█" * filled + "░" * (10 - filled)
    if score5 >= 4:
        bar = c.green(bar)
    elif score5 >= 2.5:
        bar = c.yellow(bar)
    else:
        bar = c.red(bar)
    return bar


def render_card(assessment: dict, difficulty: dict, scenario: Scenario, color: bool = True) -> str:
    c = _C(color)
    out: list[str] = []
    a = assessment

    out.append(_rule("═"))
    out.append("  " + c.bold(c.cyan("LEGORA · NEGOTIATION DEBRIEF")))
    out.append("  " + c.bold(scenario.title))
    out.append("  " + c.grey(f"Acting for: {scenario.user_side}"))
    out.append(_rule("═"))

    # Score line
    score = a["overall_score"]
    grade = f"{a['grade']} ({a['grade_label']})"
    target = a["pass_target"]
    if a["beat_target"]:
        verdict = c.green(f"✓ BEAT TARGET ({target})")
    else:
        verdict = c.red(f"✗ below target ({target}) — gap {target - score}")
    score_str = c.bold(f"{score}/100")
    out.append(f"  SCORE  {score_str}    Grade {c.bold(grade)}    {verdict}")
    mode = a.get("mode_note") or a.get("mode", "")
    model = f" · {a['model']}" if a.get("model") else ""
    out.append("  " + c.grey(f"Assessed: {mode}{model}"))
    out.append("")

    # Headline
    if a.get("headline"):
        out.append("  " + c.bold("VERDICT"))
        for line in _wrap(a["headline"], WIDTH - 4):
            out.append("  " + line)
        out.append("")

    # Scorecard
    out.append("  " + c.bold("SCORECARD"))
    for dim in a["dimensions"]:
        name = dim["name"][:26].ljust(26)
        wpct = int(round(dim.get("weight", 0) * 100))
        out.append(f"  {name} {_bar(dim['score_5'], c)} {dim['score_5']:.1f}/5  {c.grey(f'·{wpct}%')}")
    out.append("")

    # Strengths
    if a.get("strengths"):
        out.append("  " + c.bold(c.green("WHAT YOU DID WELL")))
        for s in a["strengths"]:
            for i, line in enumerate(_wrap(s, WIDTH - 6)):
                out.append(("  • " if i == 0 else "    ") + line)
        out.append("")

    # Improvements
    if a.get("improvements"):
        out.append("  " + c.bold(c.yellow("FIX THESE NEXT")))
        for i, imp in enumerate(a["improvements"], 1):
            rnd = f" (round {imp['round']})" if imp.get("round") not in (None, "") else ""
            out.append(f"  {c.bold(str(i) + '.')} {c.bold(imp.get('title',''))}{c.grey(rnd)}")
            if imp.get("what_happened"):
                for line in _wrap("What happened: " + imp["what_happened"], WIDTH - 6):
                    out.append("     " + c.grey(line))
            if imp.get("better_move"):
                for j, line in enumerate(_wrap("Better move: " + imp["better_move"], WIDTH - 6)):
                    out.append("     " + (c.green(line) if j == 0 else line))
        out.append("")

    # Turning points
    if a.get("turning_points"):
        out.append("  " + c.bold("TURNING POINTS"))
        for tp in a["turning_points"]:
            head = f"Round {tp.get('round','?')} — {tp.get('label','')}: {tp.get('what_happened','')}"
            for i, line in enumerate(_wrap(head, WIDTH - 6)):
                out.append(("  ▸ " if i == 0 else "    ") + line)
            if tp.get("should_have"):
                for line in _wrap("Should have: " + tp["should_have"], WIDTH - 6):
                    out.append("    " + c.cyan(line))
        out.append("")

    # Issue coverage
    cov = a.get("coverage", {})
    out.append("  " + c.bold(f"ISSUE COVERAGE  ({cov.get('engaged',0)}/{cov.get('total',0)})"))
    for io in a.get("issue_outcomes", []):
        if io.get("engaged"):
            mark = c.green("✓")
            tail = c.grey(io.get("result", "engaged"))
        else:
            mark = c.red("✗")
            tail = c.red("NOT ADDRESSED — silent concession")
        vs = io.get("vs_target")
        vs_str = f" [{vs}]" if vs and vs not in ("engaged", "not addressed") else ""
        out.append(f"  {mark} {io['issue'][:34].ljust(34)} {tail}{vs_str}")
    out.append("")

    # Next round / adaptive difficulty
    out.append(_rule())
    d = difficulty
    arrow = f"{d['previous_difficulty']} → {d['new_difficulty']}"
    delta = d["delta"]
    if delta > 0:
        change = c.green(f"▲ harder ({arrow})")
    elif delta < 0:
        change = c.yellow(f"▼ easier ({arrow})")
    else:
        change = c.grey(f"= steady ({arrow})")
    out.append("  " + c.bold("NEXT ROUND") + f"   Difficulty {change}  ·  Tier: {c.bold(d['tier'])}")
    streak_str = f"🔥 {d['streak']}" if d["streak"] else "0"
    out.append("  " + c.grey(f"{d['rationale']}"))
    out.append(f"  Streak: {streak_str}    Best: {d['best_score']}    Round #{d['session_count']}")
    persona = d.get("recommended_persona", {})
    if persona:
        out.append("  " + c.bold(f"Opponent: {persona.get('name','')}"))
        for line in _wrap(persona.get("why", ""), WIDTH - 6):
            out.append("    " + c.grey(line))
    if d.get("exploit_weaknesses"):
        out.append("  " + c.bold("It will target:"))
        for w in d["exploit_weaknesses"]:
            for i, line in enumerate(_wrap(w, WIDTH - 6)):
                out.append(("    – " if i == 0 else "      ") + line)
    out.append("")
    out.append("  " + c.bold(c.cyan(f"► Play again — beat {score}.")))
    out.append(_rule("═"))
    return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            if cur:
                lines.append(cur)
            cur = w
        else:
            cur = (cur + " " + w).strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def supports_color() -> bool:
    return sys.stdout.isatty()
