"""
arb_finder.py
-------------
CLI entry point for running the arbitrage finder against a saved aggregated
games JSON.  All pure maths lives in src/matched_betting/calculator.py.

Commission rates are read from .env — set SMARKETS_ZERO_COMMISSION=false when
the Smarkets introductory period ends (no source edits required).

Usage:
    python arb_finder.py
    python arb_finder.py --input outputs/latest_odds_aggregated_games.json
    python arb_finder.py --min-profit 0.5
    python arb_finder.py --no-refresh
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from matched_betting import calculator
from matched_betting.config import load_settings
from matched_betting.http import HttpClient

# Re-export the finders so scan.py can import them from here if desired.
find_sure_bets = calculator.find_sure_bets
find_back_lay_arbs = calculator.find_back_lay_arbs


# ---------------------------------------------------------------------------
# Azuro pool-size helpers
# ---------------------------------------------------------------------------

def _azuro_sure_bet_cap_line(arb: dict, game: dict | None, gbp_rate: float | None) -> str | None:
    """Return a display line showing max sure-bet profit constrained by Azuro's pool, or None."""
    if game is None:
        return None
    slots = [s for s in ("team1", "draw", "team2") if arb.get(f"{s}_back_provider") == "azuro"]
    if not slots:
        return None

    net_margin = 1.0 / (1.0 + arb["profit_pct"] / 100.0)
    parts = []
    for slot in slots:
        max_stake_usdc = game.get(f"azuro_{slot}_max_stake_usdc") or 0.0
        if max_stake_usdc <= 0:
            continue
        slot_odds = arb.get(f"{slot}_back_odds")
        if slot_odds is None:
            continue
        eff_e = calculator._eff_back_odds(slot_odds, "azuro")
        profit_usdc = max_stake_usdc * eff_e * (1.0 - net_margin)
        total_stake_usdc = max_stake_usdc * eff_e * net_margin
        outcome_label = "Draw" if slot == "draw" else (game.get(slot) or slot)
        if gbp_rate:
            parts.append(
                f"stake £{max_stake_usdc * gbp_rate:,.0f} on {outcome_label}"
                f"  →  profit £{profit_usdc * gbp_rate:,.0f}"
                f"  (total stake £{total_stake_usdc * gbp_rate:,.0f})"
            )
        else:
            parts.append(
                f"stake USDC {max_stake_usdc:,.0f} on {outcome_label}"
                f"  →  profit USDC {profit_usdc:,.0f}"
                f"  (total stake USDC {total_stake_usdc:,.0f})"
            )
    if not parts:
        return None
    return "    Azuro cap:  " + "  |  ".join(parts)


def _azuro_back_lay_cap_line(arb: dict, game: dict | None, gbp_rate: float | None) -> str | None:
    """Return a display line showing max back-lay profit constrained by Azuro's pool, or None."""
    if game is None or arb.get("back_provider") != "azuro":
        return None

    outcome_lower = (arb.get("arb_outcome") or "").lower()
    if outcome_lower in ("draw", "tie"):
        slot = "draw"
    elif _names_match(outcome_lower, (game.get("team1") or "").lower()):
        slot = "team1"
    elif _names_match(outcome_lower, (game.get("team2") or "").lower()):
        slot = "team2"
    else:
        slot = "team1"

    max_stake_usdc = game.get(f"azuro_{slot}_max_stake_usdc") or 0.0
    if max_stake_usdc <= 0:
        return None

    profit_usdc = max_stake_usdc * arb["profit_pct"] / 100.0
    if gbp_rate:
        return (
            f"    Azuro cap:  back stake £{max_stake_usdc * gbp_rate:,.0f}"
            f"  →  profit £{profit_usdc * gbp_rate:,.0f}"
        )
    return (
        f"    Azuro cap:  back stake USDC {max_stake_usdc:,.0f}"
        f"  →  profit USDC {profit_usdc:,.0f}"
    )


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def _sure_bet_stakes(arb: dict, budget_usdc: float) -> list[tuple[str, str, float, float]]:
    """
    Return per-leg stakes for a sure bet given a USDC budget.

    Stakes are proportional to 1/eff_odds so every outcome returns equal
    net profit after commission.

    Returns a list of (outcome_name, provider, raw_odds, stake_usdc).
    """
    legs = [
        (arb["team1"],  arb["team1_back_provider"], arb["team1_back_odds"]),
    ]
    if arb.get("market_type") == "three_way" and arb.get("draw_back_odds"):
        legs.append(("Draw", arb["draw_back_provider"], arb["draw_back_odds"]))
    legs.append((arb["team2"], arb["team2_back_provider"], arb["team2_back_odds"]))

    eff = [(name, prov, odds, calculator._eff_back_odds(odds, prov)) for name, prov, odds in legs]
    margin = sum(1.0 / e for *_, e in eff)
    return [
        (name, prov, odds, round(budget_usdc / (e * margin), 2))
        for name, prov, odds, e in eff
    ]


