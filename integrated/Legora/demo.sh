#!/usr/bin/env bash
# Demo of the unified Legora training loop: negotiate -> assess -> adapt.
set -e
cd "$(dirname "$0")"

DEMO_DIR=".demo"
mkdir -p "$DEMO_DIR"
SCRIPT="$DEMO_DIR/turns.txt"

cat > "$SCRIPT" <<'EOF'
100% cap or nothing — you operated these assets, we didn't. The information asymmetry is the whole point.
A tiered cap then: 50% in year one for a full generation cycle, stepping to 30% after, given hidden conversion risk.
Fine, 40% year one to 25%. But I want limitation at 6 years tax and 3 years general, tied to the independent engineer's report.
On retention there is a live planning dispute — I'll drop the general escrow for a specific indemnity ring-fencing that risk.
The earn-out is really about keeping your key staff. Solve that with a management lock-in, not deferred price.
Non-compete: three years UK-wide, but I'll carve out the founder's new venture if it is genuinely non-competing.
EOF

echo "=================================================================="
echo " ROUND 1 — negotiate (mock opponent), then auto-score + adapt"
echo "=================================================================="
python3 legora.py play --mock --no-color \
  --script "$SCRIPT" \
  --progress "$DEMO_DIR/progress.json" \
  --learnings "$DEMO_DIR/learnings.json" \
  --output "$DEMO_DIR"

echo
echo "=================================================================="
echo " ROUND 2 — opponent now sharpened against your weak spots"
echo "=================================================================="
python3 legora.py play --mock --no-color \
  --script "$SCRIPT" \
  --progress "$DEMO_DIR/progress.json" \
  --learnings "$DEMO_DIR/learnings.json" \
  --output "$DEMO_DIR" | grep -E "NEGOTIATION ──|sharpened|NEXT ROUND|Difficulty|Streak|Opponent:"

echo
echo "=================================================================="
echo " PROGRESS"
echo "=================================================================="
python3 legora.py status --progress "$DEMO_DIR/progress.json" --learnings "$DEMO_DIR/learnings.json"

echo
echo "For a live round, set ANTHROPIC_API_KEY and run:  python3 legora.py play"
