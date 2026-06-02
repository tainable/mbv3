"""
ids.py
------
Stage 1 of the pipeline: discover active games from all configured providers
and save their market/event IDs to a JSON file.

By default Smarkets is excluded. Pass --providers smarkets to include it.

Usage:
    python ids.py
    python ids.py --leagues nba epl
    python ids.py --providers matchbook polymarket sx_bet smarkets
    python ids.py --out outputs/my_ids.json
    python ids.py --debug
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from matched_betting.config import load_settings
from matched_betting.debug import noop_debug, stderr_debug
from matched_betting.event_matching import match_records_to_canonical_events
from matched_betting.http import HttpClient
from matched_betting.market_matching import is_game_win_loss_record
from matched_betting.models import OddsRecord
from matched_betting.providers.base import ProviderNotReadyError
from matched_betting.providers.registry import build_provider_registry
from matched_betting.aggregation import build_aggregated_games_payload
from matched_betting import calculator

DEFAULT_LEAGUES = ["nba", "wnba", "mlb", "mlb_spread", "mlb_totals", "kbo", "ucl", "epl", "uel", "nhl", "ipl", "seria", "laliga", "mls", "mls_spread", "mls_totals", "veikkausliiga"]
DEFAULT_PROVIDERS = ["matchbook", "polymarket", "sx_bet"]  # Smarkets and Azuro excluded by default
DEFAULT_OUT = Path("outputs/active_game_ids.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Discover active games across providers and save their "
            "market/event IDs to a JSON file for use by scan.py."
        )
    )
    parser.add_argument(
        "--leagues",
        nargs="+",
        default=DEFAULT_LEAGUES,
        choices=DEFAULT_LEAGUES,  # updated when DEFAULT_LEAGUES changes
        metavar="LEAGUE",
        help="Leagues to include: nba wnba mlb mlb_spread mlb_totals kbo ucl epl uel nhl ipl seria laliga mls mls_spread mls_totals veikkausliiga (default: all).",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        choices=["matchbook", "smarkets", "polymarket", "sx_bet", "azuro"],
        metavar="PROVIDER",
        help="Providers to query (default: matchbook polymarket sx_bet). Smarkets and Azuro excluded by default.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"Output path for the IDs JSON (default: {DEFAULT_OUT}).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print provider progress to stderr.",
    )
    args = parser.parse_args()

    debug = stderr_debug if args.debug else noop_debug

    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    print(f"IDs collection started: {started_at}")
    print(f"Leagues:                {', '.join(args.leagues)}")
    print(f"Providers:              {', '.join(args.providers)}")

    settings = load_settings(_PROJECT_ROOT)
    calculator.configure(settings.commission)
    http_client = HttpClient()
    registry = build_provider_registry(settings, http_client, debug)

    records: list[OddsRecord] = []

    with ThreadPoolExecutor(max_workers=len(args.providers)) as executor:
        futures = {
            executor.submit(registry[name].fetch_odds, args.leagues): name
            for name in args.providers
            if name in registry
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                payload = future.result()
                records.extend(payload.records)
                debug(f"{name}: {len(payload.records)} records")
                print(f"  {name}: {len(payload.records)} records fetched", flush=True)
            except ProviderNotReadyError as exc:
                print(f"  WARNING: {name} not ready — {exc}", file=sys.stderr)
            except Exception as exc:
                print(f"  WARNING: {name} failed — {exc}", file=sys.stderr)

    records = [r for r in records if is_game_win_loss_record(r)]
    debug(f"Kept {len(records)} win/loss records after filtering")

    canonical_assignment, canonical_events = match_records_to_canonical_events(
        records, debug_logger=debug
    )
    games = build_aggregated_games_payload(records, canonical_assignment, canonical_events)

    finished_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    print(f"IDs collection finished: {finished_at}")
    print(f"Games discovered:        {len(games)}")

    out_path = args.out if args.out.is_absolute() else _PROJECT_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "generated_at": finished_at,
                "leagues": args.leagues,
                "providers": args.providers,
                "game_count": len(games),
                "games": games,
            },
            indent=2,
            sort_keys=False,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Written to:              {out_path}")


if __name__ == "__main__":
    main()