def _print_sure_bets(
    arbs: list[dict],
    game: dict | None = None,
    gbp_rate: float | None = None,
    budget: float | None = None,
) -> None:
    if not arbs:
        print("  None found.\n")
        return
    for arb in arbs:
        started_flag = "  *** GAME STARTED ***" if calculator._game_started(arb.get("date_time")) else ""

        def _avail(val, provider) -> str:
            if val is None:
                return ""
            cur = "USD" if provider in ("polymarket", "azuro") else "GBP"
            gbp = calculator._to_gbp(val, cur)
            return f"  [max ~£{gbp:,.0f}]" if gbp is not None else ""

        # Pre-compute stakes so they can be shown next to each leg
        stakes: dict[str, float] = {}
        if budget is not None:
            for name, _prov, _odds, stake in _sure_bet_stakes(arb, budget):
                stakes[name] = stake

        def _stake_str(name: str, provider: str) -> str:
            if name not in stakes:
                return ""
            s = stakes[name]
            if provider in ("polymarket", "sx_bet", "azuro"):
                return f"  stake: ${s:.2f}"
            # GBP provider — show both USD and GBP equivalent
            gbp = round(s * gbp_rate, 2) if gbp_rate else None
            gbp_part = f" / £{gbp:.2f}" if gbp is not None else ""
            return f"  stake: ${s:.2f}{gbp_part}"

        spread = arb.get("spread")
        spread_s = f" {spread:+.1f}" if spread is not None else ""
        lines = [
            f"  [{arb['league'].upper()}{spread_s}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})  [{arb['market_type']}]{started_flag}",
            f"    Back {arb['team1']:<30} {arb['team1_back_odds']:.4f}  ({arb['team1_back_provider']}){_avail(arb.get('team1_back_avail'), arb['team1_back_provider'])}{_stake_str(arb['team1'], arb['team1_back_provider'])}",
        ]
        if arb["market_type"] == "three_way":
            lines.append(
                f"    Back {'Draw':<30} {arb['draw_back_odds']:.4f}  ({arb['draw_back_provider']}){_avail(arb.get('draw_back_avail'), arb['draw_back_provider'])}{_stake_str('Draw', arb['draw_back_provider'])}"
            )
        gross_str = (
            f"  (gross: {arb['gross_profit_pct']:.4f}%)"
            if arb.get("gross_profit_pct") != arb.get("profit_pct")
            else ""
        )
        lines += [
            f"    Back {arb['team2']:<30} {arb['team2_back_odds']:.4f}  ({arb['team2_back_provider']}){_avail(arb.get('team2_back_avail'), arb['team2_back_provider'])}{_stake_str(arb['team2'], arb['team2_back_provider'])}",
            f"    Margin: {arb['margin']:.6f}  |  Net profit: {arb['profit_pct']:.4f}%{gross_str}"
            + (f"  |  24h: +{arb['profit_24h_pct']:.4f}%" if arb.get('profit_24h_pct') is not None else ""),
            "",
        ]
        az_line = _azuro_sure_bet_cap_line(arb, game, gbp_rate)
        if az_line:
            lines.insert(-1, az_line)
        print("\n".join(lines))


