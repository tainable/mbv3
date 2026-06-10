"""
EV analysis for a 5-second execution window.

Model:
  - You see an arb at profit P at time T
  - You finish placing at T + EXEC_S
  - If arb still alive: you earn current profit (improved/flat/worsened)
  - If arb gone:        you lose MISS_LOSS_PCT

Key data limitation: arb_log.jsonl only records odds-change events,
not continuous state.  We therefore define:

  "survived"    = a subsequent log entry exists in the same session
  "disappeared" = this is the session's final entry (arb went quiet)

"survived" includes any odds change that kept the arb alive,
whether it happened 1 second or 25 minutes later.  We adjust
for timing via the intra-session gap distribution.
"""

import json, statistics
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_PATH    = Path("C:/mbv3/outputs/arb_log.jsonl")
SESSION_GAP_S = 30 * 60
EPSILON       = 0.005      # pp — same as stream.py PRINT_THRESHOLD
EXEC_S        = 5.0        # seconds to place a bet
MISS_LOSS_PCT = 0.5        # % loss if arb disappears mid-execution

# ── load ──────────────────────────────────────────────────────────────────────
print("Loading …")
records = []
with open(LOG_PATH) as f:
    for line in f:
        line = line.strip()
        if line:
            try: records.append(json.loads(line))
            except: pass

for r in records:
    r["_ts"] = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
records.sort(key=lambda r: r["_ts"])
print(f"  {len(records):,} records\n")

# ── rebuild sessions ──────────────────────────────────────────────────────────
def arb_key(r):
    return (r["league"], r.get("game_team1",""), r.get("game_team2",""),
            r.get("arb_type",""), r.get("spread"), r.get("total_line"),
            tuple(sorted(r.get("providers") or [])))

by_key = defaultdict(list)
for r in records:
    by_key[arb_key(r)].append((r["_ts"], r.get("profit_pct") or 0.0))
for k in by_key: by_key[k].sort()

sessions = []
for key, entries in by_key.items():
    sess = [entries[0]]
    for i in range(1, len(entries)):
        if (entries[i][0] - entries[i-1][0]).total_seconds() >= SESSION_GAP_S:
            sessions.append({"key": key, "entries": sess})
            sess = [entries[i]]
        else:
            sess.append(entries[i])
    sessions.append({"key": key, "entries": sess})

# ── gap distribution inside sessions ─────────────────────────────────────────
print("=" * 72)
print("1.  INTRA-SESSION GAP DISTRIBUTION (time between consecutive entries)")
print("    Relevant because EXEC_S=5s; gaps show how quickly odds move")
print("=" * 72)

all_gaps = []
for s in sessions:
    ents = s["entries"]
    for i in range(1, len(ents)):
        all_gaps.append((ents[i][0] - ents[i-1][0]).total_seconds())

all_gaps.sort()
n = len(all_gaps)
print(f"\n  Total within-session transitions: {n:,}")
for thresh in [1, 2, 5, 10, 30, 60, 120]:
    cnt = sum(1 for g in all_gaps if g <= thresh)
    print(f"  Gaps <= {thresh:>4}s : {cnt:>6,}  ({100*cnt/n:.1f}%)")

# Fraction of odds updates happening within EXEC_S seconds
# i.e., how often does the *next* update arrive before you finish placing?
within_exec = sum(1 for g in all_gaps if g <= EXEC_S) / n if n else 0
print(f"\n  Within {EXEC_S:.0f}s: {100*within_exec:.1f}% of all consecutive-entry gaps")
print(f"  => when odds change during execution, they typically arrive")
print(f"     within {EXEC_S:.0f}s in {100*within_exec:.1f}% of cases.")

# ── survival analysis by profit tier ─────────────────────────────────────────
# For each entry i in a session:
#   survived   = entry i+1 exists within that session
#   last_entry = no next entry (session ended; arb later disappeared)
#
# Among "survived" entries, record (current_profit → next_profit) delta.
# Among "last_entry" entries, the arb disappeared — we count this as a loss.
#
# To adjust for the 5-second window: a "survived" transition where
# the gap > EXEC_S means the arb *definitely* persisted past T+5s.
# A "survived" transition where gap <= EXEC_S means there was an odds
# change during execution; the arb survived that change.
# We split these to show both views.

