"""
aggregation.py
--------------
Builds the per-game aggregated odds payload from normalised OddsRecord objects.

Shared by ids.py, scan.py, and cli.py so the aggregation logic lives in one
place rather than being embedded in the CLI entry point.
"""
from __future__ import annotations

from typing import Any

from matched_betting.models import OddsRecord
from matched_betting.normalization import normalize_team_name


# Fields stored in the market/IDs index for targeted re-fetching.
INDEX_ID_FIELDS = (
    "polymarket_market_id",
    "matchbook_event_id",
    "smarkets_market_id",
    "sx_bet_market_hash",
    "azuro_condition_id",
)

INDEX_POLYMARKET_SLOT_FIELDS = (
    "polymarket_team1_market_id",
    "polymarket_draw_market_id",
    "polymarket_team2_market_id",
    "polymarket_team1_clob_token_id",
    "polymarket_draw_clob_token_id",
    "polymarket_team2_clob_token_id",
)

INDEX_EXTRA_FIELDS = (
    "sx_bet_outcome_one_team",
    "sx_bet_team1_market_hash",
    "sx_bet_draw_market_hash",
    "sx_bet_team2_market_hash",
)


def _extract_available(record: OddsRecord) -> float | None:
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
    if record.provider == "azuro":
        return m.get("max_stake_usdc")
    return None


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


def build_aggregated_games_payload(
    records: list[OddsRecord],
    canonical_event_assignment: dict[int, str],
    canonical_events: list[Any],
) -> list[dict[str, Any]]:
    """Group records by canonical event and return one dict per game with best
    odds and market IDs for every provider/slot/side combination."""
    events_by_id = {event.canonical_event_id: event for event in canonical_events}
    grouped_indices: dict[str, list[int]] = {}
    for index, canonical_event_id in canonical_event_assignment.items():
        grouped_indices.setdefault(canonical_event_id, []).append(index)

    payload: list[dict[str, Any]] = []
    for canonical_event_id, indices in grouped_indices.items():
        event_group = events_by_id[canonical_event_id]
        team1 = _pretty_team(event_group.home_team)
        team2 = _pretty_team(event_group.away_team)
        market_types = {records[i].market_type for i in indices}
        if "three_way" in market_types:
            market_type = "three_way"
        elif "two_way" in market_types:
            market_type = "two_way"
        else:
            market_type = next(iter(market_types), None)

        spread: float | None = None
        spread_favourite: str | None = None
        for i in indices:
            meta = records[i].metadata or {}
            if spread is None:
                s = meta.get("spread")
                if s is not None:
                    try:
                        spread = float(s)
                    except (TypeError, ValueError):
                        pass
            if spread_favourite is None:
                spread_favourite = meta.get("spread_favourite") or None

        # Normalise spread to home-team perspective.
        # Canonical convention: negative = home team must cover, positive = home is underdog.
        # Use abs() so the result is sign-correct regardless of each provider's raw convention.
        if spread is not None and spread_favourite is not None:
            favourite_norm = normalize_team_name(str(spread_favourite), event_group.league)
            if event_group.home_team and favourite_norm == event_group.home_team:
                spread = -abs(spread)
            elif event_group.away_team and favourite_norm == event_group.away_team:
                spread = abs(spread)

        entry: dict[str, Any] = {
            "team1": team1,
            "team2": team2,
            "date_time": event_group.event_start,
            "league": event_group.league,
            "sport": event_group.sport,
            "market_type": market_type,
            "spread": spread,
            "spread_favourite": spread_favourite,
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
            "azuro_condition_id": None,
            "azuro_team1_back_odds": None,
            "azuro_draw_back_odds": None,
            "azuro_team2_back_odds": None,
            "azuro_team1_max_stake_usdc": None,
            "azuro_draw_max_stake_usdc": None,
            "azuro_team2_max_stake_usdc": None,
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

        for provider_name in ("polymarket", "matchbook", "smarkets", "sx_bet", "azuro"):
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
                    if provider_name == "azuro" and side == "back":
                        max_stake = (record.metadata or {}).get("max_stake_usdc") or 0.0
                        entry[f"azuro_{team_slot}_max_stake_usdc"] = max_stake

        # Polymarket per-slot market / CLOB token IDs
        for team_slot in ("team1", "draw", "team2"):
            for side in ("back", "lay"):
                key = ("polymarket", team_slot, side)
                if key in best_by_provider_team:
                    _, record = best_by_provider_team[key]
                    entry[f"polymarket_{team_slot}_market_id"] = record.source_market_id
                    clob_token = (record.metadata or {}).get("clob_token_id")
                    if clob_token:
                        entry[f"polymarket_{team_slot}_clob_token_id"] = clob_token
                    if entry["polymarket_market_id"] is None:
                        entry["polymarket_market_id"] = record.source_market_id
                    break

        # Single market/event ID per non-Polymarket provider
        for provider_name, id_field, id_attr in (
            ("smarkets",  "smarkets_market_id",  "source_market_id"),
            ("matchbook", "matchbook_event_id",   "source_event_id"),
            ("sx_bet",    "sx_bet_market_hash",   "source_market_id"),
            ("azuro",     "azuro_condition_id",   "source_market_id"),
        ):
            for key in best_by_provider_team:
                if key[0] == provider_name:
                    _, record = best_by_provider_team[key]
                    entry[id_field] = getattr(record, id_attr)
                    if provider_name == "sx_bet":
                        outcome_one = (record.metadata or {}).get("outcome_one_team")
                        if outcome_one:
                            entry["sx_bet_outcome_one_team"] = outcome_one
                    break

        # Soccer: per-outcome SX Bet market hashes for targeted re-fetching
        if entry.get("league") in ("ucl", "epl", "uel", "seria", "laliga"):
            for team_slot in ("team1", "draw", "team2"):
                for side in ("back", "lay"):
                    chosen = best_by_provider_team.get(("sx_bet", team_slot, side))
                    if chosen:
                        _, record = chosen
                        entry[f"sx_bet_{team_slot}_market_hash"] = record.source_market_id
                        break

        payload.append(entry)

    payload.sort(
        key=lambda item: (item["date_time"] or "", item["team1"] or "", item["team2"] or "")
    )
    return payload
