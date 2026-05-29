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
    return kelly_fraction_piecewise(
        profit_pct,
        [(low_profit, low_frac), (high_profit, high_frac)],
    )


def kelly_fraction_piecewise(
    profit_pct: float,
    anchors: list[tuple[float, float]],
) -> float:
    """
    Piecewise linear interpolation over a sorted list of (profit_pct, fraction) anchors.
    Clamps to the first/last fraction outside the covered range.
    """
    if profit_pct <= anchors[0][0]:
        return anchors[0][1]
    if profit_pct >= anchors[-1][0]:
        return anchors[-1][1]
    for i in range(len(anchors) - 1):
        x0, y0 = anchors[i]
        x1, y1 = anchors[i + 1]
        if x0 <= profit_pct <= x1:
            t = (profit_pct - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return anchors[-1][1]


def compute_bankroll_for_arb(platform_balances_usd: dict[str, float | None]) -> float | None:
    """
    Returns min(arb platform balances) × 3.

    Pass only the platforms actually involved in the arb
    (e.g. {"polymarket": 120.0, "matchbook": 85.0} for a back-lay arb).
    The ×3 multiplier is fixed regardless of how many platforms the arb uses,
    so sizing is always anchored to the weakest of the involved platforms.

    Returns None if the dict is empty or any value is None (caller falls back to
    the fixed --budget cap).
    """
    if not platform_balances_usd:
        return None
    if any(v is None for v in platform_balances_usd.values()):
        return None
    return min(platform_balances_usd.values()) * 3  # type: ignore[type-var]


def compute_bankroll(
    pm_balance_usd: float | None,
    sx_balance_usd: float | None,
    mb_balance_gbp: float | None,
    gbp_rate: float | None,
) -> float | None:
    """
    Returns min(pm_usd, sx_usd, mb_usd) × 3.

    Kept for use by check_bankroll_halt (global low-balance alert across all platforms).
    For Kelly sizing per arb, use compute_bankroll_for_arb instead.
    """
    if pm_balance_usd is None or sx_balance_usd is None or mb_balance_gbp is None:
        return None
    rate = gbp_rate if gbp_rate else 0.79
    mb_usd = mb_balance_gbp / rate
    return compute_bankroll_for_arb({"polymarket": pm_balance_usd, "sx_bet": sx_balance_usd, "matchbook": mb_usd})
