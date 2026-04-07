"""
arb_finder.py

Arbitrage finder for both two-way and three-way markets.

Detects market type automatically from each game's ``market_type`` field:
  - ``"three_way"`` markets (e.g. football with draw): sure bet requires all
    three outcomes (home / draw / away) with margin = sum of reciprocals < 1.
  - All other markets (two-way, e.g. NBA moneyline): sure bet requires both
    teams with margin = 1/t1 + 1/t2 < 1.

Two arbitrage strategies:

1. Sure bet (back-back[-back]): best back odds for each outcome across all
   providers. Arb exists when sum of reciprocals < 1.

2. Back-lay arb: best back odds for an outcome vs best lay odds for the same
   outcome on an exchange. Arb exists when back_odds > lay_odds (profit per
   unit = back_odds / lay_odds - 1).

Usage:
    python arb_finder.py
    python arb_finder.py --input outputs/latest_odds_aggregated_games.json
    python arb_finder.py --min-profit 0.5
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from urllib.request import urlopen

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

_usd_to_gbp_cache: float | None = None

def _usd_to_gbp() -> float:
    global _usd_to_gbp_cache
    if _usd_to_gbp_cache is not None:
        return _usd_to_gbp_cache
    try:
        with urlopen("https://open.er-api.com/v6/latest/GBP", timeout=5) as r:
            data = json.loads(r.read())
        rate = 1.0 / data["rates"]["USD"]  # GBP per 1 USD
        _usd_to_gbp_cache = rate
        return rate
    except Exception:
        return 0.79  # fallback


def _to_gbp(amount: float | None, currency: str) -> float | None:
    if amount is None:
        return None
    if currency == "GBP":
        return amount
    if currency == "USD":
        return round(amount * _usd_to_gbp(), 2)
    return amount


BACK_ODDS_FIELDS: dict[str, list[str]] = {
    "team1": [
        "polymarket_team1_back_odds",
        "matchbook_team1_back_odds",
        "smarkets_team1_back_odds",
        "sx_bet_team1_back_odds",
    ],
    "draw": [
        "polymarket_draw_back_odds",
        "matchbook_draw_back_odds",
        "smarkets_draw_back_odds",
        "sx_bet_draw_back_odds",
    ],
    "team2": [
        "polymarket_team2_back_odds",
        "matchbook_team2_back_odds",
        "smarkets_team2_back_odds",
        "sx_bet_team2_back_odds",
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


def _game_started(date_time: str | None) -> bool:
    """Return True if the game start time is in the past."""
    if not date_time:
        return False
    try:
        dt = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
        return dt < datetime.now(timezone.utc)
    except ValueError:
        return False


def _is_three_way(game: dict) -> bool:
    return game.get("market_type") == "three_way"


def _avail_str(game: dict, provider: str, slot: str, side: str) -> str:
    val = game.get(f"{provider}_{slot}_{side}_avail")
    if val is None:
        return ""
    currency = "USD" if provider == "polymarket" else "GBP"
    gbp = _to_gbp(val, currency)
    return f"  [max ~£{gbp:,.0f}]" if gbp is not None else ""


def _provider_from_field(field: str) -> str:
    """Extract provider name from an odds field like 'sx_bet_team1_back_odds'."""
    # Fields are always {provider}_{slot}_{side}_odds where slot ∈ team1/team2/draw
    # Split off the last 3 parts (_team1_back_odds etc.) to get the provider prefix
    return "_".join(field.split("_")[:-3])


# ---------------------------------------------------------------------------
# Commission helpers
# ---------------------------------------------------------------------------

# Set to True while within Smarkets' 60-day zero-commission introductory period.
SMARKETS_ZERO_COMMISSION_PERIOD = True

# Exchange commission on net winnings (back and lay)
PROVIDER_COMMISSION: dict[str, float] = {
    "matchbook": 0.02,   # 2% UK
    "smarkets":  0.00 if SMARKETS_ZERO_COMMISSION_PERIOD else 0.02,
    "sx_bet":    0.00,   # no commission
    "polymarket": 0.00,  # handled separately (dynamic fee)
}


def _polymarket_fee_rate(decimal_odds: float) -> float:
    """Polymarket sports fee: 0.0075 * 4 * p * (1-p) on stake, max 0.75% at p=0.5."""
    p = 1.0 / decimal_odds if decimal_odds > 1 else 0.5
    return 0.0075 * 4 * p * (1.0 - p)


def _eff_back_odds(odds: float, provider: str) -> float:
    """Effective back odds after provider commission."""
    if provider == "polymarket":
        fee = _polymarket_fee_rate(odds)
        return odds - fee  # fee deducted from payout per unit staked
    c = PROVIDER_COMMISSION.get(provider, 0.0)
    return 1.0 + (odds - 1.0) * (1.0 - c)


def _eff_lay_odds(odds: float, provider: str) -> float:
    """Effective lay cost after provider commission (higher = worse for arb)."""
    c = PROVIDER_COMMISSION.get(provider, 0.0)
    if c == 0.0:
        return odds
    return 1.0 + (odds - 1.0) / (1.0 - c)


def _best_back(game: dict, slot: str) -> tuple[float, str] | tuple[None, None]:
    """Return the best (highest effective) back odds across providers."""
    best_eff, best_odds, best_provider = None, None, None
    for field in BACK_ODDS_FIELDS[slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = _provider_from_field(field)
        eff = _eff_back_odds(odds, provider)
        if best_eff is None or eff > best_eff:
            best_eff = eff
            best_odds = odds
            best_provider = provider
    return best_odds, best_provider


def _best_lay(game: dict, slot: str) -> tuple[float, str] | tuple[None, None]:
    """Return the lowest effective lay cost across providers."""
    best_eff, best_odds, best_provider = None, None, None
    for field in LAY_ODDS_FIELDS[slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = _provider_from_field(field)
        eff = _eff_lay_odds(odds, provider)
        if best_eff is None or eff < best_eff:
            best_eff = eff
            best_odds = odds
            best_provider = provider
    return best_odds, best_provider


def _outcome_label(game: dict, slot: str) -> str:
    return "Draw" if slot == "draw" else (game.get(slot) or slot)


# ---------------------------------------------------------------------------
# Sure-bet finder (back-back[-back] across providers)
# ---------------------------------------------------------------------------

def find_sure_bets(games: list[dict], min_profit_pct: float = 0.0) -> list[dict]:
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
            t1_eff = _eff_back_odds(team1_odds, team1_provider)
            d_eff = _eff_back_odds(draw_odds, draw_provider)
            t2_eff = _eff_back_odds(team2_odds, team2_provider)
            net_margin = (1 / t1_eff) + (1 / d_eff) + (1 / t2_eff)
        else:
            draw_odds, draw_provider = None, None
            gross_margin = (1 / team1_odds) + (1 / team2_odds)
            t1_eff = _eff_back_odds(team1_odds, team1_provider)
            t2_eff = _eff_back_odds(team2_odds, team2_provider)
            net_margin = (1 / t1_eff) + (1 / t2_eff)

        if net_margin >= 1.0:
            continue

        net_profit_pct = (1 / net_margin - 1) * 100
        gross_profit_pct = (1 / gross_margin - 1) * 100
        if net_profit_pct < min_profit_pct:
            continue

        result: dict = {
            "market_type": "three_way" if three_way else "two_way",
            "league": game.get("league"),
            "date_time": game.get("date_time"),
            "team1": game.get("team1"),
            "team2": game.get("team2"),
            "team1_back_odds": team1_odds,
            "team1_back_provider": team1_provider,
            "team1_back_avail": game.get(f"{team1_provider}_team1_back_avail"),
            "team2_back_odds": team2_odds,
            "team2_back_provider": team2_provider,
            "team2_back_avail": game.get(f"{team2_provider}_team2_back_avail"),
            "margin": round(gross_margin, 6),
            "profit_pct": round(net_profit_pct, 4),
            "gross_profit_pct": round(gross_profit_pct, 4),
        }
        if three_way:
            result["draw_back_odds"] = draw_odds
            result["draw_back_provider"] = draw_provider
            result["draw_back_avail"] = game.get(f"{draw_provider}_draw_back_avail")

        results.append(result)

    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Back-lay arb finder
# ---------------------------------------------------------------------------

def find_back_lay_arbs(games: list[dict], min_profit_pct: float = 0.0) -> list[dict]:
    results = []
    for game in games:
        slots = ("team1", "draw", "team2") if _is_three_way(game) else ("team1", "team2")

        for slot in slots:
            back_odds, back_provider = _best_back(game, slot)
            lay_odds, lay_provider = _best_lay(game, slot)

            if back_odds is None or lay_odds is None:
                continue

            eff_back = _eff_back_odds(back_odds, back_provider)
            eff_lay = _eff_lay_odds(lay_odds, lay_provider)

            if eff_back <= eff_lay:
                continue

            net_profit_pct = (eff_back / eff_lay - 1) * 100
            gross_profit_pct = (back_odds / lay_odds - 1) * 100
            if net_profit_pct < min_profit_pct:
                continue

            results.append({
                "market_type": "three_way" if _is_three_way(game) else "two_way",
                "league": game.get("league"),
                "date_time": game.get("date_time"),
                "team1": game.get("team1"),
                "team2": game.get("team2"),
                "arb_outcome": _outcome_label(game, slot),
                "back_odds": back_odds,
                "back_provider": back_provider,
                "back_avail": game.get(f"{back_provider}_{slot}_back_avail"),
                "lay_odds": lay_odds,
                "lay_provider": lay_provider,
                "lay_avail": game.get(f"{lay_provider}_{slot}_lay_avail"),
                "profit_pct": round(net_profit_pct, 4),
                "gross_profit_pct": round(gross_profit_pct, 4),
            })

    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_sure_bets(arbs: list[dict]) -> None:
    if not arbs:
        print("  None found.\n")
        return
    for arb in arbs:
        started_flag = "  *** GAME STARTED ***" if _game_started(arb.get("date_time")) else ""
        def _avail(val, provider) -> str:
            if val is None: return ""
            cur = "USD" if provider == "polymarket" else "GBP"
            gbp = _to_gbp(val, cur)
            return f"  [max ~£{gbp:,.0f}]" if gbp is not None else ""
        lines = [
            f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})  [{arb['market_type']}]{started_flag}",
            f"    Back {arb['team1']:<30} {arb['team1_back_odds']:.4f}  ({arb['team1_back_provider']}){_avail(arb.get('team1_back_avail'), arb['team1_back_provider'])}",
        ]
        if arb["market_type"] == "three_way":
            lines.append(
                f"    Back {'Draw':<30} {arb['draw_back_odds']:.4f}  ({arb['draw_back_provider']}){_avail(arb.get('draw_back_avail'), arb['draw_back_provider'])}"
            )
        gross_str = f"  (gross: {arb['gross_profit_pct']:.4f}%)" if arb.get("gross_profit_pct") != arb.get("profit_pct") else ""
        lines += [
            f"    Back {arb['team2']:<30} {arb['team2_back_odds']:.4f}  ({arb['team2_back_provider']}){_avail(arb.get('team2_back_avail'), arb['team2_back_provider'])}",
            f"    Margin: {arb['margin']:.6f}  |  Net profit: {arb['profit_pct']:.4f}%{gross_str}",
            "",
        ]
        print("\n".join(lines))


def _print_back_lay_arbs(arbs: list[dict]) -> None:
    if not arbs:
        print("  None found.\n")
        return
    for arb in arbs:
        started_flag = "  *** GAME STARTED ***" if _game_started(arb.get("date_time")) else ""
        def _avail(val, provider) -> str:
            if val is None: return ""
            cur = "USD" if provider == "polymarket" else "GBP"
            gbp = _to_gbp(val, cur)
            return f"  [max ~£{gbp:,.0f}]" if gbp is not None else ""
        gross_str = f"  (gross: {arb['gross_profit_pct']:.4f}%)" if arb.get("gross_profit_pct") != arb.get("profit_pct") else ""
        print(
            f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})  [{arb['market_type']}]{started_flag}\n"
            f"    Back {arb['arb_outcome']:<30} {arb['back_odds']:.4f}  ({arb['back_provider']}){_avail(arb.get('back_avail'), arb['back_provider'])}\n"
            f"    Lay  {arb['arb_outcome']:<30} {arb['lay_odds']:.4f}  ({arb['lay_provider']}){_avail(arb.get('lay_avail'), arb['lay_provider'])}\n"
            f"    Net profit: {arb['profit_pct']:.4f}%{gross_str}\n"
        )


# ---------------------------------------------------------------------------
# Refresh / targeted per-market odds check
# ---------------------------------------------------------------------------

def _names_match(a: str, b: str) -> bool:
    """Fuzzy name match: handles accents, word reordering, and abbreviated names."""
    import unicodedata

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
        return s.lower().replace("-", " ").strip()

    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    # word-subset: all words in the shorter name appear in the longer
    wa, wb = set(a.split()), set(b.split())
    shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(shorter) > 0 and shorter.issubset(longer)


def _parse_str_json_list(val: object) -> list:
    if isinstance(val, list):
        return val
    if not val:
        return []
    if isinstance(val, str):
        return json.loads(val)
    return []


def _delta_str(old: float, new: float) -> str:
    return f"  [{new - old:+.4f}]"


def _polymarket_slot_for_outcome(game: dict, outcome_name: str) -> str:
    """Map an outcome name back to team1/draw/team2 slot."""
    if outcome_name.lower() in ("draw", "tie"):
        return "draw"
    if _names_match(outcome_name, game.get("team1") or ""):
        return "team1"
    if _names_match(outcome_name, game.get("team2") or ""):
        return "team2"
    return "team1"  # fallback


def _fetch_polymarket_leg(game: dict, outcome_name: str, side: str, http) -> float | None:
    # Use per-slot market ID (UCL has one Yes/No market per outcome)
    slot = _polymarket_slot_for_outcome(game, outcome_name)
    market_id = game.get(f"polymarket_{slot}_market_id") or game.get("polymarket_market_id")
    if not market_id:
        return None
    try:
        result = http.get_json(
            "https://gamma-api.polymarket.com/markets",
            params={"id": market_id},
        )
        market = result[0] if isinstance(result, list) and result else None
        if not market:
            return None
        outcomes = _parse_str_json_list(market.get("outcomes"))
        # Yes/No binary market (UCL): the market IS the outcome — return odds directly
        if {o.lower() for o in outcomes} <= {"yes", "no"}:
            if side == "lay":
                p = float(market["bestBid"])
            else:
                p = float(market["bestAsk"])
            return round(1.0 / p, 6) if p and 0 < p < 1 else None
        # Standard multi-outcome market: find by name
        idx = next((i for i, o in enumerate(outcomes) if _names_match(str(o), outcome_name)), None)
        if idx is None:
            return None
        if len(outcomes) == 2:
            p = float(market["bestAsk"]) if idx == 0 else 1.0 - float(market["bestBid"])
        else:
            prices = _parse_str_json_list(market.get("outcomePrices"))
            p = float(prices[idx])
        return round(1.0 / p, 6) if p and p > 0 else None
    except Exception:
        return None


def _fetch_smarkets_leg(game: dict, outcome_name: str, side: str, http) -> float | None:
    market_id = game.get("smarkets_market_id")
    if not market_id:
        return None
    try:
        contracts_payload = http.get_json(
            f"https://api.smarkets.com/v3/markets/{market_id}/contracts/",
            headers={"Accept": "application/json"},
        )
        quotes_payload = http.get_json(
            f"https://api.smarkets.com/v3/markets/{market_id}/quotes/",
            headers={"Accept": "application/json"},
        )
        for contract in contracts_payload.get("contracts", []):
            if not _names_match(str(contract.get("name", "")), outcome_name):
                continue
            cid = str(contract["id"])
            quote = quotes_payload.get(cid, {})
            levels = quote.get("offers" if side == "back" else "bids", [])
            if not levels:
                return None
            probability = float(levels[0]["price"]) / 10000.0
            return round(1.0 / probability, 6) if probability > 0 else None
        return None
    except Exception:
        return None


def _fetch_matchbook_leg(game: dict, outcome_name: str, side: str, http, settings) -> float | None:
    event_id = game.get("matchbook_event_id")
    if not event_id:
        return None
    _MB_MONEYLINE = {
        "match odds", "match winner", "money line", "moneyline",
        "winner (incl. overtime)", "winner (including overtime)",
    }
    try:
        if settings.matchbook.username and settings.matchbook.password:
            http.post_json(
                f"{settings.matchbook.base_url}/bpapi/rest/security/session",
                payload={"username": settings.matchbook.username, "password": settings.matchbook.password},
                headers={"Accept": "application/json"},
            )
        event_data = http.get_json(
            f"{settings.matchbook.base_url}/edge/rest/events/{event_id}",
            headers={"Accept": "application/json"},
        )
        for market in event_data.get("markets", []):
            if market.get("status") != "open":
                continue
            if str(market.get("name") or "").lower().strip() not in _MB_MONEYLINE:
                continue
            for runner in market.get("runners", []):
                if not _names_match(str(runner.get("name") or ""), outcome_name):
                    continue
                for price in runner.get("prices", []):
                    if str(price.get("side") or "").lower() == side:
                        odds = price.get("decimal-odds") or price.get("odds")
                        return float(odds) if odds is not None else None
        return None
    except Exception:
        return None


def _fetch_sx_bet_leg(game: dict, outcome_name: str, side: str, http, settings) -> float | None:
    market_hash = game.get("sx_bet_market_hash")
    if not market_hash or side == "lay":
        return None
    try:
        raw = http.get_json(
            f"{settings.sx_bet.base_url}/orders/odds/best",
            params={"marketHashes": market_hash, "baseToken": settings.sx_bet.base_token},
        )
        best_list = raw.get("data", {}).get("bestOdds", [])
        entry = next((e for e in best_list if e["marketHash"] == market_hash), None)
        if not entry:
            return None
        # team1 = teamOneName convention; taker odds for team1 come from outcomeTwo maker
        maker_data = entry.get("outcomeTwo") if _names_match(game.get("team1", ""), outcome_name) else entry.get("outcomeOne")
        if not maker_data:
            return None
        raw_pct = maker_data.get("percentageOdds")
        if raw_pct is None:
            return None
        taker_prob = 1.0 - int(raw_pct) / (10 ** 20)
        return round(1.0 / taker_prob, 6) if 0 < taker_prob < 1 else None
    except Exception:
        return None


def _fetch_leg(provider: str, game: dict, outcome_name: str, side: str, http, settings) -> float | None:
    if provider == "polymarket":
        return _fetch_polymarket_leg(game, outcome_name, side, http)
    if provider == "smarkets":
        return _fetch_smarkets_leg(game, outcome_name, side, http)
    if provider == "matchbook":
        return _fetch_matchbook_leg(game, outcome_name, side, http, settings)
    if provider == "sx_bet":
        return _fetch_sx_bet_leg(game, outcome_name, side, http, settings)
    return None


def _game_key(league: str | None, team1: str | None, team2: str | None, date_time: str | None = None) -> tuple:
    return ((league or "").lower(), (team1 or "").lower(), (team2 or "").lower(), date_time or "")


def _refresh_sure_bet(arb: dict, game: dict | None, http, settings, refreshed_at: str) -> None:
    print(f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}")
    print(f"    Original  Back {arb['team1']:<28} {arb['team1_back_odds']:.4f}  ({arb['team1_back_provider']})")
    if arb.get("draw_back_odds"):
        print(f"    Original  Back {'Draw':<28} {arb['draw_back_odds']:.4f}  ({arb['draw_back_provider']})")
    print(f"    Original  Back {arb['team2']:<28} {arb['team2_back_odds']:.4f}  ({arb['team2_back_provider']})")
    print(f"    Original profit: {arb['profit_pct']:.4f}%")

    if game is None:
        print("    [market IDs not available — re-run run.py to populate]\n")
        return

    print("    Refreshing... ", end="", flush=True)
    fresh_t1 = _fetch_leg(arb["team1_back_provider"], game, arb["team1"], "back", http, settings)
    fresh_draw = _fetch_leg(arb["draw_back_provider"], game, "Draw", "back", http, settings) if arb.get("draw_back_provider") else None
    fresh_t2 = _fetch_leg(arb["team2_back_provider"], game, arb["team2"], "back", http, settings)
    print(f"done  ({refreshed_at})\n")

    t1_str = f"{fresh_t1:.4f}{_delta_str(arb['team1_back_odds'], fresh_t1)}" if fresh_t1 else "N/A"
    print(f"    Fresh     Back {arb['team1']:<28} {t1_str}  ({arb['team1_back_provider']})")
    if arb.get("draw_back_odds"):
        d_str = f"{fresh_draw:.4f}{_delta_str(arb['draw_back_odds'], fresh_draw)}" if fresh_draw else "N/A"
        print(f"    Fresh     Back {'Draw':<28} {d_str}  ({arb['draw_back_provider']})")
    t2_str = f"{fresh_t2:.4f}{_delta_str(arb['team2_back_odds'], fresh_t2)}" if fresh_t2 else "N/A"
    print(f"    Fresh     Back {arb['team2']:<28} {t2_str}  ({arb['team2_back_provider']})")

    fresh_odds_providers = [
        (fresh_t1, arb["team1_back_provider"]),
        (fresh_draw, arb.get("draw_back_provider")) if arb.get("draw_back_odds") else (None, None),
        (fresh_t2, arb["team2_back_provider"]),
    ]
    valid_legs = [(o, p) for o, p in fresh_odds_providers if o is not None and o > 0 and p]
    expected_legs = 3 if arb.get("draw_back_odds") else 2
    if len(valid_legs) == expected_legs:
        fresh_gross_margin = sum(1.0 / o for o, _ in valid_legs)
        fresh_net_margin = sum(1.0 / _eff_back_odds(o, p) for o, p in valid_legs)
        if fresh_net_margin < 1.0:
            fresh_net_profit = (1.0 / fresh_net_margin - 1) * 100
            fresh_gross_profit = (1.0 / fresh_gross_margin - 1) * 100
            gross_str = f"  (gross: {fresh_gross_profit:.4f}%)" if abs(fresh_gross_profit - fresh_net_profit) > 0.0001 else ""
            print(f"    Fresh net profit: {fresh_net_profit:.4f}%{gross_str}  (was {arb['profit_pct']:.4f}%, {fresh_net_profit - arb['profit_pct']:+.4f}pp)  --> ARB STILL VALID")
        else:
            print(f"    Fresh net profit: net margin {fresh_net_margin:.6f} >= 1  --> ARB GONE")
    else:
        print("    Fresh profit: N/A (could not fetch all odds)")
    print()


def _refresh_back_lay_arb(arb: dict, game: dict | None, http, settings, refreshed_at: str) -> None:
    print(f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}  ({arb['arb_outcome']})")
    print(f"    Original  Back {arb['arb_outcome']:<28} {arb['back_odds']:.4f}  ({arb['back_provider']})")
    print(f"    Original  Lay  {arb['arb_outcome']:<28} {arb['lay_odds']:.4f}  ({arb['lay_provider']})")
    print(f"    Original profit: {arb['profit_pct']:.4f}%")

    if game is None:
        print("    [market IDs not available — re-run run.py to populate]\n")
        return

    print("    Refreshing... ", end="", flush=True)
    fresh_back = _fetch_leg(arb["back_provider"], game, arb["arb_outcome"], "back", http, settings)
    fresh_lay = _fetch_leg(arb["lay_provider"], game, arb["arb_outcome"], "lay", http, settings)
    print(f"done  ({refreshed_at})\n")

    b_str = f"{fresh_back:.4f}{_delta_str(arb['back_odds'], fresh_back)}" if fresh_back else "N/A"
    print(f"    Fresh     Back {arb['arb_outcome']:<28} {b_str}  ({arb['back_provider']})")
    l_str = f"{fresh_lay:.4f}{_delta_str(arb['lay_odds'], fresh_lay)}" if fresh_lay else "N/A"
    print(f"    Fresh     Lay  {arb['arb_outcome']:<28} {l_str}  ({arb['lay_provider']})")

    if fresh_back and fresh_lay:
        eff_b = _eff_back_odds(fresh_back, arb["back_provider"])
        eff_l = _eff_lay_odds(fresh_lay, arb["lay_provider"])
        if eff_b > eff_l:
            fresh_net_profit = (eff_b / eff_l - 1) * 100
            fresh_gross_profit = (fresh_back / fresh_lay - 1) * 100
            gross_str = f"  (gross: {fresh_gross_profit:.4f}%)" if abs(fresh_gross_profit - fresh_net_profit) > 0.0001 else ""
            print(f"    Fresh net profit: {fresh_net_profit:.4f}%{gross_str}  (was {arb['profit_pct']:.4f}%, {fresh_net_profit - arb['profit_pct']:+.4f}pp)  --> ARB STILL VALID")
        else:
            print(f"    Fresh net profit: eff_back {eff_b:.4f} <= eff_lay {eff_l:.4f}  --> ARB GONE")
    else:
        print("    Fresh profit: N/A (could not fetch all odds)")
    print()


def run_refresh(sure_bets: list[dict], back_lay_arbs: list[dict], original_games: list[dict]) -> None:
    from matched_betting.http import HttpClient
    from matched_betting.config import load_settings

    if not sure_bets and not back_lay_arbs:
        return

    http = HttpClient()
    settings = load_settings(Path(__file__).resolve().parent)

    # Build lookup by (league, team1, team2, date_time) — date disambiguates multi-day markets
    games_lookup: dict[tuple, dict] = {}
    for g in original_games:
        dt = g.get("date_time")
        games_lookup[_game_key(g.get("league"), g.get("team1"), g.get("team2"), dt)] = g
        games_lookup[_game_key(g.get("league"), g.get("team2"), g.get("team1"), dt)] = g

    refreshed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    print(f"\n{'='*60}")
    print(f"=== Odds refresh (targeted market fetch) ===")
    print(f"{'='*60}\n")

    if sure_bets:
        print(f"--- Sure bet refresh ({len(sure_bets)}) ---\n")
        for arb in sure_bets:
            game = games_lookup.get(_game_key(arb.get("league"), arb.get("team1"), arb.get("team2"), arb.get("date_time")))
            _refresh_sure_bet(arb, game, http, settings, refreshed_at)

    if back_lay_arbs:
        print(f"--- Back-lay arb refresh ({len(back_lay_arbs)}) ---\n")
        for arb in back_lay_arbs:
            game = games_lookup.get(_game_key(arb.get("league"), arb.get("team1"), arb.get("team2"), arb.get("date_time")))
            _refresh_back_lay_arb(arb, game, http, settings, refreshed_at)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Find arbitrage opportunities from aggregated odds.")
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("outputs/latest_odds_aggregated_games.json"),
        help="Path to the aggregated games JSON file.",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Minimum profit percentage to report (default: 0.0).",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip the odds refresh check after identifying arbs.",
    )
    args = parser.parse_args()

    input_path = args.input
    if not input_path.is_absolute():
        input_path = Path(__file__).parent / input_path

    data = json.loads(input_path.read_text(encoding="utf-8"))
    games = data.get("aggregated_games", [])

    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sure_bets = find_sure_bets(games, min_profit_pct=args.min_profit)
    back_lay_arbs = find_back_lay_arbs(games, min_profit_pct=args.min_profit)

    smarkets_note = (
        "Smarkets: 0% commission assumed (60-day intro period)"
        if SMARKETS_ZERO_COMMISSION_PERIOD
        else "Smarkets: 2% standard commission assumed (intro period OVER — update SMARKETS_ZERO_COMMISSION_PERIOD)"
    )
    print(f"Arb finder run:  {run_at}")
    print(f"Commission assumptions: Matchbook 2% | {smarkets_note} | SX Bet 0% | Polymarket dynamic\n")
    print(f"=== Sure bets: {len(sure_bets)} found ===\n")
    _print_sure_bets(sure_bets)

    print(f"=== Back-lay arbs: {len(back_lay_arbs)} found ===\n")
    _print_back_lay_arbs(back_lay_arbs)

    if not args.no_refresh:
        run_refresh(sure_bets, back_lay_arbs, games)

    print(f"{'='*60}")
    print(f"Summary  ({run_at})")
    print(f"  Games loaded : {len(games)}")
    from collections import Counter
    league_counts = Counter(g.get("league", "?") for g in games)
    for league, count in sorted(league_counts.items()):
        print(f"    {league.upper():<6} {count} games")
    print(f"  Sure bets    : {len(sure_bets)}")
    print(f"  Back-lay arbs: {len(back_lay_arbs)}")
    if sure_bets or back_lay_arbs:
        all_arbs = sure_bets + back_lay_arbs
        by_league = Counter(a.get("league", "?") for a in all_arbs)
        for league, count in sorted(by_league.items()):
            print(f"    {league.upper():<6} {count}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
