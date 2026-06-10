"""
Execution slippage analysis.

For each arb tick at decision time T:
  - Look up profit_pct at T+EXEC_S (when the bet fills)
  - Slippage = P_T - P_{T+EXEC_S}
  - "Caught" = profit at T+EXEC_S < 0 (arb closed during execution)

Segment by arb age at T (how long the arb had been continuously live before T),
to answer: does waiting for an arb to mature reduce execution slippage risk?
"""

import sqlite3
import bisect
from collections import defaultdict

EXEC_S      = 5        # execution window (seconds to fill a bet)
LOOKUP_WIN  = 3        # tolerance for finding tick near T+EXEC_S
GAP_BREAK_S = 30       # gap that ends an arb episode
START_FROM  = "2026-06-06T12:12:00Z"

AGE_BUCKETS = [
    (0,   5,   "0-5s    (fresh)"),
    (5,   15,  "5-15s"),
    (15,  30,  "15-30s"),
    (30,  60,  "30s-1m"),
    (60,  120, "1-2m"),
    (120, 300, "2-5m"),
    (300, 9e9, "5m+     (persistent)"),
]

con = sqlite3.connect("C:/mbv3/outputs/stream.db")
cur = con.cursor()
cur.execute("""
    SELECT game_team1, game_team2, league,
           CAST(strftime('%s', timestamp) AS INTEGER) AS ts_unix,
           arb_type IS NOT NULL AS is_arb,
           profit_pct
    FROM stream_ticks
    WHERE timestamp >= ?
    ORDER BY game_team1, game_team2, league, timestamp
""", (START_FROM,))

games = defaultdict(list)
n = 0
for t1, t2, lg, ts, is_arb, pct in cur:
    games[(t1, t2, lg)].append((ts, bool(is_arb), float(pct) if pct else 0.0))
    n += 1
print(f"Ticks loaded: {n}  |  Games: {len(games)}")

# ── For each game, annotate each arb tick with its age in the current episode ─
# Then for each arb tick, look up profit at T+EXEC_S

results = {lo: {"total": 0, "caught": 0, "slippage": [], "profit_t": [], "profit_fill": []}
           for lo, hi, _ in AGE_BUCKETS}

for key, ticks in games.items():
    ts_list  = [t[0] for t in ticks]
    episode_start = None  # unix ts when current arb episode began

    for i, (ts, is_arb, pct) in enumerate(ticks):
        if not is_arb:
            episode_start = None
            continue

        # Detect episode boundary
        if episode_start is None:
            episode_start = ts
        elif ts - ticks[i-1][0] > GAP_BREAK_S:
            episode_start = ts

        age = ts - episode_start  # seconds this arb has been continuously live

        # Look up profit at T+EXEC_S
        target = ts + EXEC_S
        idx = bisect.bisect_left(ts_list, target)
        best = None
        for j in (idx, idx - 1):
            if 0 <= j < len(ts_list) and abs(ts_list[j] - target) <= LOOKUP_WIN:
                if best is None or abs(ts_list[j] - target) < abs(ts_list[best] - target):
                    best = j
        if best is None:
            continue  # no data near T+EXEC_S

        _, fill_is_arb, fill_pct = ticks[best]
        fill_profit = fill_pct if fill_is_arb else 0.0

        # Bucket by age
        for lo, hi, _ in AGE_BUCKETS:
            if lo <= age < hi:
                r = results[lo]
                r["total"]    += 1
                r["profit_t"].append(pct)
                r["profit_fill"].append(fill_profit)
                r["slippage"].append(pct - fill_profit)
                if fill_profit <= 0:
                    r["caught"] += 1
                break

# ── Print ─────────────────────────────────────────────────────────────────────
print()
print("=" * 82)
print(f"EXECUTION SLIPPAGE  (exec window = {EXEC_S}s)")
print("=" * 82)
print(f"{'Arb age at decision':22}  {'n':>6}  {'P(caught)':>10}  "
      f"{'Avg profit@T':>13}  {'Avg profit@fill':>15}  {'Avg slip':>9}")
print("-" * 82)

for lo, hi, label in AGE_BUCKETS:
    r = results[lo]
    if not r["total"]:
        continue
    caught_pct = 100 * r["caught"] / r["total"]
    avg_t    = sum(r["profit_t"])    / len(r["profit_t"])
    avg_fill = sum(r["profit_fill"]) / len(r["profit_fill"])
    avg_slip = sum(r["slippage"])    / len(r["slippage"])
    print(f"{label:22}  {r['total']:>6}  {caught_pct:>9.1f}%  "
          f"{avg_t:>12.3f}%  {avg_fill:>14.3f}%  {avg_slip:>8.3f}%")

print()
print("P(caught)    = probability arb closes to zero profit during 5s execution")
print("Avg slip     = avg profit lost between decision and fill (0 if arb persists)")

# ── Overall ───────────────────────────────────────────────────────────────────
all_caught = sum(r["caught"] for r in results.values())
all_total  = sum(r["total"]  for r in results.values())
all_slip   = [s for r in results.values() for s in r["slippage"]]
print()
print(f"Overall: {all_total} decision ticks  |  "
      f"P(caught) = {100*all_caught/all_total:.1f}%  |  "
      f"Avg slippage = {sum(all_slip)/len(all_slip):.3f}%")
