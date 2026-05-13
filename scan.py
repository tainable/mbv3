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
from datetime import datetime, timedelta, timezone
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

try:
    import requests as _requests
    def _fetch_usd_gbp_rate() -> float | None:
        try:
            r = _requests.get("https://open.er-api.com/v6/latest/USD", timeout=8)
            r.raise_for_status()
            return r.json().get("rates", {}).get("GBP")
        except Exception:
            return None
except ImportError:
    def _fetch_usd_gbp_rate() -> float | None:  # type: ignore[misc]
        return None

import arb_finder as _arb
import bet_executor as _exec

DEFAULT_LEAGUES = ["nba", "mlb", "mlb_spread", "ucl", "epl", "uel", "nhl", "ipl", "seria", "laliga"]
DEFAULT_PROVIDERS = ["matchbook", "polymarket", "sx_bet", "azuro"]  # Smarkets excluded by default
DEFAULT_IDS = Path("outputs/active_game_ids.json")


# ---------------------------------------------------------------------------
# Proxy / VPN check
# ---------------------------------------------------------------------------

def _check_proxy(proxy_url: str) -> None:
    """
    Verify the configured VPN proxy is reachable via a quick socket connect.
    If not reachable, warn the user and ask whether to continue.
    """
    import socket
    from urllib.parse import urlparse

    parsed = urlparse(proxy_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 1080

    try:
        with socket.create_connection((host, port), timeout=2):
            return  # Proxy is reachable — all good
    except OSError:
        pass

    print(
        f"\n  WARNING: VPN proxy {proxy_url} is not reachable ({host}:{port} refused/timed out).\n"
        f"  sx_bet and polymarket requests will fail or expose your real IP.\n",
        file=sys.stderr,
    )
    try:
        answer = input("  Continue anyway? [y/N] ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        sys.exit("Aborted.")
    print()


_PROVIDER_ID_FIELD = {
    "matchbook":  "matchbook_event_id",
    "polymarket": "polymarket_market_id",
    "smarkets":   "smarkets_market_id",
    "sx_bet":     "sx_bet_market_hash",
    "azuro":      "azuro_condition_id",
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


def _parse_kickoff(date_time: str | None) -> "datetime | None":
    """Parse an ISO kickoff timestamp into a timezone-aware datetime, or None."""
    if not date_time:
        return None
    try:
        return datetime.fromisoformat(date_time.replace("Z", "+00:00"))
    except ValueError:
        return None


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
) -> tuple[list[dict], list[dict], list[dict], list[OddsRecord]]:
    """Fetch all providers for one game in parallel and run the arb calculator.

    Returns (sure_bets, back_lay_arbs, games_payload, raw_records). All stdout
    printing is handled by the caller so the progress line can be cleared cleanly.
    raw_records contains all OddsRecords before win/loss filtering.
    """
    label = f"{game.get('team1')} vs {game.get('team2')}  [{game.get('league', '').upper()}]"

    raw_records: list[OddsRecord] = []
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
                raw_records.extend(payload.records)
                debug(f"  {label}  {name}: {len(payload.records)} records  ({elapsed:.1f}s)")
                for w in payload.warnings:
                    debug(f"  {label}  {name}: WARNING — {w}")
            except ProviderNotReadyError:
                pass
            except Exception as exc:
                debug(f"  {label}  {name}: failed — {exc}  ({elapsed:.1f}s)")
    game_elapsed = time.monotonic() - game_t0
    if game_elapsed > 3:
        debug(f"  {label}  SLOW GAME: total fetch took {game_elapsed:.1f}s")

    records = [r for r in raw_records if is_game_win_loss_record(r)]
    if not records:
        return [], [], [], raw_records

    canonical_assignment, canonical_events = match_records_to_canonical_events(records)
    games_payload = build_aggregated_games_payload(records, canonical_assignment, canonical_events)

    # When SX Bet (or any provider) returns a full-league scan instead of a
    # single game, multiple canonical events land in games_payload.  Narrow to
    # the entry whose teams match the requested game so arb detection and the
    # odds table show the right row.
    if len(games_payload) > 1:
        ctx_t1 = (game.get("team1") or "").lower()
        ctx_t2 = (game.get("team2") or "").lower()
        if ctx_t1 and ctx_t2:
            matched = [
                g for g in games_payload
                if (g.get("team1") or "").lower() == ctx_t1
                and (g.get("team2") or "").lower() == ctx_t2
            ]
            if matched:
                games_payload = matched

    sure_bets = _arb.find_sure_bets(games_payload, min_profit_pct=min_profit_pct)
    back_lay_arbs = _arb.find_back_lay_arbs(games_payload, min_profit_pct=min_profit_pct)

    return sure_bets, back_lay_arbs, games_payload, raw_records


# ---------------------------------------------------------------------------
# Odds display (--show-odds)
# ---------------------------------------------------------------------------

_PROVIDER_LABEL = {
    "matchbook":  "Matchbook  ",
    "smarkets":   "Smarkets   ",
    "polymarket": "Polymarket ",
    "sx_bet":     "SX Bet     ",
    "azuro":      "Azuro      ",
}

_PROVIDER_SHORT = {
    "matchbook":  "MB",
    "smarkets":   "SM",
    "polymarket": "PM",
    "sx_bet":     "SX",
    "azuro":      "AZ",
}


def _fmt_stake(usdc: float, gbp_rate: float | None) -> str:
    """Format a USDC stake as 'GBP X (USDC Y)' or 'USDC Y' if no rate."""
    if usdc <= 0:
        return "—"
    if gbp_rate:
        return f"GBP {usdc * gbp_rate:,.0f}  (USDC {usdc:,.0f})"
    return f"USDC {usdc:,.0f}"


def _print_odds_table(
    i: int,
    total: int,
    game: dict[str, Any],
    games_payload: list[dict[str, Any]],
    providers: list[str],
    raw_records: list[OddsRecord] | None = None,
    gbp_rate: float | None = None,
) -> None:
    """Print a compact odds table for one game after scanning."""
    team1 = game.get("team1") or "Team1"
    team2 = game.get("team2") or "Team2"
    league = (game.get("league") or "").upper()
    date_s = _fmt_date(game.get("date_time"))
    spread = games_payload[0].get("spread") if games_payload else game.get("spread")
    spread_s = f" {spread:+.1f}" if spread is not None else ""
    print(f"  [{i}/{total}]  {team1} vs {team2}  [{league}{spread_s}]  {date_s}")

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

    # Azuro liquidity — show max stake per slot from OddsRecord metadata
    if raw_records and "azuro" in providers:
        az_records = {
            r.selection_name: r
            for r in raw_records
            if r.provider == "azuro"
        }
        if az_records:
            # Build stake cells aligned to the same slot columns
            stake_cells = []
            for slot in slots:
                # Find the Azuro record for this slot
                az_r = None
                t1_lower = (g.get("team1") or "").lower()
                t2_lower = (g.get("team2") or "").lower()
                for r in raw_records:
                    if r.provider != "azuro":
                        continue
                    sel = r.selection_name.lower()
                    if slot == "draw" and sel == "draw":
                        az_r = r
                        break
                    elif slot == "team1" and sel == t1_lower:
                        az_r = r
                        break
                    elif slot == "team2" and sel == t2_lower:
                        az_r = r
                        break
                usdc = (az_r.metadata or {}).get("max_stake_usdc", 0.0) if az_r else 0.0
                cell = _fmt_stake(usdc, gbp_rate)
                stake_cells.append(f"{cell:<{col_w}}")
            print(f"    {'Azuro liq: ':11s}  {'  '.join(stake_cells)}")
            print(f"    {'':11s}  (max stake per leg, USDC pool)")

    print()


def _print_polymarket_debug(
    game: dict[str, Any],
    raw_records: list[OddsRecord],
) -> None:
    """Print detailed Polymarket odds-fetch diagnostics for one game.

    Shows the stored CLOB token IDs from the game index, every OddsRecord
    returned by the Polymarket provider, and an implied-probability sum check
    to flag cross-market contamination (sum < 1.0).
    """
    league = (game.get("league") or "").upper()
    team1 = game.get("team1") or "Team1"
    team2 = game.get("team2") or "Team2"
    print(f"  [PM-DEBUG]  {league}: {team1} vs {team2}")

    # ── Stored IDs from active_game_ids.json ──────────────────────────────
    market_id  = game.get("polymarket_market_id")
    t1_token   = game.get("polymarket_team1_clob_token_id")
    draw_token = game.get("polymarket_draw_clob_token_id")
    t2_token   = game.get("polymarket_team2_clob_token_id")
    t1_mkt     = game.get("polymarket_team1_market_id")
    draw_mkt   = game.get("polymarket_draw_market_id")
    t2_mkt     = game.get("polymarket_team2_market_id")

    print("    Stored IDs (active_game_ids.json):")
    print(f"      polymarket_market_id           = {market_id or '(none)'}")
    print(f"      polymarket_team1_clob_token_id  = {t1_token or '(none)'}"
          + (f"  [market_id={t1_mkt}]" if t1_mkt else ""))
    if draw_token is not None or draw_mkt is not None:
        print(f"      polymarket_draw_clob_token_id   = {draw_token or '(none)'}"
              + (f"  [market_id={draw_mkt}]" if draw_mkt else ""))
    print(f"      polymarket_team2_clob_token_id  = {t2_token or '(none)'}"
          + (f"  [market_id={t2_mkt}]" if t2_mkt else ""))

    # ── Which fetch path will be used ────────────────────────────────────
    is_soccer = league.lower() in {"ucl", "epl", "uel", "seria"}
    if is_soccer:
        path = "CLOB per-slot (soccer)" if t1_token else "Gamma binary fallback (soccer)"
    elif t1_token and t2_token:
        path = "CLOB direct (both tokens stored)"
    elif market_id:
        path = "Gamma moneyline fallback (market_id only)"
    else:
        path = "NONE — no IDs stored"
    print(f"    Fetch path: {path}")

    # ── Records returned by Polymarket ────────────────────────────────────
    pm_records = [r for r in raw_records if r.provider == "polymarket"]
    back_records = [r for r in pm_records if r.selection_side == "back"]

    if not pm_records:
        print("    Records: (none returned)")
        print()
        return

    print(f"    Records ({len(pm_records)} total, {len(back_records)} back):")
    for r in pm_records:
        meta = r.metadata or {}
        token = meta.get("token_id") or meta.get("clob_token_id") or "(unknown)"
        liq   = meta.get("liquidity_usd")
        liq_s = f"  liq=${liq:.0f}" if liq else ""
        print(
            f"      {r.selection_name:<22s}  {r.selection_side:<4s}  "
            f"prob={r.implied_probability:.5f}  odds={r.decimal_odds:.4f}  "
            f"token={token}{liq_s}"
        )

    # ── Implied-probability sanity check ─────────────────────────────────
    if back_records:
        total = sum(r.implied_probability for r in back_records if r.implied_probability)
        if total >= 1.0:
            flag = "  ← OK (margin present)"
        else:
            flag = "  ← WARNING: <1.0 — likely cross-market contamination"
        print(f"    Back implied prob sum: {total:.5f}{flag}")

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
        help="Leagues to scan: nba mlb mlb_spread ucl epl uel nhl ipl seria laliga (default: all).",
    )
    parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        choices=["matchbook", "smarkets", "polymarket", "sx_bet", "azuro"],
        metavar="PROVIDER",
        help="Providers to query (default: matchbook polymarket sx_bet azuro). Smarkets excluded by default.",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Minimum net profit %% to report (default: 0.0).",
    )
    parser.add_argument(
        "--no-skip-imminent",
        action="store_true",
        help=(
            "Include games that kick off within --imminent-minutes or are already in-play. "
            "By default these are excluded to avoid placing bets on live/imminent markets."
        ),
    )
    parser.add_argument(
        "--imminent-minutes",
        type=int,
        default=10,
        metavar="MINUTES",
        help="Games kicking off within this many minutes are treated as imminent and skipped (default: 10).",
    )
    parser.add_argument(
        "--show-odds",
        action="store_true",
        help="After each game, print the fetched back/lay odds per provider for debugging.",
    )
    parser.add_argument(
        "--azuro-cap",
        action="store_true",
        help=(
            "When an arb involves Azuro, print a line showing the maximum achievable "
            "profit and total stake constrained by Azuro's pool size."
        ),
    )
    parser.add_argument(
        "--polymarket-debug",
        action="store_true",
        help=(
            "After each game, print a detailed Polymarket diagnostics block: "
            "stored CLOB token IDs, every fetched record (selection, side, prob, odds, token), "
            "fetch path used, and implied-probability sum check."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print provider progress to stderr.",
    )
    parser.add_argument(
        "--auto-bet",
        action="store_true",
        help=(
            "Automatically place all legs of every arb found. "
            "Requires --budget. Supported providers: matchbook, polymarket, sx_bet."
        ),
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=10.0,
        metavar="USDC",
        help="Total stake budget per arb in USDC (default: 10.0). "
             "Matchbook stakes are converted to GBP at the live FX rate.",
    )
    parser.add_argument(
        "--bet-dry-run",
        action="store_true",
        help="With --auto-bet: build and sign orders but do not submit them.",
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

    imminent_skipped = 0
    if not args.no_skip_imminent:
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(minutes=args.imminent_minutes)
        filtered = []
        for g in games:
            ko = _parse_kickoff(g.get("date_time"))
            if ko is None or ko > cutoff:
                filtered.append(g)
            else:
                imminent_skipped += 1
        games = filtered

    settings = load_settings(_PROJECT_ROOT)  # loads .env → os.environ
    calculator.configure(settings.commission)
    http_client = HttpClient()
    proxied_http_client = None
    if settings.vpn_proxy_url:
        _check_proxy(settings.vpn_proxy_url)
        proxied_http_client = HttpClient(proxy_url=settings.vpn_proxy_url)
    registry = build_provider_registry(settings, http_client, debug, proxied_http_client)

    # Fetch GBP rate once — needed for --azuro-cap, --show-odds, and --auto-bet Matchbook stakes
    gbp_rate: float | None = None
    if args.auto_bet or ("azuro" in args.providers and (args.azuro_cap or args.show_odds)):
        gbp_rate = _fetch_usd_gbp_rate()
        if args.auto_bet and gbp_rate:
            print(f"  FX rate:    1 USD = {gbp_rate:.4f} GBP  (used for Matchbook stake conversion)")

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
    imminent_note = (
        f"  ({imminent_skipped} imminent/in-play skipped, cutoff={args.imminent_minutes}m)" if imminent_skipped
        else f"  (--no-skip-imminent to include; cutoff={args.imminent_minutes}m)" if not args.no_skip_imminent
        else ""
    )
    print(f"  Games:      {len(games)}  (≥2 provider coverage){imminent_note}")
    print(f"  Commission: {commission_str}")
    if args.auto_bet:
        mode = "DRY RUN" if args.bet_dry_run else "LIVE"
        print(f"  Auto-bet:   ENABLED [{mode}]  budget=${args.budget:.2f} USDC per arb")
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

        sure_bets, back_lay_arbs, games_payload, raw_records = _future.result()
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
            _game_ctx = (games_payload[0] if games_payload else None) if args.azuro_cap else None
            if sure_bets:
                print(f"  Sure bets ({len(sure_bets)}):")
                _arb._print_sure_bets(sure_bets, game=_game_ctx, gbp_rate=gbp_rate, budget=args.budget)
            if back_lay_arbs:
                print(f"  Back-lay arbs ({len(back_lay_arbs)}):")
                _arb._print_back_lay_arbs(back_lay_arbs, game=_game_ctx, gbp_rate=gbp_rate)
            print()

            # ── Auto-bet ──────────────────────────────────────────────────
            if args.auto_bet:
                if _exec._HALT:
                    print("  ⛔  AUTO-BET HALTED after a leg failure — restart process to resume.")
                    continue
                mode = "DRY RUN" if args.bet_dry_run else "LIVE"
                print(f"  AUTO-BET [{mode}]  budget: ${args.budget:.2f} USDC per arb")

                # Collect all executable candidates from both arb types, then
                # place only the single highest-profit one to avoid double-betting
                # the same game on overlapping legs.
                candidates: list[tuple[float, str, dict]] = []

                for arb in sure_bets:
                    providers_in_arb = {arb["team1_back_provider"], arb["team2_back_provider"]}
                    if arb.get("draw_back_provider"):
                        providers_in_arb.add(arb["draw_back_provider"])
                    bad = _exec._unsupported_providers(providers_in_arb)
                    if bad:
                        print(f"    ⚠  Sure-bet skipped — provider(s) {bad} not supported.")
                    else:
                        candidates.append((arb["profit_pct"], "sure_bet", arb))

                for arb in back_lay_arbs:
                    bad = _exec._unsupported_providers([arb["back_provider"], arb["lay_provider"]])
                    if bad:
                        print(f"    ⚠  Back-lay skipped — provider(s) {bad} not supported.")
                    else:
                        candidates.append((arb["profit_pct"], "back_lay", arb))

                if not candidates:
                    print("    (no executable arbs for this game)")
                else:
                    best_pct, best_type, best_arb = max(candidates, key=lambda c: c[0])
                    skipped = len(candidates) - 1
                    if skipped:
                        print(f"    {skipped} lower-profit arb(s) skipped — placing best only.")

                    if best_type == "sure_bet":
                        providers_in_arb = {best_arb["team1_back_provider"], best_arb["team2_back_provider"]}
                        if best_arb.get("draw_back_provider"):
                            providers_in_arb.add(best_arb["draw_back_provider"])
                        print(f"    Placing sure-bet ({best_pct:.2f}% net)  "
                              f"legs: {' | '.join(providers_in_arb)}")
                        bal_issues, bal_warns = _exec.check_balances_for_arb(
                            "sure_bet", best_arb, args.budget, settings, gbp_rate,
                        )
                        for w in bal_warns:
                            print(f"    ⚠  Balance: {w}")
                        if bal_issues:
                            for issue in bal_issues:
                                print(f"    ✗  Balance: {issue}")
                            print("    Arb skipped — insufficient funds to cover all legs.")
                        else:
                            results = _exec.place_sure_bet(
                                best_arb, game, args.budget, settings, gbp_rate, args.bet_dry_run,
                            )
                            _exec.print_bet_results(results)
                            if _exec.all_legs_placed(results):
                                print("  [WATCH] ALL LEGS PLACED")

                    else:
                        lay_note = ""
                        if best_arb["lay_provider"] == "sx_bet" and best_arb.get("market_type") == "three_way":
                            slot = best_arb.get("outcome_slot", "")
                            if not game.get(f"sx_bet_{slot}_market_hash"):
                                lay_note = "  ⚠ SX Bet lay = back-opposite only (draw not covered)"
                        print(f"    Placing back-lay ({best_pct:.2f}% net)  "
                              f"back: {best_arb['back_provider']} / lay: {best_arb['lay_provider']}{lay_note}")
                        bal_issues, bal_warns = _exec.check_balances_for_arb(
                            "back_lay", best_arb, args.budget, settings, gbp_rate,
                        )
                        for w in bal_warns:
                            print(f"    ⚠  Balance: {w}")
                        if bal_issues:
                            for issue in bal_issues:
                                print(f"    ✗  Balance: {issue}")
                            print("    Arb skipped — insufficient funds to cover all legs.")
                        else:
                            results = _exec.place_back_lay_arb(
                                best_arb, game, args.budget, settings, gbp_rate, args.bet_dry_run,
                            )
                            _exec.print_bet_results(results)
                            if _exec.all_legs_placed(results):
                                print("  [WATCH] ALL LEGS PLACED")

                print()

        if args.show_odds:
            if progress_on_screen:
                sys.stdout.write("\r" + " " * (tw - 1) + "\r\n")
                sys.stdout.flush()
                progress_on_screen = False
            _print_odds_table(i, len(games), game, games_payload, args.providers, raw_records, gbp_rate)

        if args.polymarket_debug and "polymarket" in args.providers:
            if progress_on_screen:
                sys.stdout.write("\r" + " " * (tw - 1) + "\r\n")
                sys.stdout.flush()
                progress_on_screen = False
            _print_polymarket_debug(game, raw_records)

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