TIERS = [
    ("0.00–0.10%",  0.00, 0.10),
    ("0.10–0.25%",  0.10, 0.25),
    ("0.25–0.50%",  0.25, 0.50),
    ("0.50–1.00%",  0.50, 1.00),
    ("1.00–1.50%",  1.00, 1.50),
    ("1.50–2.00%",  1.50, 2.00),
    ("> 2.00%",     2.00, 999.0),
]

def tier_of(pct):
    for label, lo, hi in TIERS:
        if lo <= pct < hi: return label
    return "> 2.00%"

class TierStats:
    def __init__(self):
        self.n_survived_slow = 0    # survived, gap > EXEC_S (arb definitely alive at T+5s)
        self.n_survived_fast = 0    # survived, gap <= EXEC_S (odds changed during window)
        self.n_disappeared   = 0    # last entry in session → arb gone
        self.next_profits    = []   # profit at next entry (survived cases)
        self.gaps            = []

stats = defaultdict(TierStats)

for s in sessions:
    ents = s["entries"]
    for i, (ts, pct) in enumerate(ents):
        t = tier_of(pct)
        st = stats[t]
        if i < len(ents) - 1:
            next_ts, next_pct = ents[i+1]
            gap = (next_ts - ts).total_seconds()
            st.gaps.append(gap)
            st.next_profits.append(next_pct)
            if gap <= EXEC_S:
                st.n_survived_fast += 1
            else:
                st.n_survived_slow += 1
        else:
            st.n_disappeared += 1

# ── EV calculation ────────────────────────────────────────────────────────────
# EV = P(disappeared) * (-MISS_LOSS_PCT)
#    + P(survived)    * E[next_profit]
#
# Two scenarios:
#   "Slow gaps" view: gap > EXEC_S — arb definitely alive at T+5s, profit = current_pct
#   "Fast gaps" view: gap <= EXEC_S — odds changed during window; profit = next_pct (if survived)
#
# For simplicity: use ALL survived entries' next_profit as E[profit | survive].

print()
print("=" * 72)
print("2.  SURVIVAL RATE AND EV BY CURRENT PROFIT TIER")
print(f"    EXEC_S = {EXEC_S}s  |  Miss loss = {MISS_LOSS_PCT}%")
print("=" * 72)

print(f"\n  {'Tier':<14}  {'N':>6}  {'Survive%':>9}  {'E[profit|surv]':>15}  {'EV':>8}  {'Break-even?':>12}")
print("  " + "-" * 72)

evs = {}
for label, lo, hi in TIERS:
    st = stats[label]
    n_total = st.n_survived_slow + st.n_survived_fast + st.n_disappeared
    if n_total == 0:
        continue
    n_surv = st.n_survived_slow + st.n_survived_fast
    p_surv = n_surv / n_total
    p_gone = st.n_disappeared / n_total
    e_profit = statistics.mean(st.next_profits) if st.next_profits else (lo + hi) / 2
    ev = p_surv * e_profit + p_gone * (-MISS_LOSS_PCT)
    evs[label] = ev
    breakeven = "YES" if ev > 0 else "NO"
    print(f"  {label:<14}  {n_total:>6,}  {100*p_surv:>8.1f}%  "
          f"{e_profit:>14.3f}%  {ev:>7.3f}%  {breakeven:>12}")

# ── breakeven minimum profit ──────────────────────────────────────────────────
# Solve analytically for each tier: EV = 0
# p_surv * P + p_gone * (-0.5) = 0  → P = p_gone * 0.5 / p_surv
# (assuming profit stays flat if survived)

print()
print("=" * 72)
print("3.  BREAKEVEN MINIMUM PROFIT  (assuming profit stays flat if arb survives)")
print("=" * 72)

