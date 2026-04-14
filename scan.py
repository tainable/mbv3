"""
scan.py
-------
Stage 2 of the pipeline: read the IDs JSON produced by ids.py and, for each
game, fetch live odds from all providers simultaneously, immediately run the
arb calculator (sure-bets and back-lay arbs including fees), then move to the
next game.

By default Smarkets is excluded. Pass --providers smarkets to include it.

Usage:
    python scan.py
    python scan.py --ids outputs/active_game_ids.json
    python scan.py --leagues nba epl
    python scan.py --providers matchbook polymarket sx_bet
    python scan.py --min-profit 0.5
    python scan.py --debug
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
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

import arb_finder as _arb

DEFAULT_LEAGUES = ["nba", "mlb", "ucl", "epl", "uel", "nhl", "ipl"]
DEFAULT_PROVIDERS = ["matchbook", "polymarket", "sx_bet"]  # Smarkets excluded by default
DEFAULT_IDS = Path("outputs/active_game_ids.json")

_PROVIDER_ID_FIELD = {
    "matchbook":  "matchbook_event_id",
    "polymarket": "polymarket_market_id",
    "smarkets":   "smarkets_market_id",
    "sx_bet":     "sx_bet_market_hash",
}

_MONTH = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ---------------------------------------------------------------------------
# Terminal display helpers
# ---------------------------------------------------------------------------

def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 100


def _hr(width: int | None = None) -> str:
    return "━" * (width or _term_width())


def _fmt_date(date_time: str | None) -> str:
    """Format an ISO timestamp as '13 Apr 19:30' — cross-platform."""
    if not date_time:
        return ""
    try:
        dt = datetime.fromisoformat(date_time.replace("Z", "+00:00"))
        return f"{dt.day} {_MONTH[dt.month - 1]} {dt.strftime('%H:%M')}"
    except ValueError:
        return ""



_GAME_BUDGET = 1.5   # seconds per game (rate limiting + animation window)
_ANIM_TICK   = 1 / 30  # ~30 fps refresh during the animation loop


def _progress_line(fill: float, current: int, total: int, game: dict, width: int) -> str:
    """Build a single-line progress string that fits within `width` columns.

    fill    — continuous bar position in [0, total] (float, drives smooth animation)
    current — integer game index shown in the X/Y counter
    """
    bar_w = 24
    filled = round(bar_w * fill / total) if total else 0
    bar = "█" * filled + "░" * (bar_w - filled)
    team1 = game.get("team1") or ""
    team2 = game.get("team2") or ""
    league = (game.get("league") or "").upper()
    date_s = _fmt_date(game.get("date_time"))
    line = f"[{bar}] {current}/{total}  {team1} vs {team2}  [{league}]  {date_s}"
    return line[: width - 1]  # never wrap


# ---------------------------------------------------------------------------
# Game loading
# ---------------------------------------------------------------------------

def _load_games(
    ids_path: Path,
    leagues: list[str],
    providers: list[str],
) -> list[dict[str, Any]]:
    """Load game contexts from the IDs JSON, keeping only games with ≥2 provider IDs."""
    data = json.loads(ids_path.read_text(encoding="utf-8"))
    result = []
    for game in data.get("games", []):
        if game.get("league") not in leagues:
            continue
        coverage = sum(1 for p in providers if game.get(_PROVIDER_ID_FIELD.get(p, "")))
        if coverage >= 2:
            result.append(game)
    return result


# ---------------------------------------------------------------------------
# Per-game scan (no stdout side effects — returns arbs only)
# ---------------------------------------------------------------------------

def _scan_game(
    game: dict[str, Any],
    providers: list[str],
    leagues: list[str],
    registry: dict,
    min_profit_pct: float,
    debug: Any,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Fetch all providers for one game in parallel and run the arb calculator.

    Returns (sure_bets, back_lay_arbs, games_payload). All stdout printing is
    handled by the caller so the progress line can be cleared cleanly.
    """
    label = f"{game.get('team1')} vs {game.get('team2')}  [{game.get('league', '').upper()}]"

    records: list[OddsRecord] = []
    game_t0 = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(providers)) as executor:
        futures = {
            executor.submit(registry[name].fetch_odds_by_ids, [game], leagues): (name, time.monotonic())
            for name in providers
            if name in registry
        }
        for future in as_completed(futures):
            name, t0 = futures[future]
            elapsed = time.monotonic() - t0
            try:
                payload = future.result()
                records.extend(payload.records)
                debug(f"  {label}  {name}: {len(payload.records)} records  ({elapsed:.1f}s)")
            except ProviderNotReadyError:
                pass
            except Exception as exc:
                debug(f"  {label}  {name}: failed — {exc}  ({elapsed:.1f}s)")
    game_elapsed = time.monotonic() - game_t0
    if game_elapsed > 3:
        debug(f"  {label}  SLOW GAME: total fetch took {game_elapsed:.1f}s")

    records = [r for r in records if is_game_win_loss_record(r)]
    if not records:
        return [], [], []

    canonical_assignment, canonical_events = match_records_to_canonical_events(records)
    games_payload = build_aggregated_games_payload(records, canonical_assignment, canonical_events)

    sure_bets = _arb.find_sure_bets(games_payload, min_profit_pct=min_profit_pct)
    back_lay_arbs = _arb.find_back_lay_arbs(games_payload, min_profit_pct=min_profit_pct)

    return sure_bets, back_lay_arbs, games_payload


