"""
Analyse arb_log.jsonl to measure how long arb opportunities last.

The stream tick is ~10-12s. Entries within a tick are <5s apart.
Gaps >30s indicate a restart/pause and are treated as session breaks.
"""

import json
from collections import defaultdict
from datetime import datetime

entries = []
with open("C:/mbv3/outputs/arb_log.jsonl") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            pass

for e in entries:
    ts = e["timestamp"]
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    e["_dt"] = datetime.fromisoformat(ts)

entries.sort(key=lambda e: e["_dt"])
timestamps = [e["_dt"] for e in entries]
print(f"Total log entries: {len(entries)}")
print(f"Date range: {timestamps[0]} to {timestamps[-1]}")

# ------------------------------------------------------------------
# Split into ticks. A tick boundary occurs when the gap to the
# previous entry is >= 5s (entries within a tick are <2s apart).
# ------------------------------------------------------------------
TICK_BREAK = 5.0  # seconds

ticks = []          # list of (tick_time, set_of_arb_keys)
current_tick_time = entries[0]["_dt"]
current_tick_keys = set()

for i, e in enumerate(entries):
    if i > 0:
        gap = (e["_dt"] - entries[i - 1]["_dt"]).total_seconds()
        if gap >= TICK_BREAK:
            ticks.append((current_tick_time, current_tick_keys))
            current_tick_time = e["_dt"]
            current_tick_keys = set()

    def arb_key(e):
        providers = tuple(sorted(e.get("providers", [])))
        return (
            e.get("league", ""),
            e.get("team1", ""),
            e.get("team2", ""),
            e.get("spread"),
            e.get("total_line"),
            e.get("arb_type", ""),
            providers,
        )

    current_tick_keys.add(arb_key(e))

ticks.append((current_tick_time, current_tick_keys))

print(f"\nTick count: {len(ticks)}")

tick_times = [t[0] for t in ticks]
inter_tick_gaps = [
    (tick_times[i + 1] - tick_times[i]).total_seconds()
    for i in range(len(tick_times) - 1)
]
normal_gaps = [g for g in inter_tick_gaps if g < 30]
if normal_gaps:
    normal_gaps_sorted = sorted(normal_gaps)
    median_tick = normal_gaps_sorted[len(normal_gaps_sorted) // 2]
    print(f"Median tick interval (gaps < 30s): {median_tick:.1f}s")
else:
    median_tick = 10.0
    print(f"Using default tick interval: {median_tick:.1f}s")

restart_gaps = [(i, g) for i, g in enumerate(inter_tick_gaps) if g > 30]
print(f"Session breaks (gaps > 30s): {len(restart_gaps)}")

# ------------------------------------------------------------------
# For each unique arb key, find consecutive tick runs (broken by
# session restarts or absence from a tick).
# A restart gap breaks a run even if the arb appears on both sides.
# ------------------------------------------------------------------
SESSION_BREAK = 30.0  # seconds

# Build arb presence: dict of arb_key -> sorted list of tick indices
arb_tick_presence = defaultdict(list)
for tick_idx, (tick_time, tick_keys) in enumerate(ticks):
    for k in tick_keys:
        arb_tick_presence[k].append(tick_idx)

print(f"Unique arb keys: {len(arb_tick_presence)}")

# Build a set of tick indices that are AFTER a session break
restart_tick_indices = set()
for tick_idx, gap in restart_gaps:
    restart_tick_indices.add(tick_idx + 1)

# For each arb key, find its consecutive runs (split by missing ticks or restarts)
all_run_durations_s = []
still_live_count = 0
last_tick_idx = len(ticks) - 1

for k, tick_indices in arb_tick_presence.items():
    # Split into consecutive runs
    runs = []
    current_run = [tick_indices[0]]
    for i in range(1, len(tick_indices)):
        prev_idx = tick_indices[i - 1]
        curr_idx = tick_indices[i]
        gap_s = (tick_times[curr_idx] - tick_times[prev_idx]).total_seconds()
        # Break run if: non-consecutive ticks OR session restart gap
        if curr_idx != prev_idx + 1 or gap_s > SESSION_BREAK:
            runs.append(current_run)
            current_run = [curr_idx]
        else:
            current_run.append(curr_idx)
    runs.append(current_run)

    for run in runs:
        first_idx = run[0]
        last_idx = run[-1]

        # Still live in final tick?
        if last_idx == last_tick_idx:
            still_live_count += 1
            continue

        first_time = tick_times[first_idx]
        last_time = tick_times[last_idx]
        duration_s = (last_time - first_time).total_seconds()
        all_run_durations_s.append(duration_s)

print(f"Still-live runs (excluded): {still_live_count}")
print(f"Closed arb runs analysed:   {len(all_run_durations_s)}")

if not all_run_durations_s:
    print("No closed arb runs to analyse.")
    exit()

all_run_durations_s.sort()
total = len(all_run_durations_s)

print(f"\n{'='*55}")
print("ARB DURATION DISTRIBUTION  (time first to last seen)")
print(f"{'='*55}")
print(f"  NOTE: tick interval ~ {median_tick:.0f}s, so '0s' means seen in")
print(f"  exactly one tick. Add ~{median_tick:.0f}s to get max possible duration.")
print(f"{'='*55}")
print(f"{'Threshold':>15}  {'New in bucket':>13}  {'Cumulative':>10}  {'%':>6}")
print("-" * 55)

thresholds = [
    (5,   "<= 5s"),
    (10,  "<= 10s"),
    (15,  "<= 15s"),
    (20,  "<= 20s"),
    (30,  "<= 30s"),
    (60,  "<= 1m"),
    (120, "<= 2m"),
    (300, "<= 5m"),
    (600, "<= 10m"),
    (1800,"<= 30m"),
    (3600,"<= 1h"),
]

prev_count = 0
for t, label in thresholds:
    count = sum(1 for d in all_run_durations_s if d <= t)
    new = count - prev_count
    pct = 100.0 * count / total
    print(f"{label:>15}  {new:>13}  {count:>10}  {pct:>5.1f}%")
    prev_count = count

remaining = total - prev_count
print(f"{'> 1h':>15}  {remaining:>13}  {total:>10}  100.0%")

print(f"\nSummary:")
print(f"  Total closed runs:  {total}")
print(f"  Min duration:       {all_run_durations_s[0]:.0f}s")
print(f"  p25:                {all_run_durations_s[int(0.25*total)]:.0f}s")
print(f"  Median (p50):       {all_run_durations_s[total//2]:.0f}s")
print(f"  p75:                {all_run_durations_s[int(0.75*total)]:.0f}s")
print(f"  p90:                {all_run_durations_s[int(0.90*total)]:.0f}s")
print(f"  p95:                {all_run_durations_s[int(0.95*total)]:.0f}s")
print(f"  Mean:               {sum(all_run_durations_s)/total:.0f}s")
print(f"  Max duration:       {all_run_durations_s[-1]:.0f}s  ({all_run_durations_s[-1]/3600:.1f}h)")