def _print_back_lay_arbs(arbs: list[dict], game: dict | None = None, gbp_rate: float | None = None) -> None:
    if not arbs:
        print("  None found.\n")
        return
    for arb in arbs:
        started_flag = "  *** GAME STARTED ***" if calculator._game_started(arb.get("date_time")) else ""

        def _avail(val, provider) -> str:
            if val is None:
                return ""
            cur = "USD" if provider in ("polymarket", "azuro") else "GBP"
            gbp = calculator._to_gbp(val, cur)
            return f"  [max ~£{gbp:,.0f}]" if gbp is not None else ""

        gross_str = (
            f"  (gross: {arb['gross_profit_pct']:.4f}%)"
            if arb.get("gross_profit_pct") != arb.get("profit_pct")
            else ""
        )
        az_line = _azuro_back_lay_cap_line(arb, game, gbp_rate)
        spread = arb.get("spread")
        spread_s = f" {spread:+.1f}" if spread is not None else ""
        lines = [
            f"  [{arb['league'].upper()}{spread_s}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})  [{arb['market_type']}]{started_flag}",
            f"    Back {arb['arb_outcome']:<30} {arb['back_odds']:.4f}  ({arb['back_provider']}){_avail(arb.get('back_avail'), arb['back_provider'])}",
            f"    Lay  {arb['arb_outcome']:<30} {arb['lay_odds']:.4f}  ({arb['lay_provider']}){_avail(arb.get('lay_avail'), arb['lay_provider'])}",
            f"    Net profit: {arb['profit_pct']:.4f}%{gross_str}"
            + (f"  |  24h: +{arb['profit_24h_pct']:.4f}%" if arb.get('profit_24h_pct') is not None else ""),
        ]
        if az_line:
            lines.append(az_line)
        lines.append("")
        print("\n".join(lines))


# ---------------------------------------------------------------------------
# Odds refresh — re-fetches live odds for each identified arb leg
# ---------------------------------------------------------------------------

def _names_match(a: str, b: str) -> bool:
    import unicodedata

    def _norm(s: str) -> str:
        s = unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii")
        return s.lower().replace("-", " ").strip()

    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(shorter) > 0 and shorter.issubset(longer)


def _delta_str(old: float, new: float) -> str:
    return f"  [{new - old:+.4f}]"


def _polymarket_slot_for_outcome(game: dict, outcome_name: str) -> str:
    if outcome_name.lower() in ("draw", "tie"):
        return "draw"
    if _names_match(outcome_name, game.get("team1") or ""):
        return "team1"
    if _names_match(outcome_name, game.get("team2") or ""):
        return "team2"
    return "team1"


