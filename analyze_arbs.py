"""
Arb lifetime and price-movement analysis for arb_log.jsonl.

Key caveat: entries are only written when odds change on a dirty tick,
so gaps between entries don't mean the arb disappeared — they may just
mean odds were stable.  We therefore distinguish:
  - observed lifetime: span from first to last entry in a session
  - gap threshold: gap >= SESSION_GAP_MINUTES is treated as a session break
"""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import statistics

LOG_PATH = Path("C:/mbv3/outputs/arb_log.jsonl")

# Two consecutive entries for the same arb key separated by more than this
# are treated as separate "sessions" (arb disappeared and re-appeared).
SESSION_GAP_MINUTES = 30

# ─────────────────────────────────────────────────────────────────────────────
# Load data
# ─────────────────────────────────────────────────────────────────────────────

print("Loading arb_log.jsonl …")
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

print(f"  Loaded {len(records):,} records")

# Parse timestamps
for r in records:
    r["_ts"] = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))

records.sort(key=lambda r: r["_ts"])

first_ts = records[0]["_ts"]
last_ts  = records[-1]["_ts"]
span_h   = (last_ts - first_ts).total_seconds() / 3600
print(f"  Span: {first_ts.strftime('%Y-%m-%d %H:%M')} → {last_ts.strftime('%Y-%m-%d %H:%M')} "
      f"({span_h:.1f} h)")

# ─────────────────────────────────────────────────────────────────────────────
# Build arb key → sorted list of (ts, profit_pct) entries
# ─────────────────────────────────────────────────────────────────────────────

def arb_key(r):
    providers = tuple(sorted(r.get("providers") or []))
    return (
        r["league"],
        r.get("game_team1", ""),
        r.get("game_team2", ""),
        r.get("arb_type", ""),
        r.get("spread"),
        r.get("total_line"),
        providers,
    )

by_key = defaultdict(list)
for r in records:
    by_key[arb_key(r)].append((r["_ts"], r.get("profit_pct", 0.0) or 0.0))

# Sort each series by time (should already be sorted, but just in case)
for k in by_key:
    by_key[k].sort()

print(f"  Unique arb keys: {len(by_key):,}")

# ─────────────────────────────────────────────────────────────────────────────
# Split each key's timeline into sessions
# ─────────────────────────────────────────────────────────────────────────────

GAP_S = SESSION_GAP_MINUTES * 60

sessions = []  # list of dicts

for key, entries in by_key.items():
    session_start = None
    session_entries = []

    for i, (ts, pct) in enumerate(entries):
        if session_start is None:
            session_start = ts
            session_entries = [(ts, pct)]
        else:
            gap_s = (ts - entries[i-1][0]).total_seconds()
            if gap_s >= GAP_S:
                # Flush current session
                sessions.append({
                    "key":          key,
                    "start":        session_start,
                    "end":          session_entries[-1][0],
                    "entries":      session_entries,
                    "duration_s":   (session_entries[-1][0] - session_start).total_seconds(),
                })
                # Start new session
                session_start   = ts
                session_entries = [(ts, pct)]
            else:
                session_entries.append((ts, pct))

    # Flush final session
    if session_entries:
        sessions.append({
            "key":          key,
            "start":        session_start,
            "end":          session_entries[-1][0],
            "entries":      session_entries,
            "duration_s":   (session_entries[-1][0] - session_start).total_seconds(),
        })

print(f"  Sessions (gap ≥ {SESSION_GAP_MINUTES} min): {len(sessions):,}")

# ─────────────────────────────────────────────────────────────────────────────
# Helper: print percentile table
# ─────────────────────────────────────────────────────────────────────────────

def pct_table(values, label, unit="s", divisor=1):
    if not values:
        print(f"  {label}: no data")
        return
    v = sorted(v / divisor for v in values)
    n = len(v)
    pcts = [0, 10, 25, 50, 75, 90, 95, 99, 100]
    row  = "  ".join(f"p{p}={v[min(int(p/100*n), n-1)]:.1f}" for p in pcts)
    print(f"  {label} (n={n:,}) — mean={statistics.mean(v):.1f}{unit}  median={statistics.median(v):.1f}{unit}")
    print(f"    {row} [{unit}]")

# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS 1: Session duration (arb lifetime)
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 70)
print("1.  ARB SESSION DURATION  (observed lifetime)")
print(f"    Sessions with only 1 odds-change entry have duration=0; they")
print(f"    are counted separately.")
print("═" * 70)

