"""
stream.py
---------
Real-time arb detection for high-value markets using WebSocket price feeds.

Architecture:
  - Polymarket CLOB WS  -+
  - SX Bet Centrifugo WS -+-- OddsCache -- arb detector -- alert / print
  - Matchbook (on-demand) -+

The scan runs in two tiers:
  Tier 1 (--stream-leagues, default: mlb wnba):
    Polymarket and SX Bet prices are received in real time via WebSocket.
    Matchbook is fetched on-demand -- only when a WS price change arrives
    for a specific game, and only for that game. No fixed poll interval.
    Arb detection fires on a debounce tick (--debounce, default: 0.2s).

  Tier 2 (all other active leagues):
    Standard scan.py polling approach -- not handled here.

Usage:
    python stream.py
    python stream.py --stream-leagues mlb wnba kbo nba
    python stream.py --stream-leagues mlb --min-profit 0.3
    python stream.py --ids outputs/active_game_ids.json --debug

The script reads market IDs from active_game_ids.json (run ids.py first).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC  = _ROOT / "src"
for _p in (_SRC, _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from matched_betting.config import load_settings
from matched_betting import calculator
from matched_betting import event_log as _event_log
from matched_betting.odds_cache import OddsCache, game_id as _gid
from matched_betting.ws_polymarket import PolymarketWSClient, build_token_map
from matched_betting.ws_sx_bet import SxBetWSClient, build_market_map
from matched_betting.http import HttpClient

import arb_finder as _arb

log = logging.getLogger("stream")

_DEFAULT_STREAM_LEAGUES = ["mlb", "wnba"]
_DEFAULT_DEBOUNCE_S     = 0.2
_DEFAULT_IDS_PATH       = _ROOT / "outputs" / "active_game_ids.json"

# Set by _main_async at startup; used by _autobet_task to write to stream-specific logs.
_stream_arb_log: Path = _ROOT / "outputs" / "stream_arb_log.jsonl"

# Set by _autobet_task when bet_executor._HALT is True; checked by _arb_loop to exit(2).
_halt_flag: bool = False

# In-memory record of successfully placed (gid, arb_key) pairs for this process run.
# Checked by _arb_loop to block re-scheduling after a bet completes.
# Belt-and-suspenders guard: file-based dedup via game_already_bet may fail silently
# (create_task swallows exceptions from _autobet_task after placement returns).
_bet_history: set = set()


# ---------------------------------------------------------------------------
# On-demand Matchbook fetch (called from the arb loop)
# ---------------------------------------------------------------------------

def _fetch_matchbook_sync(games: list[dict], mb_provider) -> list[dict]:
    """Fetch fresh Matchbook odds for the given games and return updated dicts."""
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
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_matchbook_sync, games, mb_provider)


# Seconds since last MB fetch before the on-demand path re-fetches.
# Covers the gap between poll cycles: WS ticks within this window reuse
# the cached MB odds rather than firing a new HTTP request each time.
_MB_FRESH_S = 60.0


# ---------------------------------------------------------------------------
# Periodic Matchbook poll (replaces on-demand-only coverage)
# ---------------------------------------------------------------------------

async def _matchbook_poll_loop(
    all_games:       list[dict],
    cache:           OddsCache,
    mb_provider,
    poll_interval_s: float,
) -> None:
    """Fetch Matchbook odds for all stream games every poll_interval_s seconds.

    After each fetch the MB fields are merged into the cache and affected games
    are marked dirty, which triggers the arb detector on the next debounce tick.
    The first poll fires after a short warm-up so WS connections are established
    before the first blocking HTTP call.
    """
    _WARMUP_S = 15.0
    await asyncio.sleep(_WARMUP_S)

    loop = asyncio.get_running_loop()
    while True:
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"\n  [{ts}]  MB POLL  fetching {len(all_games)} games ...", flush=True)
        try:
            updated: list[dict] = await loop.run_in_executor(
                None, _fetch_matchbook_sync, all_games, mb_provider
            )
        except Exception as exc:
            log.warning("matchbook poll: fetch error: %s", exc)
            await asyncio.sleep(poll_interval_s)
            continue

        n_written = 0
        for game in updated:
            mb_fields = {k: v for k, v in game.items() if k.startswith("matchbook_")}
            # Only write (and mark dirty) when at least one odds field is present.
            if any(k.endswith(("_back_odds", "_lay_odds", "_back_avail", "_lay_avail"))
                   for k in mb_fields):
                cache.merge_provider_fields(_gid(game), "matchbook", mb_fields,
                                            mark_dirty=True)
                n_written += 1

        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        print(f"  [{ts}]  MB POLL  done — {n_written}/{len(updated)} games written to cache"
              f"  (next in {poll_interval_s:.0f}s)", flush=True)

        await asyncio.sleep(poll_interval_s)


# ---------------------------------------------------------------------------
# Autobet: delayed placement task
# ---------------------------------------------------------------------------

async def _autobet_task(
    flight_key: tuple,
    arb_key:    tuple,
    arb_type:   str,
    gid:        str,
    cache,
    settings,
    args:       argparse.Namespace,
    gbp_rate:   float | None,
    in_flight:  set,
) -> None:
    """
    Wait args.autobet_delay seconds, re-validate the arb from the live cache,
    then place it using the same guard sequence as scan.py's --auto-bet path.
    Runs as an asyncio background task; placement IO is offloaded to a thread pool.
    """
    import bet_executor as _exec

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    mode = "TEST $5" if args.autobet_test else ("DRY-RUN" if args.bet_dry_run else "LIVE")
    print(f"\n  [{ts}]  AUTOBET queued ({mode})  {arb_key}  — waiting {args.autobet_delay}s",
          flush=True)

    try:
        await asyncio.sleep(args.autobet_delay)

        # Re-validate: fetch the current game state from the live cache.
        # The arb loop has kept MB odds fresh via on-demand fetches so no
        # second Matchbook call is needed here.
        fresh_game = cache.get_game(gid)
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if fresh_game is None:
            print(f"\n  [{ts}]  AUTOBET {arb_key}: game no longer in cache — skipped", flush=True)
            return

        # Refresh SX odds via HTTP so the arb calculator quotes against the
        # same price source as sx_place_bet(), not the (potentially stale)
        # WS-cache snapshot.
        if any(fresh_game.get(k) for k in (
            "sx_bet_market_hash",
            "sx_bet_team1_market_hash",
            "sx_bet_draw_market_hash",
            "sx_bet_team2_market_hash",
        )):
            import bet as _bet
            loop = asyncio.get_running_loop()
            try:
                fresh_game = await loop.run_in_executor(
                    None, _bet.refresh_sx_odds_http, fresh_game, settings
                )
            except Exception as _sx_http_err:
                log.warning("autobet: SX HTTP refresh failed: %s", _sx_http_err)

        threshold = args.autobet_min_profit
        if arb_type == "sure_bet":
            fresh_arbs = calculator.find_sure_bets([fresh_game], min_profit_pct=threshold)
        else:
            fresh_arbs = calculator.find_back_lay_arbs([fresh_game], min_profit_pct=threshold)

        fresh_arb: dict | None = None
        for _fa in fresh_arbs:
            if arb_type == "sure_bet":
                _fk: tuple = ("sb",
                               _fa.get("team1_back_provider", ""),
                               _fa.get("draw_back_provider", ""),
                               _fa.get("team2_back_provider", ""))
            else:
                _fk = ("bl",
                        (_fa.get("arb_outcome") or "").lower(),
                        _fa.get("back_provider", ""),
                        _fa.get("lay_provider", ""))
            if _fk == arb_key:
                fresh_arb = _fa
                break

        if fresh_arb is None:
            print(f"\n  [{ts}]  AUTOBET {arb_key}: arb gone after {args.autobet_delay}s — skipped",
                  flush=True)
            return

        print(f"\n  [{ts}]  AUTOBET {arb_key}: still live at {fresh_arb['profit_pct']:.4f}% — placing",
              flush=True)

        # ── Guard sequence (mirrors scan.py --auto-bet) ───────────────────
        if _exec._HALT:
            global _halt_flag
            _halt_flag = True
            print("  [AUTOBET] HALTED after prior leg failure — stream.py will exit (code 2).",
                  file=sys.stderr)
            return

        proposed_legs = _exec._arb_legs(arb_type, fresh_arb)
        if not args.bet_dry_run and _exec.game_already_bet(
            fresh_game, proposed_legs=proposed_legs, extra_log=_stream_arb_log
        ):
            print("  [AUTOBET] Already bet this game/direction — skipped.")
            return

        budget    = 5.0 if args.autobet_test else args.budget
        skip_kelly = args.autobet_test
        kelly_on  = (not skip_kelly
                     and getattr(settings, "kelly", None) is not None
                     and settings.kelly.enabled)

        if not kelly_on:
            bal_issues, bal_warns = _exec.check_balances_for_arb(
                arb_type, fresh_arb, budget, settings, gbp_rate
            )
            for _w in bal_warns:
                print(f"  [AUTOBET] Balance warning: {_w}", file=sys.stderr)
            if bal_issues:
                for _issue in bal_issues:
                    print(f"  [AUTOBET] Insufficient funds: {_issue}", file=sys.stderr)
                return

        # ── Place on thread pool (all placement calls are blocking HTTP) ──
        loop = asyncio.get_running_loop()
        if arb_type == "sure_bet":
            results = await loop.run_in_executor(
                None,
                lambda: _exec.place_sure_bet(
                    fresh_arb, fresh_game, budget, settings, gbp_rate,
                    dry_run=args.bet_dry_run, skip_kelly=skip_kelly,
                )
            )
        else:
            results = await loop.run_in_executor(
                None,
                lambda: _exec.place_back_lay_arb(
                    fresh_arb, fresh_game, budget, settings, gbp_rate,
                    dry_run=args.bet_dry_run, skip_kelly=skip_kelly,
                )
            )

        _exec.print_bet_results(results)
        if _exec.all_legs_ok(results):
            # Record in session history immediately so re-scheduling is blocked
            # even if the file log write below fails.
            global _bet_history
            _bet_history.add(flight_key)
            try:
                _exec.log_arb_success(arb_type, fresh_arb, fresh_game,
                                      results=results, dry_run=args.bet_dry_run,
                                      log_path=_stream_arb_log)
            except Exception as _log_exc:
                print(f"  [AUTOBET] WARNING: log_arb_success failed: {_log_exc}",
                      file=sys.stderr)
            if not args.bet_dry_run:
                _exec.check_bankroll_halt(settings, gbp_rate)

    finally:
        in_flight.discard(flight_key)


# ---------------------------------------------------------------------------
# Arb detection loop (async, debounced)
# ---------------------------------------------------------------------------

async def _arb_loop(
    cache: OddsCache,
    settings,
    args: argparse.Namespace,
    mb_provider,
    gbp_rate: float | None,
) -> None:
    """Consume dirty games from the cache and run arb detection.

    When WS updates mark games dirty, fresh Matchbook odds are fetched
    for exactly those games before arb detection runs.
    """
    from matched_betting.notifier import send_alert

    total_sure     = 0
    total_back_lay = 0
    total_kbo      = 0

    # Per-game arb state for dedup printing.
    # Maps gid -> {arb_key -> profit_pct}.  Only prints when an arb first
    # appears, its profit changes by >= _PRINT_THRESHOLD pp, or it disappears.
    _live: dict[str, dict[tuple, float]] = {}
    _PRINT_THRESHOLD = 0.005  # pp — sub-epsilon oscillations are silenced
    _GONE_GRACE_S    = args.gone_grace
    # gid -> {arb_key -> first-absent monotonic time}
    # Arbs stay in _live until absent for >= _GONE_GRACE_S seconds, preventing
    # P2P order-book churn (order fills/replaces in < 1s) from causing false GONE flicker.
    _gone_pending: dict[str, dict[tuple, float]] = {}

    # Autobet in-flight tracking: set of (gid, arb_key) for bets currently in
    # their delay window or being placed.  Prevents scheduling duplicates.
    _in_flight: set[tuple] = set()

    # Games already logged as dropped due to imminence — avoids repeating the
    # message on every subsequent dirty tick for the same game.
    _dropped_imminent: set[str] = set()

    while True:
        await asyncio.sleep(args.debounce)

        if _halt_flag:
            raise SystemExit(2)

        dirty = cache.pop_dirty()
        if not dirty:
            continue

        # ── Dynamic imminent-game filter ──────────────────────────────────────
        # Drop games whose start is within --min-start minutes of now, UNLESS
        # they currently have an active arb (keep monitoring until it clears).
        if args.min_start > 0:
            _now    = datetime.now(timezone.utc)
            _cutoff = _now + timedelta(minutes=args.min_start)
            _kept: list[dict] = []
            for _g in dirty:
                _g_gid   = _gid(_g)
                _dt_str  = _g.get("date_time")
                _imminent = False
                if _dt_str:
                    try:
                        _game_dt  = datetime.fromisoformat(_dt_str.replace("Z", "+00:00"))
                        _imminent = _game_dt < _cutoff
                    except ValueError:
                        pass
                if _imminent and not _live.get(_g_gid):
                    if _g_gid not in _dropped_imminent:
                        _dropped_imminent.add(_g_gid)
                        _ts_drop = _now.strftime("%H:%M:%S")
                        _league = (_g.get('league') or '').upper()
                        _spread = _g.get('spread')
                        _total  = _g.get('total_line')
                        if _spread is not None:
                            _league_disp = f"{_league} {_spread:+g}"
                        elif _total is not None:
                            _league_disp = f"{_league} O/U {_total}"
                        else:
                            _league_disp = _league
                        print(f"\n  [{_ts_drop}]  DROPPED  "
                              f"{_g.get('team1')} vs {_g.get('team2')}"
                              f"  [{_league_disp}]"
                              f"  (within {args.min_start:.0f}min of start)",
                              flush=True)
                else:
                    if not _imminent:
                        _dropped_imminent.discard(_g_gid)
                    _kept.append(_g)
            dirty = _kept

        if not dirty:
            continue

        if mb_provider is not None:
            # Skip on-demand MB fetch for games where odds are still fresh — either
            # just fetched on-demand or written by the periodic poll loop.
            stale = [g for g in dirty
                     if (cache.provider_age_s(_gid(g), "matchbook") or float("inf"))
                     > _MB_FRESH_S]
            if stale:
                fetched = await _fetch_matchbook(stale, mb_provider)
                # Write MB fields back into the cache (no dirty mark) so the next
                # WS ticks within the freshness window don't re-fetch unnecessarily.
                for _g in fetched:
                    _mb = {k: v for k, v in _g.items() if k.startswith("matchbook_")}
                    if _mb:
                        cache.merge_provider_fields(_gid(_g), "matchbook", _mb,
                                                    mark_dirty=False)
                log.debug("matchbook: on-demand %d/%d games (rest fresh)",
                          len(stale), len(dirty))
                _fetched_by_gid = {_gid(g): g for g in fetched}
                games = [_fetched_by_gid.get(_gid(g), g) for g in dirty]
            else:
                log.debug("matchbook: all %d games fresh — skipping on-demand fetch",
                          len(dirty))
                games = dirty
        else:
            games = dirty

        for game in games:
            # Detect at 0.0 threshold for logging; filter to min_profit for display.
            _log_sure = calculator.find_sure_bets([game],    min_profit_pct=0.0)
            _log_bl   = calculator.find_back_lay_arbs([game], min_profit_pct=0.0)
            _log_kbo  = calculator.find_kbo_tie_aware_arbs([game], min_profit_pct=0.0)

            sure_bets = [a for a in _log_sure if (a.get("profit_pct") or 0) >= args.min_profit]
            back_lay  = [a for a in _log_bl   if (a.get("profit_pct") or 0) >= args.min_profit]
            kbo_arbs  = [a for a in _log_kbo  if (a.get("profit_pct") or 0) >= args.min_profit]

            total_sure     += len(sure_bets)
            total_back_lay += len(back_lay)
            total_kbo      += len(kbo_arbs)

            if not args.no_arb_log:
                for _a in _log_sure:
                    _event_log.log_arb("sure_bet", _a, _ROOT, source="stream")
                for _a in _log_bl:
                    _event_log.log_arb("back_lay", _a, _ROOT, source="stream")
                for _a in _log_kbo:
                    _event_log.log_arb("kbo", _a, _ROOT, source="stream")

            # Full game-state tick log (opt-in via --stream-log).
            if args.stream_log:
                _best_arb_type = None
                _best_profit   = None
                _tagged = (
                    [("sure_bet", a) for a in _log_sure] +
                    [("back_lay", a) for a in _log_bl] +
                    [("kbo",      a) for a in _log_kbo]
                )
                if _tagged:
                    _best_arb_type, _best_a = max(
                        _tagged, key=lambda t: t[1].get("profit_pct") or 0
                    )
                    _best_profit = _best_a.get("profit_pct")
                _event_log.log_stream_tick(game, _best_arb_type, _best_profit, _ROOT)
            # ─────────────────────────────────────────────────────────────

            # ── Arb state tracking ────────────────────────────────────────────
            # Build a snapshot of currently-detected arbs for this game.
            _gid_key = _gid(game)
            _prev = _live.get(_gid_key, {})
            _curr: dict[tuple, float] = {}
            _curr_arbs: dict[tuple, tuple[str, dict]] = {}
            for _a in sure_bets:
                _k: tuple = ("sb", _a.get("team1_back_provider", ""), _a.get("draw_back_provider", ""), _a.get("team2_back_provider", ""))
                _curr[_k] = _a["profit_pct"]
                _curr_arbs[_k] = ("sure_bet", _a)
            for _a in back_lay:
                _k = ("bl", (_a.get("arb_outcome") or "").lower(), _a.get("back_provider", ""), _a.get("lay_provider", ""))
                _curr[_k] = _a["profit_pct"]
                _curr_arbs[_k] = ("back_lay", _a)
            for _a in kbo_arbs:
                _k = ("kbo", _a.get("poly_underdog_slot", ""), _a.get("sx_fav_slot", ""))
                _curr[_k] = _a["profit_pct"]
                # KBO not in _curr_arbs — no placement support

            # Grace-period GONE: an arb must be absent for >= _GONE_GRACE_S seconds
            # before being reported gone.  Pending arbs stay in _live so they are
            # not re-shown as "appeared" if they recover within the window.
            _now_mono = time.monotonic()
            _gp = _gone_pending.setdefault(_gid_key, {})

            for k in _prev:
                if k not in _curr and k not in _gp:
                    _gp[k] = _now_mono  # record first-absence time

            for k in list(_gp):
                if k in _curr:
                    del _gp[k]  # recovered — reset grace timer

            _truly_gone = {k: _prev[k] for k, t in list(_gp.items())
                           if (_now_mono - t) >= _GONE_GRACE_S and k in _prev}
            for k in _truly_gone:
                del _gp[k]

            if not _gp:
                _gone_pending.pop(_gid_key, None)

            # Rebuild _live: keep pending arbs, add/update current, drop confirmed-gone
            _new_live = {k: v for k, v in _prev.items() if k not in _truly_gone}
            _new_live.update(_curr)
            if _new_live:
                _live[_gid_key] = _new_live
            else:
                _live.pop(_gid_key, None)

            _appeared = {k for k in _curr if k not in _prev}
            _gone     = _truly_gone
            _changed  = {k for k in _curr if k in _prev and abs(_curr[k] - _prev[k]) >= _PRINT_THRESHOLD}
            _to_print = _appeared | _changed

            if not _to_print and not _gone:
                continue

            ts    = datetime.now(timezone.utc).strftime("%H:%M:%S")
            label = (f"{game.get('team1')} vs {game.get('team2')}"
                     f"  [{(game.get('league') or '').upper()}]")
            print(f"\n  [{ts}]  ARB  {label}")

            _print_sb  = [a for a in sure_bets if ("sb",  a.get("team1_back_provider", ""), a.get("draw_back_provider", ""), a.get("team2_back_provider", "")) in _to_print]
            _print_bl  = [a for a in back_lay  if ("bl",  (a.get("arb_outcome") or "").lower(), a.get("back_provider", ""), a.get("lay_provider", "")) in _to_print]
            _print_kbo = [a for a in kbo_arbs  if ("kbo", a.get("poly_underdog_slot", ""), a.get("sx_fav_slot", "")) in _to_print]

            if _print_sb:
                print(f"  Sure bets ({len(_print_sb)}):")
                _arb._print_sure_bets(_print_sb, gbp_rate=gbp_rate, budget=args.budget)
            if _print_bl:
                print(f"  Back-lay arbs ({len(_print_bl)}):")
                _arb._print_back_lay_arbs(_print_bl, game=game, gbp_rate=gbp_rate, budget=args.budget)
            if _print_kbo:
                print(f"  KBO tie-aware arbs ({len(_print_kbo)}):")
                _arb._print_kbo_arbs(_print_kbo, gbp_rate=gbp_rate, budget=args.budget)
            for _k, _old_pct in _gone.items():
                _kind = _k[0]
                if _kind == "sb":
                    _parts = [p for p in (_k[1], _k[2], _k[3]) if p]
                    _desc = f"sure-bet ({'/'.join(_parts)})"
                elif _kind == "bl":
                    _desc = f"back-lay {_k[1]} ({_k[2]}/{_k[3]})"
                else:
                    _desc = f"KBO ({_k[1]}/{_k[2]})"
                print(f"  [{ts}]  {_desc}  GONE  (was {_old_pct:.4f}%)")

            # Alert (off by default in stream mode — pass --alert to enable).
            # Only fires for new or materially changed arbs, not for persistent ones.
            if args.alert and _to_print and settings.alert_enabled and settings.alert_webhook_url:
                best_pct = max(
                    (a.get("profit_pct", 0) for arbs in (_print_sb, _print_bl, _print_kbo) for a in arbs),
                    default=0,
                )
                subj = f"STREAM ARB {best_pct:+.2f}% {label}"
                body = subj
                try:
                    send_alert(subj, body, settings)
                except Exception as exc:
                    log.warning("alert failed: %s", exc)

            # ── Autobet scheduling ────────────────────────────────────────────
            # Schedule a delayed placement task for each newly-appeared arb that
            # clears the autobet threshold.  In-flight dedup ensures we don't
            # schedule the same bet twice during its delay window.
            if args.autobet:
                import bet_executor as _exec
                if _exec._HALT:
                    print("  [AUTOBET] HALTED — restart process to resume.", file=sys.stderr)
                else:
                    for _ak in _appeared:
                        if _ak[0] == "kbo":
                            continue  # no KBO placement support
                        _flight_key = (_gid_key, _ak)
                        if _flight_key in _in_flight or _flight_key in _bet_history:
                            continue
                        _ab_type, _ab_arb = _curr_arbs.get(_ak, (None, None))
                        if _ab_arb is None:
                            continue
                        if (_ab_arb.get("profit_pct") or 0) < args.autobet_min_profit:
                            continue
                        _in_flight.add(_flight_key)
                        asyncio.create_task(
                            _autobet_task(
                                _flight_key, _ak, _ab_type, _gid_key,
                                cache, settings, args, gbp_rate, _in_flight,
                            ),
                            name=f"autobet-{_gid_key}-{_ak[0]}",
                        )


# ---------------------------------------------------------------------------
# Game filters
# ---------------------------------------------------------------------------

def _exclude_imminent(games: list[dict], min_start_minutes: float) -> list[dict]:
    """Remove games whose start time is within min_start_minutes of now."""
    cutoff = datetime.now(timezone.utc) + timedelta(minutes=min_start_minutes)
    kept = []
    for g in games:
        dt_str = g.get("date_time")
        if not dt_str:
            kept.append(g)
            continue
        try:
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        except ValueError:
            kept.append(g)
            continue
        if dt >= cutoff:
            kept.append(g)
    return kept


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def _main_async(args: argparse.Namespace) -> None:
    settings = load_settings(_ROOT)
    calculator.configure(settings.commission)

    # Apply VPN proxy for Polymarket placement calls (mirrors scan.py).
    # The WS clients receive proxy_url directly; HTTP placement goes through
    # os.environ and the py_clob_client_v2 httpx singleton which must be
    # patched because it is created at import time before env vars are set.
    if settings.vpn_proxy_url:
        os.environ["HTTPS_PROXY"] = settings.vpn_proxy_url
        os.environ["HTTP_PROXY"]  = settings.vpn_proxy_url
        import httpx as _httpx
        _httpx_proxy = settings.vpn_proxy_url.replace("socks5h://", "socks5://")
        try:
            import py_clob_client_v2.http_helpers.helpers as _pm_helpers_v2
            _pm_helpers_v2._http_client = _httpx.Client(http2=True, proxy=_httpx_proxy)
        except Exception:
            pass  # library not present or API changed — env vars still apply

    # Redirect bet logging to stream-specific files so stream and scan logs stay separate.
    global _stream_arb_log
    _stream_arb_log = _ROOT / "outputs" / "stream_arb_log.jsonl"
    import bet as _bet_module
    _bet_module.set_log_path(_ROOT / "outputs" / "stream_bet_log.jsonl")

    ids_path = Path(args.ids)
    if not ids_path.exists():
        print(f"  ERROR: {ids_path} not found -- run ids.py first", file=sys.stderr)
        sys.exit(1)

    with open(ids_path) as f:
        index = json.load(f)

    all_games: list[dict] = index.get("games", [])
    stream_leagues = set(args.stream_leagues)
    games = [g for g in all_games if g.get("league") in stream_leagues]

    if args.min_start > 0:
        before = len(games)
        games = _exclude_imminent(games, args.min_start)
        excluded = before - len(games)
        if excluded:
            print(f"  Excluded {excluded} imminent game(s) (< {args.min_start:.0f} min to start)", flush=True)

    if not games:
        print(f"  No games found for leagues: {stream_leagues}", file=sys.stderr)
        print(f"  Run: python ids.py --leagues {' '.join(stream_leagues)}", file=sys.stderr)
        sys.exit(1)

    # FX rate (for arb display)
    gbp_rate: float | None = None
    try:
        import requests as _req
        r = _req.get(
            "https://open.er-api.com/v6/latest/USD",
            timeout=8,
            proxies={"https": None, "http": None},
        )
        r.raise_for_status()
        gbp_rate = r.json().get("rates", {}).get("GBP")
    except Exception:
        pass

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("━" * 72, flush=True)
    print(f"  Stream started: {started_at}", flush=True)
    print(f"  Leagues:        {', '.join(sorted(stream_leagues))}", flush=True)
    print(f"  Games:          {len(games)}", flush=True)
    if gbp_rate:
        print(f"  FX rate:        1 USD = {gbp_rate:.4f} GBP", flush=True)
    print("━" * 72, flush=True)

    # Build shared cache.
    # Strip all stale odds/avail from the seed — active_game_ids.json stores
    # snapshot odds from the last scan which can be hours old and include dust
    # liquidity values (e.g. lay1=110.0).  Only fresh WS data drives detection.
    #
    # Merge duplicate gids by preferring non-None values.  If event matching
    # creates two entries for the same physical game (e.g. one with sx_bet_market_hash
    # and one without), last-write-wins in the cache would drop the hash.
    # build_market_map still subscribes via the entry that has the hash, so WS prices
    # flow in but placement then fails with "No sx_bet_market_hash in game context".
    _ODDS_SUFFIXES = ("_back_odds", "_lay_odds", "_back_avail", "_lay_avail")
    _seed_by_gid: dict[str, dict] = {}
    for _g in games:
        _stripped = {k: v for k, v in _g.items() if not any(k.endswith(s) for s in _ODDS_SUFFIXES)}
        _key = _gid(_stripped)
        if _key not in _seed_by_gid:
            _seed_by_gid[_key] = _stripped
        else:
            for k, v in _stripped.items():
                if v is not None:
                    _seed_by_gid[_key][k] = v
    _seed_games = list(_seed_by_gid.values())
    cache = OddsCache()
    cache.seed(_seed_games)

    # Polymarket WS
    token_map = build_token_map(games)
    pm_games  = sum(1 for g in games if g.get("polymarket_team1_clob_token_id"))
    print(f"  Polymarket: {len(token_map)} tokens across {pm_games} games", flush=True)

    pm_client = PolymarketWSClient(
        cache     = cache,
        token_map = token_map,
        proxy_url = settings.vpn_proxy_url,
        ws_url    = settings.polymarket.ws_url,
    )

    # SX Bet WS
    sx_client = None
    if not settings.sx_bet.api_key:
        print("  SX Bet: DISABLED (SX_BET_API_KEY not set)", file=sys.stderr, flush=True)
    else:
        market_map = build_market_map(games)
        sx_games   = sum(1 for g in games if g.get("sx_bet_market_hash"))
        print(f"  SX Bet: {len(market_map)} market subscriptions across {sx_games} games", flush=True)
        sx_client = SxBetWSClient(
            cache      = cache,
            market_map = market_map,
            api_key    = settings.sx_bet.api_key,
            token_url  = settings.sx_bet.realtime_token_url,
            ws_url     = settings.sx_bet.realtime_url,
            proxy_url  = settings.vpn_proxy_url,
        )

    # Matchbook provider (always direct — never via proxy)
    mb_provider = None
    if args.no_matchbook:
        print("  Matchbook: DISABLED (--no-matchbook)", flush=True)
    else:
        from matched_betting.providers.registry import build_provider_registry
        mb_http     = HttpClient(proxy_url=None)
        mb_registry = build_provider_registry(settings, http_client=mb_http)
        mb_provider = mb_registry.get("matchbook")
        if mb_provider:
            if args.no_mb_ondemand:
                print(f"  Matchbook: poll only every {args.mb_poll_interval:.0f}s "
                      f"(on-demand disabled)", flush=True)
            else:
                print(f"  Matchbook: on-demand (WS trigger) + periodic poll every "
                      f"{args.mb_poll_interval:.0f}s", flush=True)
        else:
            print("  Matchbook: DISABLED (credentials missing)", flush=True)

    # mb_provider_for_arb_loop: None when poll-only so _arb_loop never fires on-demand fetches.
    # The poll loop still runs with the real provider and writes MB fields into the cache.
    mb_provider_for_arb_loop = None if args.no_mb_ondemand else mb_provider

    print(flush=True)
    stream_log_note = "outputs/stream.db" if args.stream_log else "off (--stream-log to enable)"
    arb_log_note    = "off (--no-arb-log)" if args.no_arb_log else "outputs/arb_log.jsonl"
    print(f"  Min profit: {args.min_profit:.2f}%   Debounce: {args.debounce}s   "
          f"Tick log: {stream_log_note}   Arb log: {arb_log_note}", flush=True)
    if args.autobet:
        _ab_mode = "TEST $5" if args.autobet_test else ("DRY-RUN" if args.bet_dry_run else "LIVE")
        print(f"  Autobet:    ENABLED [{_ab_mode}]  "
              f"threshold={args.autobet_min_profit:.2f}%  delay={args.autobet_delay}s  "
              f"budget=${args.budget:.2f}", flush=True)
    print(flush=True)
    print("  Listening for arbs -- Ctrl-C to stop", flush=True)
    print(flush=True)

    tasks: list[asyncio.Task] = [
        asyncio.create_task(pm_client.run(), name="pm-ws"),
        asyncio.create_task(
            _arb_loop(cache, settings, args, mb_provider_for_arb_loop, gbp_rate), name="arb-loop"
        ),
    ]
    if sx_client:
        tasks.append(asyncio.create_task(sx_client.run(), name="sx-ws"))
    if mb_provider:
        tasks.append(asyncio.create_task(
            _matchbook_poll_loop(games, cache, mb_provider, args.mb_poll_interval),
            name="mb-poll",
        ))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
        "--gone-grace", type=float, default=3.0, metavar="SECONDS",
        help="Seconds an arb must be continuously absent before reporting GONE "
             "(default: 3.0). Suppresses false disappearances from P2P order-book "
             "churn where orders fill and are replaced within a second.",
    )
    parser.add_argument(
        "--min-profit", type=float, default=0.0, metavar="PCT",
        help="Minimum profit %% to display (default: 0.0).",
    )
    parser.add_argument(
        "--budget", type=float, default=10.0, metavar="USDC",
        help="Max USDC stake budget per arb. Used as the display stake and as the Kelly cap "
             "when --autobet is active (default: 10.0).",
    )
    parser.add_argument(
        "--min-start", type=float, default=10.0, metavar="MINUTES",
        help="Exclude games starting within this many minutes (default: 10).",
    )
    parser.add_argument(
        "--no-matchbook", action="store_true",
        help="Disable Matchbook fetches entirely (both on-demand and periodic poll).",
    )
    parser.add_argument(
        "--no-mb-ondemand", action="store_true",
        help="Disable on-demand Matchbook fetches triggered by WS price changes. "
             "Matchbook odds are refreshed only by the periodic poll (--mb-poll-interval). "
             "Use this to avoid rate-limiting when markets are very active.",
    )
    parser.add_argument(
        "--mb-poll-interval", type=float, default=600.0, metavar="SECONDS",
        help="Seconds between periodic Matchbook scans (default: 600 = 10 min). "
             "With --no-mb-ondemand this is the only refresh path.",
    )
    parser.add_argument(
        "--alert", action="store_true",
        help="Send ntfy alerts when arbs are found (default: off).",
    )
    parser.add_argument(
        "--stream-log", action="store_true",
        help="Write per-game odds snapshots to outputs/stream_log.jsonl on every "
             "detector tick (includes ticks with no arb found). Arb detections are "
             "always written to arb_log.jsonl regardless of this flag.",
    )
    parser.add_argument(
        "--no-arb-log", action="store_true",
        help="Suppress writing arb detections to outputs/arb_log.jsonl. "
             "Useful when running alongside the daemon to avoid duplicate log entries.",
    )
    parser.add_argument("--debug", action="store_true", help="Verbose logging.")

    # ── Autobet ────────────────────────────────────────────────────────────
    parser.add_argument(
        "--autobet", action="store_true",
        help="Automatically place arbs found during streaming. "
             "Waits --autobet-delay seconds then re-validates before placing.",
    )
    parser.add_argument(
        "--autobet-delay", type=float, default=10.0, metavar="SECONDS",
        help="Seconds to wait after an arb is detected before placing (default: 10). "
             "Arb must still be profitable after the delay or the bet is skipped.",
    )
    parser.add_argument(
        "--autobet-min-profit", type=float, default=None, metavar="PCT",
        help="Minimum profit %% to trigger autobet (default: same as --min-profit).",
    )
    parser.add_argument(
        "--autobet-test", action="store_true",
        help="Test mode: place real bets sized to exactly $5 total stake, "
             "bypassing Kelly. All other guards (dedup, bankroll, balance) still apply.",
    )
    parser.add_argument(
        "--bet-dry-run", action="store_true",
        help="With --autobet: build and log orders but do not submit them.",
    )

    args = parser.parse_args()

    # Default autobet threshold to --min-profit if not set explicitly.
    if args.autobet_min_profit is None:
        args.autobet_min_profit = args.min_profit

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