def _fetch_polymarket_leg(game: dict, outcome_name: str, side: str, http, settings) -> float | None:
    slot = _polymarket_slot_for_outcome(game, outcome_name)
    clob_base = settings.polymarket.clob_base_url
    token_id = game.get(f"polymarket_{slot}_clob_token_id")
    if token_id:
        try:
            book = http.get_json(f"{clob_base}/book", params={"token_id": token_id})
            levels = book.get("bids" if side == "lay" else "asks", [])
            if not levels:
                return None
            p = float(levels[-1]["price"])
            return round(1.0 / p, 6) if 0 < p < 1 else None
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"WARNING: polymarket CLOB fetch failed for token {token_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return None

    market_id = game.get(f"polymarket_{slot}_market_id") or game.get("polymarket_market_id")
    if not market_id:
        return None
    try:
        gamma_base = settings.polymarket.gamma_base_url
        raw = http.get_json(f"{gamma_base}/markets", params={"id": market_id})
        market_data = raw[0] if isinstance(raw, list) else raw
        outcomes_raw = market_data.get("outcomes") or []
        if isinstance(outcomes_raw, str):
            outcomes_raw = json.loads(outcomes_raw)
        clob_ids = market_data.get("clobTokenIds") or []
        if isinstance(clob_ids, str):
            clob_ids = json.loads(clob_ids)
        if not clob_ids:
            return None
        is_binary = {o.lower() for o in outcomes_raw} <= {"yes", "no"}
        if is_binary:
            yes_idx = next((i for i, o in enumerate(outcomes_raw) if o.lower() == "yes"), 0)
            token_id = clob_ids[yes_idx] if yes_idx < len(clob_ids) else None
        else:
            token_id = next(
                (tid for o, tid in zip(outcomes_raw, clob_ids) if _names_match(str(o), outcome_name)),
                None,
            )
        if not token_id:
            return None
        book = http.get_json(f"{clob_base}/book", params={"token_id": token_id})
        levels = book.get("bids" if side == "lay" else "asks", [])
        if not levels:
            return None
        p = float(levels[-1]["price"])
        return round(1.0 / p, 6) if 0 < p < 1 else None
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"WARNING: polymarket CLOB fetch failed for market {market_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
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
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"WARNING: smarkets live fetch failed for market {market_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
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
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"WARNING: matchbook live fetch failed for event {event_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
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
        maker_data = (
            entry.get("outcomeTwo")
            if _names_match(game.get("team1", ""), outcome_name)
            else entry.get("outcomeOne")
        )
        if not maker_data:
            return None
        raw_pct = maker_data.get("percentageOdds")
        if raw_pct is None:
            return None
        taker_prob = 1.0 - int(raw_pct) / (10 ** 20)
        return round(1.0 / taker_prob, 6) if 0 < taker_prob < 1 else None
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"WARNING: sx_bet live fetch failed for market {market_hash}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _fetch_azuro_leg(game: dict, outcome_name: str, side: str, http, settings) -> float | None:
    """Re-fetch current Azuro odds for one outcome via the subgraph condition query."""
    condition_id = game.get("azuro_condition_id")
    if not condition_id or side == "lay":
        return None
    # Determine which slot we're re-fetching so we know which outcomeId to read
    league = game.get("league", "")
    if outcome_name.lower() in ("draw", "tie"):
        slot = "draw"
    elif _names_match(game.get("team1", ""), outcome_name):
        slot = "team1"
    else:
        slot = "team2"
    try:
        resp = http.post_json(
            settings.azuro.subgraph_url,
            payload={
                "query": """
query FetchCondition($id: String!) {
  condition(id: $id) {
    state
    outcomes(orderBy: sortOrder) { outcomeId currentOdds sortOrder }
    game { participants { name } }
  }
}""",
                "variables": {"id": condition_id},
            },
        )
        condition = (resp.get("data") or {}).get("condition")
        if not condition or condition.get("state") != "Active":
            return None
        participants = [
            p.get("name", "") for p in
            (condition.get("game") or {}).get("participants", [])
        ]
        outcomes = condition.get("outcomes", [])
        for o in outcomes:
            sort_order = o.get("sortOrder")
            # Map sortOrder to slot: 0=team1, 1=draw (3-way) or team2 (2-way), 2=team2
            if len(outcomes) == 3:
                outcome_slot = {0: "team1", 1: "draw", 2: "team2"}.get(sort_order)
            else:
                outcome_slot = {0: "team1", 1: "team2"}.get(sort_order)
            if outcome_slot != slot:
                continue
            raw = o.get("currentOdds")
            if raw is None:
                return None
            odds = float(raw)
            return odds if odds > 1.0 else None
    except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"WARNING: azuro live fetch failed for condition {condition_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return None


def _fetch_leg(provider: str, game: dict, outcome_name: str, side: str, http, settings) -> float | None:
    if provider == "polymarket":
        return _fetch_polymarket_leg(game, outcome_name, side, http, settings)
    if provider == "smarkets":
        return _fetch_smarkets_leg(game, outcome_name, side, http)
    if provider == "matchbook":
        return _fetch_matchbook_leg(game, outcome_name, side, http, settings)
    if provider == "sx_bet":
        return _fetch_sx_bet_leg(game, outcome_name, side, http, settings)
    if provider == "azuro":
        return _fetch_azuro_leg(game, outcome_name, side, http, settings)
    return None


