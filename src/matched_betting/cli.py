from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from pathlib import Path
from typing import Any

from matched_betting.config import load_settings
from matched_betting.debug import noop_debug, stderr_debug
from matched_betting.event_matching import match_records_to_canonical_events
from matched_betting.http import HttpClient
from matched_betting.market_matching import is_game_win_loss_record
from matched_betting.models import OddsRecord
from matched_betting.providers.base import ProviderNotReadyError
from matched_betting.providers.registry import build_provider_registry


DEFAULT_LEAGUES = ["nba", "mlb"]
DEFAULT_PROVIDERS = ["matchbook", "smarkets", "polymarket"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Retrieve and normalize odds for matched betting.")
    parser.add_argument("--leagues", nargs="+", default=DEFAULT_LEAGUES, choices=DEFAULT_LEAGUES)
    league_shortcuts = parser.add_mutually_exclusive_group()
    league_shortcuts.add_argument("--nba", action="store_true", help="Shortcut for --leagues nba")
    league_shortcuts.add_argument("--mlb", action="store_true", help="Shortcut for --leagues mlb")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        choices=DEFAULT_PROVIDERS,
    )
    parser.add_argument("--format", choices=["json"], default="json")
    parser.add_argument("--out", type=Path, help="Optional output path. Defaults to MATCHED_BETTING_OUTPUT_PATH.")
    parser.add_argument("--debug", action="store_true", help="Print progress messages to stderr.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    debug = stderr_debug if args.debug else noop_debug
    leagues = _resolve_leagues(args)

    project_root = Path(__file__).resolve().parents[2]
    settings = load_settings(project_root)
    http_client = HttpClient()
    providers = build_provider_registry(settings, http_client, debug)
    debug(
        f"starting run providers={','.join(args.providers)} leagues={','.join(leagues)}"
    )

    records: list[OddsRecord] = []
    warnings: list[str] = []

    with ThreadPoolExecutor(max_workers=len(args.providers)) as executor:
        future_to_provider = {}
        for provider_name in args.providers:
            provider = providers[provider_name]
            debug(f"provider {provider_name}: start")
            future_to_provider[executor.submit(provider.fetch_odds, leagues)] = provider_name

        provider_results: dict[str, tuple[list[OddsRecord], list[str]]] = {}
        provider_failures: dict[str, str] = {}

        for future in as_completed(future_to_provider):
            provider_name = future_to_provider[future]
            try:
                payload = future.result()
            except ProviderNotReadyError as exc:
                provider_failures[provider_name] = str(exc)
                debug(f"provider {provider_name}: not ready: {exc}")
                continue
            except Exception as exc:
                provider_failures[provider_name] = f"request failed: {exc}"
                debug(f"provider {provider_name}: failed: {exc}")
                continue
            provider_results[provider_name] = (payload.records, payload.warnings)
            debug(
                f"provider {provider_name}: completed with {len(payload.records)} records and {len(payload.warnings)} warnings"
            )

    for provider_name in args.providers:
        if provider_name in provider_failures:
            warnings.append(f"{provider_name}: {provider_failures[provider_name]}")
            continue
        if provider_name not in provider_results:
            warnings.append(f"{provider_name}: request failed: unknown provider execution state")
            continue
        provider_records, provider_warnings = provider_results[provider_name]
        records.extend(provider_records)
        warnings.extend(provider_warnings)

    records.sort(
        key=lambda item: (
            item.league,
            item.event_start or "",
            item.event_name.lower(),
            item.market_name.lower(),
            item.selection_name.lower(),
            item.provider,
        )
    )
    original_record_count = len(records)
    records = [record for record in records if is_game_win_loss_record(record)]
    debug(
        f"filtered to game win/loss back bets: kept {len(records)} of {original_record_count} records"
    )
    debug("matching canonical events")
    canonical_assignment, canonical_events = match_records_to_canonical_events(
        records,
        debug_logger=debug,
    )

    normalized_records = [
        {
            **record.to_dict(),
            "canonical_event_id": canonical_assignment[index],
        }
        for index, record in enumerate(records)
    ]
    aggregated_games_payload = _build_aggregated_games_payload(
        records,
        canonical_assignment,
        canonical_events,
    )

    output_payload: dict[str, Any] = {
        "leagues": leagues,
        "providers_requested": args.providers,
        "record_count": len(records),
        "aggregated_game_count": len(aggregated_games_payload),
        "warnings": warnings,
        "all_odds_output_path": None,
        "aggregated_games_output_path": None,
    }

    rendered = json.dumps(output_payload, indent=2, sort_keys=False)

    output_path = (args.out or settings.default_output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_odds_output_path = output_path.with_name(f"{output_path.stem}_all_odds.json")
    aggregated_games_output_path = output_path.with_name(f"{output_path.stem}_aggregated_games.json")
    output_payload["all_odds_output_path"] = str(all_odds_output_path)
    output_payload["aggregated_games_output_path"] = str(aggregated_games_output_path)

    debug(f"writing all odds to {all_odds_output_path}")
    all_odds_output_path.write_text(
        json.dumps(
            {
                "leagues": leagues,
                "providers_requested": args.providers,
                "record_count": len(records),
                "warnings": warnings,
                "all_odds": normalized_records,
            },
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    debug(f"writing aggregated games to {aggregated_games_output_path}")
    aggregated_games_output_path.write_text(
        json.dumps(
            {
                "leagues": leagues,
                "providers_requested": args.providers,
                "aggregated_game_count": len(aggregated_games_payload),
                "aggregated_games": aggregated_games_payload,
            },
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    rendered = json.dumps(output_payload, indent=2, sort_keys=False)
    print(rendered)
    debug(f"finished run record_count={len(records)} warning_count={len(warnings)}")
    return 0


def _resolve_leagues(args: argparse.Namespace) -> list[str]:
    if args.nba:
        return ["nba"]
    if args.mlb:
        return ["mlb"]
    return list(args.leagues)


def _build_aggregated_games_payload(
    records: list[OddsRecord],
    canonical_event_assignment: dict[int, str],
    canonical_events: list[Any],
) -> list[dict[str, Any]]:
    events_by_id = {event.canonical_event_id: event for event in canonical_events}
    grouped_indices: dict[str, list[int]] = {}
    for index, canonical_event_id in canonical_event_assignment.items():
        grouped_indices.setdefault(canonical_event_id, []).append(index)

    payload: list[dict[str, Any]] = []
    for canonical_event_id, indices in grouped_indices.items():
        event_group = events_by_id[canonical_event_id]
        team1 = _pretty_team(event_group.away_team)
        team2 = _pretty_team(event_group.home_team)
        entry: dict[str, Any] = {
            "team1": team1,
            "team2": team2,
            "date_time": event_group.event_start,
            "league": event_group.league,
            "sport": event_group.sport,
            "polymarket_team1_back_odds": None,
            "polymarket_team2_back_odds": None,
            "matchbook_team1_back_odds": None,
            "matchbook_team2_back_odds": None,
            "matchbook_team1_lay_odds": None,
            "matchbook_team2_lay_odds": None,
            "smarkets_team1_back_odds": None,
            "smarkets_team2_back_odds": None,
            "smarkets_team1_lay_odds": None,
            "smarkets_team2_lay_odds": None,
        }

        best_by_provider_team: dict[tuple[str, str, str], tuple[int, OddsRecord]] = {}
        for index in indices:
            record = records[index]
            normalized_selection = _normalize_team_for_output(record.selection_name, record.league)
            team_slot = None
            if event_group.away_team and normalized_selection == event_group.away_team:
                team_slot = "team1"
            elif event_group.home_team and normalized_selection == event_group.home_team:
                team_slot = "team2"
            if team_slot is None:
                continue
            side = record.selection_side.lower()
            key = (record.provider, team_slot, side)
            current = best_by_provider_team.get(key)
            try:
                if current is None:
                    best_by_provider_team[key] = (index, record)
                elif side == "lay":
                    if record.decimal_odds < current[1].decimal_odds:
                        best_by_provider_team[key] = (index, record)
                else:
                    if record.decimal_odds > current[1].decimal_odds:
                        best_by_provider_team[key] = (index, record)
            except:
                best_by_provider_team[key] = (index, record)

        for provider_name in ("polymarket", "matchbook", "smarkets"):
            for team_slot in ("team1", "team2"):
                for side in ("back", "lay"):
                    chosen = best_by_provider_team.get((provider_name, team_slot, side))
                    if chosen is None:
                        continue
                    _, record = chosen
                    entry[f"{provider_name}_{team_slot}_{side}_odds"] = record.decimal_odds

        payload.append(entry)

    payload.sort(key=lambda item: (item["date_time"] or "", item["team1"] or "", item["team2"] or ""))
    return payload


def _pretty_team(team_name: str | None) -> str | None:
    if team_name is None:
        return None
    parts = team_name.split()
    titled: list[str] = []
    for part in parts:
        if part == "la":
            titled.append("LA")
        elif part == "okc":
            titled.append("OKC")
        elif any(char.isdigit() for char in part):
            titled.append(part)
        else:
            titled.append(part.capitalize())
    return " ".join(titled)


def _normalize_team_for_output(team_name: str, league: str) -> str:
    from matched_betting.event_matching import normalize_team_name

    return normalize_team_name(team_name, league)
