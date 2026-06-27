# Legora — Negotiation Training Ground

A junior lawyer negotiates a contract clause-by-clause against an AI opponent,
gets scored and coached when the round ends, and goes again — against an
opponent that gets **harder and sharper against their specific weak spots** each
time. Two agents, one CLI:

* **Negotiator** (`monsieur-argue-main/`) — plays the opposing counsel and emits
  the session transcript.
* **Assessor** (`assessor/`) — grades the lawyer against a playbook, writes the
  coaching debrief, and computes adaptive difficulty.

The two are wired into a closed loop by `legora.py`:

```
                         legora.py play
   ┌──────────────┐   transcript.json     ┌──────────────┐
   │  NEGOTIATOR  │ ────────────────────► │   ASSESSOR   │
   │  (opponent)  │                       │              │
   └──────────────┘                       └──────────────┘
          ▲                                  │        │
          │ persona sharpened                │        ▼  debrief card + score
          │ + difficulty up/down             │   progress.json (streak, best)
          │                                  ▼
          └──────── learnings.json ◄─── weaknesses + difficulty
            (negotiator's --learnings hook)
```

Each round the assessor's findings are written as `learnings.json` in the
**exact schema the negotiator's `--learnings` hook already consumes**, so the
next opponent targets the weaknesses you just showed — and the earned difficulty
level tunes its aggression and the round budget.

## Quickstart

No dependencies, Python 3.10+. One key (`ANTHROPIC_API_KEY`) drives both agents.

```bash
# Play the whole loop with NO api key (canned opponent + offline scoring):
python3 legora.py play --mock

# Live round (interactive) — set the key, drop --mock:
export ANTHROPIC_API_KEY=sk-ant-...
python3 legora.py play

# See your progression, streak and what the opponent is now targeting:
python3 legora.py status

# Two-round demo + progress, then the difficulty-curve simulation:
bash demo.sh
python3 legora.py simulate

# Tests (includes a bridge test: the negotiator can load the assessor's output):
python3 tests/run_tests.py
```

### Assessor on its own

The assessor still runs standalone on any transcript (`legora.py assess` forwards
to it, or call `assess.py` directly):

```bash
# Offline heuristic scorer (no key needed):
python3 legora.py assess -t negotiation_summary_20260627_115604.json -s scenario.json --offline

# LLM-based scoring: set ANTHROPIC_API_KEY and drop --offline.
```

## Inputs (3 files)

| Input | Flag | Schema |
|-------|------|--------|
| **Transcript** | `-t/--transcript` | The negotiator's session summary. Fixed schema — see `negotiation_summary_20260627_115604.json`. Key field: `dialogue[]` of `{round, role: "AI"\|"USER", content}`. |
| **Scenario** | `-s/--scenario` | Role, background, agreed & contested points. Fixed schema — see `scenario.json`. |
| **Playbook** | `-p/--playbook` | The marking standard: a generic rubric + per-scenario model answers. See `playbook.json`. |

### The playbook = "what good looks like"

`playbook.json` has two parts so feedback is grounded, not hand-wavy:

1. **`rubric.dimensions`** — a *generic*, reusable rubric that applies to any
   clause-by-clause negotiation. Seven weighted dimensions (anchoring, legal
   grounding, concession discipline, probing, creativity/trade-craft, composure,
   coverage/outcome), each with 0–5 scoring bands and red flags.
2. **`scenarios.<title>`** — *scenario-specific* model answers: per-issue
   target outcomes (strong / acceptable / weak), the model moves a great lawyer
   would make, the traps to avoid, and optional `weight_overrides`.

To support a **new scenario**, add a block under `scenarios` keyed by the
scenario `title`. If no matching block exists, the generic rubric still applies.

## Output

The CLI prints a **debrief card** to the terminal and (with `-o`) writes the
full **assessment JSON**. Key fields:

```jsonc
{
  "overall_score": 68,            // 0-100, weighted from the rubric
  "grade": "C", "grade_label": "Competent",
  "beat_target": false, "pass_target": 70,
  "headline": "...",
  "dimensions": [                 // one per rubric dimension
    {"id": "coverage_outcome", "name": "...", "score_5": 2.5,
     "weight": 0.22, "weighted_points": 11.0,
     "comment": "...", "evidence": ["..."]}
  ],
  "strengths": ["..."],
  "improvements": [               // specific, ordered by impact
    {"title": "...", "round": 3, "what_happened": "...", "better_move": "..."}
  ],
  "turning_points": [             // for the "replay the turning point" bonus
    {"round": 3, "label": "Big concession", "what_happened": "...", "should_have": "..."}
  ],
  "issue_outcomes": [             // per contested issue, incl. silent concessions
    {"issue": "Warranty cap", "engaged": true, "vs_target": "acceptable", "result": "...", "note": "..."}
  ],
  "coverage": {"engaged": 2, "total": 5, "untouched": ["Earn-out mechanism", ...]},
  "adaptive_difficulty": { ... }  // see below — consumed by the negotiator
}
```

### The contract for the negotiator: `adaptive_difficulty`

