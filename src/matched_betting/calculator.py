"""
calculator.py
-------------
Pure arbitrage maths: commission-adjusted effective odds, sure-bet detection,
and back-lay arb detection.

Commission rates are not hardcoded — call configure() with a CommissionSettings
instance (loaded from config.py / .env) before running any calculations.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from urllib.request import urlopen, build_opener, ProxyHandler

if TYPE_CHECKING:
    from matched_betting.config import CommissionSettings

# ---------------------------------------------------------------------------
# FX helper (USD → GBP for display purposes)
# ---------------------------------------------------------------------------

_usd_to_gbp_cache: tuple[float, float] | None = None  # (rate, monotonic_ts)
_FX_TTL = 1800  # 30 minutes


def _usd_to_gbp() -> float:
    global _usd_to_gbp_cache
    if _usd_to_gbp_cache is not None:
        rate, fetched_at = _usd_to_gbp_cache
        if time.monotonic() - fetched_at < _FX_TTL:
            return rate
    try:
        _opener = build_opener(ProxyHandler({}))
        with _opener.open("https://open.er-api.com/v6/latest/GBP", timeout=5) as r:
            data = json.loads(r.read())
        rate = 1.0 / data["rates"]["USD"]
        _usd_to_gbp_cache = (rate, time.monotonic())
        return rate
    except Exception as exc:
        print(
            f"WARNING: FX rate fetch failed ({exc}); using fallback rate 0.79 GBP/USD",
            file=sys.stderr,
        )
        return 0.79


def _to_gbp(amount: float | None, currency: str) -> float | None:
    if amount is None:
        return None
    if currency == "GBP":
        return amount
    if currency == "USD":
        return round(amount * _usd_to_gbp(), 2)
    return amount


# ---------------------------------------------------------------------------
# Commission — module-level state, populated via configure()
# ---------------------------------------------------------------------------

SMARKETS_ZERO_COMMISSION_PERIOD: bool = True

PROVIDER_COMMISSION: dict[str, float] = {
    "matchbook":  0.02,
    "smarkets":   0.00,  # overridden by configure()
    "sx_bet":     0.00,
    "polymarket": 0.00,
    "azuro":      0.00,
}


def configure(commission: "CommissionSettings") -> None:
    """Apply commission settings loaded from .env.  Call once at startup."""
    global SMARKETS_ZERO_COMMISSION_PERIOD
    SMARKETS_ZERO_COMMISSION_PERIOD = commission.smarkets_zero_commission_period
    PROVIDER_COMMISSION["matchbook"]  = commission.matchbook
    PROVIDER_COMMISSION["smarkets"]   = commission.smarkets
    PROVIDER_COMMISSION["sx_bet"]     = commission.sx_bet


# ---------------------------------------------------------------------------
# Odds field maps — which game-dict keys hold each provider's odds
# ---------------------------------------------------------------------------

BACK_ODDS_FIELDS: dict[str, list[str]] = {
    "team1": [
        "polymarket_team1_back_odds",
        "matchbook_team1_back_odds",
        "smarkets_team1_back_odds",
        "sx_bet_team1_back_odds",
        "azuro_team1_back_odds",
    ],
    "draw": [
        "polymarket_draw_back_odds",
        "matchbook_draw_back_odds",
        "smarkets_draw_back_odds",
        "sx_bet_draw_back_odds",
        "azuro_draw_back_odds",
    ],
    "team2": [
        "polymarket_team2_back_odds",
        "matchbook_team2_back_odds",
        "smarkets_team2_back_odds",
        "sx_bet_team2_back_odds",
        "azuro_team2_back_odds",
    ],
}

LAY_ODDS_FIELDS: dict[str, list[str]] = {
    "team1": [
        "polymarket_team1_lay_odds",
        "matchbook_team1_lay_odds",
        "smarkets_team1_lay_odds",
        "sx_bet_team1_lay_odds",
    ],
    "draw": [
        "polymarket_draw_lay_odds",
        "matchbook_draw_lay_odds",
        "smarkets_draw_lay_odds",
        "sx_bet_draw_lay_odds",
    ],
    "team2": [
        "polymarket_team2_lay_odds",
        "matchbook_team2_lay_odds",
        "smarkets_team2_lay_odds",
        "sx_bet_team2_lay_odds",
    ],
}


# ---------------------------------------------------------------------------
# Commission helpers
# ---------------------------------------------------------------------------

def _polymarket_fee_rate(decimal_odds: float) -> float:
    """Polymarket sports fee: 0.0075 × 4 × p × (1−p) on stake, max 0.75% at p=0.5."""
    p = 1.0 / decimal_odds if decimal_odds > 1 else 0.5
    return 0.0075 * 4 * p * (1.0 - p)


def _eff_back_odds(odds: float, provider: str) -> float:
    """Effective back odds after provider commission."""
    c = _polymarket_fee_rate(odds) if provider == "polymarket" else PROVIDER_COMMISSION.get(provider, 0.0)
    return 1.0 + (odds - 1.0) * (1.0 - c)


def _eff_lay_odds(odds: float, provider: str) -> float:
    """Effective lay cost after provider commission (higher = worse for the arb)."""
    if provider == "polymarket":
        # Polymarket fee is charged on stake (order size), not on net winnings.
        # Correct break-even: eff_lay = L / (1 - (L-1)*f), not the Matchbook formula.
        fee = _polymarket_fee_rate(odds)
        return odds / (1.0 - (odds - 1.0) * fee)
    c = PROVIDER_COMMISSION.get(provider, 0.0)
    if c == 0.0:
        return odds
    return 1.0 + (odds - 1.0) / (1.0 - c)


# ---------------------------------------------------------------------------
# Best-odds helpers
# ---------------------------------------------------------------------------

def _provider_from_field(field: str) -> str:
    """Extract provider name from an odds field like 'sx_bet_team1_back_odds'."""
    return "_".join(field.split("_")[:-3])


def _best_back(game: dict, slot: str) -> tuple[float, str] | tuple[None, None]:
    """Highest effective back odds for a slot across all providers."""
    best_eff, best_odds, best_provider = None, None, None
    for field in BACK_ODDS_FIELDS[slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = _provider_from_field(field)
        eff = _eff_back_odds(odds, provider)
        if best_eff is None or eff > best_eff:
            best_eff, best_odds, best_provider = eff, odds, provider
    return best_odds, best_provider


def _best_lay(game: dict, slot: str) -> tuple[float, str] | tuple[None, None]:
    """Lowest effective lay cost for a slot across all providers."""
    best_eff, best_odds, best_provider = None, None, None
    for field in LAY_ODDS_FIELDS[slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = _provider_from_field(field)
        eff = _eff_lay_odds(odds, provider)
        if best_eff is None or eff < best_eff:
            best_eff, best_odds, best_provider = eff, odds, provider
    return best_odds, best_provider


def _outcome_label(game: dict, slot: str) -> str:
    return "Draw" if slot == "draw" else (game.get(slot) or slot)


def _game_started(date_time: str | None) -> bool:
    if not date_time:
        return False
    try:
        dt = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
        return dt < datetime.now(timezone.utc)
    except ValueError:
        return False


def _hours_until(date_time: str | None) -> float | None:
    """Hours from now until game start. Returns None if past or unparseable."""
    if not date_time:
        return None
    try:
        dt = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
        h = (dt - datetime.now(timezone.utc)).total_seconds() / 3600
        return h if h > 0 else None
    except ValueError:
        return None


def _profit_24h(profit_pct: float, date_time: str | None) -> float | None:
    """Compound-annualise profit_pct to a 24-hour rate. Returns None when hours unavailable."""
    h = _hours_until(date_time)
    if h is None:
        return None
    return round(((1 + profit_pct / 100) ** (24 / h) - 1) * 100, 4)


def _is_three_way(game: dict) -> bool:
    return game.get("market_type") == "three_way"


def _avail_str(game: dict, provider: str, slot: str, side: str) -> str:
    val = game.get(f"{provider}_{slot}_{side}_avail")
    if val is None:
        return ""
    currency = "USD" if provider in ("polymarket", "azuro") else "GBP"
    gbp = _to_gbp(val, currency)
    return f"  [max ~£{gbp:,.0f}]" if gbp is not None else ""


# ---------------------------------------------------------------------------
# Arb finders
# ---------------------------------------------------------------------------

def find_sure_bets(games: list[dict], min_profit_pct: float = 0.0) -> list[dict]:
    """Return sure bets (back-back[-back] across providers) sorted by net profit."""
    results = []
    for game in games:
        three_way = _is_three_way(game)
        team1_odds, team1_provider = _best_back(game, "team1")
        team2_odds, team2_provider = _best_back(game, "team2")

        if team1_odds is None or team2_odds is None:
            continue

        if three_way:
            draw_odds, draw_provider = _best_back(game, "draw")
            if draw_odds is None:
                continue
            gross_margin = (1 / team1_odds) + (1 / draw_odds) + (1 / team2_odds)
            net_margin = (
                (1 / _eff_back_odds(team1_odds, team1_provider))
                + (1 / _eff_back_odds(draw_odds, draw_provider))
                + (1 / _eff_back_odds(team2_odds, team2_provider))
            )
        else:
            draw_odds, draw_provider = None, None
            gross_margin = (1 / team1_odds) + (1 / team2_odds)
            net_margin = (
                (1 / _eff_back_odds(team1_odds, team1_provider))
                + (1 / _eff_back_odds(team2_odds, team2_provider))
            )

        if net_margin >= 1.0:
            continue

        providers_used = {team1_provider, team2_provider}
        if draw_provider:
            providers_used.add(draw_provider)
        if len(providers_used) == 1:
            continue

        net_profit_pct = (1 / net_margin - 1) * 100
        profit_24h_pct = _profit_24h(net_profit_pct, game.get("date_time"))
        effective_pct = min(net_profit_pct, profit_24h_pct) if profit_24h_pct is not None else net_profit_pct
        if effective_pct < min_profit_pct:
            continue

        result: dict = {
            "market_type": "three_way" if three_way else "two_way",
            "league": game.get("league"),
            "date_time": game.get("date_time"),
            "team1": game.get("team1"),
            "team2": game.get("team2"),
            "spread": game.get("spread"),
            "team1_back_odds": team1_odds,
            "team1_back_provider": team1_provider,
            "team1_back_avail": game.get(f"{team1_provider}_team1_back_avail"),
            "team2_back_odds": team2_odds,
            "team2_back_provider": team2_provider,
            "team2_back_avail": game.get(f"{team2_provider}_team2_back_avail"),
            "margin": round(gross_margin, 6),
            "profit_pct": round(net_profit_pct, 4),
            "profit_24h_pct": profit_24h_pct,
            "gross_profit_pct": round((1 / gross_margin - 1) * 100, 4),
        }
        if three_way:
            result["draw_back_odds"] = draw_odds
            result["draw_back_provider"] = draw_provider
            result["draw_back_avail"] = game.get(f"{draw_provider}_draw_back_avail")

        results.append(result)

    results.sort(
        key=lambda x: x["profit_24h_pct"] if x["profit_24h_pct"] is not None else x["profit_pct"],
        reverse=True,
    )
    return results


def find_back_lay_arbs(games: list[dict], min_profit_pct: float = 0.0) -> list[dict]:
    """Return back-lay arbs sorted by net profit."""
    results = []
    for game in games:
        slots = ("team1", "draw", "team2") if _is_three_way(game) else ("team1", "team2")

        for slot in slots:
            back_odds, back_provider = _best_back(game, slot)
            lay_odds, lay_provider = _best_lay(game, slot)

            if back_odds is None or lay_odds is None:
                continue

            if back_provider == lay_provider:
                continue

            eff_back = _eff_back_odds(back_odds, back_provider)
            eff_lay = _eff_lay_odds(lay_odds, lay_provider)

            if eff_back <= eff_lay:
                continue

            net_profit_pct = (eff_back / eff_lay - 1) * 100
            profit_24h_pct = _profit_24h(net_profit_pct, game.get("date_time"))
            effective_pct = min(net_profit_pct, profit_24h_pct) if profit_24h_pct is not None else net_profit_pct
            if effective_pct < min_profit_pct:
                continue

            results.append({
                "market_type": "three_way" if _is_three_way(game) else "two_way",
                "league": game.get("league"),
                "date_time": game.get("date_time"),
                "team1": game.get("team1"),
                "team2": game.get("team2"),
                "spread": game.get("spread"),
                "outcome_slot": slot,
                "arb_outcome": _outcome_label(game, slot),
                "back_odds": back_odds,
                "back_provider": back_provider,
                "back_avail": game.get(f"{back_provider}_{slot}_back_avail"),
                "lay_odds": lay_odds,
                "lay_provider": lay_provider,
                "lay_avail": game.get(f"{lay_provider}_{slot}_lay_avail"),
                "profit_pct": round(net_profit_pct, 4),
                "profit_24h_pct": profit_24h_pct,
                "gross_profit_pct": round((back_odds / lay_odds - 1) * 100, 4),
            })

    results.sort(
        key=lambda x: x["profit_24h_pct"] if x["profit_24h_pct"] is not None else x["profit_pct"],
        reverse=True,
    )
    return results


# ---------------------------------------------------------------------------
# Best-opportunity helpers (include losing positions — for debug display)
# ---------------------------------------------------------------------------

def best_sure_bet_opportunity(game: dict) -> dict | None:
    """Return the best sure-bet opportunity for a game even if it is a loss.

    Like find_sure_bets() but removes the 'net_margin >= 1' early exit so
    profit_pct may be negative.  Returns None if odds are missing.
    """
    three_way = _is_three_way(game)
    team1_odds, team1_provider = _best_back(game, "team1")
    team2_odds, team2_provider = _best_back(game, "team2")
    if team1_odds is None or team2_odds is None:
        return None

    if three_way:
        draw_odds, draw_provider = _best_back(game, "draw")
        if draw_odds is None:
            return None
        net_margin = (
            1 / _eff_back_odds(team1_odds, team1_provider)
            + 1 / _eff_back_odds(draw_odds, draw_provider)
            + 1 / _eff_back_odds(team2_odds, team2_provider)
        )
    else:
        draw_odds, draw_provider = None, None
        net_margin = (
            1 / _eff_back_odds(team1_odds, team1_provider)
            + 1 / _eff_back_odds(team2_odds, team2_provider)
        )

    result: dict = {
        "team1_back_odds": team1_odds,
        "team1_back_provider": team1_provider,
        "team2_back_odds": team2_odds,
        "team2_back_provider": team2_provider,
        "profit_pct": round((1 / net_margin - 1) * 100, 4),
    }
    if three_way:
        result["draw_back_odds"] = draw_odds
        result["draw_back_provider"] = draw_provider
    return result


def best_back_lay_opportunity(game: dict) -> dict | None:
    """Return the best back-lay opportunity for a game even if it is a loss.

    Like find_back_lay_arbs() but removes the 'eff_back <= eff_lay' early exit
    so profit_pct may be negative.  Picks the outcome with the highest profit.
    Returns None if back or lay odds are missing for every outcome.
    """
    slots = ("team1", "draw", "team2") if _is_three_way(game) else ("team1", "team2")
    best: dict | None = None
    for slot in slots:
        b_odds, b_prov = _best_back(game, slot)
        l_odds, l_prov = _best_lay(game, slot)
        if b_odds is None or l_odds is None:
            continue
        eff_b = _eff_back_odds(b_odds, b_prov)
        eff_l = _eff_lay_odds(l_odds, l_prov)
        pct = round((eff_b / eff_l - 1) * 100, 4)
        if best is None or pct > best["profit_pct"]:
            best = {
                "arb_outcome": _outcome_label(game, slot),
                "back_odds": b_odds,
                "back_provider": b_prov,
                "lay_odds": l_odds,
                "lay_provider": l_prov,
                "profit_pct": pct,
            }
    return best
