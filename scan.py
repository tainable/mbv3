"""
scan.py
-------
Per-game pipeline: for every multi-provider game in the market index, fetch
odds from all providers simultaneously, run the arb finder on that game
immediately, and print any arbs found straight away — without waiting for
other games to finish.

Usage:
    python scan.py
    python scan.py --leagues nba epl
    python scan.py --providers polymarket smarkets
    python scan.py --min-profit 0.5
    python scan.py --no-refresh
    python scan.py --debug
    python scan.py --index outputs/latest_odds_market_index.json
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
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
from matched_betting.cli import _build_aggregated_games_payload

import arb_finder as _arb


DEFAULT_LEAGUES = ["nba", "mlb", "ucl", "epl"]
DEFAULT_PROVIDERS = ["matchbook", "smarkets", "polymarket", "sx_bet"]

_PROVIDER_ID_FIELD = {
    "polymarket": "polymarket_market_id",
    "smarkets":   "smarkets_market_id",
    "matchbook":  "matchbook_event_id",
    "sx_bet":     "sx_bet_market_hash",
}

# Serialises print output so concurrent game results don't interleave.
_print_lock = threading.Lock()


def _provider_coverage(game: dict[str, Any], providers: list[str]) -> int:
    """Count how many requested providers have a stored market ID for this game."""
    return sum(1 for p in providers if game.get(_PROVIDER_ID_FIELD.get(p, "")))


def _load_game_contexts(
    index_path: Path,
    leagues: list[str],
    providers: list[str],
) -> list[dict[str, Any]]:
    """Flatten the market index into game-context dicts, keeping only multi-provider games."""
    data = json.loads(index_path.read_text(encoding="utf-8"))
    market_index = data.get("market_index", {})
    contexts: list[dict[str, Any]] = []
    for league_key, games in market_index.items():
        if league_key not in leagues or not isinstance(games, list):
            continue
        for g in games:
            ctx = {**g, "league": league_key}
            if _provider_coverage(ctx, providers) >= 2:
                contexts.append(ctx)
    return contexts


def _scan_game(
    game_ctx: dict[str, Any],
    providers: list[str],
    leagues: list[str],
    registry: dict,
    min_profit_pct: float,
    debug: Any,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch all providers for one game, run the arb finder, print arbs immediately.

    Returns (aggregated_games, sure_bets, back_lay_arbs) so the caller can
    pass aggregated_games to run_refresh if needed.
    """
    # Fetch all providers for this single game in parallel.
    records: list[OddsRecord] = []
    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        futures = {
            executor.submit(registry[name].fetch_odds_by_ids, [game_ctx], leagues): name
            for name in providers
            if name in registry
        }
        for future in as_completed(futures):
            provider_name = futures[future]
            try:
                payload = future.result()
                records.extend(payload.records)
                debug(
                    f"  {game_ctx.get('team1')} vs {game_ctx.get('team2')}"
                    f" [{game_ctx.get('league', '').upper()}]"
                    f" {provider_name}: {len(payload.records)} records"
                )
            except ProviderNotReadyError:
                pass
            except Exception as exc:
                debug(
                    f"  {game_ctx.get('team1')} vs {game_ctx.get('team2')}"
                    f" {provider_name}: failed — {exc}"
                )

    records = [r for r in records if is_game_win_loss_record(r)]
    game_league = game_ctx.get("league", "?")
    provider_hits = {(r.provider, game_league) for r in records}

    if not records:
        return [], [], [], provider_hits

    canonical_assignment, canonical_events = match_records_to_canonical_events(records)
    games = _build_aggregated_games_payload(records, canonical_assignment, canonical_events)

    sure_bets = _arb.find_sure_bets(games, min_profit_pct=min_profit_pct)
    back_lay_arbs = _arb.find_back_lay_arbs(games, min_profit_pct=min_profit_pct)

    if sure_bets or back_lay_arbs:
        label = f"{game_ctx.get('team1')} vs {game_ctx.get('team2')}  [{game_ctx.get('league', '').upper()}]"
        with _print_lock:
            print(f"\n{'─'*60}")
            print(f"ARB FOUND  {label}")
            print(f"{'─'*60}")
            if sure_bets:
                print(f"  Sure bets ({len(sure_bets)}):")
                _arb._print_sure_bets(sure_bets)
            if back_lay_arbs:
                print(f"  Back-lay arbs ({len(back_lay_arbs)}):")
                _arb._print_back_lay_arbs(back_lay_arbs)

    return games, sure_bets, back_lay_arbs, provider_hits


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch live odds per game and check for arbs immediately — "
            "results print as soon as each game's odds are ready."
        )
    )
    parser.add_argument(
        "--index",
        type=Path,
        default=Path("outputs/latest_odds_market_index.json"),
        help="Path to the market index JSON (default: outputs/latest_odds_market_index.json).",
    )
    parser.add_argument(
        "--leagues",
        nargs="+",
        default=DEFAULT_LEAGUES,
        choices=DEFAULT_LEAGUES,
        metavar="LEAGUE",
        help="Leagues to include: nba mlb ucl epl (default: all).",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        choices=DEFAULT_PROVIDERS,
        metavar="PROVIDER",
        help="Providers to query: matchbook smarkets polymarket sx_bet (default: all).",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Minimum net profit %% to report (default: 0.0).",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip the targeted odds re-fetch after all games are scanned.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print provider progress to stderr.",
    )
    args = parser.parse_args()

    debug = stderr_debug if args.debug else noop_debug

    index_path = args.index
    if not index_path.is_absolute():
        index_path = _PROJECT_ROOT / index_path

    if not index_path.exists():
        print(
            f"ERROR: market index not found at {index_path}\n"
            "Run  python run.py  first to build the index.",
            file=sys.stderr,
        )
        sys.exit(1)

    game_contexts = _load_game_contexts(index_path, args.leagues, args.providers)

    run_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    print(f"Scan started:    {run_at}")
    print(f"Leagues:         {', '.join(args.leagues)}")
    print(f"Providers:       {', '.join(args.providers)}")
    print(f"Games in index:  {len(game_contexts)} multi-provider games")

    if not game_contexts:
        print(
            "\nNo multi-provider games found. Run  python run.py  to populate the index."
        )
        return

    by_league = Counter(g["league"] for g in game_contexts)
    for league, count in sorted(by_league.items()):
        print(f"    {league.upper():<6} {count} games")

    smarkets_note = (
        "Smarkets: 0% commission (60-day intro)"
        if _arb.SMARKETS_ZERO_COMMISSION_PERIOD
        else "Smarkets: 2% standard commission"
    )
    print(f"\nCommission: Matchbook 2% | {smarkets_note} | SX Bet 0% | Polymarket dynamic")
    print("\nScanning — arbs print as they are found...\n")

    settings = load_settings(_PROJECT_ROOT)
    http_client = HttpClient()
    registry = build_provider_registry(settings, http_client, debug)

    all_games: list[dict] = []
    all_sure_bets: list[dict] = []
    all_back_lay_arbs: list[dict] = []
    games_scanned = 0
    provider_hit_counts: Counter = Counter()  # keyed by (provider, league)
    league_game_counts: Counter = Counter(g["league"] for g in game_contexts)

    # Each game is dispatched as a task. Within each task all providers are
    # fetched in parallel, the arb finder runs, and any arb is printed
    # immediately — without waiting for the other game tasks to finish.
    with ThreadPoolExecutor(max_workers=min(len(game_contexts), 8)) as executor:
        future_to_ctx = {
            executor.submit(
                _scan_game,
                ctx, args.providers, args.leagues, registry,
                args.min_profit, debug,
            ): ctx
            for ctx in game_contexts
        }
        for future in as_completed(future_to_ctx):
            games_scanned += 1
            try:
                games, sure_bets, back_lay_arbs, provider_hits = future.result()
                all_games.extend(games)
                all_sure_bets.extend(sure_bets)
                all_back_lay_arbs.extend(back_lay_arbs)
                provider_hit_counts.update(provider_hits)
            except Exception as exc:
                ctx = future_to_ctx[future]
                debug(
                    f"Unhandled error for {ctx.get('team1')} vs {ctx.get('team2')}: {exc}"
                )

    finished_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    if not args.no_refresh and (all_sure_bets or all_back_lay_arbs):
        _arb.run_refresh(all_sure_bets, all_back_lay_arbs, all_games)

    print(f"\n{'='*60}")
    print(f"Summary  ({finished_at})")
    print(f"  Games scanned: {games_scanned}")
    print(f"  Sure bets    : {len(all_sure_bets)}")
    print(f"  Back-lay arbs: {len(all_back_lay_arbs)}")
    print(f"  Provider coverage (games with odds returned):")
    for provider in args.providers:
        total_hits = sum(v for (p, _l), v in provider_hit_counts.items() if p == provider)
        pct = f"{total_hits / games_scanned * 100:.0f}%" if games_scanned else "n/a"
        print(f"    {provider:<12} {total_hits:>3} / {games_scanned}  ({pct})")
        for league in sorted(args.leagues):
            league_total = league_game_counts.get(league, 0)
            league_hits = provider_hit_counts.get((provider, league), 0)
            lpct = f"{league_hits / league_total * 100:.0f}%" if league_total else "n/a"
            print(f"      {league.upper():<6}  {league_hits:>3} / {league_total}  ({lpct})")
    if all_sure_bets or all_back_lay_arbs:
        all_arbs = all_sure_bets + all_back_lay_arbs
        arb_by_league = Counter(a.get("league", "?") for a in all_arbs)
        for league, count in sorted(arb_by_league.items()):
            print(f"    {league.upper():<6} {count} arbs")


if __name__ == "__main__":
    main()
