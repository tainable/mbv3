from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
import json
from pathlib import Path
import sys
from typing import Any

from matched_betting.config import load_settings
from matched_betting.debug import noop_debug, stderr_debug
from matched_betting.event_matching import infer_event_identity, match_records_to_canonical_events
from matched_betting.http import HttpClient
from matched_betting.market_matching import is_game_win_loss_record
from matched_betting.models import OddsRecord, utc_now_iso
from matched_betting.providers.base import ProviderNotReadyError
from matched_betting.providers.registry import build_provider_registry


DEFAULT_LEAGUES = ["nba", "mlb", "ucl", "epl"]
ALL_LEAGUES = ["nba", "mlb", "ucl", "epl"]
DEFAULT_PROVIDERS = ["matchbook", "smarkets", "polymarket", "sx_bet"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Retrieve and normalize odds for matched betting.")
    parser.add_argument("--leagues", nargs="+", default=DEFAULT_LEAGUES, choices=ALL_LEAGUES)
    league_shortcuts = parser.add_mutually_exclusive_group()
    league_shortcuts.add_argument("--nba", action="store_true", help="Shortcut for --leagues nba")
    league_shortcuts.add_argument("--mlb", action="store_true", help="Shortcut for --leagues mlb")
    league_shortcuts.add_argument("--ucl", action="store_true", help="Shortcut for --leagues ucl")
    league_shortcuts.add_argument("--epl", action="store_true", help="Shortcut for --leagues epl")
    parser.add_argument(
        "--providers",
        nargs="+",
        default=DEFAULT_PROVIDERS,
        choices=["matchbook", "smarkets", "polymarket", "sx_bet"],
    )
    parser.add_argument("--format", choices=["json"], default="json")
    parser.add_argument("--out", type=Path, help="Optional output path. Defaults to MATCHED_BETTING_OUTPUT_PATH.")
    parser.add_argument("--debug", action="store_true", help="Print progress messages to stderr.")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--update",
        action="store_true",
        help=(
            "Targeted fetch mode: use stored market/event IDs from the previous "
            "_aggregated_games.json to refresh only known markets. Faster than a "
            "full discovery run. Falls back to full discovery if no previous output exists."
        ),
    )
    mode_group.add_argument(
        "--reuse",
        action="store_true",
        help=(
            "Re-emit the previous _aggregated_games.json and _all_odds.json without "
            "making any network calls."
        ),
    )
    parser.add_argument(
        "--multi-provider",
        action="store_true",
        help=(
            "With --update: skip games covered by only one provider. "
            "Useful for ignoring far-future games that only Polymarket has listed."
        ),
    )
    return parser


def fetch_aggregated_games(
    leagues: list[str],
    providers: list[str],
    project_root: Path | None = None,
    per_provider_timeout: float = 90.0,
) -> list[dict[str, Any]]:
    """Fetch fresh odds from providers and return the aggregated games list.

    Each provider is given up to ``per_provider_timeout`` seconds before being
    skipped so a single slow provider cannot block the refresh indefinitely.
    """
    if project_root is None:
        project_root = Path(__file__).resolve().parents[2]
    settings = load_settings(project_root)
    http_client = HttpClient()
    provider_registry = build_provider_registry(settings, http_client, noop_debug)

    records: list[OddsRecord] = []

    executor = ThreadPoolExecutor(max_workers=len(providers))
    try:
        future_to_provider: dict[Future, str] = {
            executor.submit(provider_registry[name].fetch_odds, leagues): name
            for name in providers
            if name in provider_registry
        }
        try:
            for future in as_completed(future_to_provider, timeout=per_provider_timeout):
                try:
                    payload = future.result()
                    records.extend(payload.records)
                except Exception as exc:
                    print(f"WARNING: provider failed in fetch_aggregated_games: {exc}", file=sys.stderr)
        except FuturesTimeoutError:
            pass  # timed out — use whatever records arrived so far
    finally:
        executor.shutdown(wait=False)  # don't block on slow provider threads

    records = [r for r in records if is_game_win_loss_record(r)]
    canonical_assignment, canonical_events = match_records_to_canonical_events(records)
    return _build_aggregated_games_payload(records, canonical_assignment, canonical_events)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    debug = stderr_debug if args.debug else noop_debug
    leagues = _resolve_leagues(args)

    project_root = Path(__file__).resolve().parents[2]
    settings = load_settings(project_root)

    if args.reuse:
        return _run_reuse(args, settings, debug)
    if args.update:
        return _run_update(args, settings, debug, leagues, project_root)

    return _run_full_fetch(args, settings, debug, leagues, project_root)


