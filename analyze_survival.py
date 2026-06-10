"""
Survival curve analysis for arb opportunities in stream.db.
"""

import sqlite3
import bisect
from collections import defaultdict

DELAYS_S    = [5, 15, 30, 60, 120, 300]
GAP_BREAK_S = 30
LOOKUP_WIN  = 4
START_FROM  = "2026-06-06T12:12:00Z"

con = sqlite3.connect("C:/mbv3/outputs/stream.db")

# ── Load minimal columns, filtered to window of interest ──────────────────────
# We need every tick (arb or not) to detect when arbs close, but only 4 columns.
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
print(f"Ticks loaded: {n}  |  Unique games: {len(games)}")

# ── Find arb onsets ───────────────────────────────────────────────────────────
onsets = []  # (onset_ts, onset_profit, ticks_for_game)
for key, ticks in games.items():
    prev_arb, prev_ts = False, None
    for ts, is_arb, pct in ticks:
        gap = (ts - prev_ts) if prev_ts else 999
        if is_arb and (not prev_arb or gap > GAP_BREAK_S):
            onsets.append((ts, pct, ticks))
        prev_arb = is_arb
        prev_ts  = ts

print(f"Arb onsets:   {len(onsets)}")

# ── Survival analysis ─────────────────────────────────────────────────────────
results = {d: dict(survived=0, died=0, ambiguous=0,
                   profits=[], onset_profits=[]) for d in DELAYS_S}

for onset_ts, onset_profit, ticks in onsets:
    ts_list = [t[0] for t in ticks]
    for d in DELAYS_S:
        target = onset_ts + d
        idx = bisect.bisect_left(ts_list, target)
        best = None
        for i in (idx, idx - 1):
            if 0 <= i < len(ts_list) and abs(ts_list[i] - target) <= LOOKUP_WIN:
                if best is None or abs(ts_list[i] - target) < abs(ts_list[best] - target):
                    best = i
        if best is None:
            results[d]["ambiguous"] += 1
            continue
        _, is_arb, pct = ticks[best]
        if is_arb:
            results[d]["survived"] += 1
            results[d]["profits"].append(pct)
            results[d]["onset_profits"].append(onset_profit)
        else:
            results[d]["died"] += 1

# ── Profit-weighted miss rate ─────────────────────────────────────────────────
def arb_profit_seconds(ticks, onset_ts, cutoff_ts):
    total, prev_ts, prev_pct = 0.0, None, None
    for ts, is_arb, pct in ticks:
        if ts < onset_ts:
            continue
        if not is_arb or (prev_ts and ts - prev_ts > GAP_BREAK_S):
            break
        eff_ts = min(ts, cutoff_ts)
        if prev_ts is not None:
            total += prev_pct * (eff_ts - prev_ts)
        prev_ts, prev_pct = eff_ts, pct
        if eff_ts >= cutoff_ts:
            break
    return total

total_ps = 0.0
missed   = {d: 0.0 for d in DELAYS_S}

for onset_ts, _, ticks in onsets:
    # episode end = last consecutive arb tick
    end_ts = onset_ts
    for ts, is_arb, pct in ticks:
        if ts < onset_ts: continue
        if not is_arb or ts - end_ts > GAP_BREAK_S: break
        end_ts = ts
    ep = arb_profit_seconds(ticks, onset_ts, end_ts)
    total_ps += ep
    for d in DELAYS_S:
        missed[d] += arb_profit_seconds(ticks, onset_ts, onset_ts + d)

# ── Print results ─────────────────────────────────────────────────────────────
print()
print("=" * 72)
print("ARB SURVIVAL CURVE")
print("=" * 72)
print(f"{'Delay':>6}  {'Surv':>5}  {'Died':>5}  {'Ambig':>5}  "
      f"{'Surv%':>6}  {'Profit@T':>9}  {'Profit@T+D':>11}")
print("-" * 72)
for d in DELAYS_S:
    r   = results[d]
    tot = r["survived"] + r["died"]
    if not tot: continue
    sp  = 100 * r["survived"] / tot
    at  = sum(r["onset_profits"]) / len(r["onset_profits"]) if r["onset_profits"] else 0
    atd = sum(r["profits"])       / len(r["profits"])       if r["profits"]       else 0
    lbl = f"{d}s" if d < 60 else f"{d//60}m"
    print(f"{lbl:>6}  {r['survived']:>5}  {r['died']:>5}  {r['ambiguous']:>5}  "
          f"{sp:>5.1f}%  {at:>8.3f}%  {atd:>10.3f}%")

print()
print("Surv%       = % of resolvable onsets still live at T+delay")
print("Profit@T    = avg profit at onset (for onsets with a tick near T+delay)")
print("Profit@T+D  = avg profit at T+delay (survivors only)")

print()
print("=" * 72)
print("PROFIT-WEIGHTED MISS RATE")
print("=" * 72)
for d in DELAYS_S:
    pct = 100 * missed[d] / total_ps if total_ps else 0
    lbl = f"{d}s" if d < 60 else f"{d//60}m"
    print(f"  Wait {lbl:>4}: miss {pct:>5.1f}% of total profit-exposure")
print()
print("(profit-exposure = sum of profit_pct x seconds across all arb episodes)")
