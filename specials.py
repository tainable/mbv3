"""
specials.py
-----------
Read-only orchestrator for the specials pipeline.

Pulls live odds for every event in specials_registry.SPECIALS_EVENTS, runs every
enabled strategy, writes a snapshot + an audit log per event, and prints a
human-readable summary.

This file deliberately does NOT call any provider class from src/matched_betting/
that does sports-side event matching.  The catalog gives us provider market IDs
directly, so we hit each provider's HTTP endpoint with a thin fetcher.  That
keeps specials decoupled from the sports pipeline by design.

This file contains NO bet-placement code.  Use specials_place.py to place bets.

CLI examples
------------
    python specials.py                          # process every registered event
    python specials.py --event makerfield_by_election_2026
    python specials.py --debug                  # verbose fetch/evaluation tracing

Outputs (under outputs/)
------------------------
    specials_<event>.json             snapshot: fetched prices + every evaluation
    specials_<event>_evaluations.jsonl  append-only audit log (one line per evaluation)
"""
from __future__ import annotations

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

from bet import _mb_login, _mb_best_price
from specials_registry import list_events, get_event
from specials_strategies import evaluate, PriceMap, LiquidityMap
from specials_active import is_active


_OUTPUTS = _ROOT / "outputs"

# Polymarket asks below this price (odds above this threshold) are treated as
# illiquid stubs — automated market-maker quotes with effectively no depth.
# 100 = 1% implied probability.  Adjust if you trade genuine long-shots.
_PM_MAX_ODDS = 100


# ---------------------------------------------------------------------------
# Per-provider fetchers
# ---------------------------------------------------------------------------

def fetch_polymarket(
    catalog: dict[str, Any],
    settings,
    http:    HttpClient,
    debug:   bool,
) -> tuple[PriceMap, LiquidityMap, dict[str, dict[str, float | None]]]:
    """Return (price_map, liquidity_map, per_outcome_detail) for a Polymarket catalog section.

    Sides surfaced:
      yes : back the outcome  — buy the YES token at its best ask
            decimal odds = 1 / best_ask_yes
      no  : back the negation — buy the NO token at its best ask
            decimal odds = 1 / best_ask_no
            Requires clob_token_id_no to be set in the catalog outcome.
            Falls back to the bid-proxy (1 / (1 - best_bid_yes)) and logs a
            warning when the NO token ID is absent.

    Liquidity is the `size` (shares) available at the best ask level.
    """
    prices:    PriceMap    = {}
    liquidity: LiquidityMap = {}
    detail: dict[str, dict[str, float | None]] = {}

    pm_section = catalog.get("providers", {}).get("polymarket")
    if not pm_section or not pm_section.get("outcomes"):
        return prices, liquidity, detail

    base = settings.polymarket.clob_base_url
    for outcome_key, meta in pm_section["outcomes"].items():
        token = meta.get("clob_token_id")
        if not token:
            if debug:
                print(f"  polymarket: outcome {outcome_key!r} has no clob_token_id — skipped", file=sys.stderr)
            detail[outcome_key] = {"yes_odds": None, "no_odds": None, "skip_reason": "no_token_id"}
            continue
        try:
            yes_book = http.get_json(f"{base}/book", params={"token_id": token})
        except Exception as exc:
            if debug:
                print(f"  polymarket: YES book fetch failed for {outcome_key}: {exc}", file=sys.stderr)
            detail[outcome_key] = {"yes_odds": None, "no_odds": None, "skip_reason": f"fetch_error: {exc}"}
            continue

        yes_asks = yes_book.get("asks", [])
        best_ask_yes = float(yes_asks[-1]["price"]) if yes_asks else None
        yes_odds = (1.0 / best_ask_yes) if best_ask_yes and 0 < best_ask_yes < 1 else None
        yes_size = float(yes_asks[-1].get("size", 0)) if yes_asks else None

        # NO odds — use the real NO token book when available, otherwise proxy.
        no_token = meta.get("clob_token_id_no")
        no_odds: float | None = None
        no_odds_source = "none"
        no_size: float | None = None
        if no_token:
            try:
                no_book = http.get_json(f"{base}/book", params={"token_id": no_token})
                no_asks = no_book.get("asks", [])
                best_ask_no = float(no_asks[-1]["price"]) if no_asks else None
                no_odds = (1.0 / best_ask_no) if best_ask_no and 0 < best_ask_no < 1 else None
                no_size = float(no_asks[-1].get("size", 0)) if no_asks else None
                no_odds_source = "no_token_book"
            except Exception as exc:
                if debug:
                    print(f"  polymarket: NO book fetch failed for {outcome_key}: {exc}", file=sys.stderr)
                no_odds_source = f"no_token_fetch_error: {exc}"
        else:
            # Proxy: derive from YES best bid.  Less accurate — the spread
            # between YES bid and NO ask means this overstates the NO price.
            yes_bids = yes_book.get("bids", [])
            best_bid_yes = float(yes_bids[-1]["price"]) if yes_bids else None
            if best_bid_yes and 0 < best_bid_yes < 1:
                no_odds = 1.0 / (1.0 - best_bid_yes)
                no_odds_source = "bid_proxy"
            if debug:
                print(f"  polymarket: {outcome_key} no clob_token_id_no — using bid proxy for no_odds", file=sys.stderr)

        yes_illiquid = yes_odds is not None and yes_odds > _PM_MAX_ODDS
        no_illiquid  = no_odds  is not None and no_odds  > _PM_MAX_ODDS

        detail[outcome_key] = {
            "yes_odds": yes_odds, "no_odds": no_odds,
            "best_ask_yes": best_ask_yes,
            "no_odds_source": no_odds_source,
            "yes_illiquid": yes_illiquid,
            "no_illiquid":  no_illiquid,
            "yes_size": yes_size,
            "no_size":  no_size,
        }
        if yes_odds is not None and not yes_illiquid:
            prices[("polymarket", outcome_key, "yes")] = yes_odds
            if yes_size is not None:
                liquidity[("polymarket", outcome_key, "yes")] = yes_size
        if no_odds is not None and not no_illiquid:
            prices[("polymarket", outcome_key, "no")] = no_odds
            if no_size is not None:
                liquidity[("polymarket", outcome_key, "no")] = no_size
        if debug:
            yes_tag = f"{yes_odds:.4f}" if yes_odds and not yes_illiquid else f"{yes_odds} [ILLIQUID]" if yes_illiquid else "None"
            no_tag  = f"{no_odds:.4f}"  if no_odds  and not no_illiquid  else f"{no_odds} [ILLIQUID]"  if no_illiquid  else "None"
            print(f"  polymarket: {outcome_key} yes={yes_tag} (size={yes_size}) no={no_tag} (size={no_size}) (no_source={no_odds_source})", file=sys.stderr)

    return prices, liquidity, detail