def _run_full_fetch(
    args: argparse.Namespace,
    settings: Any,
    debug: Any,
    leagues: list[str],
    project_root: Path,
) -> int:
    """Run a full discovery fetch across all providers."""
    http_client = HttpClient()
    providers = build_provider_registry(settings, http_client, debug)
    debug(
        f"starting run providers={','.join(args.providers)} leagues={','.join(leagues)}"
    )

    records: list[OddsRecord] = []
    warnings: list[str] = []

    scrape_started_at = utc_now_iso()
    print(f"Scrape started:  {scrape_started_at}", flush=True)

    with ThreadPoolExecutor(max_workers=len(args.providers)) as executor:
        future_to_provider: dict[Future, str] = {}
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

    scrape_ended_at = utc_now_iso()
    print(f"Scrape finished: {scrape_ended_at}", flush=True)

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
    for record in records:
        if not is_game_win_loss_record(record):
            identity = infer_event_identity(record)
            selection = record.selection_name
            passed_moneyline = record.market_type in {"two_way", "three_way"}
            debug(
                f"  DROP provider={record.provider} event={record.event_name!r}"
                f" selection={record.selection_name!r} -> normalized={selection!r}"
                f" home={identity.home_team!r} away={identity.away_team!r}"
                f" moneyline_ok={passed_moneyline}"
            )
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
        "scrape_started_at": scrape_started_at,
        "scrape_ended_at": scrape_ended_at,
        "leagues": leagues,
        "providers_requested": args.providers,
        "record_count": len(records),
        "aggregated_game_count": len(aggregated_games_payload),
        "warnings": warnings,
        "all_odds_output_path": None,
        "aggregated_games_output_path": None,
    }

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
    market_index_output_path = output_path.with_name(f"{output_path.stem}_market_index.json")
    debug(f"writing market index to {market_index_output_path}")
    existing_index = _load_market_index(market_index_output_path)
    new_index = _build_market_index(aggregated_games_payload)
    merged_index = _merge_market_index_additive(existing_index, new_index)
    market_index_output_path.write_text(
        json.dumps(merged_index, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    rendered = json.dumps(output_payload, indent=2, sort_keys=False)
    print(rendered)
    debug(f"finished run record_count={len(records)} warning_count={len(warnings)}")
    return 0


def _run_reuse(
    args: argparse.Namespace,
    settings: Any,
    debug: Any,
) -> int:
    """Re-emit the previous output files without any network calls."""
    output_path = (args.out or settings.default_output_path).resolve()
    aggregated_games_path = output_path.with_name(f"{output_path.stem}_aggregated_games.json")
    all_odds_path = output_path.with_name(f"{output_path.stem}_all_odds.json")

    if not aggregated_games_path.exists():
        print(
            f"ERROR: no previous aggregated games file found at {aggregated_games_path}\n"
            "Run without --reuse first to generate the initial output.",
            flush=True,
        )
        return 1

    debug(f"--reuse: reading {aggregated_games_path}")
    agg_data: dict[str, Any] = json.loads(aggregated_games_path.read_text(encoding="utf-8"))

    all_odds_data: dict[str, Any] | None = None
    if all_odds_path.exists():
        debug(f"--reuse: reading {all_odds_path}")
        all_odds_data = json.loads(all_odds_path.read_text(encoding="utf-8"))

    reused_at = utc_now_iso()
    print(f"Reuse mode:      {reused_at}", flush=True)

    # Re-write files to update their filesystem mtime (useful for downstream watchers).
    debug(f"--reuse: writing {aggregated_games_path}")
    aggregated_games_path.write_text(
        json.dumps(agg_data, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    if all_odds_data is not None:
        debug(f"--reuse: writing {all_odds_path}")
        all_odds_path.write_text(
            json.dumps(all_odds_data, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )

    output_payload: dict[str, Any] = {
        "mode": "reuse",
        "reused_at": reused_at,
        "leagues": agg_data.get("leagues"),
        "aggregated_game_count": agg_data.get("aggregated_game_count", 0),
        "aggregated_games_output_path": str(aggregated_games_path),
        "all_odds_output_path": str(all_odds_path) if all_odds_data is not None else None,
    }
    print(json.dumps(output_payload, indent=2, sort_keys=False))
    return 0


def _run_update(
    args: argparse.Namespace,
    settings: Any,
    debug: Any,
    leagues: list[str],
    project_root: Path,
) -> int:
    """Targeted fetch mode: use stored market IDs from the previous run."""
    output_path = (args.out or settings.default_output_path).resolve()
    aggregated_games_path = output_path.with_name(f"{output_path.stem}_aggregated_games.json")

    if not aggregated_games_path.exists():
        print(
            f"WARNING: no previous aggregated games file found at {aggregated_games_path}. "
            "Falling back to full discovery.",
            flush=True,
        )
        return _run_full_fetch(args, settings, debug, leagues, project_root)

    debug(f"--update: reading {aggregated_games_path}")
    prev_data: dict[str, Any] = json.loads(aggregated_games_path.read_text(encoding="utf-8"))
    all_game_contexts: list[dict[str, Any]] = prev_data.get("aggregated_games", [])
    game_contexts = [g for g in all_game_contexts if g.get("league") in leagues]

    if args.multi_provider:
        before = len(game_contexts)
        game_contexts = [g for g in game_contexts if _provider_count(g) > 1]
        debug(f"--multi-provider: kept {len(game_contexts)} of {before} games (≥2 providers)")

    if not game_contexts:
        print(
            f"WARNING: no games for leagues {leagues} in previous output. "
            "Falling back to full discovery.",
            flush=True,
        )
        return _run_full_fetch(args, settings, debug, leagues, project_root)

    debug(f"--update: {len(game_contexts)} game contexts for leagues {leagues}")

    http_client = HttpClient()
    providers = build_provider_registry(settings, http_client, debug)

    records: list[OddsRecord] = []
    warnings: list[str] = []
    scrape_started_at = utc_now_iso()
    print(f"Update started:  {scrape_started_at}", flush=True)

    with ThreadPoolExecutor(max_workers=len(args.providers)) as executor:
        future_to_provider: dict[Future, str] = {
            executor.submit(providers[name].fetch_odds_by_ids, game_contexts, leagues): name
            for name in args.providers
            if name in providers
        }

        provider_results: dict[str, tuple[list[OddsRecord], list[str]]] = {}
        provider_failures: dict[str, str] = {}

        for future in as_completed(future_to_provider):
            provider_name = future_to_provider[future]
            try:
                payload = future.result()
                provider_results[provider_name] = (payload.records, payload.warnings)
                debug(
                    f"provider {provider_name}: completed with "
                    f"{len(payload.records)} records and {len(payload.warnings)} warnings"
                )
            except ProviderNotReadyError as exc:
                provider_failures[provider_name] = str(exc)
                debug(f"provider {provider_name}: not ready: {exc}")
            except Exception as exc:
                provider_failures[provider_name] = f"request failed: {exc}"
                debug(f"provider {provider_name}: failed: {exc}")

    for provider_name in args.providers:
        if provider_name in provider_failures:
            warnings.append(f"{provider_name}: {provider_failures[provider_name]}")
            continue
        if provider_name not in provider_results:
            warnings.append(f"{provider_name}: unknown execution state")
            continue
        provider_records, provider_warnings = provider_results[provider_name]
        records.extend(provider_records)
        warnings.extend(provider_warnings)

    scrape_ended_at = utc_now_iso()
    print(f"Update finished: {scrape_ended_at}", flush=True)

    requested_ids = _collect_requested_ids(game_contexts, leagues)
    found_ids = _collect_found_ids(records)
    records = [r for r in records if is_game_win_loss_record(r)]
    debug(f"filtered to game win/loss records: {len(records)} kept")
    canonical_assignment, canonical_events = match_records_to_canonical_events(
        records, debug_logger=debug
    )
    normalized_records = [
        {**record.to_dict(), "canonical_event_id": canonical_assignment[index]}
        for index, record in enumerate(records)
    ]
    aggregated_games_payload = _build_aggregated_games_payload(
        records, canonical_assignment, canonical_events
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    all_odds_output_path = output_path.with_name(f"{output_path.stem}_all_odds.json")
    market_index_output_path = output_path.with_name(f"{output_path.stem}_market_index.json")

    all_odds_output_path.write_text(
        json.dumps(
            {
                "mode": "update",
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
    aggregated_games_path.write_text(
        json.dumps(
            {
                "mode": "update",
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
    existing_index = _load_market_index(market_index_output_path)
    new_index = _build_market_index(aggregated_games_payload)
    pruned_index = _prune_market_index(existing_index, requested_ids, found_ids)
    merged_index = _merge_market_index_additive(pruned_index, new_index)
    market_index_output_path.write_text(
        json.dumps(merged_index, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )

    output_payload: dict[str, Any] = {
        "mode": "update",
        "scrape_started_at": scrape_started_at,
        "scrape_ended_at": scrape_ended_at,
        "leagues": leagues,
        "providers_requested": args.providers,
        "record_count": len(records),
        "aggregated_game_count": len(aggregated_games_payload),
        "warnings": warnings,
        "all_odds_output_path": str(all_odds_output_path),
        "aggregated_games_output_path": str(aggregated_games_path),
    }
    print(json.dumps(output_payload, indent=2, sort_keys=False))
    debug(f"--update finished record_count={len(records)} warning_count={len(warnings)}")
    return 0


def _load_market_index(path: Path) -> dict[str, Any]:
    """Load the existing market index from disk, or return an empty structure."""
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data.get("market_index"), dict):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            print(f"WARNING: could not read market index at {path}: {exc}", file=sys.stderr)
    return {"market_index": {}}


def _merge_market_index_additive(
    existing: dict[str, Any],
    new_index: dict[str, Any],
) -> dict[str, Any]:
    """Merge new IDs into the existing index without removing any existing IDs."""
    existing_mi = existing.get("market_index", {})
    new_mi = new_index.get("market_index", {})
    merged: dict[str, dict[str, list[str]]] = {}

    known_order = ["nba", "mlb", "ucl", "epl"]
    all_leagues = set(existing_mi) | set(new_mi)
    ordered_leagues = [lg for lg in known_order if lg in all_leagues]
    ordered_leagues += sorted(lg for lg in all_leagues if lg not in known_order)

    for league in ordered_leagues:
        existing_league = existing_mi.get(league, {})
        new_league = new_mi.get(league, {})
        all_providers = set(existing_league) | set(new_league)
        merged[league] = {}
        for provider in all_providers:
            existing_ids = existing_league.get(provider, [])
            new_ids = new_league.get(provider, [])
            merged_ids = list(existing_ids)
            for id_ in new_ids:
                if id_ not in merged_ids:
                    merged_ids.append(id_)
            merged[league][provider] = merged_ids

    return {"market_index": merged}


def _collect_requested_ids(
    game_contexts: list[dict[str, Any]],
    leagues: list[str],
) -> dict[tuple[str, str], set[str]]:
    """Return {(league, provider): set of IDs} that were passed to fetch_odds_by_ids."""
    result: dict[tuple[str, str], set[str]] = {}
    provider_fields = [
        ("polymarket", "polymarket_market_id"),
        ("smarkets",   "smarkets_market_id"),
        ("matchbook",  "matchbook_event_id"),
        ("sx_bet",     "sx_bet_market_hash"),
    ]
    for game in game_contexts:
        league = game.get("league")
        if league not in leagues:
            continue
        for provider, field in provider_fields:
            id_ = game.get(field)
            if id_:
                result.setdefault((league, provider), set()).add(str(id_))
        # Soccer per-slot polymarket IDs
        for slot in ("team1", "draw", "team2"):
            slot_id = game.get(f"polymarket_{slot}_market_id")
            if slot_id:
                result.setdefault((league, "polymarket"), set()).add(str(slot_id))
    return result


def _collect_found_ids(records: list[OddsRecord]) -> dict[tuple[str, str], set[str]]:
    """Return {(league, provider): set of IDs} that had at least one odds record."""
    result: dict[tuple[str, str], set[str]] = {}
    for record in records:
        id_ = record.source_event_id if record.provider == "matchbook" else record.source_market_id
        if id_:
            result.setdefault((record.league, record.provider), set()).add(str(id_))
    return result


def _prune_market_index(
    existing: dict[str, Any],
    requested_ids: dict[tuple[str, str], set[str]],
    found_ids: dict[tuple[str, str], set[str]],
) -> dict[str, Any]:
    """Remove IDs from the index that were requested but returned no odds."""
    existing_mi = existing.get("market_index", {})
    pruned: dict[str, dict[str, list[str]]] = {}

    for league, providers in existing_mi.items():
        pruned_league: dict[str, list[str]] = {}
        for provider, ids in providers.items():
            key = (league, provider)
            requested = requested_ids.get(key, set())
            found = found_ids.get(key, set())
            kept = [id_ for id_ in ids if id_ not in requested or id_ in found]
            if kept:
                pruned_league[provider] = kept
        if pruned_league:
            pruned[league] = pruned_league

    return {"market_index": pruned}


def _build_market_index(aggregated_games: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a league → provider → [market IDs] index from the aggregated games payload."""
    index: dict[str, dict[str, list[str]]] = {}
    provider_fields = [
        ("polymarket", "polymarket_market_id"),
        ("matchbook",  "matchbook_event_id"),
        ("smarkets",   "smarkets_market_id"),
        ("sx_bet",     "sx_bet_market_hash"),
    ]
    for game in aggregated_games:
        league = (game.get("league") or "other").lower()
        if league not in index:
            index[league] = {}
        for provider, field in provider_fields:
            market_id = game.get(field)
            if not market_id:
                continue
            index[league].setdefault(provider, [])
            if market_id not in index[league][provider]:
                index[league][provider].append(market_id)

    # Sort leagues predictably: known leagues first, then alphabetical
    known_order = ["nba", "mlb", "ucl", "epl"]
    sorted_index = {}
    for league in known_order:
        if league in index:
            sorted_index[league] = index[league]
    for league in sorted(index):
        if league not in sorted_index:
            sorted_index[league] = index[league]

    return {"market_index": sorted_index}


def _resolve_leagues(args: argparse.Namespace) -> list[str]:
    if args.nba:
        return ["nba"]
    if args.mlb:
        return ["mlb"]
    if args.ucl:
        return ["ucl"]
    if args.epl:
        return ["epl"]
    return list(args.leagues)


def _provider_count(game: dict[str, Any]) -> int:
    """Count how many providers have a stored market ID for this game."""
    fields = ["polymarket_market_id", "smarkets_market_id", "matchbook_event_id", "sx_bet_market_hash"]
    return sum(1 for f in fields if game.get(f))


def _extract_available(record: OddsRecord) -> float | None:
    """Return the available amount (in native currency) for a single odds record."""
    m = record.metadata
    if record.provider == "matchbook":
        return m.get("available_amount")
    if record.provider == "smarkets":
        raw = m.get("raw_quantity")
        return round(raw / 10000, 2) if raw is not None else None
    if record.provider == "polymarket":
        liq = m.get("liquidity_usd")
        return round(liq / 2, 2) if liq is not None else None
    if record.provider == "sx_bet":
        return m.get("available_usd")


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
        team1 = _pretty_team(event_group.home_team)
        team2 = _pretty_team(event_group.away_team)
        # Derive market_type: prefer three_way if any record has it
        market_types = {records[i].market_type for i in indices}
        if "three_way" in market_types:
            market_type = "three_way"
        elif "two_way" in market_types:
            market_type = "two_way"
        else:
            market_type = next(iter(market_types), None)

        entry: dict[str, Any] = {
            "team1": team1,
            "team2": team2,
            "date_time": event_group.event_start,
            "league": event_group.league,
            "sport": event_group.sport,
            "market_type": market_type,
            "polymarket_market_id": None,
            "smarkets_market_id": None,
            "matchbook_event_id": None,
            "sx_bet_market_hash": None,
            "polymarket_team1_back_odds": None,
            "polymarket_team1_lay_odds": None,
            "polymarket_draw_back_odds": None,
            "polymarket_draw_lay_odds": None,
            "polymarket_team2_back_odds": None,
            "polymarket_team2_lay_odds": None,
            "matchbook_team1_back_odds": None,
            "matchbook_team1_lay_odds": None,
            "matchbook_draw_back_odds": None,
            "matchbook_draw_lay_odds": None,
            "matchbook_team2_back_odds": None,
            "matchbook_team2_lay_odds": None,
            "smarkets_team1_back_odds": None,
            "smarkets_team1_lay_odds": None,
            "smarkets_draw_back_odds": None,
            "smarkets_draw_lay_odds": None,
            "smarkets_team2_back_odds": None,
            "smarkets_team2_lay_odds": None,
            "sx_bet_team1_back_odds": None,
            "sx_bet_team1_lay_odds": None,
            "sx_bet_draw_back_odds": None,
            "sx_bet_draw_lay_odds": None,
            "sx_bet_team2_back_odds": None,
            "sx_bet_team2_lay_odds": None,
        }

        best_by_provider_team: dict[tuple[str, str, str], tuple[int, OddsRecord]] = {}
        for index in indices:
            record = records[index]
            normalized_selection = record.selection_name
            team_slot = None
            if event_group.home_team and normalized_selection == event_group.home_team:
                team_slot = "team1"
            elif event_group.away_team and normalized_selection == event_group.away_team:
                team_slot = "team2"
            elif normalized_selection in ("draw", "tie"):
                team_slot = "draw"
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
            except TypeError:
                best_by_provider_team[key] = (index, record)

        for provider_name in ("polymarket", "matchbook", "smarkets", "sx_bet"):
            for team_slot in ("team1", "draw", "team2"):
                for side in ("back", "lay"):
                    chosen = best_by_provider_team.get((provider_name, team_slot, side))
                    if chosen is None:
                        continue
                    _, record = chosen
                    odds_key = f"{provider_name}_{team_slot}_{side}_odds"
                    avail_key = f"{provider_name}_{team_slot}_{side}_avail"
                    if odds_key in entry:
                        entry[odds_key] = record.decimal_odds
                    entry[avail_key] = _extract_available(record)

        # Store market/event IDs for targeted refresh later.
        # Polymarket UCL: each outcome is a separate Yes/No binary market, so store
        # per-slot IDs. Other providers use one ID per event/market.
        for team_slot in ("team1", "draw", "team2"):
            for side in ("back", "lay"):
                key = ("polymarket", team_slot, side)
                if key in best_by_provider_team:
                    _, record = best_by_provider_team[key]
                    entry[f"polymarket_{team_slot}_market_id"] = record.source_market_id
                    # Also set the legacy single-ID field to the first one found
                    if entry["polymarket_market_id"] is None:
                        entry["polymarket_market_id"] = record.source_market_id
                    break

        for provider_name, id_field, id_attr in (
            ("smarkets",  "smarkets_market_id",  "source_market_id"),
            ("matchbook", "matchbook_event_id",   "source_event_id"),
            ("sx_bet",    "sx_bet_market_hash",   "source_market_id"),
        ):
            for key in best_by_provider_team:
                if key[0] == provider_name:
                    _, record = best_by_provider_team[key]
                    entry[id_field] = getattr(record, id_attr)
                    break

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


