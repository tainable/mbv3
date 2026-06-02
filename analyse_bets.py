import json
from datetime import datetime
from pathlib import Path

GBP_RATE = 1.35  # approximate $/£ for Matchbook GBP amounts

lines = Path("outputs/bet_log.jsonl").read_text(encoding="utf-8").splitlines()
records = [json.loads(l) for l in lines if l.strip()]

placed = [r for r in records if r.get("status") == "PLACED"]

# Include ALL individual leg records — Matchbook legs carry a status field
# ("matched"/"open") so the original filter excluded them.
SUMMARY_STATUSES = {"PLACED", "FAILED_LEG", "DRY_RUN"}
leg_records = [
    r for r in records
    if r.get("platform") and "amount" in r and r.get("status") not in SUMMARY_STATUSES
]

print(f"Total PLACED arbs: {len(placed)}\n")

total_profit   = 0.0
total_capital  = 0.0

for p in placed:
    ts        = p["timestamp"][:16]
    team1     = p.get("team1", "?")
    team2     = p.get("team2", "?")
    league    = p.get("league", "?").upper()
    profit_pct = p.get("profit_pct", 0)
    arb_type  = p.get("arb_type", "?")
    game_time = (p.get("date_time") or "?")[:16]

    t_placed = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00")).timestamp()
    legs = [
        r for r in leg_records
        if abs(datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")).timestamp() - t_placed) < 60
    ]

    # Absolute profit: sum of non-Matchbook stakes × profit_pct (confirmed correct by user)
    non_mb = sum(r["amount"] for r in legs if r.get("platform") != "Matchbook")
    abs_profit = non_mb * profit_pct / 100

    # True capital deployed:
    #   - Matchbook lay  → liability = amount × (odds − 1), converted GBP→USD
    #   - Matchbook back → amount × GBP_RATE
    #   - All others     → amount (already USD)
    true_capital = 0.0
    for leg in legs:
        platform = leg.get("platform", "")
        amount   = leg.get("amount", 0)
        side     = leg.get("side", "")
        odds     = leg.get("decimal_odds", 1)
        if platform == "Matchbook":
            if side == "lay":
                true_capital += amount * (odds - 1) * GBP_RATE
            else:
                true_capital += amount * GBP_RATE
        else:
            true_capital += amount

    eff_pct = abs_profit / true_capital * 100 if true_capital else profit_pct

    total_profit  += abs_profit
    total_capital += true_capital

    print(f"[{ts}] {team1} vs {team2} [{league}] ({arb_type})")
    print(f"  game: {game_time}  profit: ${abs_profit:.4f}  "
          f"capital: ${true_capital:.2f}  eff. margin: {eff_pct:.4f}%")
    exec_odds = p.get("execution_odds", {})
    for k, v in exec_odds.items():
        print(f"    {k}: {v}")
    print()

print("--- TOTALS ---")
print(f"Total capital deployed: ${total_capital:.2f}")
print(f"Total expected profit:  ${total_profit:.4f}")
print(f"Effective margin:       {total_profit / total_capital * 100:.4f}%  "
      f"(Matchbook GBP converted at ~${GBP_RATE}/£)")