def _game_key(league, team1, team2, date_time=None) -> tuple:
    return ((league or "").lower(), (team1 or "").lower(), (team2 or "").lower(), date_time or "")


def _refresh_sure_bet(arb: dict, game: dict | None, http, settings, refreshed_at: str) -> None:
    print(f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}")
    print(f"    Original  Back {arb['team1']:<28} {arb['team1_back_odds']:.4f}  ({arb['team1_back_provider']})")
    if arb.get("draw_back_odds"):
        print(f"    Original  Back {'Draw':<28} {arb['draw_back_odds']:.4f}  ({arb['draw_back_provider']})")
    print(f"    Original  Back {arb['team2']:<28} {arb['team2_back_odds']:.4f}  ({arb['team2_back_provider']})")
    print(f"    Original profit: {arb['profit_pct']:.4f}%")

    if game is None:
        print("    [market IDs not available — re-run ids.py to populate]\n")
        return

    print("    Refreshing... ", end="", flush=True)
    try:
        fresh_t1 = _fetch_leg(arb["team1_back_provider"], game, arb["team1"], "back", http, settings)
        fresh_draw = (
            _fetch_leg(arb["draw_back_provider"], game, "Draw", "back", http, settings)
            if arb.get("draw_back_provider") else None
        )
        fresh_t2 = _fetch_leg(arb["team2_back_provider"], game, arb["team2"], "back", http, settings)
    except Exception as exc:
        print(f"failed\n    [refresh error: {exc}]\n")
        return
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
        fresh_net_margin = sum(1.0 / calculator._eff_back_odds(o, p) for o, p in valid_legs)
        if fresh_net_margin < 1.0:
            fresh_net_profit = (1.0 / fresh_net_margin - 1) * 100
            fresh_gross_profit = (1.0 / fresh_gross_margin - 1) * 100
            gross_str = f"  (gross: {fresh_gross_profit:.4f}%)" if abs(fresh_gross_profit - fresh_net_profit) > 0.0001 else ""
            delta = fresh_net_profit - arb["profit_pct"]
            status = "ARB UNCHANGED" if abs(delta) < 0.0001 else ("ARB INCREASED" if delta > 0 else "ARB DECREASED")
            print(f"    Fresh net profit: {fresh_net_profit:.4f}%{gross_str}  (was {arb['profit_pct']:.4f}%, {delta:+.4f}pp)  --> {status}")
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
        print("    [market IDs not available — re-run ids.py to populate]\n")
        return

    print("    Refreshing... ", end="", flush=True)
    try:
        fresh_back = _fetch_leg(arb["back_provider"], game, arb["arb_outcome"], "back", http, settings)
        fresh_lay  = _fetch_leg(arb["lay_provider"],  game, arb["arb_outcome"], "lay",  http, settings)
    except Exception as exc:
        print(f"failed\n    [refresh error: {exc}]\n")
        return
    print(f"done  ({refreshed_at})\n")

    b_str = f"{fresh_back:.4f}{_delta_str(arb['back_odds'], fresh_back)}" if fresh_back else "N/A"
    print(f"    Fresh     Back {arb['arb_outcome']:<28} {b_str}  ({arb['back_provider']})")
    l_str = f"{fresh_lay:.4f}{_delta_str(arb['lay_odds'], fresh_lay)}" if fresh_lay else "N/A"
    print(f"    Fresh     Lay  {arb['arb_outcome']:<28} {l_str}  ({arb['lay_provider']})")

    if fresh_back and fresh_lay:
        eff_b = calculator._eff_back_odds(fresh_back, arb["back_provider"])
        eff_l = calculator._eff_lay_odds(fresh_lay, arb["lay_provider"])
        if eff_b > eff_l:
            fresh_net_profit = (eff_b / eff_l - 1) * 100
            fresh_gross_profit = (fresh_back / fresh_lay - 1) * 100
            gross_str = f"  (gross: {fresh_gross_profit:.4f}%)" if abs(fresh_gross_profit - fresh_net_profit) > 0.0001 else ""
            delta = fresh_net_profit - arb["profit_pct"]
            status = "ARB UNCHANGED" if abs(delta) < 0.0001 else ("ARB INCREASED" if delta > 0 else "ARB DECREASED")
            print(f"    Fresh net profit: {fresh_net_profit:.4f}%{gross_str}  (was {arb['profit_pct']:.4f}%, {delta:+.4f}pp)  --> {status}")
        else:
            print(f"    Fresh net profit: eff_back {eff_b:.4f} <= eff_lay {eff_l:.4f}  --> ARB GONE")
    else:
        print("    Fresh profit: N/A (could not fetch all odds)")
    print()


