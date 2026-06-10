"""
Strategy analysis: optimal profit threshold and execution approach.

The entry-level survival rate (99.5%) is biased: long sessions (hundreds
of entries) swamp the count.  The relevant question is:

  "I just saw an arb alert (first detection).  Will it survive 5 seconds?"

This depends on the SESSION-LEVEL flash rate:
  - 26% of sessions are single-entry ('flash arbs')
  - 74% are multi-entry (arb persisted through multiple odds updates)

Flash arbs disappear at an unknown time after their single log entry.
We bound the analysis conservatively (flash arbs die immediately) and
optimistically (flash arbs persist as long as needed) and show both.

We also analyse a 'wait-for-confirm' strategy: delay execution 1-2 seconds
to see if the arb fires again — eliminating flash arbs at the cost of
reduced execution window.
"""

import json, statistics, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

LOG_PATH      = Path("C:/mbv3/outputs/arb_log.jsonl")
SESSION_GAP_S = 30 * 60
EPSILON       = 0.005
EXEC_S        = 5.0
MISS_PCT      = 0.5    # % loss if arb disappears mid-execution

# ── load + sessions ────────────────────────────────────────────────────────────
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
            sessions.append({"key": key, "entries": sess,
                             "start_profit": sess[0][1]})
            sess = [entries[i]]
        else:
            sess.append(entries[i])
    sessions.append({"key": key, "entries": sess,
                     "start_profit": sess[0][1]})

# ── gap distribution (needed for confirmation strategy) ─────────────────────
all_gaps = []
for s in sessions:
    ents = s["entries"]
    for i in range(1, len(ents)):
        all_gaps.append((ents[i][0] - ents[i-1][0]).total_seconds())
all_gaps.sort()
n_gaps = len(all_gaps)

def pct_within(threshold):
    return sum(1 for g in all_gaps if g <= threshold) / n_gaps

# ── session-level stats by profit tier ─────────────────────────────────────
TIERS = [
    ("0.00–0.10%",  0.00, 0.10),
    ("0.10–0.25%",  0.10, 0.25),
    ("0.25–0.50%",  0.25, 0.50),
    ("0.50–1.00%",  0.50, 1.00),
    ("1.00–2.00%",  1.00, 2.00),
    ("> 2.00%",     2.00, 999.0),
]
def tier_of(p):
    for label, lo, hi in TIERS:
        if lo <= p < hi: return label, (lo+hi)/2
    return "> 2.00%", 2.5

tier_sessions  = defaultdict(list)  # list of session dicts
for s in sessions:
    label, mid = tier_of(s["start_profit"])
    tier_sessions[label].append(s)

# ── EV model ────────────────────────────────────────────────────────────────
# At first detection:
#   P(flash arb)  = n_flash / n_sessions   (session is single-entry)
#   P(multi arb)  = 1 - P(flash)
#
# Conservative: flash arbs die before execution completes → loss of MISS_PCT
# Optimistic:   flash arbs survive → earn start_profit
#
# Multi-entry arbs: we know they survived at least one more odds change.
# E[profit | multi] = mean of session's NEXT entry profit (profit after first odds update)
# P(survive 5s | multi) ≈ 1 (since next entry almost always arrives within 5s and survives)
#
# EV_conservative = p_flash * (-MISS_PCT) + p_multi * E[profit_after_first_update]
# EV_optimistic   = p_flash * start_profit_mean + p_multi * E[profit_after_first_update]

print("=" * 74)
print("1.  SESSION-LEVEL FLASH RATE AND EV MODEL BY STARTING PROFIT TIER")
print("=" * 74)
print(f"\n  EXEC_S = {EXEC_S}s | Loss if missed = {MISS_PCT}%")
print(f"\n  {'Tier':<14}  {'Sessions':>8}  {'Flash%':>7}  "
      f"{'EV (conserv)':>13}  {'EV (optimist)':>14}  {'Bet?':>6}")
print("  " + "-" * 70)

