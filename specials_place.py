# DEPRECATED — use specials_bet.py instead.
# This file is kept for reference only and will be removed.
"""
specials_place.py
-----------------
Interactive placement CLI for specials arbs.

Scans registered events via specials.py, then prompts to place each fired
strategy that has alert_only=False in the registry.

Use specials.py for read-only scanning.  This file is the only entry point
that can submit orders.

CLI examples
------------
    python specials_place.py                          # scan all events, prompt for each fire
    python specials_place.py --event makerfield_by_election_2026
    python specials_place.py --dry-run                # size orders but do not submit
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_SRC  = _ROOT / "src"
for _p in (_SRC, _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient
from matched_betting import calculator

from bet import mb_get_balance, mb_place_bet
from specials import (
    process_event,
    fetch_polymarket,
    fetch_matchbook,
    _fmt_liquidity,
    _utc_iso_now,
)
from specials_registry import list_events, get_event
from specials_strategies import evaluate, size_cross_hedge, PriceMap, LiquidityMap


_GBP_RATE_FALLBACK = 0.79   # GBP per USD (£1 ≈ $1.27); used when live rate unavailable
_POSITIONS = _ROOT / "outputs" / "positions.jsonl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_usd_gbp_rate() -> float | None:
    """Return live USD→GBP rate (GBP per 1 USD), or None on failure."""
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False
        r = s.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        return float(r.json()["rates"]["GBP"])
    except Exception:
        return None


def _fetch_specials_balances(settings, direct_http: HttpClient) -> dict[str, float | None]:
    """Return {'matchbook': GBP_balance, 'polymarket': USD_balance}."""
    balances: dict[str, float | None] = {"matchbook": None, "polymarket": None}
    try:
        balances["matchbook"] = mb_get_balance(settings)
    except Exception:
        pass
    try:
        from polymarket_bet import _build_client
        from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
        pk = settings.polymarket.private_key
        if pk:
            client = _build_client(pk, settings)
            client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            balances["polymarket"] = float(bal.get("balance", 0)) / 1_000_000
    except Exception:
        pass
    return balances


def _compute_specials_bankroll(
    balances:      dict[str, float | None],
    back_provider: str,
    lay_provider:  str,
    gbp_rate:      float,
) -> float | None:
    """Convert the two arb-platform balances to USD and return min × 2."""
    from matched_betting import kelly as _kelly_mod
    mb_gbp = balances.get("matchbook")
    pm_usd = balances.get("polymarket")
    provs_usd: dict[str, float | None] = {}
    for prov in (back_provider, lay_provider):
        if prov == "matchbook":
            provs_usd["matchbook"] = mb_gbp / gbp_rate if mb_gbp is not None else None
        elif prov == "polymarket":
            provs_usd["polymarket"] = pm_usd
    return _kelly_mod.compute_bankroll_for_arb(provs_usd)


# ---------------------------------------------------------------------------
# Position ledger
# ---------------------------------------------------------------------------

def _record_position(
    event_key:      str,
    eval_result:    dict,
    catalog:        dict[str, Any],
    results:        list[dict],
    back_stake_usd: float,
    pm_no_spend:    float | None,
) -> None:
    """Append an open-position record to outputs/positions.jsonl."""
    back_cfg  = eval_result["back"]
    lay_cfg   = eval_result["lay"]
    back_prov = back_cfg["provider"]
    lay_prov  = lay_cfg["provider"]
    back_res  = results[0]
    lay_res   = results[-1]

    mb_section = catalog["providers"].get("matchbook", {})
    pm_section = catalog["providers"].get("polymarket", {})

    def _mb_leg(cfg: dict, res: dict) -> dict:
        out = mb_section.get("outcomes", {}).get(cfg["outcome"], {})
        return {
            "platform":   "matchbook",
            "outcome":    cfg["outcome"],
            "event_id":   mb_section.get("event_id"),
            "market_id":  mb_section.get("market_id"),
            "runner_id":  out.get("runner_id"),
            "offer_id":   res.get("offer_id"),
            "stake_gbp":  res.get("amount"),
            "entry_odds": cfg["raw_odds"],
        }

    def _pm_leg(cfg: dict, spend_usd: float, token_side: str) -> dict:
        out      = pm_section.get("outcomes", {}).get(cfg["outcome"], {})
        token_id = out.get("clob_token_id_no" if token_side == "no" else "clob_token_id")
        raw_odds = cfg["raw_odds"] or 0.0
        shares   = round(spend_usd / (1.0 / raw_odds), 4) if raw_odds else 0.0
        return {
            "platform":    "polymarket",
            "outcome":     cfg["outcome"],
            "token_id":    token_id,
            "token_side":  token_side,
            "spend_usd":   round(spend_usd, 4),
            "shares":      shares,
            "entry_price": round(1.0 / raw_odds, 6) if raw_odds else 0.0,
        }

    back_entry = _mb_leg(back_cfg, back_res) if back_prov == "matchbook" \
                 else _pm_leg(back_cfg, back_stake_usd, back_cfg["side"])

    if lay_prov == "matchbook":
        lay_entry = _mb_leg(lay_cfg, lay_res)
    else:
        actual_spend = pm_no_spend if pm_no_spend is not None else 0.0
        lay_entry = _pm_leg(lay_cfg, actual_spend, lay_cfg["side"])

    placed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    record = {
        "position_id": f"{placed_at}|{event_key}|{eval_result.get('name', '')}",
        "event_key":   event_key,
        "strategy":    eval_result.get("name", ""),
        "placed_at":   placed_at,
        "status":      "open",
        "back":        back_entry,
        "lay":         lay_entry,
    }
    _POSITIONS.parent.mkdir(parents=True, exist_ok=True)
    with _POSITIONS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _preflight_cross_hedge(
    eval_result: dict,
    catalog:     dict[str, Any],
    settings,
    direct_http: HttpClient,
    vpn_http:    HttpClient,
    debug:       bool,
) -> tuple[bool, str, dict]:
    """Re-fetch prices and re-evaluate. Returns (ok, reason, refreshed_eval)."""
    strategy_name = eval_result["name"]
    strategy = next(
        (s for s in catalog.get("strategies", []) if s["name"] == strategy_name),
        None,
    )
    if strategy is None:
        return False, f"strategy {strategy_name!r} not found in catalog", {}

    prices:    PriceMap     = {}
    liquidity: LiquidityMap = {}

    if "polymarket" in catalog.get("providers", {}):
        pm_prices, pm_liq, _ = fetch_polymarket(catalog, settings, vpn_http, debug)
        prices.update(pm_prices)
        liquidity.update(pm_liq)
    if "matchbook" in catalog.get("providers", {}):
        mb_prices, mb_liq, _ = fetch_matchbook(catalog, settings, direct_http, debug)
        prices.update(mb_prices)
        liquidity.update(mb_liq)

    refreshed = evaluate(strategy, prices, catalog, settings, liquidity)
    refreshed["evaluated_at"] = _utc_iso_now()

    if refreshed.get("status") != "fires":
        return False, f"prices moved — strategy now {refreshed.get('status')}", refreshed
    return True, "ok", refreshed


# ---------------------------------------------------------------------------
# Placement
# ---------------------------------------------------------------------------

def _pm_place(settings, token_id: str, amount_usdc: float, side: str = "BUY", dry_run: bool = False) -> dict:
    """Build a PM CLOB client and place a market order. Returns a result dict."""
    try:
        from polymarket_bet import _build_client, place_market_order
        _proxy = settings.vpn_proxy_url
        if _proxy:
            import httpx
            import py_clob_client.http_helpers.helpers as _pm_helpers
            _hx_proxy = _proxy.replace("socks5h://", "socks5://")
            _pm_helpers._http_client = httpx.Client(http2=True, proxy=_hx_proxy)
        pk = settings.polymarket.private_key
        if not pk:
            return {"ok": False, "error": "POLYMARKET_PRIVATE_KEY not set"}
        client = _build_client(pk, settings)
        resp = place_market_order(client, token_id, amount_usdc, dry_run=dry_run, side=side)
        ok = resp.get("status") == "matched" or resp.get("dry_run") or bool(resp.get("orderID"))
        return {"platform": "Polymarket", "ok": ok, "response": resp,
                "amount": amount_usdc, "token_id": token_id}
    except Exception as exc:
        return {"platform": "Polymarket", "ok": False, "error": str(exc)}


def _place_cross_hedge(
    eval_result: dict,
    back_stake:  float,
    lay_stake:   float,
    catalog:     dict[str, Any],
    event_key:   str,
    settings,
    direct_http: HttpClient,
    vpn_http:    HttpClient,
    dry_run:     bool,
    gbp_rate:    float,
    pm_no_spend: float | None = None,
) -> list[dict]:
    """
    Atomically place both legs of a cross_hedge.

    Returns a list of result dicts (one per leg).  If back placement fails,
    lay is never attempted.  If lay fails after a successful back, the result
    is marked UNHEDGED so the caller can alert.
    """
    back_cfg  = eval_result["back"]
    lay_cfg   = eval_result["lay"]
    back_prov = back_cfg["provider"]
    lay_prov  = lay_cfg["provider"]
    outcome   = back_cfg["outcome"]

    mb_section = catalog["providers"].get("matchbook", {})
    pm_section = catalog["providers"].get("polymarket", {})
    event_id   = mb_section.get("event_id")
    market_id  = mb_section.get("market_id")

    results: list[dict] = []

    # ── Back leg ──────────────────────────────────────────────────────────
    if back_prov == "matchbook":
        runner_id = (mb_section.get("outcomes", {}).get(outcome) or {}).get("runner_id")
        if not runner_id:
            return [{"ok": False, "leg": "back", "error": f"no runner_id for {outcome}"}]
        mb_back_stake = round(back_stake * gbp_rate, 2)
        back_result = mb_place_bet(
            settings, event_id, market_id, runner_id,
            stake=mb_back_stake, side="back", odds=back_cfg["raw_odds"],
            dry_run=dry_run,
        )
        back_result["leg"] = "back"
        back_result["usdc_equiv"] = back_stake
        results.append(back_result)

    elif back_prov == "polymarket":
        token_id = (pm_section.get("outcomes", {}).get(outcome) or {}).get("clob_token_id")
        if not token_id:
            return [{"ok": False, "leg": "back", "error": f"no clob_token_id for {outcome}"}]
        back_result = _pm_place(settings, token_id, back_stake, side="BUY", dry_run=dry_run)
        back_result["leg"] = "back"
        results.append(back_result)

    else:
        return [{"ok": False, "leg": "back", "error": f"unsupported back provider: {back_prov}"}]

    if not results[-1].get("ok"):
        return results  # back failed — abort before touching lay

    # ── Lay leg ───────────────────────────────────────────────────────────
    if lay_prov == "matchbook" and lay_cfg["side"] == "lay":
        runner_id = (mb_section.get("outcomes", {}).get(outcome) or {}).get("runner_id")
        if not runner_id:
            results.append({"ok": False, "leg": "lay", "error": f"no runner_id for {outcome}", "UNHEDGED": True})
        else:
            mb_lay_stake = round(lay_stake * gbp_rate, 2)
            lay_result = mb_place_bet(
                settings, event_id, market_id, runner_id,
                stake=mb_lay_stake, side="lay", odds=lay_cfg["raw_odds"],
                dry_run=dry_run,
            )
            lay_result["leg"] = "lay"
            lay_result["usdc_equiv"] = lay_stake
            results.append(lay_result)

    elif lay_prov == "polymarket" and lay_cfg["side"] == "no":
        no_token = (pm_section.get("outcomes", {}).get(outcome) or {}).get("clob_token_id_no")
        if not no_token:
            results.append({"ok": False, "leg": "lay", "error": f"no clob_token_id_no for {outcome}", "UNHEDGED": True})
        else:
            # pm_no_spend is the actual USDC cost of the NO tokens, not the exchange-equivalent lay_stake
            if pm_no_spend is None:
                from matched_betting.calculator import _eff_back_odds as _eff
                pm_no_eff   = _eff(lay_cfg["raw_odds"], "polymarket")
                pm_no_spend = lay_stake / (pm_no_eff - 1) if pm_no_eff > 1 else lay_stake
            lay_result = _pm_place(settings, no_token, pm_no_spend, side="BUY", dry_run=dry_run)
            lay_result["leg"] = "lay"
            lay_result["pm_no_spend"] = pm_no_spend
            results.append(lay_result)

    else:
        results.append({"ok": False, "leg": "lay",
                        "error": f"unsupported lay leg: {lay_prov}/{lay_cfg['side']}", "UNHEDGED": True})

    if not results[-1].get("ok"):
        results[-1]["UNHEDGED"] = True

    if all(r.get("ok") for r in results) and not dry_run:
        _record_position(event_key, eval_result, catalog, results, back_stake, pm_no_spend)

    return results


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _print_placement_results(results: list[dict]) -> None:
    for r in results:
        leg  = r.get("leg", "?")
        ok   = r.get("ok", False)
        plat = r.get("platform", "?")
        if r.get("dry_run") or (r.get("response") or {}).get("dry_run"):
            print(f"    [{leg}] DRY RUN  {plat}  amount=${r.get('usdc_equiv') or r.get('amount_usdc') or r.get('amount', '?'):.2f}")
        elif ok:
            offer_id  = r.get("offer_id") or (r.get("response") or {}).get("orderID") or ""
            matched   = r.get("matched") or ""
            id_str    = f"  id={offer_id}" if offer_id else ""
            match_str = f"  matched={matched}" if matched else ""
            print(f"    [{leg}] OK  {plat}{id_str}{match_str}")
        else:
            err = r.get("error", "unknown error")
            unhedged = "  *** UNHEDGED — CHECK BACK BET ***" if r.get("UNHEDGED") else ""
            print(f"    [{leg}] FAILED  {plat}  {err}{unhedged}", file=sys.stderr)


def _print_sizing(sizing: dict, back_prov: str, lay_prov: str, gbp_rate: float) -> None:
    back_s = f"${sizing['back_stake']:.2f}"
    if back_prov == "matchbook":
        back_s += f"  (£{sizing['back_stake']*gbp_rate:.2f})"

    if lay_prov == "polymarket" and sizing.get("pm_no_spend") is not None:
        lay_s = f"PM NO ${sizing['pm_no_spend']:.2f}"
    elif lay_prov == "matchbook" and sizing.get("lay_liability") is not None:
        lay_s = (f"MB lay_stake £{sizing['lay_stake']*gbp_rate:.2f}"
                 f"  liability £{sizing['lay_liability']*gbp_rate:.2f}")
    else:
        lay_s = f"${sizing['lay_stake']:.2f}"

    print(f"    back: {back_s}   lay: {lay_s}")


# ---------------------------------------------------------------------------
# Interactive flow
# ---------------------------------------------------------------------------

def _interactive_place(
    fires:       list[dict],
    catalog:     dict[str, Any],
    event_key:   str,
    settings,
    direct_http: HttpClient,
    vpn_http:    HttpClient,
    dry_run:     bool,
    debug:       bool,
) -> None:
    """For each fired strategy, show sizing and prompt for approval before placing."""
    gbp_rate = _fetch_usd_gbp_rate() or _GBP_RATE_FALLBACK
    print(f"\n  FX: 1 USD = {gbp_rate:.4f} GBP")

    print("  Fetching balances... ", end="", flush=True)
    balances = _fetch_specials_balances(settings, direct_http)
    mb_gbp = balances.get("matchbook")
    pm_usd = balances.get("polymarket")
    mb_s = f"£{mb_gbp:.2f}" if mb_gbp is not None else "unavailable"
    pm_s = f"${pm_usd:.2f}" if pm_usd is not None else "unavailable"
    print(f"MB={mb_s}  PM={pm_s}")

    for ev in fires:
        name      = ev.get("name", "?")
        edge_pct  = ev.get("edge_pct", 0.0)
        back      = ev.get("back") or {}
        lay       = ev.get("lay")  or {}
        back_prov = back.get("provider", "?")
        lay_prov  = lay.get("provider",  "?")

        bankroll = _compute_specials_bankroll(balances, back_prov, lay_prov, gbp_rate)
        sizing   = size_cross_hedge(ev, bankroll, settings)

        back_liq_s = _fmt_liquidity(back.get("liquidity"), back_prov)
        lay_liq_s  = _fmt_liquidity(lay.get("liquidity"),  lay_prov)

        print(f"\n  {'─'*60}")
        print(f"  {name}  edge={edge_pct:+.2f}%")
        print(f"    back: {back_prov} {back.get('outcome')} @ {back.get('raw_odds')}  {back_liq_s}")
        print(f"    lay:  {lay_prov}  {lay.get('outcome')} @ {lay.get('raw_odds')}  {lay_liq_s}")
        if bankroll is not None:
            print(f"    bankroll=${bankroll:.2f}  kelly={sizing['kelly_frac']*100:.1f}%")
        _print_sizing(sizing, back_prov, lay_prov, gbp_rate)
        print(f"    total capital: ${sizing['total_capital']:.2f}  profit: ${sizing['profit']:.2f}"
              f"  ({sizing['profit']/sizing['total_capital']*100:.2f}% on capital)")
        if dry_run:
            print("    [DRY RUN mode]")

        try:
            raw = input("    Place? [y / n / <custom back stake $>]: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.")
            return

        if not raw or raw.lower() == "n":
            print("    Skipped.")
            continue

        if raw.lower() == "y":
            final_sizing = sizing
        else:
            try:
                custom_back = float(raw)
                final_sizing = size_cross_hedge(ev, bankroll, settings, override_back_stake=custom_back)
                print(f"    Override:")
                _print_sizing(final_sizing, back_prov, lay_prov, gbp_rate)
                print(f"    total capital: ${final_sizing['total_capital']:.2f}  profit: ${final_sizing['profit']:.2f}"
                      f"  ({final_sizing['profit']/final_sizing['total_capital']*100:.2f}% on capital)")
            except ValueError:
                print(f"    Invalid input {raw!r} — skipped.")
                continue

        print("    Pre-flight check... ", end="", flush=True)
        ok, reason, refreshed = _preflight_cross_hedge(ev, catalog, settings, direct_http, vpn_http, debug)
        if not ok:
            print(f"FAILED — {reason}")
            print("    Aborted — nothing placed.")
            continue
        print("ok — strategy still fires")

        final_sizing = size_cross_hedge(refreshed, bankroll, settings,
                                        override_back_stake=final_sizing["back_stake"] if raw.lower() != "y" else None)

        print("    Placing bets...")
        results = _place_cross_hedge(
            refreshed, final_sizing["back_stake"], final_sizing["lay_stake"],
            catalog, event_key, settings, direct_http, vpn_http, dry_run, gbp_rate,
            pm_no_spend=final_sizing.get("pm_no_spend"),
        )
        _print_placement_results(results)

        all_ok = all(r.get("ok") for r in results)
        any_unhedged = any(r.get("UNHEDGED") for r in results)
        if all_ok:
            print(f"    Done.  Expected profit: ${final_sizing['profit']:.2f}")
        elif any_unhedged:
            print("    *** ALERT: back placed but lay FAILED — position is UNHEDGED ***", file=sys.stderr)
        else:
            print("    Placement failed — no position taken.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan specials and interactively place fired strategies."
    )
    parser.add_argument(
        "--event",
        action="append",
        help="Process only the named event(s). Repeat the flag for multiple. "
             "Default: every event in specials_registry.SPECIALS_EVENTS.",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose fetch/evaluation tracing.")
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Build orders but do not submit them.")
    args = parser.parse_args(argv)

    settings = load_settings(_ROOT)
    calculator.configure(settings.commission)

    direct_http = HttpClient()
    vpn_http    = HttpClient(proxy_url=settings.vpn_proxy_url) if settings.vpn_proxy_url else direct_http

    targets = args.event or list_events()
    if not targets:
        print("No events registered. Add an entry to specials_registry.SPECIALS_EVENTS.", file=sys.stderr)
        return 1

    print(f"Scanning {len(targets)} event(s) at {_utc_iso_now()}")
    for key in targets:
        try:
            snapshot = process_event(key, settings, direct_http, vpn_http, args.debug)
        except Exception as exc:
            print(f"  ERROR processing {key}: {exc}", file=sys.stderr)
            if args.debug:
                import traceback
                traceback.print_exc()
            continue

        fires = [e for e in snapshot.get("evaluations", [])
                 if e.get("status") == "fires" and not e.get("alert_only", True)]
        alert_only_fires = [e for e in snapshot.get("evaluations", [])
                            if e.get("status") == "fires" and e.get("alert_only", True)]
        if alert_only_fires:
            names = ", ".join(e["name"] for e in alert_only_fires)
            print(f"\n  Skipping {len(alert_only_fires)} alert_only fire(s) — flip alert_only=False to enable: {names}")
        if not fires:
            print("  No placement-eligible fires.")
        else:
            catalog = get_event(key)
            _interactive_place(fires, catalog, key, settings, direct_http, vpn_http,
                               dry_run=args.dry_run, debug=args.debug)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
