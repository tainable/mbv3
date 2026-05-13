"""
kelly.py
--------
Profit-scaled bet sizing for arbitrage opportunities.

Since arbs have near-guaranteed returns, Kelly is applied to the execution
risk rather than outcome uncertainty. The fraction of bankroll staked is a
clamped linear function of the arb's net profit percentage, anchored at two
configurable points (e.g. 0.2% → 10%, 1.5% → 25%).

Bankroll is defined as min(pm_balance, sx_balance, mb_balance_usd) × 3,
which ensures sizing is limited by the weakest platform.
"""
from __future__ import annotations


def kelly_fraction(
    profit_pct: float,
    low_profit: float = 0.2,
    high_profit: float = 1.5,
    low_frac: float = 0.10,
    high_frac: float = 0.25,
) -> float:
    """
    Clamped linear interpolation between two (profit, fraction) anchors.

    Examples with defaults:
        0.2% profit → 10.0% of bankroll
        0.5% profit → 13.5% of bankroll
        1.0% profit → 19.2% of bankroll
        1.5% profit → 25.0% of bankroll  (capped)
    """
    if profit_pct <= low_profit:
        return low_frac
    if profit_pct >= high_profit:
        return high_frac
    t = (profit_pct - low_profit) / (high_profit - low_profit)
    return low_frac + t * (high_frac - low_frac)


def compute_bankroll(
    pm_balance_usd: float | None,
    sx_balance_usd: float | None,
    mb_balance_gbp: float | None,
    gbp_rate: float | None,
) -> float | None:
    """
    Returns min(pm_usd, sx_usd, mb_usd) × 3.

    The ×3 factor reflects having three platforms; using the minimum ensures
    sizing is limited by whichever platform is most depleted.

    Returns None if any balance is unavailable (caller should fall back to
    the fixed --budget cap).
    """
    if pm_balance_usd is None or sx_balance_usd is None or mb_balance_gbp is None:
        return None
    rate = gbp_rate if gbp_rate else 0.79
    mb_usd = mb_balance_gbp / rate
    return min(pm_balance_usd, sx_balance_usd, mb_usd) * 3