# ---------------------------------------------------------------------------
# Odds display (--show-odds)
# ---------------------------------------------------------------------------

_PROVIDER_LABEL = {
    "matchbook":  "Matchbook  ",
    "smarkets":   "Smarkets   ",
    "polymarket": "Polymarket ",
    "sx_bet":     "SX Bet     ",
}

_PROVIDER_SHORT = {
    "matchbook":  "MB",
    "smarkets":   "SM",
    "polymarket": "PM",
    "sx_bet":     "SX",
}


def _print_odds_table(
    i: int,
    total: int,
    game: dict[str, Any],
    games_payload: list[dict[str, Any]],
    providers: list[str],
) -> None:
    """Print a compact odds table for one game after scanning."""
    team1 = game.get("team1") or "Team1"
    team2 = game.get("team2") or "Team2"
    league = (game.get("league") or "").upper()
    date_s = _fmt_date(game.get("date_time"))
    print(f"  [{i}/{total}]  {team1} vs {team2}  [{league}]  {date_s}")

    if not games_payload:
        print("    (no odds returned by any provider)")
        print()
        return

    g = games_payload[0]
    three_way = g.get("market_type") == "three_way"
    slots = ("team1", "draw", "team2") if three_way else ("team1", "team2")
    slot_names = {"team1": team1, "draw": "Draw", "team2": team2}

    # Column headers — shorten team names to keep lines tidy
    def _short(name: str, max_len: int = 14) -> str:
        return name if len(name) <= max_len else name[:max_len - 1] + "…"

    headers = [_short(slot_names[s]) for s in slots]
    col_w = max(6, *(len(h) for h in headers))

    header_row = "  ".join(f"{h:<{col_w}}" for h in headers)
    print(f"    {'':11s}  {header_row}")

    for provider in providers:
        label = _PROVIDER_LABEL.get(provider, f"{provider:<11s}")
        cells = []
        for slot in slots:
            back = g.get(f"{provider}_{slot}_back_odds")
            lay  = g.get(f"{provider}_{slot}_lay_odds")
            if back is not None and lay is not None:
                cell = f"{back:.3f}/{lay:.3f}"
            elif back is not None:
                cell = f"{back:.3f}"
            else:
                cell = "—"
            cells.append(f"{cell:<{col_w}}")
        print(f"    {label}  {'  '.join(cells)}")

    # Closest arb lines
    def _sp(name: str) -> str:
        return _PROVIDER_SHORT.get(name, name[:2].upper())

    def _pct_str(pct: float) -> str:
        sign = "+" if pct >= 0 else ""
        flag = "  ✓ ARB" if pct >= 0 else ""
        return f"{sign}{pct:.2f}%{flag}"

    sb = calculator.best_sure_bet_opportunity(g)
    bl = calculator.best_back_lay_opportunity(g)

    print(f"    {'':11s}  {'─' * max(20, col_w * len(slots) + 2 * (len(slots) - 1))}")

    if sb:
        t1n = _short(team1, 12)
        t2n = _short(team2, 12)
        t1_part = f"{t1n} @{sb['team1_back_odds']:.3f}({_sp(sb['team1_back_provider'])})"
        t2_part = f"{t2n} @{sb['team2_back_odds']:.3f}({_sp(sb['team2_back_provider'])})"
        if sb.get("draw_back_odds") is not None:
            draw_part = f"Draw @{sb['draw_back_odds']:.3f}({_sp(sb['draw_back_provider'])})  "
        else:
            draw_part = ""
        print(f"    Sure bet:    {t1_part}  {draw_part}{t2_part}  →  {_pct_str(sb['profit_pct'])}")

    if bl:
        outcome = _short(bl["arb_outcome"], 12)
        b_part = f"back @{bl['back_odds']:.3f}({_sp(bl['back_provider'])})"
        l_part = f"lay @{bl['lay_odds']:.3f}({_sp(bl['lay_provider'])})"
        print(f"    Back-lay:    {outcome}  {b_part} / {l_part}  →  {_pct_str(bl['profit_pct'])}")

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Scan live odds game by game using IDs from ids.py, "
            "reporting arbs immediately as each game is processed."
        )
    )
    parser.add_argument(
        "--ids",
        type=Path,
        default=DEFAULT_IDS,
        help=f"Path to the IDs JSON produced by ids.py (default: {DEFAULT_IDS}).",
    )
    parser.add_argument(
        "--leagues",
        nargs="+",
        default=DEFAULT_LEAGUES,
        choices=DEFAULT_LEAGUES,
        metavar="LEAGUE",
        help="Leagues to scan: nba mlb ucl epl uel nhl ipl (default: all).",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        choices=["matchbook", "smarkets", "polymarket", "sx_bet"],
        metavar="PROVIDER",
        help="Providers to query (default: matchbook polymarket sx_bet). Smarkets excluded by default.",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Minimum net profit %% to report (default: 0.0).",
    )
    parser.add_argument(
        "--show-odds",
        action="store_true",
        help="After each game, print the fetched back/lay odds per provider for debugging.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print provider progress to stderr.",
    )
    args = parser.parse_args()

    debug = stderr_debug if args.debug else noop_debug

    ids_path = args.ids if args.ids.is_absolute() else _PROJECT_ROOT / args.ids
    if not ids_path.exists():
        print(
            f"ERROR: IDs file not found at {ids_path}\n"
            "Run  python ids.py  first to build the game ID index.",
            file=sys.stderr,
        )
        sys.exit(1)

    games = _load_games(ids_path, args.leagues, args.providers)

    settings = load_settings(_PROJECT_ROOT)
    calculator.configure(settings.commission)
    http_client = HttpClient()
    registry = build_provider_registry(settings, http_client, debug)

    smarkets_note = (
        "Smarkets 0% (60-day intro)"
        if calculator.SMARKETS_ZERO_COMMISSION_PERIOD
        else "Smarkets 2%"
    )
    commission_str = (
        f"Matchbook {settings.commission.matchbook * 100:.0f}%"
        f" | {smarkets_note}"
        f" | SX Bet 0%"
        f" | Polymarket dynamic"
    )

    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    tw = _term_width()
    print(_hr(tw))
    print(f"  Started:    {started_at}")
    print(f"  Leagues:    {',  '.join(args.leagues)}")
    print(f"  Providers:  {',  '.join(args.providers)}")
    print(f"  Games:      {len(games)}  (≥2 provider coverage)")
    print(f"  Commission: {commission_str}")
    print(_hr(tw))

    if not games:
        print("\nNo multi-provider games found. Run  python ids.py  to refresh the ID index.")
        return

    print()

    total_sure_bets: list[dict] = []
    total_back_lay_arbs: list[dict] = []
    progress_on_screen = False

    for i, game in enumerate(games, 1):
        debug(f"[{i}/{len(games)}] {game.get('team1')} vs {game.get('team2')}")
        progress_on_screen = True
        game_start = time.monotonic()

        # Run the scan in a background thread so the main thread can animate
        # the progress bar smoothly throughout the full _GAME_BUDGET window.
        with ThreadPoolExecutor(max_workers=1) as _scan_ex:
            _future = _scan_ex.submit(
                _scan_game, game, args.providers, args.leagues, registry, args.min_profit, debug
            )
            while True:
                elapsed = time.monotonic() - game_start
                # smooth_pos moves continuously from (i-1) → i over _GAME_BUDGET seconds
                smooth_pos = (i - 1) + min(elapsed / _GAME_BUDGET, 1.0)
                line = _progress_line(smooth_pos, i, len(games), game, tw)
                sys.stdout.write(f"\r{line:<{tw - 1}}")
                sys.stdout.flush()
                if elapsed >= _GAME_BUDGET and _future.done():
                    break
                time.sleep(_ANIM_TICK)

        sure_bets, back_lay_arbs, games_payload = _future.result()
        total_sure_bets.extend(sure_bets)
        total_back_lay_arbs.extend(back_lay_arbs)

        if sure_bets or back_lay_arbs:
            # Clear the progress line before printing arb details
            sys.stdout.write("\r" + " " * (tw - 1) + "\r\n")
            sys.stdout.flush()
            progress_on_screen = False

            label = (
                f"{game.get('team1')} vs {game.get('team2')}"
                f"  [{(game.get('league') or '').upper()}]"
            )
            date_s = _fmt_date(game.get("date_time"))
            print(_hr(tw))
            print(f"  ARB FOUND  {label}  {date_s}")
            print(_hr(tw))
            if sure_bets:
                print(f"  Sure bets ({len(sure_bets)}):")
                _arb._print_sure_bets(sure_bets)
            if back_lay_arbs:
                print(f"  Back-lay arbs ({len(back_lay_arbs)}):")
                _arb._print_back_lay_arbs(back_lay_arbs)
            print()

        if args.show_odds:
            if progress_on_screen:
                sys.stdout.write("\r" + " " * (tw - 1) + "\r\n")
                sys.stdout.flush()
                progress_on_screen = False
            _print_odds_table(i, len(games), game, games_payload, args.providers)

    # End progress line before summary
    if progress_on_screen:
        print()

    finished_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    print(_hr(tw))
    print(f"  DONE  {finished_at}")
    print(f"  Games scanned:  {len(games)}")
    print(f"  Sure bets:      {len(total_sure_bets)}")
    print(f"  Back-lay arbs:  {len(total_back_lay_arbs)}")
    print(_hr(tw))


if __name__ == "__main__":
    main()
