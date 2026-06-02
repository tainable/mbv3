"""
find_sx_bet_league_ids.py
-------------------------
Query the SX Bet /leagues/active endpoint and search for a league by name.

Usage:
    # List all active leagues
    python find_sx_bet_league_ids.py

    # Search by keyword (case-insensitive)
    python find_sx_bet_league_ids.py --search "premier"
    python find_sx_bet_league_ids.py --search "england"
    python find_sx_bet_league_ids.py --search "football"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Find SX Bet league IDs.")
    parser.add_argument("--search", type=str, default=None, help="Keyword to filter league names (case-insensitive).")
    args = parser.parse_args()

    project_root = Path(__file__).parent
    settings = load_settings(project_root)
    http = HttpClient()

    print(f"SX Bet base URL: {settings.sx_bet.base_url}\n")

    raw = http.get_json(f"{settings.sx_bet.base_url}/leagues/active")

    data = raw.get("data", {})
    leagues = data.get("leagues", []) if isinstance(data, dict) else []

    if not leagues:
        print("No leagues returned. Raw response:")
        print(raw)
        return

    if args.search:
        keyword = args.search.lower()
        leagues = [l for l in leagues if keyword in str(l).lower()]
        print(f"{len(leagues)} leagues matching {args.search!r}:\n")
    else:
        print(f"{len(leagues)} active leagues:\n")

    for league in sorted(leagues, key=lambda l: str(l)):
        print(league)


if __name__ == "__main__":
    main()
