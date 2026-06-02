"""
stream.py
---------
Real-time arb detection for high-value markets using WebSocket price feeds.

Architecture:
  - Polymarket CLOB WS  ─┐
  - SX Bet Centrifugo WS ─┼── OddsCache ── arb detector ── alert / auto-bet
  - Matchbook (on-demand) ─┘

The scan runs in two tiers:
  Tier 1 (--stream-leagues, default: mlb wnba):
    Polymarket and SX Bet prices are received in real time via WebSocket.
    Matchbook is fetched on-demand — only when a WS price change arrives
    for a specific game, and only for that game. No fixed poll interval.
    Arb detection fires on a debounce tick (--debounce, default: 0.2s).

  Tier 2 (all other active leagues):
    Standard scan.py polling approach — not handled here.

Usage:
    python stream.py
    python stream.py --stream-leagues mlb wnba kbo nba
    python stream.py --stream-leagues mlb --min-profit 0.3
    python stream.py --stream-leagues mlb --auto-bet --budget 50
    python stream.py --stream-leagues mlb --bet-dry-run
    python stream.py --ids outputs/active_game_ids.json --debug

The script reads market IDs from active_game_ids.json (run ids.py first).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC  = _ROOT / "src"
for _p in (_SRC, _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from matched_betting.config import load_settings
from matched_betting import calculator
from matched_betting.odds_cache import OddsCache, game_id as _gid
from matched_betting.ws_polymarket import PolymarketWSClient, build_token_map
from matched_betting.ws_sx_bet import SxBetWSClient, build_market_map
from matched_betting.http import HttpClient

import arb_finder as _arb

log = logging.getLogger("stream")

_DEFAULT_STREAM_LEAGUES = ["mlb", "wnba"]
_DEFAULT_DEBOUNCE_S     = 0.2
_DEFAULT_IDS_PATH       = _ROOT / "outputs" / "active_game_ids.json"


# ---------------------------------------------------------------------------
# On-demand Matchbook fetch (called from the arb loop)
# ---------------------------------------------------------------------------

def _fetch_matchbook_sync(games: list[dict], mb_provider) -> list[dict]:
    """Fetch fresh Matchbook odds for the given games and return updated dicts.

    Runs synchronously — intended to be called via run_in_executor so it
    does not block the event loop while the HTTP request is in flight.
    """
    from matched_betting.event_matching import match_records_to_canonical_events
    from matched_betting.market_matching import is_game_win_loss_record
    from matched_betting.aggregation import build_aggregated_games_payload

    league_list = list({g.get("league") for g in games if g.get("league")})
    try:
        payload = mb_provider.fetch_odds_by_ids(games, league_list)
    except Exception as exc:
        log.warning("matchbook fetch error: %s", exc)
        return games

    records = [r for r in payload.records if is_game_win_loss_record(r)]
    if not records:
        return games

    ca, ce = match_records_to_canonical_events(records)
    agg    = build_aggregated_games_payload(records, ca, ce)

    # Merge fresh MB fields into the input game dicts
    agg_by_id = {_gid(g): g for g in agg}
    result = []
    for game in games:
        merged = dict(game)
        mb_game = agg_by_id.get(_gid(game))
        if mb_game:
            for key, val in mb_game.items():
                if key.startswith("matchbook_"):
                    merged[key] = val
        result.append(merged)
    return result


async def _fetch_matchbook(games: list[dict], mb_provider) -> list[dict]:
    """Async wrapper: run the blocking Matchbook fetch in a thread pool executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_matchbook_sync, games, mb_provider)


# ---------------------------------------------------------------------------
# Arb detection loop (async, debounced)
# ---------------------------------------------------------------------------