ev_table = {}   # label -> (ev_conservative, ev_optimistic, p_flash, e_multi_profit)
for label, lo, hi in TIERS:
    sess_list = tier_sessions[label]
    if not sess_list: continue
    n = len(sess_list)

    flash = [s for s in sess_list if len(s["entries"]) == 1]
    multi = [s for s in sess_list if len(s["entries"]) > 1]

    p_flash = len(flash) / n
    p_multi = len(multi) / n

    # E[profit | multi]: use profit at second entry (first odds update after detection)
    # This is what you actually earn if the arb survives one odds cycle
    multi_next_profits = [s["entries"][1][1] for s in multi if len(s["entries"]) >= 2]
    e_multi = statistics.mean(multi_next_profits) if multi_next_profits else (lo + hi) / 2

    flash_start_profits = [s["start_profit"] for s in flash]
    e_flash_start = statistics.mean(flash_start_profits) if flash_start_profits else (lo + hi) / 2

    ev_cons  = p_flash * (-MISS_PCT) + p_multi * e_multi
    ev_opt   = p_flash * e_flash_start + p_multi * e_multi

    ev_table[label] = (ev_cons, ev_opt, p_flash, e_multi)

    bet = "YES" if ev_cons > 0 else "MAYBE" if ev_opt > 0 else "NO"
    print(f"  {label:<14}  {n:>8}  {100*p_flash:>6.1f}%  "
          f"{ev_cons:>+12.3f}%  {ev_opt:>+13.3f}%  {bet:>6}")

print()
print("  Conservative: flash arbs die before execution (worst case)")
print("  Optimistic:   flash arbs survive (best case)")

# ── breakeven minimum profit under conservative model ────────────────────────
# EV = 0: p_flash * (-0.5) + p_multi * P_min = 0
# P_min = 0.5 * p_flash / p_multi
print()
print("=" * 74)
print("2.  BREAKEVEN MINIMUM PROFIT  (conservative model)")
print("=" * 74)
print(f"\n  {'Tier':<14}  {'Flash%':>7}  {'Min to break even':>18}  {'Verdict':>10}")
print("  " + "-" * 56)
for label, lo, hi in TIERS:
    if label not in ev_table: continue
    ev_cons, ev_opt, p_flash, e_multi = ev_table[label]
    p_multi = 1 - p_flash
    if p_multi > 0:
        min_p = MISS_PCT * p_flash / p_multi
    else:
        min_p = float("inf")
    # mid-tier profit for comparison
    mid = (lo + min(hi, 3.0)) / 2
    verdict = "SKIP (all below breakeven)" if mid < min_p else "BET"
    print(f"  {label:<14}  {100*p_flash:>6.1f}%  {min_p:>17.3f}%  {verdict:>10}")

# ── confirmation-wait strategy ───────────────────────────────────────────────
# Idea: wait W seconds before placing.  If arb fires a second time within W
# seconds, you know it's NOT a flash arb → bet with higher confidence.
# Cost: you have only (EXEC_S - W) seconds left to execute.
#
# With W = 2s:
#   P(multi-arb fires again within 2s) = fraction of gaps <= 2s
#   If confirmed: P(flash) drops to near zero → P(survive) ≈ 1
#   If NOT confirmed: either flash arb (don't bet) OR multi-arb with slow gap
#
# EV_wait = P(confirm in W) * e_multi + P(no confirm, still bet) * ...

print()
print("=" * 74)
print("3.  'WAIT FOR CONFIRMATION' STRATEGY")
print(f"    Wait W seconds for a 2nd alert before placing (reduces flash risk)")
print(f"    Trade-off: W seconds less to execute, but near-zero flash-arb risk")
print("=" * 74)

print(f"\n  Gap distribution (how quickly 2nd entry arrives in multi-entry sessions):")
for w in [1, 2, 3, 5]:
    frac = pct_within(w)
    print(f"    Within {w}s: {100*frac:.1f}% of all within-session transitions")

