"""
analyze_sharps.py — run on your real alerts_log.jsonl.
Answers, exactly (no hand-counting):
  1. frozen-A win rate: sharp_driven vs not, with Wilson CIs
  2. per-sharp frozen-A record (who's actually carrying losses)
  3. min-count floor counterfactual: what a "sharp can't push a thin cohort to A" rule does
Frozen predicate: grade A, archetype outcome, price_at_alert >= 0.20, resolved, single match.
This is MEASUREMENT. Any rule you like from it is applied FORWARD, not to retrim history.
"""
import json, math
from pathlib import Path
from collections import defaultdict

LOG = Path("alerts_log.jsonl")
EXCLUDE_TEAM_TO_ADVANCE = False   # set True to treat 'Team to Advance' as futures-ish and drop it

def wilson(w, n):
    if n == 0: return (0, 0)
    z = 1.96; p = w / n
    d = 1 + z*z/n
    c = (p + z*z/(2*n)) / d
    h = z*math.sqrt(p*(1-p)/n + z*z/(4*n*n)) / d
    return (max(0, c-h), min(1, c+h))

def frozen(r):
    if r.get("grade") != "A" or r.get("archetype") != "outcome": return False
    if (r.get("price_at_alert") or 0) < 0.20: return False
    if r.get("won") is None or r.get("resolved") is None: return False
    if EXCLUDE_TEAM_TO_ADVANCE and "Advance" in (r.get("title") or ""): return False
    return True

rows = [json.loads(l) for l in LOG.read_text().splitlines() if l.strip()]
A = [r for r in rows if frozen(r)]
print(f"frozen-A resolved: {len(A)}\n")

# 1. sharp-driven vs not
for label, sub in (("sharp_driven", [r for r in A if r.get("sharp_driven")]),
                   ("NOT sharp",    [r for r in A if not r.get("sharp_driven")])):
    w = sum(1 for r in sub if r["won"]); n = len(sub)
    lo, hi = wilson(w, n)
    print(f"{label:14} {w}/{n} = {100*w/n:.0f}%  95% CI [{100*lo:.0f}%, {100*hi:.0f}%]" if n else f"{label}: none")

# 2. per-sharp frozen-A record
print("\nper-sharp frozen-A record (a sharp gets credit for every A it's named on):")
rec = defaultdict(lambda: [0, 0])   # name -> [wins, losses]
for r in A:
    for s in (r.get("sharps") or []):
        rec[s][0 if r["won"] else 1] += 1
for name, (w, l) in sorted(rec.items(), key=lambda kv: -(kv[1][0]+kv[1][1])):
    print(f"  {name:16} {w}W {l}L  ({w}/{w+l})")

# 3. min-count floor: if a sharp-driven A needs count >= FLOOR to stay an A
print("\nmin-count floor counterfactual (sharp-driven A's below floor get demoted):")
for floor in (0, 6, 7, 8):
    kept = [r for r in A if (not r.get("sharp_driven")) or (r.get("count", 0) >= floor)]
    w = sum(1 for r in kept if r["won"]); n = len(kept)
    dropped = [r for r in A if r not in kept]
    dl = sum(1 for r in dropped if not r["won"]); dw = sum(1 for r in dropped if r["won"])
    print(f"  floor {floor}: keep {w}/{n} = {100*w/n:.0f}%   (dropped {len(dropped)}: {dw}W {dl}L)")
