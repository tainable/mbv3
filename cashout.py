"""
cashout.py
----------
Interactive arb cashout tool.

Reads bet_log.jsonl to reconstruct open arb positions, fetches current market
prices to calculate the P&L if each leg were closed now, and executes close
orders on request.

Usage:
    python cashout.py                       # live mode
    python cashout.py --dry-run             # preview only, no orders placed
    python cashout.py --max-age-hours 48    # look back further in the log
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT         = Path(__file__).resolve().parent
_BET_LOG      = _ROOT / "outputs" / "bet_log.jsonl"
_CASHOUT_LOG  = _ROOT / "outputs" / "cashout_log.jsonl"

sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from matched_betting.config import load_settings


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class BetLeg:
    timestamp:     str
    platform:      str
    side:          str
    amount:        float
    original_odds: float
    # Polymarket
    token_id:   str | None = None
    # Matchbook
    event_id:   int | None = None
    market_id:  int | None = None
    runner_id:  int | None = None
    offer_id:   int | None = None
    mb_status:  str | None = None   # status at placement time ("open" / "matched")
    # SX Bet
    market_hash: str | None  = None
    outcome:     str | None  = None   # "one" | "two"
    order_hash:  str | None  = None   # only set for maker orders
    take:        bool | None = None


@dataclass
class OpenArb:
    placed_at:  str
    arb_type:   str
    team1:      str
    team2:      str
    league:     str
    game_time:  str
    profit_pct: float
    legs:       list[BetLeg] = field(default_factory=list)


@dataclass
class LegCashout:
    leg:          BetLeg
    close_method: str
    current_odds: float | None = None   # counterpart odds used in offset formula
    close_stake:  float | None = None   # stake of new offsetting bet (where applicable)
    net_pnl:      float | None = None   # gain/loss vs break-even (USD, or GBP for MB legs)
    pm_size:      float | None = None   # Polymarket: number of shares to sell
    currency:     str = "USD"           # "GBP" for Matchbook legs


# close_method values
_SELL_TOKEN   = "sell_token"    # Polymarket: FOK SELL market order
_CANCEL_OFFER = "cancel_offer"  # Matchbook: cancel an unmatched offer
_PLACE_OFFSET = "place_offset"  # Matchbook: place offsetting back/lay on matched bet
_CANCEL_SX    = "cancel_sx"     # SX Bet: cancel unmatched maker order
_TAKER_OFFSET = "taker_offset"  # SX Bet: opposite-outcome taker fill
_UNAVAILABLE  = "unavailable"   # price fetch failed or position not found
_RESOLVED     = "resolved"      # position confirmed settled on the platform


# ─── Bet log parsing ───────────────────────────────────────────────────────────

def _parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


_SUMMARY_STATUSES = {"PLACED", "PLACED_TOPUP", "FAILED_LEG", "DRY_RUN"}


def load_open_arbs(bet_log_path: Path, max_age_hours: int = 24) -> list[OpenArb]:
    """Return arbs from bet_log.jsonl whose game has not resolved (game_time + 6h > now).

    Pairing strategy: for each PLACED summary record, collect per-leg sub-records
    whose timestamp falls within ±90 seconds (identical window used by analyse_bets.py).
    """
    if not bet_log_path.exists():
        return []

    records: list[dict] = []
    for line in bet_log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    placed_records = [r for r in records if r.get("status") in ("PLACED", "PLACED_TOPUP")]
    leg_records    = [r for r in records
                      if "platform" in r and r.get("status") not in _SUMMARY_STATUSES]

    now          = datetime.now(timezone.utc)
    game_cutoff  = now - timedelta(hours=6)
    place_cutoff = now - timedelta(hours=max_age_hours)

    arbs: list[OpenArb] = []
    for placed in placed_records:
        game_time_str = placed.get("date_time", "")
        try:
            game_time = _parse_ts(game_time_str)
        except (ValueError, TypeError):
            continue
        if game_time < game_cutoff:
            continue

        placed_ts = _parse_ts(placed["timestamp"])
        if placed_ts < place_cutoff:
            continue

        near_legs = [r for r in leg_records
                     if abs((_parse_ts(r["timestamp"]) - placed_ts).total_seconds()) <= 90]

        legs: list[BetLeg] = []
        for r in near_legs:
            platform = r.get("platform", "")
            if platform == "Polymarket":
                legs.append(BetLeg(
                    timestamp=r["timestamp"],
                    platform="Polymarket",
                    side=str(r.get("side", "BUY")),
                    amount=float(r.get("amount") or 0),
                    original_odds=float(r.get("execution_odds") or r.get("decimal_odds") or 0),
                    token_id=r.get("token_id"),
                ))
            elif platform == "Matchbook":
                legs.append(BetLeg(
                    timestamp=r["timestamp"],
                    platform="Matchbook",
                    side=str(r.get("side", "back")),
                    amount=float(r.get("amount") or 0),
                    original_odds=float(r.get("decimal_odds") or 0),
                    event_id=r.get("event_id"),
                    market_id=r.get("market_id"),
                    runner_id=r.get("runner_id"),
                    offer_id=r.get("offer_id"),
                    mb_status=r.get("status"),
                ))
            elif platform == "SX Bet":
                legs.append(BetLeg(
                    timestamp=r["timestamp"],
                    platform="SX Bet",
                    side="back",
                    amount=float(r.get("amount") or 0),
                    original_odds=float(r.get("execution_odds") or r.get("decimal_odds") or 0),
                    market_hash=r.get("market_hash"),
                    outcome=r.get("outcome"),
                    order_hash=r.get("order_hash"),
                    take=r.get("take"),
                ))

        if not legs:
            continue

        arbs.append(OpenArb(
            placed_at=placed["timestamp"],
            arb_type=placed.get("arb_type", "?"),
            team1=placed.get("team1", "?"),
            team2=placed.get("team2", "?"),
            league=placed.get("league", "?"),
            game_time=game_time_str,
            profit_pct=float(placed.get("profit_pct") or 0),
            legs=legs,
        ))

    return arbs


# ─── Price fetching ────────────────────────────────────────────────────────────

def _fetch_pm_data(settings) -> dict:
    """Fetch Polymarket positions.
    Returns {"positions": {token_id: pos}, "redeemable": {token_id, ...}}.
    """
    try:
        from portfolio import fetch_polymarket
        data = fetch_polymarket(settings)
        if not data.get("ok"):
            return {"positions": {}, "redeemable": set()}
        positions = data.get("positions", [])
        return {
            "positions":  {p["token_id"]: p for p in positions if p.get("token_id")},
            "redeemable": {p["token_id"] for p in positions
                           if p.get("token_id") and p.get("redeemable")},
        }
    except Exception as exc:
        print(f"  [cashout] PM price fetch error: {exc}")
        return {"positions": {}, "redeemable": set()}


def _fetch_sx_odds(settings, market_hashes: list[str]) -> dict:
    """Fetch current best taker odds for SX Bet markets.
    Returns {market_hash: {"outcome_one": decimal, "outcome_two": decimal}}.
    """
    if not market_hashes:
        return {}
    try:
        from bet import _sx_get, _sx_best_taker_odds, _sx_proxies
        base_url   = settings.sx_bet.base_url
        base_token = settings.sx_bet.base_token
        _prx       = _sx_proxies(settings)
        result: dict = {}
        for mh in market_hashes:
            try:
                raw  = _sx_get(f"{base_url}/orders/odds/best",
                               params={"marketHashes": mh, "baseToken": base_token},
                               proxies=_prx)
                best = (raw.get("data") or {}).get("bestOdds") or []
                if best:
                    t = _sx_best_taker_odds(best[0])
                    result[mh] = {
                        "outcome_one": (t.get("outcome_one") or {}).get("decimal"),
                        "outcome_two": (t.get("outcome_two") or {}).get("decimal"),
                    }
            except Exception:
                pass
        return result
    except Exception as exc:
        print(f"  [cashout] SX price fetch error: {exc}")
        return {}


def _fetch_mb_data(settings, mb_legs: list[BetLeg]) -> dict:
    """Fetch Matchbook runner prices and live offer statuses.

    Returns:
        {
          "prices":       {(event_id, market_id, runner_id): {"back_odds", "lay_odds"}},
          "offer_status": {offer_id: current_status},
        }
    """
    result: dict = {"prices": {}, "offer_status": {}, "settled_offer_ids": set()}
    if not mb_legs:
        return result

    # Live offer status — determines cancel vs offset; collect settled IDs separately
    try:
        from portfolio import fetch_matchbook
        port = fetch_matchbook(settings)
        if port.get("ok"):
            for o in port.get("open_offers", []) + port.get("matched_offers", []):
                if o.get("id") is not None:
                    result["offer_status"][o["id"]] = o["status"]
            for o in port.get("settled_offers", []):
                if o.get("id") is not None:
                    result["settled_offer_ids"].add(o["id"])
    except Exception as exc:
        print(f"  [cashout] MB portfolio fetch error: {exc}")

    # Current runner prices for offset calculation
    try:
        from bet import mb_get_runner_prices
        unique = {(l.event_id, l.market_id, l.runner_id)
                  for l in mb_legs if l.event_id and l.runner_id}
        for key in unique:
            result["prices"][key] = mb_get_runner_prices(settings, *key)
    except Exception as exc:
        print(f"  [cashout] MB runner price fetch error: {exc}")

    return result


def fetch_all_prices(arbs: list[OpenArb], settings) -> dict:
    """Fetch current prices for all platforms concurrently."""
    all_legs = [leg for arb in arbs for leg in arb.legs]
    pm_legs  = [l for l in all_legs if l.platform == "Polymarket"]
    sx_legs  = [l for l in all_legs if l.platform == "SX Bet"]
    mb_legs  = [l for l in all_legs if l.platform == "Matchbook"]
    sx_hashes = list({l.market_hash for l in sx_legs if l.market_hash})

    prices: dict = {
        "pm": {}, "pm_redeemable": set(),
        "sx": {},
        "mb": {"prices": {}, "offer_status": {}}, "mb_settled": set(),
    }

    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {}
        if pm_legs:
            futs["pm"] = pool.submit(_fetch_pm_data, settings)
        if sx_hashes:
            futs["sx"] = pool.submit(_fetch_sx_odds, settings, sx_hashes)
        if mb_legs:
            futs["mb"] = pool.submit(_fetch_mb_data, settings, mb_legs)

        for key, fut in futs.items():
            try:
                fetched = fut.result(timeout=30) or {}
                if key == "pm":
                    prices["pm"]           = fetched.get("positions", {})
                    prices["pm_redeemable"] = fetched.get("redeemable", set())
                elif key == "mb":
                    prices["mb"]        = {"prices":       fetched.get("prices", {}),
                                           "offer_status": fetched.get("offer_status", {})}
                    prices["mb_settled"] = fetched.get("settled_offer_ids", set())
                else:
                    prices[key] = fetched
            except Exception as exc:
                print(f"  [cashout] Price fetch timeout ({key}): {exc}")

    return prices


# ─── Cashout calculation ───────────────────────────────────────────────────────
#
# Matchbook back+lay offset (leg is back, offset is a LAY at counterpart lay odds L):
#   close_stake = S × O / L
#   net_pnl     = S × (O/L − 1)
#   Intuition: lay offset costs you only the backer's stake T, not T×L.
#
# SX Bet back+back offset (leg is back, offset is a TAKER BACK on the opposite outcome):
#   close_stake = S × O / C
#   locked_return = S × O  (guaranteed regardless of which outcome wins)
#   net_pnl     = S × O − S − close_stake = S × (O − 1 − O/C)
#   Intuition: you pay close_stake in full and receive close_stake × C if offset wins.
#
# For Polymarket, the portfolio already provides the mark-to-market value directly.

def calc_cashout(arb: OpenArb, prices: dict) -> list[LegCashout]:
    results: list[LegCashout] = []

    for leg in arb.legs:

        # ── Polymarket ────────────────────────────────────────────────────────
        if leg.platform == "Polymarket":
            if leg.token_id and leg.token_id in prices.get("pm_redeemable", set()):
                results.append(LegCashout(leg=leg, close_method=_RESOLVED))
                continue
            pos = prices["pm"].get(leg.token_id or "")
            if pos and pos.get("size", 0) > 0 and pos.get("cur", 0) > 0:
                results.append(LegCashout(
                    leg=leg,
                    close_method=_SELL_TOKEN,
                    current_odds=round(1.0 / pos["cur"], 4),
                    net_pnl=pos.get("pnl"),
                    pm_size=pos["size"],
                    currency="USD",
                ))
            else:
                results.append(LegCashout(leg=leg, close_method=_UNAVAILABLE))

        # ── Matchbook ────────────────────────────────────────────────────────
        elif leg.platform == "Matchbook":
            if not leg.offer_id:
                results.append(LegCashout(leg=leg, close_method=_UNAVAILABLE))
                continue
            if leg.offer_id in prices.get("mb_settled", set()):
                results.append(LegCashout(leg=leg, close_method=_RESOLVED))
                continue

            # Prefer live status from portfolio; fall back to logged placement status
            live_status = (prices["mb"].get("offer_status") or {}).get(leg.offer_id)
            mb_status   = live_status if live_status is not None else leg.mb_status

            mb_prices = (prices["mb"].get("prices") or {}).get(
                (leg.event_id, leg.market_id, leg.runner_id), {}
            )
            counterpart_odds = (mb_prices.get("lay_odds")  if leg.side == "back"
                                else mb_prices.get("back_odds"))

            if mb_status == "open":
                # Unmatched: cancel returns full stake, net P&L = 0
                results.append(LegCashout(
                    leg=leg,
                    close_method=_CANCEL_OFFER,
                    current_odds=counterpart_odds,
                    net_pnl=0.0,
                    currency="GBP",
                ))
            else:
                # Matched (or unknown): need an offsetting back/lay
                if counterpart_odds and leg.original_odds:
                    close_stake = leg.amount * leg.original_odds / counterpart_odds
                    net_pnl     = leg.amount * (leg.original_odds / counterpart_odds - 1)
                    results.append(LegCashout(
                        leg=leg,
                        close_method=_PLACE_OFFSET,
                        current_odds=counterpart_odds,
                        close_stake=close_stake,
                        net_pnl=net_pnl,
                        currency="GBP",
                    ))
                else:
                    results.append(LegCashout(leg=leg, close_method=_UNAVAILABLE,
                                              current_odds=counterpart_odds))

        # ── SX Bet ───────────────────────────────────────────────────────────
        elif leg.platform == "SX Bet":
            sx_data  = (prices["sx"] or {}).get(leg.market_hash or "")
            opposite = "two" if (leg.outcome or "one") == "one" else "one"
            current_odds = (sx_data or {}).get(f"outcome_{opposite}")

            if leg.take is False and leg.order_hash:
                # Unmatched maker order: cancel, net P&L = 0
                results.append(LegCashout(
                    leg=leg,
                    close_method=_CANCEL_SX,
                    current_odds=current_odds,
                    net_pnl=0.0,
                    currency="USD",
                ))
            else:
                # Taker fill: place offsetting taker on opposite outcome
                if current_odds and leg.original_odds:
                    close_stake = leg.amount * leg.original_odds / current_odds
                    net_pnl     = leg.amount * (leg.original_odds - 1) - close_stake
                    results.append(LegCashout(
                        leg=leg,
                        close_method=_TAKER_OFFSET,
                        current_odds=current_odds,
                        close_stake=close_stake,
                        net_pnl=net_pnl,
                        currency="USD",
                    ))
                else:
                    results.append(LegCashout(leg=leg, close_method=_UNAVAILABLE,
                                              current_odds=current_odds))

    return results


def _is_arb_resolved(arb: OpenArb, cashouts: list[LegCashout]) -> bool:
    """True if all legs are confirmed resolved (not just price-missing).

    Requires at least one leg with _RESOLVED status (definitive platform signal)
    and the game to have already started, so we don't hide arbs due to fetch failures.
    """
    if not cashouts:
        return False
    game_started = _parse_ts(arb.game_time) < datetime.now(timezone.utc)
    all_done     = all(co.close_method in (_RESOLVED, _UNAVAILABLE) for co in cashouts)
    any_resolved = any(co.close_method == _RESOLVED for co in cashouts)
    return game_started and all_done and any_resolved


# ─── Display ──────────────────────────────────────────────────────────────────

_HR = "─" * 62


def _fmt_pnl(pnl: float | None, currency: str = "USD") -> str:
    if pnl is None:
        return "?"
    sym = "GBP " if currency == "GBP" else "$"
    return (f"+{sym}{pnl:.2f}" if pnl >= 0 else f"-{sym}{abs(pnl):.2f}")


def display_list(arbs: list[OpenArb], all_cashouts: list[list[LegCashout]]) -> None:
    print()
    print(f"  {_HR}")
    print(f"  {'#':<4} {'Game':<34} {'League':<10} {'Orig':>7}  Cashout net P&L")
    print(f"  {_HR}")
    for i, (arb, cashouts) in enumerate(zip(arbs, all_cashouts)):
        usd = sum(co.net_pnl for co in cashouts if co.net_pnl is not None and co.currency == "USD")
        gbp = sum(co.net_pnl for co in cashouts if co.net_pnl is not None and co.currency == "GBP")
        has_usd = any(co.net_pnl is not None and co.currency == "USD" for co in cashouts)
        has_gbp = any(co.net_pnl is not None and co.currency == "GBP" for co in cashouts)
        parts   = (([_fmt_pnl(usd, "USD")] if has_usd else []) +
                   ([_fmt_pnl(gbp, "GBP")] if has_gbp else []))
        net_str = "  ".join(parts) if parts else "?"
        flag    = " *" if any(co.close_method == _UNAVAILABLE for co in cashouts) else ""
        game    = f"{arb.team1} vs {arb.team2}"[:34]
        print(f"  [{i+1}]  {game:<34} {arb.league.upper():<10} "
              f"{arb.profit_pct:+.2f}%  {net_str}{flag}")
    print(f"  {_HR}")
    if any(any(co.close_method == _UNAVAILABLE for co in cs) for cs in all_cashouts):
        print("  * one or more legs unavailable (price fetch failed)")
    print()


def display_detail(arb: OpenArb, cashouts: list[LegCashout]) -> None:
    print()
    print(f"  {_HR}")
    print(f"  {arb.team1} vs {arb.team2}  [{arb.league.upper()}]  ({arb.arb_type})")
    print(f"  Game: {arb.game_time[:16]}   Placed: {arb.placed_at[:16]}   "
          f"Orig profit: {arb.profit_pct:+.2f}%")
    print(f"  {_HR}")

    for i, co in enumerate(cashouts):
        leg = co.leg
        print(f"  [{i + 1}]  {leg.platform:<12}  {leg.side:<4}  "
              f"stake={leg.amount:.2f}  orig_odds={leg.original_odds:.4f}")

        if co.close_method == _SELL_TOKEN:
            cur = f"{co.current_odds:.4f}" if co.current_odds else "?"
            print(f"       SELL {co.pm_size:.2f} shares  cur_odds={cur}  "
                  f"net P&L={_fmt_pnl(co.net_pnl, co.currency)}")

        elif co.close_method == _CANCEL_OFFER:
            cur = f"{co.current_odds:.4f}" if co.current_odds else "?"
            print(f"       CANCEL unmatched offer (id={leg.offer_id})  "
                  f"cur_counterpart={cur}  net P&L=+GBP 0.00 (stake returned)")

        elif co.close_method == _PLACE_OFFSET:
            side = "LAY" if leg.side == "back" else "BACK"
            cur  = f"{co.current_odds:.4f}" if co.current_odds else "?"
            stk  = f"GBP {co.close_stake:.2f}" if co.close_stake is not None else "?"
            print(f"       {side} offset  cur_odds={cur}  close_stake={stk}  "
                  f"net P&L={_fmt_pnl(co.net_pnl, co.currency)}")

        elif co.close_method == _CANCEL_SX:
            cur = f"{co.current_odds:.4f}" if co.current_odds else "?"
            oh  = f"...{leg.order_hash[-8:]}" if leg.order_hash else "?"
            print(f"       CANCEL maker order ({oh})  cur_counterpart={cur}  "
                  f"net P&L=+$0.00 (no stake at risk)")

        elif co.close_method == _TAKER_OFFSET:
            opp = "two" if (leg.outcome or "one") == "one" else "one"
            cur = f"{co.current_odds:.4f}" if co.current_odds else "?"
            stk = f"${co.close_stake:.2f}" if co.close_stake is not None else "?"
            print(f"       TAKER outcome_{opp}  cur_odds={cur}  close_stake={stk}  "
                  f"net P&L={_fmt_pnl(co.net_pnl, co.currency)}")

        elif co.close_method == _RESOLVED:
            print(f"       RESOLVED — market settled, position no longer open")

        else:
            print(f"       UNAVAILABLE — price not found")
        print()

    usd = sum(co.net_pnl for co in cashouts if co.net_pnl is not None and co.currency == "USD")
    gbp = sum(co.net_pnl for co in cashouts if co.net_pnl is not None and co.currency == "GBP")
    has_usd = any(co.net_pnl is not None and co.currency == "USD" for co in cashouts)
    has_gbp = any(co.net_pnl is not None and co.currency == "GBP" for co in cashouts)
    parts   = (([f"USD {_fmt_pnl(usd, 'USD')}"] if has_usd else []) +
               ([f"GBP {_fmt_pnl(gbp, 'GBP')}"] if has_gbp else []))
    if parts:
        print(f"  Combined cashout net P&L: {'  '.join(parts)}")
    print(f"  {_HR}")
    print()


# ─── Execution ────────────────────────────────────────────────────────────────

def _log_cashout(entry: dict) -> None:
    import datetime as _dt
    record = {"timestamp": _dt.datetime.utcnow().isoformat() + "Z", "status": "CASHOUT", **entry}
    _CASHOUT_LOG.parent.mkdir(exist_ok=True)
    with _CASHOUT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def execute_close(co: LegCashout, settings, dry_run: bool = False) -> dict:
    """Dispatch to the appropriate close function for this leg."""
    from bet import pm_place_bet, mb_place_bet, sx_place_bet
    from portfolio import cancel_matchbook_offer, cancel_sx_order

    leg = co.leg

    if co.close_method == _UNAVAILABLE:
        return {"ok": False, "error": "No price data — cannot close automatically"}

    if co.close_method == _SELL_TOKEN:
        if not co.pm_size:
            return {"ok": False, "error": "Share size unavailable"}
        result = pm_place_bet(settings, leg.token_id, co.pm_size, side="SELL", dry_run=dry_run)
        if not dry_run and result.get("ok"):
            _log_cashout({"platform": "Polymarket", "action": "sell",
                          "token_id": leg.token_id, "shares": co.pm_size, "result": result})
        return result

    if co.close_method == _CANCEL_OFFER:
        if dry_run:
            print(f"    [dry-run] Would cancel Matchbook offer {leg.offer_id}")
            return {"ok": True, "dry_run": True}
        cancel_matchbook_offer(settings, leg.offer_id)
        _log_cashout({"platform": "Matchbook", "action": "cancel", "offer_id": leg.offer_id})
        return {"ok": True}

    if co.close_method == _PLACE_OFFSET:
        if not co.close_stake:
            return {"ok": False, "error": "Close stake not computed"}
        offset_side = "lay" if leg.side == "back" else "back"
        # Pass odds=0.0 so mb_place_bet fetches current best price automatically
        result = mb_place_bet(settings, leg.event_id, leg.market_id, leg.runner_id,
                              stake=co.close_stake, side=offset_side, odds=0.0,
                              dry_run=dry_run)
        if not dry_run and result.get("ok"):
            _log_cashout({"platform": "Matchbook", "action": "offset",
                          "event_id": leg.event_id, "side": offset_side,
                          "stake": co.close_stake, "result": result})
        return result

    if co.close_method == _CANCEL_SX:
        if not leg.order_hash:
            return {"ok": False, "error": "No order hash in log"}
        if dry_run:
            print(f"    [dry-run] Would cancel SX Bet order ...{leg.order_hash[-16:]}")
            return {"ok": True, "dry_run": True}
        cancel_sx_order(settings, leg.order_hash)
        _log_cashout({"platform": "SX Bet", "action": "cancel", "order_hash": leg.order_hash})
        return {"ok": True}

    if co.close_method == _TAKER_OFFSET:
        if not co.close_stake:
            return {"ok": False, "error": "Close stake not computed"}
        opposite = "two" if (leg.outcome or "one") == "one" else "one"
        result = sx_place_bet(settings, leg.market_hash, amount=co.close_stake,
                              outcome=opposite, take=True, dry_run=dry_run)
        if not dry_run and result.get("ok"):
            _log_cashout({"platform": "SX Bet", "action": "taker_offset",
                          "market_hash": leg.market_hash, "outcome": opposite,
                          "amount": co.close_stake, "result": result})
        return result

    return {"ok": False, "error": f"Unknown close method: {co.close_method}"}


# ─── Interactive loop ─────────────────────────────────────────────────────────

def _ask(prompt: str) -> str:
    try:
        return input(f"  {prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "0"


def _pause() -> None:
    try:
        input("  Press Enter to continue...")
    except (EOFError, KeyboardInterrupt):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive arb cashout: view open positions and close legs early."
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate closes without placing orders")
    parser.add_argument("--max-age-hours", type=int, default=24, metavar="N",
                        help="Show arbs placed in the last N hours (default: 24)")
    args = parser.parse_args()

    settings = load_settings(_ROOT)

    if args.dry_run:
        print()
        print("  [DRY RUN — no orders will be placed]")

    print()
    print("  Loading open arbs from bet_log.jsonl ...")
    arbs = load_open_arbs(_BET_LOG, max_age_hours=args.max_age_hours)

    if not arbs:
        print("  No open arbs found in the log.")
        print(f"  (Searched the last {args.max_age_hours}h of bet_log.jsonl)")
        _pause()
        return

    print(f"  Found {len(arbs)} arb(s) in log.  Fetching current prices ...")
    prices       = fetch_all_prices(arbs, settings)
    all_cashouts = [calc_cashout(arb, prices) for arb in arbs]

    resolved_count = sum(1 for a, cs in zip(arbs, all_cashouts) if _is_arb_resolved(a, cs))
    if resolved_count:
        pairs        = [(a, cs) for a, cs in zip(arbs, all_cashouts)
                        if not _is_arb_resolved(a, cs)]
        arbs         = [p[0] for p in pairs]
        all_cashouts = [p[1] for p in pairs]
        print(f"  {resolved_count} resolved arb(s) hidden.  {len(arbs)} still open.")

    if not arbs:
        print("  No open arbs remaining.")
        _pause()
        return

    selected: int | None = None

    while True:
        if selected is None:
            display_list(arbs, all_cashouts)
            print("  [N]  View arb detail     [r]  Refresh prices     [0]  Quit")
            c = _ask("Choice")
            if c == "0":
                break
            elif c.lower() == "r":
                print("  Refreshing ...")
                prices       = fetch_all_prices(arbs, settings)
                all_cashouts = [calc_cashout(arb, prices) for arb in arbs]
            else:
                try:
                    n = int(c) - 1
                    if 0 <= n < len(arbs):
                        selected = n
                    else:
                        print(f"  Enter 1-{len(arbs)}")
                except ValueError:
                    print("  Invalid choice")

        else:
            arb      = arbs[selected]
            cashouts = all_cashouts[selected]
            display_detail(arb, cashouts)

            closeable = [i for i, co in enumerate(cashouts)
                         if co.close_method != _UNAVAILABLE]
            if not closeable:
                print("  No closeable legs (all prices unavailable).")
                _pause()
                selected = None
                continue

            dry_tag = "  [DRY RUN]" if args.dry_run else ""
            print(f"  [a]  Close ALL legs{dry_tag}")
            print(f"  [1-{len(cashouts)}]  Close specific leg{dry_tag}")
            print(f"  [0]  Back to list")
            c = _ask("Choice")

            if c == "0":
                selected = None

            elif c.lower() == "a":
                print()
                for i in closeable:
                    co  = cashouts[i]
                    leg = co.leg
                    print(f"  Closing leg {i + 1}: {leg.platform} ({co.close_method}) ...")
                    result = execute_close(co, settings, dry_run=args.dry_run)
                    if result.get("ok"):
                        tag = " [dry-run]" if result.get("dry_run") else ""
                        print(f"    OK{tag}")
                    else:
                        print(f"    FAILED: {result.get('error', 'unknown')}")
                _pause()
                print("  Refreshing ...")
                prices       = fetch_all_prices(arbs, settings)
                all_cashouts = [calc_cashout(arb, prices) for arb in arbs]
                selected     = None

            else:
                try:
                    idx = int(c) - 1
                    if not (0 <= idx < len(cashouts)):
                        print(f"  Enter 1-{len(cashouts)}")
                        continue
                    co = cashouts[idx]
                    if co.close_method == _UNAVAILABLE:
                        print("  Leg is unavailable — cannot close automatically.")
                        _pause()
                        continue
                    leg = co.leg
                    print(f"  Closing {leg.platform} leg ({co.close_method}) ...")
                    result = execute_close(co, settings, dry_run=args.dry_run)
                    if result.get("ok"):
                        tag = " [dry-run]" if result.get("dry_run") else ""
                        print(f"  OK{tag}")
                    else:
                        print(f"  FAILED: {result.get('error', 'unknown')}")
                    _pause()
                    print("  Refreshing ...")
                    prices       = fetch_all_prices(arbs, settings)
                    all_cashouts = [calc_cashout(arb, prices) for arb in arbs]
                except ValueError:
                    print("  Invalid choice")


if __name__ == "__main__":
    main()
