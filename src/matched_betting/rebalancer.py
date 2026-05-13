"""
rebalancer.py
-------------
Post-bet balance monitoring for Polymarket (Polygon) and SX Bet (SX Network).

After each successful arb placement, fetches both USDC balances and sends an
alert when either drops below MIN_BALANCE_USDC. The alert specifies exactly how
much to bridge and in which direction.

Automated bridging is not wired up — the route Polygon → SX Network requires
either a multi-hop canonical bridge (Polygon → Ethereum → SX Rollup) or a
third-party aggregator (e.g. the SX bridge at https://sx.bet/wallet/bridge).
Configure the alert so you can act on it promptly, then bridge manually.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matched_betting.config import Settings


def check_rebalance_needed(
    pm_balance: float,
    sx_balance: float,
    min_balance: float,
) -> tuple[bool, str, float]:
    """
    Determine whether and what rebalancing is needed.

    Returns (needs_rebalance, direction, amount_usdc).
    direction:  'polygon_to_sx' | 'sx_to_polygon' | 'both_low' | ''
    amount:     how much USDC to bridge (0.0 when direction == 'both_low')
    """
    pm_low = pm_balance < min_balance
    sx_low = sx_balance < min_balance

    if pm_low and not sx_low:
        # Top Polygon up to min_balance, capped by SX surplus
        amount = min(min_balance - pm_balance, sx_balance - min_balance)
        return True, "sx_to_polygon", max(0.0, amount)

    if sx_low and not pm_low:
        # Top SX up to min_balance, capped by Polygon surplus
        amount = min(min_balance - sx_balance, pm_balance - min_balance)
        return True, "polygon_to_sx", max(0.0, amount)

    if pm_low and sx_low:
        return True, "both_low", 0.0

    return False, "", 0.0


def run_post_bet_rebalance(
    settings: "Settings",
    dry_run: bool = False,
) -> None:
    """
    Called after each successful arb placement.

    Fetches Polymarket and SX Bet USDC balances, checks against
    MIN_BALANCE_USDC, and sends an alert when rebalancing is required.
    """
    rs = getattr(settings, "rebalancer", None)
    if rs is None or not rs.enabled:
        return

    try:
        import importlib
        bet_mod = importlib.import_module("bet")
        pm_get_balance = bet_mod.pm_get_balance  # type: ignore[attr-defined]
        sx_get_balance = bet_mod.sx_get_balance  # type: ignore[attr-defined]
        pm_bal = pm_get_balance(settings)
        sx_bal = sx_get_balance(settings)
    except Exception as exc:
        print(f"  [rebalancer] Balance fetch failed: {exc}", file=sys.stderr)
        return

    if pm_bal is None or sx_bal is None:
        print(
            f"  [rebalancer] Balance unavailable (pm={pm_bal}, sx={sx_bal}) — skipping check",
            file=sys.stderr,
        )
        return

    needs, direction, amount = check_rebalance_needed(pm_bal, sx_bal, rs.min_balance_usdc)
    status_line = f"PM=${pm_bal:.2f}  SX=${sx_bal:.2f}  min=${rs.min_balance_usdc:.2f}"

    if not needs:
        print(f"  [rebalancer] Balances OK — {status_line}")
        return

    if direction == "both_low":
        summary = f"both platforms below ${rs.min_balance_usdc:.2f} — manual top-up needed"
        subject = "⚠ matched-betting: both platforms low — top-up required"
        body = (
            f"Polymarket balance: ${pm_bal:.2f} USDC\n"
            f"SX Bet balance:     ${sx_bal:.2f} USDC\n"
            f"Minimum threshold:  ${rs.min_balance_usdc:.2f} USDC\n\n"
            "Both platforms are below the minimum. Manual top-up required on both."
        )
    else:
        direction_label = (
            "SX Network → Polygon" if direction == "sx_to_polygon" else "Polygon → SX Network"
        )
        summary = f"bridge ${amount:.2f} USDC {direction_label}"
        subject = f"⚖ matched-betting: rebalance needed — {summary}"
        body = (
            f"Polymarket balance: ${pm_bal:.2f} USDC\n"
            f"SX Bet balance:     ${sx_bal:.2f} USDC\n"
            f"Minimum threshold:  ${rs.min_balance_usdc:.2f} USDC\n\n"
            f"Action: bridge ${amount:.2f} USDC {direction_label}\n"
            f"Bridge: https://sx.bet/wallet/bridge"
        )

    print(f"  [rebalancer] ⚠  Rebalance needed — {summary}")
    print(f"  [rebalancer]    {status_line}")

    if dry_run:
        print(f"  [rebalancer] DRY RUN — alert suppressed")
        return

    try:
        from matched_betting import notifier
        notifier.send_alert(subject, body, settings)
    except Exception as exc:
        print(f"  [rebalancer] Alert send failed: {exc}", file=sys.stderr)
