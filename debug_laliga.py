"""
debug_laliga.py
---------------
Fetch La Liga odds from all providers and show three views:

  1. Raw records  — every OddsRecord returned, with the normalized
                    team name used for canonical matching.
  2. Merge report — which records collapsed into each canonical event
                    and which providers contributed.
  3. Games table  — the final aggregated payload: one row per game,
                    showing odds coverage across providers.

Usage:
    python debug_laliga.py
    python debug_laliga.py --providers matchbook sx_bet
    python debug_laliga.py --ids outputs/active_game_ids.json   # targeted re-fetch
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys

_PROJECT_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from matched_betting.config import load_settings
from matched_betting.debug import stderr_debug
from matched_betting.event_matching import infer_event_identity, match_records_to_canonical_events
from matched_betting.http import HttpClient
from matched_betting.market_matching import is_game_win_loss_record
from matched_betting.models import OddsRecord
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.base import ProviderNotReadyError
from matched_betting.providers.registry import build_provider_registry
from matched_betting.aggregation import build_aggregated_games_payload

import json

LEAGUE = "laliga"
DEFAULT_PROVIDERS = ["matchbook", "polymarket", "sx_bet", "azuro"]


def _hr(char: str = "─", width: int = 80) -> str:
    return char * width


def _fetch_full(registry: dict, providers: list[str]) -> list[OddsRecord]:
    """Full fetch via fetch_odds (no stored IDs needed)."""
    records: list[OddsRecord] = []
    with ThreadPoolExecutor(max_workers=len(providers)) as ex:
        futures = {
            ex.submit(registry[p].fetch_odds, [LEAGUE]): p
            for p in providers if p in registry
        }
        for future in as_completed(futures):
            p = futures[future]
            try:
                payload = future.result()
                records.extend(payload.records)
                print(f"  {p}: {len(payload.records)} records")
                for w in payload.warnings:
                    print(f"    WARNING: {w}")
            except ProviderNotReadyError as exc:
                print(f"  {p}: not ready — {exc}")
            except Exception as exc:
                print(f"  {p}: failed — {exc}")
    return records


def _fetch_targeted(registry: dict, providers: list[str], ids_path: Path) -> list[OddsRecord]:
    """Targeted fetch using stored game IDs (mirrors scan.py behaviour)."""
    import json as _json
    data = _json.loads(ids_path.read_text(encoding="utf-8"))
    games = [g for g in data.get("games", []) if g.get("league") == LEAGUE]
    print(f"  Loaded {len(games)} {LEAGUE} games from {ids_path.name}")

    records: list[OddsRecord] = []
    for game in games:
        with ThreadPoolExecutor(max_workers=len(providers)) as ex:
            futures = {
                ex.submit(registry[p].fetch_odds_by_ids, [game], [LEAGUE]): p
                for p in providers if p in registry
            }
            for future in as_completed(futures):
                p = futures[future]
                try:
                    payload = future.result()
                    records.extend(payload.records)
                except ProviderNotReadyError:
                    pass
                except Exception as exc:
                    print(f"  {p}: failed for {game.get('team1')} vs {game.get('team2')} — {exc}")
    return records


def _print_raw_records(records: list[OddsRecord]) -> None:
    print(_hr("═"))
    print("  RAW RECORDS")
    print(_hr("═"))
    by_provider: dict[str, list[OddsRecord]] = {}
    for r in records:
        by_provider.setdefault(r.provider, []).append(r)

    for provider, recs in sorted(by_provider.items()):
        print(f"\n  [{provider.upper()}]  {len(recs)} records")
        print(f"  {'Event name':<40}  {'Selection':<28}  {'Side':<4}  {'Odds':>6}  {'Norm →'}")
        print(f"  {'-'*40}  {'-'*28}  {'-'*4}  {'-'*6}  {'-'*28}")
        for r in recs:
            norm = normalize_team_name(r.selection_name, LEAGUE)
            print(
                f"  {r.event_name[:40]:<40}  {r.selection_name[:28]:<28}  "
                f"{r.selection_side:<4}  {r.decimal_odds:>6.3f}  → {norm!r}"
            )
    print()


def _print_merge_report(
    records: list[OddsRecord],
    assignment: dict[int, str],
) -> None:
    print(_hr("═"))
    print("  CANONICAL EVENT MERGE REPORT")
    print(_hr("═"))

    groups: dict[str, dict] = {}
    for idx, canon_id in assignment.items():
        r = records[idx]
        g = groups.setdefault(canon_id, {"providers": set(), "records": [], "id": canon_id})
        g["providers"].add(r.provider)
        g["records"].append(r)

    # Sort by event start then canonical id
    for canon_id, g in sorted(groups.items(), key=lambda x: x[0]):
        providers = sorted(g["providers"])
        provider_str = " + ".join(p.upper()[:2] for p in providers)
        first = g["records"][0]
        identity = infer_event_identity(first)
        home = identity.home_team or "?"
        away = identity.away_team or "?"
        print(f"\n  [{provider_str}]  {home} vs {away}")
        print(f"    canonical_id: {canon_id}")
        print(f"    providers:    {providers}  ({len(g['records'])} records)")

        # Check for normalization problems: selection names that don't map to home/away
        unmatched = set()
        for r in g["records"]:
            norm = normalize_team_name(r.selection_name, LEAGUE)
            if norm not in {home, away, "draw"}:
                unmatched.add(f"{r.provider}:{r.selection_name!r} → {norm!r}")
        if unmatched:
            for u in sorted(unmatched):
                print(f"    ⚠  unmatched selection: {u}")
    print()


def _print_games_table(games_payload: list[dict]) -> None:
    print(_hr("═"))
    print("  GAMES TABLE  (final aggregated payload)")
    print(_hr("═"))
    print(f"\n  {'Home':<28}  {'Away':<28}  {'MB':^5}  {'PM':^5}  {'SX':^5}  {'AZ':^5}")
    print(f"  {'-'*28}  {'-'*28}  {'-'*5}  {'-'*5}  {'-'*5}  {'-'*5}")

    multi_provider = 0
    for g in games_payload:
        t1 = (g.get("team1") or "?")[:28]
        t2 = (g.get("team2") or "?")[:28]

        def _tick(prefix: str) -> str:
            return "✓" if any(
                g.get(f"{prefix}_{slot}_{side}_odds") is not None
                for slot in ("team1", "draw", "team2")
                for side in ("back", "lay")
            ) else "—"

        mb = _tick("matchbook")
        pm = _tick("polymarket")
        sx = _tick("sx_bet")
        az = _tick("azuro")
        n_providers = sum(1 for x in (mb, pm, sx, az) if x == "✓")
        flag = "  ◄ MULTI" if n_providers >= 2 else ""
        print(f"  {t1:<28}  {t2:<28}  {mb:^5}  {pm:^5}  {sx:^5}  {az:^5}{flag}")
        if n_providers >= 2:
            multi_provider += 1

    print(f"\n  Total games: {len(games_payload)}   Multi-provider: {multi_provider}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Debug La Liga odds ingestion.")
    parser.add_argument(
        "--providers", nargs="+", default=DEFAULT_PROVIDERS,
        choices=["matchbook", "polymarket", "sx_bet", "azuro", "smarkets"],
        metavar="PROVIDER",
    )
    parser.add_argument(
        "--ids", type=Path, default=None,
        help="Use targeted fetch from a stored IDs JSON instead of a full provider scan.",
    )
    parser.add_argument("--no-raw", action="store_true", help="Skip the raw records section.")
    args = parser.parse_args()

    settings = load_settings(_PROJECT_ROOT)
    http_client = HttpClient()
    proxied = None
    if settings.vpn_proxy_url:
        proxied = HttpClient(proxy_url=settings.vpn_proxy_url)
    registry = build_provider_registry(settings, http_client, stderr_debug, proxied)

    print(_hr("═"))
    if args.ids:
        print(f"  debug_laliga — TARGETED fetch from {args.ids}")
    else:
        print("  debug_laliga — FULL provider scan")
    print(f"  Providers: {args.providers}")
    print(_hr("═"))
    print()

    if args.ids:
        ids_path = args.ids if args.ids.is_absolute() else _PROJECT_ROOT / args.ids
        raw_records = _fetch_targeted(registry, args.providers, ids_path)
    else:
        raw_records = _fetch_full(registry, args.providers)

    print(f"\n  Total raw records: {len(raw_records)}")

    if not args.no_raw:
        print()
        _print_raw_records(raw_records)

    # Filter to win/loss records and run canonical matching
    records = [r for r in raw_records if is_game_win_loss_record(r)]
    print(f"  Win/loss records after filter: {len(records)}")

    if not records:
        print("  No records to process.")
        return

    assignment, canonical_events = match_records_to_canonical_events(records)
    _print_merge_report(records, assignment)

    games_payload = build_aggregated_games_payload(records, assignment, canonical_events)
    _print_games_table(games_payload)


if __name__ == "__main__":
    main()