async def _arb_loop(
    cache: OddsCache,
    settings,
    args: argparse.Namespace,
    mb_provider,
) -> None:
    """Consume dirty games from the cache and run arb detection.

    When WS updates mark games dirty, fresh Matchbook odds are fetched
    for exactly those games before arb detection runs. No fixed timer.
    """
    import bet_executor as _exec

    total_sure     = 0
    total_back_lay = 0
    total_kbo      = 0
    gbp_rate: float | None = None

    while True:
        await asyncio.sleep(args.debounce)

        dirty = cache.pop_dirty()
        if not dirty:
            continue

        # Fetch fresh Matchbook odds for the games that just moved on PM/SX
        if mb_provider is not None:
            games = await _fetch_matchbook(dirty, mb_provider)
            log.debug("matchbook: refreshed %d games on WS trigger", len(games))
        else:
            games = dirty

        for game in games:
            sure_bets   = calculator.find_sure_bets([game],   min_profit_pct=args.min_profit)
            back_lay    = calculator.find_back_lay_arbs([game], min_profit_pct=args.min_profit)
            kbo_arbs    = calculator.find_kbo_tie_aware_arbs([game], min_profit_pct=args.min_profit)

            total_sure     += len(sure_bets)
            total_back_lay += len(back_lay)
            total_kbo      += len(kbo_arbs)

            if not (sure_bets or back_lay or kbo_arbs):
                continue

            label = f"{game.get('team1')} vs {game.get('team2')} [{(game.get('league') or '').upper()}]"
            print(f"\n  {label}")

            if sure_bets:
                print(f"  Sure bets ({len(sure_bets)}):")
                _arb._print_sure_bets(sure_bets, gbp_rate=gbp_rate, budget=args.budget)
            if back_lay:
                print(f"  Back-lay arbs ({len(back_lay)}):")
                _arb._print_back_lay_arbs(back_lay, game=game, gbp_rate=gbp_rate, budget=args.budget)
            if kbo_arbs:
                print(f"  KBO tie-aware arbs ({len(kbo_arbs)}):")
                _arb._print_kbo_arbs(kbo_arbs, gbp_rate=gbp_rate, budget=args.budget)

            # Auto-bet
            if args.auto_bet and not _exec._HALT:
                all_arbs = [
                    *[(a, "sure_bet")  for a in sure_bets],
                    *[(a, "back_lay")  for a in back_lay],
                ]
                if all_arbs:
                    best_arb, best_type = max(all_arbs, key=lambda x: x[0].get("profit_pct", 0))
                    if best_type == "sure_bet":
                        _exec.place_sure_bet(best_arb, game, settings, dry_run=args.bet_dry_run)
                    else:
                        _exec.place_back_lay_arb(best_arb, game, settings, dry_run=args.bet_dry_run)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main_async(args: argparse.Namespace) -> None:
    settings = load_settings(_ROOT)
    calculator.configure(settings.commission)

    # Load games index
    ids_path = Path(args.ids)
    if not ids_path.exists():
        print(f"  ERROR: {ids_path} not found — run ids.py first", file=sys.stderr)
        sys.exit(1)

    with open(ids_path) as f:
        index = json.load(f)

    all_games: list[dict] = index.get("games", [])
    stream_leagues = set(args.stream_leagues)
    games = [g for g in all_games if g.get("league") in stream_leagues]

    if not games:
        print(f"  No games found for leagues: {stream_leagues}", file=sys.stderr)
        print(f"  Run: python ids.py --leagues {' '.join(stream_leagues)}", file=sys.stderr)
        sys.exit(1)

    print(f"  Streaming {len(games)} games across {sorted(stream_leagues)}")

    # Build cache
    cache = OddsCache()
    cache.seed(games)

    # Polymarket WS
    token_map = build_token_map(games)
    pm_tokens = sum(1 for g in games if g.get("polymarket_team1_clob_token_id"))
    print(f"  Polymarket: {len(token_map)} tokens across {pm_tokens} games")

    pm_client = PolymarketWSClient(
        cache     = cache,
        token_map = token_map,
        proxy_url = settings.vpn_proxy_url,
        ws_url    = settings.polymarket.ws_url,
    )

    # SX Bet WS
    if not settings.sx_bet.api_key:
        print("  WARNING: SX_BET_API_KEY not set — SX Bet WS disabled", file=sys.stderr)
        sx_client = None
    else:
        market_map = build_market_map(games)
        sx_games   = sum(1 for g in games if g.get("sx_bet_market_hash"))
        print(f"  SX Bet: {len(market_map)} market subscriptions across {sx_games} games")

        sx_client = SxBetWSClient(
            cache      = cache,
            market_map = market_map,
            api_key    = settings.sx_bet.api_key,
            token_url  = settings.sx_bet.realtime_token_url,
            ws_url     = settings.sx_bet.realtime_url,
            proxy_url  = settings.vpn_proxy_url,
        )

    # Matchbook provider (reused across all on-demand fetches)
    from matched_betting.providers.registry import build_provider_registry
    mb_http     = HttpClient(proxy_url=None)  # Matchbook always direct, never proxied
    mb_registry = build_provider_registry(settings, http_client=mb_http)
    mb_provider = mb_registry.get("matchbook")
    if mb_provider:
        print("  Matchbook: on-demand fetch on every WS price change")
    else:
        print("  Matchbook: unavailable (credentials missing)")

    print()
    print("  Listening for arbs — Ctrl-C to stop")
    print()

    # Run WS clients and arb loop concurrently
    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(pm_client.run(), name="pm-ws"))
    if sx_client:
        tasks.append(asyncio.create_task(sx_client.run(), name="sx-ws"))
    tasks.append(asyncio.create_task(
        _arb_loop(cache, settings, args, mb_provider), name="arb-loop"
    ))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        print("\n  Stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Real-time arb detection via WebSocket feeds.")
    parser.add_argument(
        "--stream-leagues", nargs="+", default=_DEFAULT_STREAM_LEAGUES,
        metavar="LEAGUE",
        help="Leagues to stream via WS (default: mlb wnba).",
    )
    parser.add_argument(
        "--ids", default=str(_DEFAULT_IDS_PATH),
        help="Path to active_game_ids.json (default: outputs/active_game_ids.json).",
    )
    parser.add_argument(
        "--debounce", type=float, default=_DEFAULT_DEBOUNCE_S, metavar="SECONDS",
        help="Arb detector tick interval in seconds (default: 0.2).",
    )
    parser.add_argument(
        "--min-profit", type=float, default=0.0, metavar="PCT",
        help="Minimum profit %% to display (default: 0.0).",
    )
    parser.add_argument("--auto-bet",    action="store_true", help="Place bets automatically.")
    parser.add_argument("--bet-dry-run", action="store_true", help="Sign orders but do not submit.")
    parser.add_argument("--budget",      type=float, default=None, metavar="USDC",
                        help="Maximum stake per arb in USDC.")
    parser.add_argument("--debug",       action="store_true", help="Verbose logging.")

    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
