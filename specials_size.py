"""
specials_size.py
----------------
Sizing calculator. Fetches fresh prices for a specific strategy and shows
stake breakdowns — no bets placed.

    python specials_size.py --event fifa_world_cup_2026_top_goalscorer --strategy harry_kane-mb-pm
    python specials_size.py --event ... --strategy ... --stake 4
    python specials_size.py --event ... --strategy ... --kelly
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC  = _ROOT / "src"
for _p in (_SRC, _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient
from matched_betting import calculator, kelly as _kelly_mod

from specials import fetch_polymarket, fetch_matchbook, _fmt_liquidity, _utc_iso_now
from specials_registry import get_event
from specials_strategies import evaluate, size_cross_hedge, back_stake_for_total_capital

_GBP_RATE_FALLBACK = 0.79


def _fetch_rate() -> float | None:
    try:
        import requests as _r
        s = _r.Session()
        s.trust_env = False
        return float(s.get("https://open.er-api.com/v6/latest/USD", timeout=5).json()["rates"]["GBP"])
    except Exception:
        return None


def _fetch_balances(settings, http: HttpClient) -> dict[str, float | None]:
    out: dict[str, float | None] = {"matchbook": None, "polymarket": None}
    try:
        from bet import mb_get_balance
        out["matchbook"] = mb_get_balance(settings)
    except Exception:
        pass
    try:
        from polymarket_bet import _build_client
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        if settings.polymarket.private_key:
            c = _build_client(settings.polymarket.private_key, settings)
            c.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            bal = c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            out["polymarket"] = float(bal.get("balance", 0)) / 1_000_000
    except Exception:
        pass
    return out


def _bankroll(balances: dict, back_prov: str, lay_prov: str, gbp_rate: float) -> float | None:
    provs: dict[str, float | None] = {}
    mb = balances.get("matchbook")
    pm = balances.get("polymarket")
    for p in (back_prov, lay_prov):
        if p == "matchbook":
            provs["matchbook"] = mb / gbp_rate if mb is not None else None
        elif p == "polymarket":
            provs["polymarket"] = pm
    return _kelly_mod.compute_bankroll_for_arb(provs)


def _row(label: str, sizing: dict, back_prov: str, lay_prov: str, gbp_rate: float) -> None:
    back_s = f"${sizing['back_stake']:.2f}"
    if back_prov == "matchbook":
        back_s += f" (£{sizing['back_stake'] * gbp_rate:.2f})"
    if lay_prov == "polymarket" and sizing.get("pm_no_spend") is not None:
        lay_s = f"PM NO ${sizing['pm_no_spend']:.2f}"
    elif lay_prov == "matchbook" and sizing.get("lay_liability") is not None:
        lay_s = (f"MB lay £{sizing['lay_stake'] * gbp_rate:.2f}"
                 f"  liability £{sizing['lay_liability'] * gbp_rate:.2f}")
    else:
        lay_s = f"${sizing['lay_stake']:.2f}"
    pct = sizing["profit"] / sizing["total_capital"] * 100 if sizing["total_capital"] else 0
    print(f"  {label:<22}  back={back_s:<24}  {lay_s:<30}  "
          f"total=${sizing['total_capital']:.2f}  profit=${sizing['profit']:.2f} ({pct:.2f}%)")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Specials sizing calculator — no bets placed.")
    p.add_argument("--event",    required=True, help="Event key  (specials_scan.py --list)")
    p.add_argument("--strategy", required=True, help="Strategy name  (specials_scan.py output)")
    p.add_argument("--stake",    type=float,    help="Total capital to deploy in USD (both legs combined)")
    p.add_argument("--kelly",    action="store_true",
                   help="Show Kelly-sized stake using live balances")
    p.add_argument("--debug",    action="store_true")
    args = p.parse_args(argv)

    if args.stake and args.kelly:
        p.error("Use --stake or --kelly, not both.")

    settings = load_settings(_ROOT)
    calculator.configure(settings.commission)

    direct_http = HttpClient()
    vpn_http = HttpClient(proxy_url=settings.vpn_proxy_url) if settings.vpn_proxy_url else direct_http

    catalog = get_event(args.event)
    if catalog is None:
        print(f"Unknown event: {args.event!r}", file=sys.stderr)
        return 1

    strategy = next((s for s in catalog.get("strategies", []) if s["name"] == args.strategy), None)
    if strategy is None:
        print(f"Strategy {args.strategy!r} not found in event {args.event!r}", file=sys.stderr)
        return 1

    # Fetch fresh prices
    prices: dict = {}
    liquidity: dict = {}
    if "polymarket" in catalog.get("providers", {}):
        pp, pl, _ = fetch_polymarket(catalog, settings, vpn_http, args.debug)
        prices.update(pp)
        liquidity.update(pl)
    if "matchbook" in catalog.get("providers", {}):
        mp, ml, _ = fetch_matchbook(catalog, settings, direct_http, args.debug)
        prices.update(mp)
        liquidity.update(ml)

    ev = evaluate(strategy, prices, catalog, settings, liquidity)
    back = ev.get("back") or {}
    lay  = ev.get("lay")  or {}
    back_prov = back.get("provider", "")
    lay_prov  = lay.get("provider", "")

    edge_s = f"{ev['edge_pct']:+.4f}%" if isinstance(ev.get("edge_pct"), float) else "n/a"
    print(f"\n  {args.strategy}  status={ev['status']}  edge={edge_s}")

    if ev.get("status") not in ("fires", "below_threshold"):
        print(f"  Cannot size: no valid prices ({ev['status']}).")
        return 1

    back_liq_s = _fmt_liquidity(back.get("liquidity"), back_prov)
    lay_liq_s  = _fmt_liquidity(lay.get("liquidity"),  lay_prov)
    print(f"  back: {back_prov} {back.get('outcome')} @ {back.get('raw_odds')} (eff {back.get('eff_odds')})  {back_liq_s}")
    print(f"  lay:  {lay_prov}  {lay.get('outcome')} @ {lay.get('raw_odds')} (eff {lay.get('eff_odds')})  {lay_liq_s}")

    gbp_rate = _fetch_rate() or _GBP_RATE_FALLBACK
    print(f"  FX: 1 USD = {gbp_rate:.4f} GBP\n")

    ks = settings.kelly

    if args.stake:
        back_stake = back_stake_for_total_capital(ev, args.stake)
        s = size_cross_hedge(ev, None, settings, override_back_stake=back_stake)
        _row(f"total=${args.stake:.2f}", s, back_prov, lay_prov, gbp_rate)
        return 0

    if args.kelly:
        print("  Fetching balances...")
        bal = _fetch_balances(settings, direct_http)
        mb_s = f"£{bal['matchbook']:.2f}" if bal["matchbook"] is not None else "unavailable"
        pm_s = f"${bal['polymarket']:.2f}" if bal["polymarket"] is not None else "unavailable"
        print(f"  MB={mb_s}  PM={pm_s}")
        br = _bankroll(bal, back_prov, lay_prov, gbp_rate)
        if br is None:
            print("  Balances unavailable — cannot compute Kelly. Use --stake instead.", file=sys.stderr)
            return 1
        s = size_cross_hedge(ev, br, settings)
        print(f"  bankroll=${br:.2f}  kelly={s['kelly_frac'] * 100:.1f}%")
        _row("kelly", s, back_prov, lay_prov, gbp_rate)
        return 0

    # No flag: reference table at several total-capital levels
    frac = _kelly_mod.kelly_fraction(
        ev.get("edge_pct") or 0,
        ks.low_profit, ks.high_profit, ks.low_fraction, ks.high_fraction,
    )
    # Approximate max total capital from max_stake_usdc (back stake cap)
    max_bs = size_cross_hedge(ev, None, settings, override_back_stake=float(ks.max_stake_usdc))
    print(f"  kelly_frac={frac * 100:.1f}% at this edge  "
          f"max_back_stake=${ks.max_stake_usdc:.0f} (~${max_bs['total_capital']:.0f} total)\n")

    # Build table — MB liquidity row first (convert MB back liquidity to total capital),
    # then round total-capital values
    mb_liq_back = (back.get("liquidity") if back_prov == "matchbook"
                   else lay.get("liquidity") if lay_prov == "matchbook" else None)
    ref: list[tuple[str, float]] = []
    if mb_liq_back:
        mb_liq_sz = size_cross_hedge(ev, None, settings, override_back_stake=mb_liq_back)
        mb_liq_total = mb_liq_sz["total_capital"]
        ref.append((f"MB liq ~${mb_liq_total:.0f}", mb_liq_total))
    for total in (10, 20, 50, 100, 200, 500, 1000):
        if total <= max_bs["total_capital"] * 1.1:
            ref.append((f"${total}", float(total)))
    if max_bs["total_capital"] not in (10, 20, 50, 100, 200, 500, 1000):
        ref.append((f"max ~${max_bs['total_capital']:.0f}", max_bs["total_capital"]))

    for label, total_cap in ref:
        back_s = back_stake_for_total_capital(ev, total_cap)
        sz = size_cross_hedge(ev, None, settings, override_back_stake=back_s)
        _row(label, sz, back_prov, lay_prov, gbp_rate)

    # Kelly row — silently skip if balances unavailable
    try:
        bal = _fetch_balances(settings, direct_http)
        br = _bankroll(bal, back_prov, lay_prov, gbp_rate)
        if br is not None:
            sz = size_cross_hedge(ev, br, settings)
            mb_s = f"£{bal['matchbook']:.2f}" if bal["matchbook"] is not None else "?"
            pm_s = f"${bal['polymarket']:.2f}" if bal["polymarket"] is not None else "?"
            print()
            print(f"  Live: MB={mb_s}  PM={pm_s}  bankroll=${br:.2f}  kelly={sz['kelly_frac'] * 100:.1f}%")
            _row("kelly", sz, back_prov, lay_prov, gbp_rate)
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