def run_refresh(sure_bets: list[dict], back_lay_arbs: list[dict], original_games: list[dict]) -> None:
    if not sure_bets and not back_lay_arbs:
        return

    http = HttpClient()
    settings = load_settings(Path(__file__).resolve().parent)
    calculator.configure(settings.commission)

    games_lookup: dict[tuple, dict] = {}
    for g in original_games:
        dt = g.get("date_time")
        games_lookup[_game_key(g.get("league"), g.get("team1"), g.get("team2"), dt)] = g
        games_lookup[_game_key(g.get("league"), g.get("team2"), g.get("team1"), dt)] = g

    refreshed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    print(f"\n{'=' * 60}")
    print("=== Odds refresh (targeted market fetch) ===")
    print(f"{'=' * 60}\n")

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
    parser.add_argument(
        "--budget",
        type=float,
        default=None,
        metavar="USDC",
        help="Show per-leg stake sizes for this USDC budget alongside each sure bet.",
    )
    args = parser.parse_args()

    # Load commission settings from .env before running any calculations.
    settings = load_settings(Path(__file__).resolve().parent)
    calculator.configure(settings.commission)

    input_path = args.input
    if not input_path.is_absolute():
        input_path = Path(__file__).parent / input_path

    data = json.loads(input_path.read_text(encoding="utf-8"))
    games = data.get("aggregated_games", [])

    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sure_bets = calculator.find_sure_bets(games, min_profit_pct=args.min_profit)
    back_lay_arbs = calculator.find_back_lay_arbs(games, min_profit_pct=args.min_profit)

    smarkets_note = (
        "Smarkets: 0% commission (60-day intro)"
        if calculator.SMARKETS_ZERO_COMMISSION_PERIOD
        else "Smarkets: 2% standard commission"
    )
    print(f"Arb finder run:  {run_at}")
    print(f"Commission: Matchbook 2% | {smarkets_note} | SX Bet 0% | Polymarket dynamic\n")
    print(f"=== Sure bets: {len(sure_bets)} found ===\n")
    _print_sure_bets(sure_bets, budget=args.budget)

    print(f"=== Back-lay arbs: {len(back_lay_arbs)} found ===\n")
    _print_back_lay_arbs(back_lay_arbs)

    if not args.no_refresh:
        run_refresh(sure_bets, back_lay_arbs, games)

    print(f"{'=' * 60}")
    print(f"Summary  ({run_at})")
    print(f"  Games loaded : {len(games)}")
    league_counts = Counter(g.get("league", "?") for g in games)
    for league, count in sorted(league_counts.items()):
        print(f"    {league.upper():<6} {count} games")
    print(f"  Sure bets    : {len(sure_bets)}")
    print(f"  Back-lay arbs: {len(back_lay_arbs)}")
    if sure_bets or back_lay_arbs:
        all_arbs = sure_bets + back_lay_arbs
        for league, count in sorted(Counter(a.get("league", "?") for a in all_arbs).items()):
            print(f"    {league.upper():<6} {count}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