print()
print(f"  If you wait W seconds and a 2nd alert fires → you know it's a multi-arb")
print(f"  If no 2nd alert within W seconds → likely flash, skip")
print()

for W in [1, 2, 3]:
    confirm_rate = pct_within(W)
    remaining_exec = EXEC_S - W
    print(f"  W = {W}s wait:")
    print(f"    {100*confirm_rate:.1f}% of multi-arbs confirmed (2nd alert within {W}s)")
    print(f"    {remaining_exec:.0f}s left to execute after confirmation")
    # EV with wait: only bet on confirmed arbs (flash eliminated)
    # EV = confirm_rate * p_multi * e_multi_profit + (1-confirm_rate) * 0 (skip)
    # vs no-wait EV = p_multi * e_multi + p_flash * (-MISS_PCT)
    # The improvement is: flash risk eliminated for confirmed arbs
    # but you miss (1-confirm_rate) of multi-arbs
    #
    # Overall improvement vs just betting everything immediately:
    # Saved: p_flash * MISS_PCT (no longer lose on flash arbs)
    # Lost:  (1-confirm_rate) * p_multi * e_multi (missed slow multi-arbs)
    print()

# ── expected profit per arb detected ─────────────────────────────────────────
print("=" * 74)
print("4.  EXPECTED PROFIT PER DETECTION  (conservative model, all tiers)")
print("=" * 74)

print(f"\n  {'Tier':<14}  {'N sessions':>10}  {'EV (conserv)':>13}  {'Action':>8}")
print("  " + "-" * 52)
for label, lo, hi in TIERS:
    if label not in ev_table: continue
    n = len(tier_sessions[label])
    ev_cons = ev_table[label][0]
    action = "BET" if ev_cons > 0 else "SKIP"
    bar = "+" * int(abs(ev_cons) * 20) if ev_cons > 0 else "-" * int(abs(ev_cons) * 20)
    print(f"  {label:<14}  {n:>10}  {ev_cons:>+12.3f}%  {action:>8}  {bar}")

# ── single-number recommendation ─────────────────────────────────────────────
print()
print("=" * 74)
print("5.  RECOMMENDATION")
print("=" * 74)
print()

# Find the lowest tier with positive conservative EV
breakeven_tier = None
for label, lo, hi in TIERS:
    if label in ev_table and ev_table[label][0] > 0:
        breakeven_tier = (label, lo, hi)
        break

# Compute overall minimum threshold
all_flash = sum(1 for s in sessions if len(s["entries"]) == 1)
all_multi = len(sessions) - all_flash
p_flash_overall = all_flash / len(sessions)
p_multi_overall = all_multi / len(sessions)
overall_min = MISS_PCT * p_flash_overall / p_multi_overall if p_multi_overall > 0 else 999

print(f"  Overall flash rate: {100*p_flash_overall:.1f}% of sessions")
print(f"  Overall breakeven profit (conservative): {overall_min:.3f}%")
print()
if breakeven_tier:
    print(f"  Conservative minimum: bet when profit >= {breakeven_tier[1]:.2f}%")
    print(f"  (The {breakeven_tier[0]} tier already clears the breakeven)")
print()

# Confirmation strategy recommendation
confirm_2s = pct_within(2)
print(f"  Confirmation strategy (wait 2s for 2nd alert):")
print(f"    Eliminates ~{100*p_flash_overall:.0f}% flash-arb risk")
print(f"    Confirms {100*confirm_2s:.0f}% of real arbs within 2s")
print(f"    Leaves {EXEC_S-2:.0f}s to execute — still feasible")
print(f"    Recommended if execution is reliable within {EXEC_S-2:.0f}s")
print()
print(f"  Summary:")
print(f"    No wait: bet when profit >= {overall_min:.2f}%  (breakeven with flash risk)")
print(f"    2s wait: bet on any confirmed arb  (flash risk eliminated, 75% confirmed)")
print()
print("Done.")
