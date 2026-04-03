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


def _best_back(game: dict, slot: str) -> tuple[float, str] | tuple[None, None]:
    best_odds, best_provider = None, None
    for field in BACK_ODDS_FIELDS[slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = field.split("_")[0]
        if best_odds is None or odds > best_odds:
            best_odds = odds
            best_provider = provider
    return best_odds, best_provider


def _best_lay(game: dict, slot: str) -> tuple[float, str] | tuple[None, None]:
    """Return the lowest (cheapest) lay odds available — best for the layer."""
    best_odds, best_provider = None, None
    for field in LAY_ODDS_FIELDS[slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = field.split("_")[0]
        if best_odds is None or odds < best_odds:
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
            margin = (1 / team1_odds) + (1 / draw_odds) + (1 / team2_odds)
        else:
            draw_odds, draw_provider = None, None
            margin = (1 / team1_odds) + (1 / team2_odds)

        if margin >= 1.0:
            continue

        profit_pct = (1 / margin - 1) * 100
        if profit_pct < min_profit_pct:
            continue

        result: dict = {
            "market_type": "three_way" if three_way else "two_way",
            "league": game.get("league"),
            "date_time": game.get("date_time"),
            "team1": game.get("team1"),
            "team2": game.get("team2"),
            "team1_back_odds": team1_odds,
            "team1_back_provider": team1_provider,
            "team2_back_odds": team2_odds,
            "team2_back_provider": team2_provider,
            "margin": round(margin, 6),
            "profit_pct": round(profit_pct, 4),
        }
        if three_way:
            result["draw_back_odds"] = draw_odds
            result["draw_back_provider"] = draw_provider

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
            if back_odds <= lay_odds:
                continue

            profit_pct = (back_odds / lay_odds - 1) * 100
            if profit_pct < min_profit_pct:
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
                "lay_odds": lay_odds,
                "lay_provider": lay_provider,
                "profit_pct": round(profit_pct, 4),
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
        lines = [
            f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})  [{arb['market_type']}]{started_flag}",
            f"    Back {arb['team1']:<30} {arb['team1_back_odds']:.4f}  ({arb['team1_back_provider']})",
        ]
        if arb["market_type"] == "three_way":
            lines.append(
                f"    Back {'Draw':<30} {arb['draw_back_odds']:.4f}  ({arb['draw_back_provider']})"
            )
        lines += [
            f"    Back {arb['team2']:<30} {arb['team2_back_odds']:.4f}  ({arb['team2_back_provider']})",
            f"    Margin: {arb['margin']:.6f}  |  Profit: {arb['profit_pct']:.4f}%",
            "",
        ]
        print("\n".join(lines))


def _print_back_lay_arbs(arbs: list[dict]) -> None:
    if not arbs:
        print("  None found.\n")
        return
    for arb in arbs:
        started_flag = "  *** GAME STARTED ***" if _game_started(arb.get("date_time")) else ""
        print(
            f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})  [{arb['market_type']}]{started_flag}\n"
            f"    Back {arb['arb_outcome']:<30} {arb['back_odds']:.4f}  ({arb['back_provider']})\n"
            f"    Lay  {arb['arb_outcome']:<30} {arb['lay_odds']:.4f}  ({arb['lay_provider']})\n"
            f"    Profit: {arb['profit_pct']:.4f}%\n"
        )


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
    args = parser.parse_args()

    input_path = args.input
    if not input_path.is_absolute():
        input_path = Path(__file__).parent / input_path

    data = json.loads(input_path.read_text(encoding="utf-8"))
    games = data.get("aggregated_games", [])

    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    sure_bets = find_sure_bets(games, min_profit_pct=args.min_profit)
    back_lay_arbs = find_back_lay_arbs(games, min_profit_pct=args.min_profit)

    print(f"Arb finder run:  {run_at}\n")
    print(f"=== Sure bets: {len(sure_bets)} found ===\n")
    _print_sure_bets(sure_bets)

    print(f"=== Back-lay arbs: {len(back_lay_arbs)} found ===\n")
    _print_back_lay_arbs(back_lay_arbs)


if __name__ == "__main__":
    main()