dur_all  = [s["duration_s"] for s in sessions]
dur_gt0  = [d for d in dur_all if d > 0]
dur_zero = [d for d in dur_all if d == 0]

print(f"\n  Single-entry sessions (duration=0):  {len(dur_zero):,}  "
      f"({100*len(dur_zero)/len(dur_all):.1f}% of sessions)")
print(f"  Multi-entry sessions:                {len(dur_gt0):,}")
print()
pct_table(dur_all,  "All sessions",   unit=" min", divisor=60)
print()
pct_table(dur_gt0,  "Multi-entry sessions", unit=" min", divisor=60)

# Bucketed histogram
buckets = [
    ("< 1 min",   0,    60),
    ("1–5 min",   60,   300),
    ("5–15 min",  300,  900),
    ("15–30 min", 900,  1800),
    ("30–60 min", 1800, 3600),
    ("> 60 min",  3600, float("inf")),
]
print()
print("  Duration distribution:")
total = len(dur_all)
for label, lo, hi in buckets:
    cnt = sum(1 for d in dur_all if lo <= d < hi)
    bar = "█" * (cnt * 40 // total) if total else ""
    print(f"    {label:>12}  {cnt:>5,}  ({100*cnt/total:4.1f}%)  {bar}")

# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS 2: Number of entries per session (how many odds-change events)
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 70)
print("2.  ODDS-CHANGE EVENTS PER SESSION")
print("═" * 70)

n_entries = [len(s["entries"]) for s in sessions]
pct_table(n_entries, "Entries per session", unit="")

# breakdown
for threshold in [1, 2, 3, 5, 10]:
    cnt = sum(1 for n in n_entries if n >= threshold)
    print(f"  Sessions with ≥ {threshold:>2} entries: {cnt:>5,}  ({100*cnt/len(n_entries):.1f}%)")

# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS 3: Intra-session gap distribution
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 70)
print("3.  GAPS BETWEEN CONSECUTIVE ENTRIES (within sessions)")
print("═" * 70)

intra_gaps = []
for s in sessions:
    ents = s["entries"]
    for i in range(1, len(ents)):
        gap = (ents[i][0] - ents[i-1][0]).total_seconds()
        intra_gaps.append(gap)

pct_table(intra_gaps, "Intra-session gaps", unit=" min", divisor=60)

gap_buckets = [
    ("< 10 s",    0,    10),
    ("10–60 s",   10,   60),
    ("1–5 min",   60,   300),
    ("5–15 min",  300,  900),
    ("15–30 min", 900,  1800),
]
total_g = len(intra_gaps)
print()
print("  Gap distribution:")
for label, lo, hi in gap_buckets:
    cnt = sum(1 for g in intra_gaps if lo <= g < hi)
    bar = "█" * (cnt * 40 // total_g) if total_g else ""
    print(f"    {label:>12}  {cnt:>5,}  ({100*cnt/total_g:4.1f}%)  {bar}")

# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS 4: Profit direction within sessions (improve / worsen / flat)
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 70)
print("4.  PROFIT DIRECTION BETWEEN CONSECUTIVE ENTRIES")
print("    (within a session — each entry is an odds-change event)")
print("═" * 70)

EPSILON = 0.005  # pp — same threshold stream.py uses for _PRINT_THRESHOLD

n_improved = 0
n_worsened = 0
n_flat     = 0
delta_pcts = []
time_flat  = []   # seconds spent flat between two entries

for s in sessions:
    ents = s["entries"]
    for i in range(1, len(ents)):
        prev_ts, prev_pct = ents[i-1]
        curr_ts, curr_pct = ents[i]
        delta = curr_pct - prev_pct
        delta_pcts.append(delta)
        gap_s = (curr_ts - prev_ts).total_seconds()
        if abs(delta) < EPSILON:
            n_flat += 1
            time_flat.append(gap_s)
        elif delta > 0:
            n_improved += 1
        else:
            n_worsened += 1

total_transitions = n_improved + n_worsened + n_flat
if total_transitions:
    print(f"\n  Total transitions (between consecutive entries): {total_transitions:,}")
    print(f"  Improved  (Δ ≥ +{EPSILON:.3f} pp): {n_improved:>5,}  ({100*n_improved/total_transitions:.1f}%)")
    print(f"  Worsened  (Δ ≤ -{EPSILON:.3f} pp): {n_worsened:>5,}  ({100*n_worsened/total_transitions:.1f}%)")
    print(f"  Flat      (|Δ| < {EPSILON:.3f} pp): {n_flat:>5,}  ({100*n_flat/total_transitions:.1f}%)")

    print()
    print("  Magnitude of changes (improved + worsened only):")
    non_flat = [abs(d) for d in delta_pcts if abs(d) >= EPSILON]
    pct_table(non_flat, "  |Δ profit_pct|", unit=" pp")

    print()
    print("  Time spent flat (same price level, gap between entries):")
    pct_table(time_flat, "  Flat gap", unit=" min", divisor=60)

# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS 5: Time at same profit level (flat runs within a session)
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 70)
print("5.  TIME AT SAME PROFIT LEVEL (consecutive flat entries = 'price stable')")
print("═" * 70)

flat_run_durations = []  # total seconds of each flat run

for s in sessions:
    ents = s["entries"]
    run_start = ents[0][0]
    run_pct   = ents[0][1]
    run_len   = 1
    for i in range(1, len(ents)):
        curr_ts, curr_pct = ents[i]
        if abs(curr_pct - run_pct) < EPSILON:
            run_len += 1
        else:
            if run_len >= 2:
                dur = (ents[i-1][0] - run_start).total_seconds()
                flat_run_durations.append(dur)
            run_start = curr_ts
            run_pct   = curr_pct
            run_len   = 1
    if run_len >= 2:
        dur = (ents[-1][0] - run_start).total_seconds()
        flat_run_durations.append(dur)

pct_table(flat_run_durations, "Flat-price run duration", unit=" min", divisor=60)

flat_buckets = [
    ("< 1 min",    0,    60),
    ("1–5 min",    60,   300),
    ("5–15 min",   300,  900),
    ("15–30 min",  900,  1800),
    ("> 30 min",   1800, float("inf")),
]
total_fr = len(flat_run_durations)
if total_fr:
    print()
    print("  Flat-run distribution:")
    for label, lo, hi in flat_buckets:
        cnt = sum(1 for d in flat_run_durations if lo <= d < hi)
        bar = "█" * (cnt * 40 // total_fr) if total_fr else ""
        print(f"    {label:>12}  {cnt:>5,}  ({100*cnt/total_fr:4.1f}%)  {bar}")

# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS 6: By league
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 70)
print("6.  BY LEAGUE  (session count, median duration, improve/worsen ratio)")
print("═" * 70)

league_stats = defaultdict(lambda: {"sessions": [], "transitions": {"up": 0, "down": 0, "flat": 0}})

for s in sessions:
    league = s["key"][0]
    league_stats[league]["sessions"].append(s["duration_s"])
    ents = s["entries"]
    for i in range(1, len(ents)):
        delta = ents[i][1] - ents[i-1][1]
        if abs(delta) < EPSILON:
            league_stats[league]["transitions"]["flat"] += 1
        elif delta > 0:
            league_stats[league]["transitions"]["up"] += 1
        else:
            league_stats[league]["transitions"]["down"] += 1

print(f"\n  {'League':<20} {'Sessions':>8}  {'Med dur':>8}  {'Improved':>9}  {'Worsened':>9}  {'Flat':>6}")
print("  " + "-" * 68)
for league, data in sorted(league_stats.items(), key=lambda x: -len(x[1]["sessions"])):
    s_list = data["sessions"]
    t = data["transitions"]
    total_t = t["up"] + t["down"] + t["flat"] or 1
    med = statistics.median(s_list) / 60
    print(f"  {league:<20} {len(s_list):>8,}  {med:>7.1f}m  "
          f"{100*t['up']/total_t:>8.1f}%  "
          f"{100*t['down']/total_t:>8.1f}%  "
          f"{100*t['flat']/total_t:>5.1f}%")

# ─────────────────────────────────────────────────────────────────────────────
# ANALYSIS 7: Most durable arbs
# ─────────────────────────────────────────────────────────────────────────────

print()
print("═" * 70)
print("7.  TOP 15 LONGEST-LIVED ARB SESSIONS")
print("═" * 70)

top_sessions = sorted(sessions, key=lambda s: -s["duration_s"])[:15]
for i, s in enumerate(top_sessions, 1):
    k = s["key"]
    dur_m = s["duration_s"] / 60
    n = len(s["entries"])
    pcts = [e[1] for e in s["entries"]]
    print(f"  {i:>2}. {k[0]:<18}  {k[1][:18]:<18} vs {k[2][:18]:<18}  "
          f"{dur_m:>7.1f} min  {n:>3} entries  "
          f"profit {min(pcts):.2f}%–{max(pcts):.2f}%")

print()
print("Done.")