def _mb_best_available(prices_list: list[dict], side: str) -> float | None:
    """Sum available-amount at the single best price level for a given side."""
    entries = [p for p in prices_list if p.get("side") == side and (p.get("decimal-odds") or p.get("odds"))]
    if not entries:
        return None
    best_odds = max(float(p.get("decimal-odds") or p.get("odds")) for p in entries) if side == "back" \
           else min(float(p.get("decimal-odds") or p.get("odds")) for p in entries)
    total = sum(
        float(p.get("available-amount") or 0)
        for p in entries
        if float(p.get("decimal-odds") or p.get("odds")) == best_odds
    )
    return total if total > 0 else None


def fetch_matchbook(
    catalog: dict[str, Any],
    settings,
    http:    HttpClient,
    debug:   bool,
) -> tuple[PriceMap, LiquidityMap, dict[str, dict[str, float | None]]]:
    """Return (price_map, liquidity_map, per_outcome_detail) for a Matchbook catalog section.

    Sides surfaced: 'back' and 'lay' per runner, taken from the runner's
    price ladder (best back = highest, best lay = lowest).

    Liquidity is the total available-amount across all price levels for each side.
    """
    prices:    PriceMap    = {}
    liquidity: LiquidityMap = {}
    detail: dict[str, dict[str, float | None]] = {}

    mb_section = catalog.get("providers", {}).get("matchbook")
    if not mb_section or not mb_section.get("outcomes"):
        return prices, liquidity, detail

    event_id   = mb_section.get("event_id")
    market_id  = mb_section.get("market_id")
    mb_outcomes = mb_section.get("outcomes", {})
    if not event_id or not market_id:
        if debug:
            print("  matchbook: event_id/market_id missing — skipped", file=sys.stderr)
        return prices, liquidity, detail

    mb = settings.matchbook
    if not (mb.username and mb.password):
        if debug:
            print("  matchbook: credentials missing — skipped", file=sys.stderr)
        return prices, liquidity, detail

    try:
        token = _mb_login(http, mb.base_url, mb.username, mb.password)
        event = http.get_json(
            f"{mb.base_url}/edge/rest/events/{event_id}",
            headers={"session-token": token, "Accept": "application/json"},
        )
    except Exception as exc:
        if debug:
            print(f"  matchbook: event fetch failed: {exc}", file=sys.stderr)
        return prices, liquidity, detail

    # Find the target market
    market = next((m for m in event.get("markets", []) if m.get("id") == market_id), None)
    if market is None:
        if debug:
            print(f"  matchbook: market {market_id} not found on event {event_id}", file=sys.stderr)
        return prices, liquidity, detail

    # Index runners by ID for fast lookup
    runners_by_id = {r.get("id"): r for r in market.get("runners", [])}

    for outcome_key, meta in mb_outcomes.items():
        runner_id = meta.get("runner_id")
        if not runner_id:
            detail[outcome_key] = {"back_odds": None, "lay_odds": None, "skip_reason": "no_runner_id"}
            continue
        runner = runners_by_id.get(runner_id)
        if runner is None:
            detail[outcome_key] = {"back_odds": None, "lay_odds": None, "skip_reason": "runner_not_in_market"}
            continue
        prices_list = runner.get("prices", []) or []
        back_odds = _mb_best_price(prices_list, "back")
        lay_odds  = _mb_best_price(prices_list, "lay")
        back_avail = _mb_best_available(prices_list, "back")
        lay_avail  = _mb_best_available(prices_list, "lay")
        detail[outcome_key] = {
            "back_odds": back_odds, "lay_odds": lay_odds,
            "back_available": back_avail, "lay_available": lay_avail,
        }
        if back_odds is not None:
            prices[("matchbook", outcome_key, "back")] = back_odds
            if back_avail is not None:
                liquidity[("matchbook", outcome_key, "back")] = back_avail
        if lay_odds is not None:
            prices[("matchbook", outcome_key, "lay")] = lay_odds
            if lay_avail is not None:
                liquidity[("matchbook", outcome_key, "lay")] = lay_avail
        if debug:
            print(f"  matchbook: {outcome_key} back={back_odds} (avail={back_avail}) lay={lay_odds} (avail={lay_avail})", file=sys.stderr)

    return prices, liquidity, detail


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def process_event(
    event_key: str,
    settings,
    direct_http: HttpClient,
    vpn_http:    HttpClient,
    debug:       bool,
) -> dict[str, Any]:
    catalog = get_event(event_key)
    if catalog is None:
        return {"event_key": event_key, "error": "unknown event"}

    print(f"\n  Event: {catalog.get('title', event_key)}  ({event_key})")
    if catalog.get("status_note"):
        print(f"    note: {catalog['status_note']}")

    # Active-window check — skip fetch and evaluation when the event is
    # outside its trading window (e.g. inside an overnight count blackout,
    # past the pre-resolution cutoff, or when a dynamic callable fires).
    window = is_active(catalog)
    if not window["active"]:
        ts = _utc_iso_now()
        reason = window["reason"]
        blocked_by = window["blocked_by"]
        print(f"    [INACTIVE] blocked_by={blocked_by!r}  reason={reason}")
        inactive_snapshot = {
            "event_key":    event_key,
            "title":        catalog.get("title"),
            "resolves_by":  catalog.get("resolves_by"),
            "evaluated_at": ts,
            "active":       False,
            "blocked_by":   blocked_by,
            "reason":       reason,
        }
        _OUTPUTS.mkdir(parents=True, exist_ok=True)
        snap_path = _OUTPUTS / f"specials_{event_key}.json"
        snap_path.write_text(
            json.dumps(inactive_snapshot, indent=2, sort_keys=False) + "\n", encoding="utf-8"
        )
        log_path = _OUTPUTS / f"specials_{event_key}_evaluations.jsonl"
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({**inactive_snapshot}) + "\n")
        print(f"    snapshot -> {snap_path}")
        return inactive_snapshot

    # Fetch prices from each configured provider.
    # Matchbook goes direct; Polymarket goes via the VPN bridge if configured.
    prices:    PriceMap    = {}
    liquidity: LiquidityMap = {}
    provider_detail: dict[str, dict[str, dict[str, float | None]]] = {}

    if "polymarket" in catalog.get("providers", {}):
        if debug:
            print("  fetching polymarket ...", file=sys.stderr)
        pm_prices, pm_liq, pm_detail = fetch_polymarket(catalog, settings, vpn_http, debug)
        prices.update(pm_prices)
        liquidity.update(pm_liq)
        provider_detail["polymarket"] = pm_detail

    if "matchbook" in catalog.get("providers", {}):
        if debug:
            print("  fetching matchbook ...", file=sys.stderr)
        mb_prices, mb_liq, mb_detail = fetch_matchbook(catalog, settings, direct_http, debug)
        prices.update(mb_prices)
        liquidity.update(mb_liq)
        provider_detail["matchbook"] = mb_detail

    # Run every strategy (disabled ones return status=disabled but still recorded for audit)
    evaluations: list[dict[str, Any]] = []
    for strategy in catalog.get("strategies", []):
        result = evaluate(strategy, prices, catalog, settings, liquidity)
        result["evaluated_at"] = _utc_iso_now()
        evaluations.append(result)

    # Attach instant-close spread cost to each evaluated strategy so it appears
    # in the summary and is persisted to the snapshot.
    for ev in evaluations:
        if ev.get("back") and ev.get("lay"):
            ic = _instant_close_pcts(ev, provider_detail)
            if ic:
                ev["instant_close_mb_pct"] = ic.get("mb_pct")
                ev["instant_close_pm_pct"] = ic.get("pm_pct")

    # Snapshot file: full state at this evaluation tick
    snapshot = {
        "event_key":    event_key,
        "title":        catalog.get("title"),
        "resolves_by":  catalog.get("resolves_by"),
        "evaluated_at": _utc_iso_now(),
        "providers":    provider_detail,
        "evaluations":  evaluations,
    }
    _OUTPUTS.mkdir(parents=True, exist_ok=True)
    snap_path = _OUTPUTS / f"specials_{event_key}.json"
    snap_path.write_text(json.dumps(snapshot, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    # Audit log: append one line per evaluation so trends are auditable over time
    log_path = _OUTPUTS / f"specials_{event_key}_evaluations.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        for ev in evaluations:
            f.write(json.dumps({**ev, "event_key": event_key}) + "\n")

    _print_summary(evaluations)
    print(f"    snapshot -> {snap_path}")
    print(f"    audit log -> {log_path}")
    return snapshot


def _fmt_liquidity(val: float | None, provider: str) -> str:
    if val is None:
        return "liq=?"
    if provider == "matchbook":
        return f"liq=${val:.2f}"
    # Polymarket: size is in shares; report as shares (not dollars)
    return f"liq={val:.1f}sh"


def _instant_close_pcts(
    eval_result:     dict,
    provider_detail: dict[str, dict],
) -> dict[str, float | None] | None:
    """Return the approximate round-trip spread cost for each leg as positive percentages.

    MB cost  = (lay_odds - back_odds) / lay_odds * 100
    PM cost  = (token_ask - token_bid) / token_ask * 100
               where token_bid is proxied as 1 - counterpart_ask

    Returns None when prices are insufficient to compute either leg.
    """
    back = eval_result.get("back")
    lay  = eval_result.get("lay")
    if not back or not lay:
        return None

    mb_pct: float | None = None
    pm_pct: float | None = None

    # ── MB bid-ask spread ────────────────────────────────────────────────
    mb_detail = provider_detail.get("matchbook", {})

    if back.get("provider") == "matchbook":
        outcome   = back["outcome"]
        back_odds = back["raw_odds"]
        lay_odds  = (mb_detail.get(outcome) or {}).get("lay_odds")
        if back_odds and lay_odds and lay_odds > back_odds > 1:
            mb_pct = (lay_odds - back_odds) / lay_odds * 100

    elif lay.get("provider") == "matchbook":
        outcome    = lay["outcome"]
        lay_entry  = lay["raw_odds"]
        back_close = (mb_detail.get(outcome) or {}).get("back_odds")
        if lay_entry and back_close and lay_entry > back_close > 1:
            mb_pct = (lay_entry - back_close) / lay_entry * 100

    # ── PM bid-ask spread ────────────────────────────────────────────────
    pm_detail = provider_detail.get("polymarket", {})

    if lay.get("provider") == "polymarket" and lay.get("side") == "no":
        outcome = lay["outcome"]
        out     = pm_detail.get(outcome) or {}
        no_raw  = lay["raw_odds"]
        yes_ask = out.get("best_ask_yes")
        if no_raw and yes_ask and no_raw > 1 and 0 < yes_ask < 1:
            no_ask = 1.0 / no_raw
            no_bid = 1.0 - yes_ask  # NO bid ≈ 1 - YES ask
            if 0 < no_bid < no_ask:
                pm_pct = (no_ask - no_bid) / no_ask * 100

    elif back.get("provider") == "polymarket" and back.get("side") == "yes":
        outcome = back["outcome"]
        out     = pm_detail.get(outcome) or {}
        yes_ask = out.get("best_ask_yes")
        lay_raw = lay["raw_odds"] if lay.get("provider") == "polymarket" else None
        if yes_ask and lay_raw and 0 < yes_ask < 1 and lay_raw > 1:
            no_ask  = 1.0 / lay_raw
            yes_bid = 1.0 - no_ask  # YES bid ≈ 1 - NO ask
            if 0 < yes_bid < yes_ask:
                pm_pct = (yes_ask - yes_bid) / yes_ask * 100

    if mb_pct is None and pm_pct is None:
        return None
    return {
        "mb_pct": round(mb_pct, 1) if mb_pct is not None else None,
        "pm_pct": round(pm_pct, 2) if pm_pct is not None else None,
    }


def _print_summary(evaluations: list[dict[str, Any]]) -> None:
    fires = [e for e in evaluations if e.get("status") == "fires"]
    if fires:
        print(f"    FIRES ({len(fires)}):")
        for e in fires:
            edge = e.get("edge_pct")
            risk = e.get("risk_class", "?")
            mode = "ALERT" if e.get("alert_only", True) else "LIVE"
            edge_s = f"edge={edge:+.2f}%" if isinstance(edge, (int, float)) else "edge=?"
            extra = ""
            if e.get("excluded_prob") is not None:
                extra = f"  excluded={e['excluded_prob']*100:.2f}%"
            print(f"      {e.get('name')}  [{mode}]  {edge_s}  risk={risk}{extra}")
            back = e.get("back") or {}
            lay  = e.get("lay")  or {}
            legs = e.get("legs") or []
            if back or lay:
                back_liq_s = _fmt_liquidity(back.get("liquidity"), back.get("provider", ""))
                lay_liq_s  = _fmt_liquidity(lay.get("liquidity"),  lay.get("provider", ""))
                print(f"        back: {back.get('provider')} {back.get('outcome')} @ {back.get('raw_odds')}  {back_liq_s}")
                print(f"        lay:  {lay.get('provider')}  {lay.get('outcome')} @ {lay.get('raw_odds')}  {lay_liq_s}")
            elif legs:
                si = e.get("sum_implied")
                si_s = f"  sum_implied={si:.4f}" if si is not None else ""
                print(f"        legs:{si_s}")
                for leg in legs:
                    liq_s = _fmt_liquidity(leg.get("liquidity"), leg.get("provider", ""))
                    print(f"          {leg.get('provider')} {leg.get('outcome')} @ {leg.get('raw_odds')}  {liq_s}")
            mb_ic = e.get("instant_close_mb_pct")
            pm_ic = e.get("instant_close_pm_pct")
            if mb_ic is not None or pm_ic is not None:
                mb_s = f"MB -{mb_ic:.1f}%" if mb_ic is not None else "MB -?%"
                pm_s = f"PM -{pm_ic:.2f}%" if pm_ic is not None else "PM -?%"
                print(f"        instant close:  {mb_s}  {pm_s}")
            if e.get("notes"):
                print(f"        note: {e['notes']}")
    other = [e for e in evaluations if e.get("status") != "fires"]
    if other:
        print(f"    Non-fires ({len(other)}):")
        for e in other:
            print(f"      {e.get('name')}  status={e.get('status')}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Specials pipeline — fetch prices and evaluate strategies.")
    parser.add_argument("--event", metavar="KEY", help="Process a single event by key (default: all registered events)")
    parser.add_argument("--debug", action="store_true", help="Verbose fetch/evaluation tracing on stderr")
    args = parser.parse_args()

    _settings    = load_settings(_ROOT)
    _direct_http = HttpClient()
    _vpn_http    = HttpClient(proxy_url=_settings.vpn_proxy_url) if _settings.vpn_proxy_url else _direct_http

    calculator.configure(_settings.commission)

    keys = [args.event] if args.event else list_events()
    print(f"Specials scan — {len(keys)} event(s)")

    fires_total = 0
    for key in keys:
        result = process_event(key, _settings, _direct_http, _vpn_http, args.debug)
        fires_total += sum(
            1 for ev in result.get("evaluations", []) if ev.get("status") == "fires"
        )

    print(f"\nDone. Total strategies firing: {fires_total}")