print(f"\n  {'Tier':<14}  {'Survive%':>9}  {'Disappear%':>11}  {'Min profit to bet':>18}")
print("  " + "-" * 58)
for label, lo, hi in TIERS:
    st = stats[label]
    n_total = st.n_survived_slow + st.n_survived_fast + st.n_disappeared
    if n_total == 0: continue
    p_surv = (st.n_survived_slow + st.n_survived_fast) / n_total
    p_gone = st.n_disappeared / n_total
    if p_surv > 0:
        min_p = p_gone * MISS_LOSS_PCT / p_surv
    else:
        min_p = float("inf")
    print(f"  {label:<14}  {100*p_surv:>8.1f}%  {100*p_gone:>10.1f}%  {min_p:>17.3f}%")

# ── how often does a worsened arb still leave profit? ───────────────────────
print()
print("=" * 72)
print("4.  WHEN ARBS WORSEN: DO THEY STAY POSITIVE?")
print("    (Worsening != disappearing; the arb might still be profitable)")
print("=" * 72)

tier_worsen = defaultdict(lambda: {"still_pos": 0, "gone_neg": 0, "deltas": []})

for s in sessions:
    ents = s["entries"]
    for i in range(1, len(ents)):
        prev_pct = ents[i-1][1]
        curr_pct = ents[i][1]
        delta    = curr_pct - prev_pct
        if delta < -EPSILON:
            t = tier_of(prev_pct)
            tier_worsen[t]["deltas"].append(delta)
            if curr_pct > 0:
                tier_worsen[t]["still_pos"] += 1
            else:
                tier_worsen[t]["gone_neg"] += 1

print(f"\n  {'Tier':<14}  {'Worsen events':>14}  {'Still > 0%':>11}  {'Went to 0':>10}  {'Med delta':>10}")
print("  " + "-" * 66)
for label, lo, hi in TIERS:
    d = tier_worsen[label]
    n = d["still_pos"] + d["gone_neg"]
    if n == 0: continue
    med = statistics.median(d["deltas"])
    print(f"  {label:<14}  {n:>14}  "
          f"{100*d['still_pos']/n:>10.1f}%  "
          f"{100*d['gone_neg']/n:>9.1f}%  "
          f"{med:>9.3f}pp")

# ── optimal threshold summary ─────────────────────────────────────────────────
print()
print("=" * 72)
print("5.  OPTIMAL STRATEGY SUMMARY")
print("=" * 72)

# Use the actual EV numbers from section 2
print(f"\n  Execution time: {EXEC_S}s  |  Miss loss: {MISS_LOSS_PCT}%\n")
print("  EV by profit tier:")
for label, lo, hi in TIERS:
    if label not in evs: continue
    ev = evs[label]
    bar = "+" * int(abs(ev) * 40) if ev > 0 else "-" * int(abs(ev) * 40)
    sign = "+" if ev > 0 else ""
    print(f"    {label:<14}  EV = {sign}{ev:.3f}%  {'[BET]' if ev > 0 else '[SKIP]':>8}  {bar}")

print()
print("  Rule of thumb:")
# Find the crossover tier
pos_tiers = [l for l, lo, hi in TIERS if l in evs and evs[l] > 0]
neg_tiers = [l for l, lo, hi in TIERS if l in evs and evs[l] <= 0]
if pos_tiers and neg_tiers:
    print(f"    Bet:  profit tiers {pos_tiers[0]} through {pos_tiers[-1]}")
    print(f"    Skip: profit tiers {neg_tiers[0]} and above")
elif pos_tiers:
    print(f"    All displayed tiers have positive EV — bet everything above {pos_tiers[0]}")

# The "disappear rate" in different tiers shows that higher profit = higher disappear rate
# Compute the true breakeven using the empirical p_surv from each tier
print()
print("  Exact breakeven (EV = 0, holding profit flat):")
all_min_ps = []
for label, lo, hi in TIERS:
    st = stats[label]
    n_total = st.n_survived_slow + st.n_survived_fast + st.n_disappeared
    if n_total == 0: continue
    p_surv = (st.n_survived_slow + st.n_survived_fast) / n_total
    p_gone = st.n_disappeared / n_total
    if p_surv > 0:
        min_p = p_gone * MISS_LOSS_PCT / p_surv
        all_min_ps.append((label, lo, hi, min_p, p_surv))
        print(f"    {label:<14}  need profit >= {min_p:.3f}%  (survive rate {100*p_surv:.1f}%)")

print()
print("Done.")
