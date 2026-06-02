"""
stream.py
---------
Real-time arb detection for high-value markets using WebSocket price feeds.

Architecture:
  - Polymarket CLOB WS  ─┐
  - SX Bet Centrifugo WS ─┼── OddsCache ── arb detector ── alert / auto-bet
  - Matchbook poll loop  ─┘

The scan runs in two tiers:
  Tier 1 (--stream-leagues, default: mlb wnba):
    Polymarket and SX Bet prices are received in real time via WebSocket.
    Matchbook is polled on a short interval (--mb-poll, default: 20s).
    Arb detection fires on a debounce tick (--debounce, default: 0.2s).

  Tier 2 (all other active leagues):
    Standard scan.py polling approach — not handled here.

Usage:
    python stream.py
    python stream.py --stream-leagues mlb wnba kbo nba
    python stream.py --stream-leagues mlb --mb-poll 15 --min-profit 0.3
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
import time
import threading
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
from matched_betting.notifier import send_alert
from matched_betting.http import HttpClient

import arb_finder as _arb

log = logging.getLogger("stream")

_DEFAULT_STREAM_LEAGUES = ["mlb", "wnba"]
_DEFAULT_MB_POLL_S      = 20.0
_DEFAULT_DEBOUNCE_S     = 0.2
_DEFAULT_IDS_PATH       = _ROOT / "outputs" / "active_game_ids.json"


# ---------------------------------------------------------------------------
# Matchbook poll loop (runs in a background thread)
# ---------------------------------------------------------------------------

def _matchbook_poll_loop(
    cache: OddsCache,
    games: list[dict],
    settings,
    poll_interval_s: float,
    stop_event: threading.Event,
    debug: bool = False,
) -> None:
    """Fetch Matchbook odds for all stream-tier games on a fixed interval."""
    from matched_betting.providers.registry import build_provider_registry
    from matched_betting.event_matching import match_records_to_canonical_events
    from matched_betting.market_matching import is_game_win_loss_record
    from matched_betting.aggregation import build_aggregated_games_payload
    from matched_betting.providers.base import ProviderNotReadyError

    http = HttpClient(proxy_url=None)  # Matchbook always direct
    registry = build_provider_registry(settings, http_client=http, proxy_http_client=http)
    mb_provider = registry.get("matchbook")
    if mb_provider is None:
        log.warning("matchbook not available — poll loop disabled")
        return

    league_list = list({g.get("league") for g in games if g.get("league")})

    while not stop_event.wait(timeout=poll_interval_s):
        try:
            payload = mb_provider.fetch_odds_by_ids(games, league_list)
            records = [r for r in payload.records if is_game_win_loss_record(r)]
            if not records:
                continue
            ca, ce = match_records_to_canonical_events(records)
            agg = build_aggregated_games_payload(records, ca, ce)

            for g in agg:
                gid = _gid(g)
                for slot in ("team1", "team2", "draw"):
                    back = g.get(f"matchbook_{slot}_back_odds")
                    lay  = g.get(f"matchbook_{slot}_lay_odds")
                    if back is not None or lay is not None:
                        cache.update_back_and_lay_odds(gid, "matchbook", slot, back, lay)

            if debug:
                log.debug("matchbook poll: updated %d games", len(agg))
        except Exception as exc:
            log.warning("matchbook poll error: %s", exc)


# ---------------------------------------------------------------------------
# Arb detection loop (async, debounced)
# ---------------------------------------------------------------------------

async def _arb_loop(
    cache: OddsCache,
    settings,
    args: argparse.Namespace,
) -> None:
    """Consume dirty games from the cache and run arb detection."""
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

        for game in dirty:
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

    # Matchbook poll thread
    stop_event = threading.Event()
    mb_thread  = threading.Thread(
        target   = _matchbook_poll_loop,
        args     = (cache, games, settings, args.mb_poll, stop_event, args.debug),
        daemon   = True,
        name     = "matchbook-poll",
    )
    mb_thread.start()
    print(f"  Matchbook: polling every {args.mb_poll}s")

    print()
    print("  Listening for arbs — Ctrl-C to stop")
    print()

    # Run WS clients and arb loop concurrently
    tasks: list[asyncio.Task] = []
    tasks.append(asyncio.create_task(pm_client.run(), name="pm-ws"))
    if sx_client:
        tasks.append(asyncio.create_task(sx_client.run(), name="sx-ws"))
    tasks.append(asyncio.create_task(_arb_loop(cache, settings, args), name="arb-loop"))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        stop_event.set()
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
        "--mb-poll", type=float, default=_DEFAULT_MB_POLL_S, metavar="SECONDS",
        help="Matchbook poll interval in seconds (default: 20).",
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
