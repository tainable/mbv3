"""
Profit-stratified arb analysis:
  1. Do higher-profit arbs disappear faster?
  2. At what profit level does worsening become more likely than improving?
"""

import json
import statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_PATH = Path("C:/mbv3/outputs/arb_log.jsonl")
SESSION_GAP_S = 30 * 60   # 30-minute session break
EPSILON       = 0.005     # pp — same as stream.py PRINT_THRESHOLD

# ─────────────────────────────────────────────────────────────────────────────
# Load + parse
# ─────────────────────────────────────────────────────────────────────────────

print("Loading …")
records = []
with open(LOG_PATH) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

for r in records:
    r["_ts"] = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))

records.sort(key=lambda r: r["_ts"])
print(f"  {len(records):,} records loaded\n")

# ─────────────────────────────────────────────────────────────────────────────
# Rebuild sessions (same logic as analyze_arbs.py)
# ─────────────────────────────────────────────────────────────────────────────

def arb_key(r):
    return (
        r["league"],
        r.get("game_team1", ""),
        r.get("game_team2", ""),
        r.get("arb_type", ""),
        r.get("spread"),
        r.get("total_line"),
        tuple(sorted(r.get("providers") or [])),
    )

by_key = defaultdict(list)
for r in records:
    by_key[arb_key(r)].append((r["_ts"], r.get("profit_pct") or 0.0))

for k in by_key:
    by_key[k].sort()

sessions = []
for key, entries in by_key.items():
    session_entries = [entries[0]]
    for i in range(1, len(entries)):
        gap_s = (entries[i][0] - entries[i-1][0]).total_seconds()
        if gap_s >= SESSION_GAP_S:
            sessions.append({"key": key, "entries": session_entries})
            session_entries = [entries[i]]
        else:
            session_entries.append(entries[i])
    sessions.append({"key": key, "entries": session_entries})

# Annotate each session
for s in sessions:
    ents      = s["entries"]
    s["start_profit"] = ents[0][1]
    s["peak_profit"]  = max(e[1] for e in ents)
    s["end_profit"]   = ents[-1][1]
    s["duration_s"]   = (ents[-1][0] - ents[0][0]).total_seconds()
    s["n"]            = len(ents)

# ─────────────────────────────────────────────────────────────────────────────
# Profit tiers
# ─────────────────────────────────────────────────────────────────────────────

# Defined on START profit of each session
TIERS = [
    ("0.00–0.10%",  0.00, 0.10),
    ("0.10–0.25%",  0.10, 0.25),
    ("0.25–0.50%",  0.25, 0.50),
    ("0.50–1.00%",  0.50, 1.00),
    ("1.00–2.00%",  1.00, 2.00),
    ("> 2.00%",     2.00, 999),
]

def tier_label(pct):
    for label, lo, hi in TIERS:
        if lo <= pct < hi:
            return label
    return "> 2.00%"

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1: Session duration by starting profit tier
# ─────────────────────────────────────────────────────────────────────────────

print("=" * 72)
print("1.  SESSION DURATION BY STARTING PROFIT TIER")
print("    (Does a more profitable arb disappear faster?)")
print("=" * 72)

tier_durations = defaultdict(list)
tier_single    = defaultdict(int)  # single-entry sessions (duration=0)

for s in sessions:
    t = tier_label(s["start_profit"])
    tier_durations[t].append(s["duration_s"])
    if s["n"] == 1:
        tier_single[t] += 1

print(f"\n  {'Tier':<14} {'N':>5}  {'Single%':>8}  {'Med dur':>8}  {'Mean dur':>9}  {'p75':>8}  {'p90':>8}")
print("  " + "-" * 68)
for label, lo, hi in TIERS:
    durs = tier_durations[label]
    if not durs:
        continue
    single_pct = 100 * tier_single[label] / len(durs)
    med  = statistics.median(durs) / 60
    mean = statistics.mean(durs)   / 60
    p75  = sorted(durs)[int(0.75 * len(durs))] / 60
    p90  = sorted(durs)[int(0.90 * len(durs))] / 60
    print(f"  {label:<14} {len(durs):>5}  {single_pct:>7.1f}%  "
          f"{med:>7.1f}m  {mean:>8.1f}m  {p75:>7.1f}m  {p90:>7.1f}m")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2: Transition direction by CURRENT profit level at time of transition
# ─────────────────────────────────────────────────────────────────────────────

print()
print("=" * 72)
print("2.  IMPROVE vs. WORSEN vs. FLAT  BY CURRENT PROFIT LEVEL")
print("    (At what profit does worsening become more likely than improving?)")
print("=" * 72)

# Fine-grained buckets for this analysis
FINE_TIERS = [
    ("0.00–0.05%",  0.00, 0.05),
    ("0.05–0.10%",  0.05, 0.10),
    ("0.10–0.20%",  0.10, 0.20),
    ("0.20–0.35%",  0.20, 0.35),
    ("0.35–0.50%",  0.35, 0.50),
    ("0.50–0.75%",  0.50, 0.75),
    ("0.75–1.00%",  0.75, 1.00),
    ("1.00–1.50%",  1.00, 1.50),
    ("1.50–2.00%",  1.50, 2.00),
    ("> 2.00%",     2.00, 999),
]

def fine_tier(pct):
    for label, lo, hi in FINE_TIERS:
        if lo <= pct < hi:
            return label
    return "> 2.00%"

