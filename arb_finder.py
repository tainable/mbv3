"""
arb_finder.py

Two arbitrage finders:

1. Sure bet (back-back): best back odds for each team across all providers.
   Arb exists when sum of reciprocals < 1.

2. Back-lay arb: best back odds for a team vs best lay odds for the same team
   on an exchange. Arb exists when back_odds > lay_odds (profit per unit =
   back_odds / lay_odds - 1).

Usage:
    python arb_finder.py
    python arb_finder.py --input outputs/latest_odds_aggregated_games.json
    python arb_finder.py --min-profit 0.5
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


BACK_ODDS_FIELDS = {
    "team1": [
        "polymarket_team1_back_odds",
        "matchbook_team1_back_odds",
        "smarkets_team1_back_odds",
    ],
    "team2": [
        "polymarket_team2_back_odds",
        "matchbook_team2_back_odds",
        "smarkets_team2_back_odds",
    ],
}

LAY_ODDS_FIELDS = {
    "team1": [
        "matchbook_team1_lay_odds",
        "smarkets_team1_lay_odds",
    ],
    "team2": [
        "matchbook_team2_lay_odds",
        "smarkets_team2_lay_odds",
    ],
}


def best_back_odds(game: dict, team_slot: str) -> tuple[float, str] | tuple[None, None]:
    best_odds = None
    best_provider = None
    for field in BACK_ODDS_FIELDS[team_slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = field.split("_")[0]
        if best_odds is None or odds > best_odds:
            best_odds = odds
            best_provider = provider
    return best_odds, best_provider


def best_lay_odds(game: dict, team_slot: str) -> tuple[float, str] | tuple[None, None]:
    """Return the lowest (cheapest) lay odds available — best for the layer."""
    best_odds = None
    best_provider = None
    for field in LAY_ODDS_FIELDS[team_slot]:
        odds = game.get(field)
        if odds is None:
            continue
        provider = field.split("_")[0]
        if best_odds is None or odds < best_odds:
            best_odds = odds
            best_provider = provider
    return best_odds, best_provider


# ---------------------------------------------------------------------------
# Sure-bet finder (back-back across providers)
# ---------------------------------------------------------------------------

def find_sure_bets(games: list[dict], min_profit_pct: float = 0.0) -> list[dict]:
    results = []
    for game in games:
        team1_odds, team1_provider = best_back_odds(game, "team1")
        team2_odds, team2_provider = best_back_odds(game, "team2")

        if team1_odds is None or team2_odds is None:
            continue

        margin = (1 / team1_odds) + (1 / team2_odds)
        if margin >= 1.0:
            continue

        profit_pct = (1 / margin - 1) * 100
        if profit_pct < min_profit_pct:
            continue

        results.append({
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
        })

    results.sort(key=lambda x: x["profit_pct"], reverse=True)
    return results


# ---------------------------------------------------------------------------
# Back-lay arb finder
# ---------------------------------------------------------------------------

def find_back_lay_arbs(games: list[dict], min_profit_pct: float = 0.0) -> list[dict]:
    results = []
    for game in games:
        for team_slot in ("team1", "team2"):
            back_odds, back_provider = best_back_odds(game, team_slot)
            lay_odds, lay_provider = best_lay_odds(game, team_slot)

            if back_odds is None or lay_odds is None:
                continue
            if back_odds <= lay_odds:
                continue

            profit_pct = (back_odds / lay_odds - 1) * 100
            if profit_pct < min_profit_pct:
                continue

            team_name = game.get(team_slot)
            other_slot = "team2" if team_slot == "team1" else "team1"
            other_name = game.get(other_slot)

            results.append({
                "league": game.get("league"),
                "date_time": game.get("date_time"),
                "team1": game.get("team1"),
                "team2": game.get("team2"),
                "arb_team": team_name,
                "other_team": other_name,
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
        print(
            f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})\n"
            f"    Back {arb['team1']:<30} {arb['team1_back_odds']:.4f}  ({arb['team1_back_provider']})\n"
            f"    Back {arb['team2']:<30} {arb['team2_back_odds']:.4f}  ({arb['team2_back_provider']})\n"
            f"    Margin: {arb['margin']:.6f}  |  Profit: {arb['profit_pct']:.4f}%\n"
        )


def _print_back_lay_arbs(arbs: list[dict]) -> None:
    if not arbs:
        print("  None found.\n")
        return
    for arb in arbs:
        print(
            f"  [{arb['league'].upper()}] {arb['team1']} vs {arb['team2']}  ({arb['date_time']})\n"
            f"    Back {arb['arb_team']:<30} {arb['back_odds']:.4f}  ({arb['back_provider']})\n"
            f"    Lay  {arb['arb_team']:<30} {arb['lay_odds']:.4f}  ({arb['lay_provider']})\n"
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

    sure_bets = find_sure_bets(games, min_profit_pct=args.min_profit)
    back_lay_arbs = find_back_lay_arbs(games, min_profit_pct=args.min_profit)

    print(f"=== Sure bets (back-back): {len(sure_bets)} found ===\n")
    _print_sure_bets(sure_bets)

    print(f"=== Back-lay arbs: {len(back_lay_arbs)} found ===\n")
    _print_back_lay_arbs(back_lay_arbs)


if __name__ == "__main__":
    main()