```jsonc
"adaptive_difficulty": {
  "previous_difficulty": 4, "new_difficulty": 5, "delta": 1,
  "tier": "Hardball",                  // Cooperative→Seasoned→Hardball→Shark→Ruthless
  "rationale": "Rolling average 81 cleared the step-up line (75).",
  "rolling_avg": 81.3, "streak": 3, "best_score": 84, "session_count": 5,
  "recommended_persona": {"name": "The Filibusterer", "why": "..."},
  "exploit_weaknesses": ["Filibuster the issue they fixate on to burn the clock; ..."],
  "negotiator_directives": {           // knobs the negotiator should apply
    "persona_intensity": 0.64,
    "aggression": 0.48,
    "concession_rate": 0.38,           // lower = gives less ground
    "patience": 0.69,
    "anchor_extremity": 0.69,
    "concede_only_for_reciprocity": true,
    "use_logical_traps": true,
    "bundle_issues_under_pressure": true,
    "will_walk_away": false,
    "manufactured_deadlines": false,
    "suggested_max_rounds": 10,
    "targeted_weaknesses": ["concedes_fast", "poor_coverage"]
  }
}
```

The negotiator maps these directives onto its own behaviour/prompt; this repo
defines the contract, not the negotiator's implementation.

## Adaptive difficulty — how it works

The point of the app is that the lawyer gets *measurably better each time*, so
difficulty has to track skill without whipsawing on a single fluky round.

1. **Persisted progress.** Each round's score is appended to `progress.json`
   (per `--user`), with streak and best-score history.
2. **Rolling-average hysteresis.** Difficulty (1–10) moves on the average of the
   last 3 rounds, not the latest one:
   - rolling avg ≥ **75** → difficulty **+1** (harder)
   - rolling avg ≤ **50** → difficulty **−1** (easier)
   - otherwise → **hold**

   So one off-day eases pressure only if the *trend* drops. (See the simulation:
   a single 47 after three strong rounds does **not** crash the level.)
3. **Difficulty → concrete directives.** The integer level is translated into a
   stingier concession rate, higher aggression/anchor-extremity, and unlocks
   harder tactics (traps, issue-bundling, walk-aways, deadlines) as it climbs.
4. **Targeted, not just louder.** The *specific* weaknesses found this round are
   fed back as `exploit_weaknesses` and pick the `recommended_persona`, so the
   opponent attacks exactly where the user was soft (e.g. left issues untabled →
   *The Filibusterer* runs the clock). That is what makes it adaptive rather
   than a single volume knob.

Run `python3 legora.py simulate` to watch the curve over a scripted sequence.

### How the loop is wired (the bridge)

`legora.py play` does this end to end:

1. Reads the difficulty earned so far from `progress.json`; uses it to set the
   round budget and inject an intensity directive into the opponent's persona.
2. If `learnings.json` exists, injects past weaknesses into the persona via the
   negotiator's own `learnings_loader` (so the AI targets them).
3. Runs the session and writes the transcript.
4. Scores it, updates `progress.json` (difficulty, streak, best).
5. Writes the round's weaknesses to `learnings.json` **in the negotiator's
   `past_learnings.json` schema** — `aggregate_patterns.recurring_weaknesses`
   with `pattern` / `exploit_instruction` / `severity` — accumulating across
   rounds. The `tests/` suite asserts the negotiator's loader accepts this file.

Both agents call Claude through the same dependency-free client
(`assessor/llm.py`), so there's one API key and no `anthropic` install. With no
key, `--mock` (a canned opponent) lets the whole loop run offline.

## LLM vs offline

- **LLM mode (default when `ANTHROPIC_API_KEY` is set).** Claude grades each
  rubric dimension and writes specific, quote-grounded coaching. The computed
  signals (below) are passed in to keep it anchored to verifiable facts. The
  weighted score, grade and pass/fail are computed in Python in *both* modes, so
  the maths is never hallucinated. Model via `--model` or `ASSESSOR_MODEL`.
- **Offline mode (`--offline`, or automatic fallback if no key / API error).**
  A deterministic scorer over signals extracted from the transcript: issue
  coverage, the percentage/concession trajectory, creative-structure and
  legal-grounding cues, professionalism incidents, probing. Always produces a
  usable, reproducible assessment — handy for demos and CI.

## Files

```
legora.py                 # ★ unified CLI: play / assess / status / simulate
assess.py                 # assessor-only entry point
simulate_progress.py      # adaptive-difficulty demo
demo.sh                   # two-round loop demo
playbook.json             # generic rubric + Greenvale model answers
assessor/
  schemas.py              # load + validate the 3 inputs (fixed schemas)
  signals.py              # deterministic signal extraction from the transcript
  scoring.py              # LLM scorer + offline heuristic scorer (same output shape)
  difficulty.py           # adaptive-difficulty engine + progress persistence
  debrief.py              # terminal debrief card renderer
  llm.py                  # dependency-free Anthropic client (shared by both agents)
  cli.py                  # assessor argument parsing + orchestration
  integration.py          # ★ bridge: drives the negotiator, LLM routing, learnings export
monsieur-argue-main/      # the negotiator agent (engine, persona, summariser, …)
tests/run_tests.py        # zero-dependency test suite (incl. the bridge contract)
```