trans_by_tier = defaultdict(lambda: {"up": 0, "down": 0, "flat": 0, "deltas": []})

for s in sessions:
    ents = s["entries"]
    for i in range(1, len(ents)):
        prev_pct = ents[i-1][1]
        curr_pct = ents[i][1]
        delta    = curr_pct - prev_pct
        t        = fine_tier(prev_pct)   # bucket by profit BEFORE the move
        trans_by_tier[t]["deltas"].append(delta)
        if abs(delta) < EPSILON:
            trans_by_tier[t]["flat"] += 1
        elif delta > 0:
            trans_by_tier[t]["up"] += 1
        else:
            trans_by_tier[t]["down"] += 1

print(f"\n  {'Profit tier':<14}  {'N':>6}  {'Improved':>9}  {'Worsened':>9}  {'Flat':>7}  {'I/(I+W)':>8}  {'Med |delta|':>12}")
print("  " + "-" * 75)
for label, lo, hi in FINE_TIERS:
    d = trans_by_tier[label]
    total = d["up"] + d["down"] + d["flat"]
    if total == 0:
        continue
    non_flat = [abs(x) for x in d["deltas"] if abs(x) >= EPSILON]
    med_delta = statistics.median(non_flat) if non_flat else 0.0
    iw = d["up"] + d["down"] or 1
    ratio = 100 * d["up"] / iw   # % of non-flat transitions that improved
    print(f"  {label:<14}  {total:>6,}  "
          f"{100*d['up']/total:>8.1f}%  "
          f"{100*d['down']/total:>8.1f}%  "
          f"{100*d['flat']/total:>6.1f}%  "
          f"{ratio:>7.1f}%  "
          f"{med_delta:>11.3f}pp")

print()
print("  I/(I+W): share of non-flat transitions that improved (>50% = arb more")
print("  likely to get better; <50% = more likely to get worse).")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3: Within-session profit trajectories
#   What happens to the arb after it first appears at a given profit level?
#   Does it tend to rise, fall, or stay flat?
# ─────────────────────────────────────────────────────────────────────────────

print()
print("=" * 72)
print("3.  SESSION OUTCOME BY STARTING PROFIT TIER")
print("    (Where does the arb end up vs. where it started?)")
print("=" * 72)

tier_outcomes = defaultdict(lambda: {"rose": 0, "fell": 0, "flat": 0,
                                      "start_pcts": [], "end_pcts": [],
                                      "peak_pcts": [], "delta_pcts": []})

for s in sessions:
    if s["n"] < 2:
        continue   # skip single-entry sessions — no trajectory
    t = tier_label(s["start_profit"])
    delta = s["end_profit"] - s["start_profit"]
    tier_outcomes[t]["start_pcts"].append(s["start_profit"])
    tier_outcomes[t]["end_pcts"].append(s["end_profit"])
    tier_outcomes[t]["peak_pcts"].append(s["peak_profit"])
    tier_outcomes[t]["delta_pcts"].append(delta)
    if delta > EPSILON:
        tier_outcomes[t]["rose"] += 1
    elif delta < -EPSILON:
        tier_outcomes[t]["fell"] += 1
    else:
        tier_outcomes[t]["flat"] += 1

print(f"\n  {'Tier':<14}  {'Sessions':>8}  {'Rose':>6}  {'Fell':>6}  {'Flat':>6}  "
      f"{'Med start':>10}  {'Med end':>9}  {'Med peak':>9}")
print("  " + "-" * 80)
for label, lo, hi in TIERS:
    d = tier_outcomes[label]
    n = d["rose"] + d["fell"] + d["flat"]
    if n == 0:
        continue
    med_start = statistics.median(d["start_pcts"])
    med_end   = statistics.median(d["end_pcts"])
    med_peak  = statistics.median(d["peak_pcts"])
    print(f"  {label:<14}  {n:>8}  "
          f"{100*d['rose']/n:>5.1f}%  "
          f"{100*d['fell']/n:>5.1f}%  "
          f"{100*d['flat']/n:>5.1f}%  "
          f"{med_start:>9.3f}%  "
          f"{med_end:>8.3f}%  "
          f"{med_peak:>8.3f}%")

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4: Speed of disappearance — for single-entry sessions
#   (arb seen once and gone) vs multi-entry
# ─────────────────────────────────────────────────────────────────────────────

print()
print("=" * 72)
print("4.  FLASH ARBS (single odds-change, then gone) BY PROFIT TIER")
print("    (These are the arbs that disappeared before a second odds update)")
print("=" * 72)

flash_by_tier   = defaultdict(int)
multi_by_tier   = defaultdict(int)

for s in sessions:
    t = tier_label(s["start_profit"])
    if s["n"] == 1:
        flash_by_tier[t] += 1
    else:
        multi_by_tier[t] += 1

print(f"\n  {'Tier':<14}  {'Flash':>6}  {'Multi':>6}  {'Flash%':>8}")
print("  " + "-" * 42)
for label, lo, hi in TIERS:
    fl = flash_by_tier[label]
    mu = multi_by_tier[label]
    total = fl + mu
    if total == 0:
        continue
    print(f"  {label:<14}  {fl:>6}  {mu:>6}  {100*fl/total:>7.1f}%")

print()
print("Done.")
