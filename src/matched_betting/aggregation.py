"""
aggregation.py
--------------
Builds the per-game aggregated odds payload from normalised OddsRecord objects.

Shared by ids.py, scan.py, and cli.py so the aggregation logic lives in one
place rather than being embedded in the CLI entry point.
"""
from __future__ import annotations

from typing import Any

from matched_betting.leagues import SPREAD_LEAGUES, TOTALS_LEAGUES
from matched_betting.models import OddsRecord
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.sx_bet import _SOCCER_LEAGUES as _SX_SOCCER_LEAGUES


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

INDEX_TOTALS_FIELDS = (
    "total_line",
    "polymarket_over_clob_token_id",
    "polymarket_under_clob_token_id",
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

        # MLB/MLS totals: one entry per (game, total_line) pair with over/under slot names
        if event_group.league in TOTALS_LEAGUES:
            lines_to_indices: dict[float, list[int]] = {}
            for index in indices:
                meta = records[index].metadata or {}
                tl = meta.get("total_line")
                if tl is not None:
                    try:
                        lines_to_indices.setdefault(float(tl), []).append(index)
                    except (TypeError, ValueError):
                        pass

            team1 = _pretty_team(event_group.home_team)
            team2 = _pretty_team(event_group.away_team)

            for total_line, line_indices in sorted(lines_to_indices.items()):
                entry: dict[str, Any] = {
                    "team1": team1,
                    "team2": team2,
                    "date_time": event_group.event_start,
                    "league": event_group.league,
                    "sport": "soccer" if event_group.league in ("mls_totals", "wc_totals") else "baseball",
                    "market_type": "two_way",
                    "total_line": total_line,
                    "polymarket_market_id": None,
                    "matchbook_event_id": None,
                    "sx_bet_market_hash": None,
                    "sx_bet_outcome_one_team": None,
                    "polymarket_over_back_odds": None,
                    "polymarket_under_back_odds": None,
                    "matchbook_over_back_odds": None,
                    "matchbook_over_lay_odds": None,
                    "matchbook_under_back_odds": None,
                    "matchbook_under_lay_odds": None,
                    "sx_bet_over_back_odds": None,
                    "sx_bet_under_back_odds": None,
                    "polymarket_over_clob_token_id": None,
                    "polymarket_under_clob_token_id": None,
                }

                best_by_provider_slot: dict[tuple[str, str, str], tuple[int, OddsRecord]] = {}
                for index in line_indices:
                    record = records[index]
                    sel = record.selection_name
                    if sel not in ("over", "under"):
                        continue
                    key = (record.provider, sel, record.selection_side.lower())
                    current = best_by_provider_slot.get(key)
                    if record.decimal_odds is None:
                        continue
                    if current is None:
                        best_by_provider_slot[key] = (index, record)
                    elif record.selection_side.lower() == "lay":
                        if current[1].decimal_odds is None or record.decimal_odds < current[1].decimal_odds:
                            best_by_provider_slot[key] = (index, record)
                    else:
                        if current[1].decimal_odds is None or record.decimal_odds > current[1].decimal_odds:
                            best_by_provider_slot[key] = (index, record)

                for provider_name in ("polymarket", "matchbook", "sx_bet"):
                    for ou_slot in ("over", "under"):
                        for side in ("back", "lay"):
                            chosen = best_by_provider_slot.get((provider_name, ou_slot, side))
                            if chosen is None:
                                continue
                            _, record = chosen
                            odds_key = f"{provider_name}_{ou_slot}_{side}_odds"
                            avail_key = f"{provider_name}_{ou_slot}_{side}_avail"
                            if odds_key in entry:
                                entry[odds_key] = record.decimal_odds
                            entry[avail_key] = _extract_available(record)

                # Polymarket over/under CLOB token IDs and market ID
                for ou_slot in ("over", "under"):
                    for side in ("back",):
                        key = ("polymarket", ou_slot, side)
                        if key in best_by_provider_slot:
                            _, record = best_by_provider_slot[key]
                            clob_token = (record.metadata or {}).get("clob_token_id")
                            if clob_token:
                                entry[f"polymarket_{ou_slot}_clob_token_id"] = clob_token
                            if entry["polymarket_market_id"] is None:
                                entry["polymarket_market_id"] = record.source_market_id
                            break

                # Matchbook and SX Bet market IDs
                for provider_name, id_field, id_attr in (
                    ("matchbook", "matchbook_event_id", "source_event_id"),
                    ("sx_bet", "sx_bet_market_hash", "source_market_id"),
                ):
                    for key in best_by_provider_slot:
                        if key[0] == provider_name:
                            _, record = best_by_provider_slot[key]
                            entry[id_field] = getattr(record, id_attr)
                            # SX Bet: store which outcome is outcomeOne so
                            # _sx_outcome_for() bets Over vs Under on the correct side.
                            # Without this, _sx_outcome_for falls back to team-name
                            # matching which never matches "Over"/"Under", causing
                            # both sure-bet legs to land on the same side.
                            if provider_name == "sx_bet":
                                outcome_one = (record.metadata or {}).get("outcome_one_team")
                                if outcome_one:
                                    entry["sx_bet_outcome_one_team"] = outcome_one
                            break

                payload.append(entry)
            continue  # skip normal two_way/three_way entry building

        # MLB/MLS spread: one entry per (game, spread_line) pair.
        # Without this split, a Matchbook -1.0 record and a Polymarket -1.5
        # record for the same match would be merged into one entry, creating
        # phantom cross-line arbs with inflated margins.
        if event_group.league in SPREAD_LEAGUES:
            home_team = event_group.home_team
            away_team = event_group.away_team

            def _norm_spread(raw_spread, raw_fav: str | None, _ht=home_team, _at=away_team, _lg=event_group.league) -> float | None:
                if raw_spread is None:
                    return None
                try:
                    val = abs(float(raw_spread))
                except (TypeError, ValueError):
                    return None
                if raw_fav is None:
                    return val
                fav_n = normalize_team_name(str(raw_fav), _lg)
                if _ht and fav_n == _ht:
                    return -val
                elif _at and fav_n == _at:
                    return val
                return val

            # Group record indices by normalized spread value (home-team perspective).
            spread_lines_to_indices: dict[float, list[int]] = {}
            for index in indices:
                meta = records[index].metadata or {}
                norm = _norm_spread(meta.get("spread"), meta.get("spread_favourite"))
                if norm is not None:
                    spread_lines_to_indices.setdefault(norm, []).append(index)

            _sp_team1 = _pretty_team(home_team)
            _sp_team2 = _pretty_team(away_team)

            for spread_val, line_indices in sorted(spread_lines_to_indices.items()):
                # Retrieve spread_favourite label from the first record that has one.
                _spread_favourite: str | None = None
                for _idx in line_indices:
                    _sf = (records[_idx].metadata or {}).get("spread_favourite")
                    if _sf:
                        _spread_favourite = _sf
                        break

                sp_entry: dict[str, Any] = {
                    "team1": _sp_team1,
                    "team2": _sp_team2,
                    "date_time": event_group.event_start,
                    "league": event_group.league,
                    "sport": event_group.sport,
                    "market_type": "two_way",
                    "spread": spread_val,
                    "spread_favourite": _spread_favourite,
                    "polymarket_market_id": None,
                    "smarkets_market_id": None,
                    "matchbook_event_id": None,
                    "sx_bet_market_hash": None,
                    "polymarket_team1_back_odds": None,
                    "polymarket_team1_lay_odds": None,
                    "polymarket_team2_back_odds": None,
                    "polymarket_team2_lay_odds": None,
                    "matchbook_team1_back_odds": None,
                    "matchbook_team1_lay_odds": None,
                    "matchbook_team2_back_odds": None,
                    "matchbook_team2_lay_odds": None,
                    "smarkets_team1_back_odds": None,
                    "smarkets_team1_lay_odds": None,
                    "smarkets_team2_back_odds": None,
                    "smarkets_team2_lay_odds": None,
                    "sx_bet_team1_back_odds": None,
                    "sx_bet_team1_lay_odds": None,
                    "sx_bet_team2_back_odds": None,
                    "sx_bet_team2_lay_odds": None,
                }

                best_sp: dict[tuple[str, str, str], tuple[int, OddsRecord]] = {}
                for index in line_indices:
                    record = records[index]
                    sel = record.selection_name
                    if home_team and sel == home_team:
                        team_slot = "team1"
                    elif away_team and sel == away_team:
                        team_slot = "team2"
                    else:
                        continue
                    side = record.selection_side.lower()
                    key = (record.provider, team_slot, side)
                    cur = best_sp.get(key)
                    if record.decimal_odds is None:
                        continue
                    if cur is None:
                        best_sp[key] = (index, record)
                    elif side == "lay":
                        if cur[1].decimal_odds is None or record.decimal_odds < cur[1].decimal_odds:
                            best_sp[key] = (index, record)
                    else:
                        if cur[1].decimal_odds is None or record.decimal_odds > cur[1].decimal_odds:
                            best_sp[key] = (index, record)

                for provider_name in ("polymarket", "matchbook", "smarkets", "sx_bet"):
                    for team_slot in ("team1", "team2"):
                        for side in ("back", "lay"):
                            chosen = best_sp.get((provider_name, team_slot, side))
                            if chosen is None:
                                continue
                            _, record = chosen
                            odds_key = f"{provider_name}_{team_slot}_{side}_odds"
                            avail_key = f"{provider_name}_{team_slot}_{side}_avail"
                            if odds_key in sp_entry:
                                sp_entry[odds_key] = record.decimal_odds
                            sp_entry[avail_key] = _extract_available(record)

                # Polymarket per-slot CLOB token IDs and market IDs.
                for team_slot in ("team1", "team2"):
                    for side in ("back", "lay"):
                        key = ("polymarket", team_slot, side)
                        if key in best_sp:
                            _, record = best_sp[key]
                            clob_token = (record.metadata or {}).get("clob_token_id")
                            if clob_token:
                                sp_entry[f"polymarket_{team_slot}_clob_token_id"] = clob_token
                            sp_entry[f"polymarket_{team_slot}_market_id"] = record.source_market_id
                            if sp_entry["polymarket_market_id"] is None:
                                sp_entry["polymarket_market_id"] = record.source_market_id
                            break

                # Single market/event ID per non-Polymarket provider.
                for provider_name, id_field, id_attr in (
                    ("smarkets",  "smarkets_market_id",  "source_market_id"),
                    ("matchbook", "matchbook_event_id",   "source_event_id"),
                    ("sx_bet",    "sx_bet_market_hash",   "source_market_id"),
                ):
                    for key in best_sp:
                        if key[0] == provider_name:
                            _, record = best_sp[key]
                            sp_entry[id_field] = getattr(record, id_attr)
                            # SX Bet: capture which team is outcomeOne so
                            # _sx_outcome_for() bets on the correct side.
                            if provider_name == "sx_bet":
                                outcome_one = (record.metadata or {}).get("outcome_one_team")
                                if outcome_one:
                                    sp_entry["sx_bet_outcome_one_team"] = outcome_one
                            break

                payload.append(sp_entry)
            continue  # skip general two_way/three_way entry building

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
            if record.decimal_odds is None:
                continue
            if current is None:
                best_by_provider_team[key] = (index, record)
            elif side == "lay":
                if current[1].decimal_odds is None or record.decimal_odds < current[1].decimal_odds:
                    best_by_provider_team[key] = (index, record)
            else:
                if current[1].decimal_odds is None or record.decimal_odds > current[1].decimal_odds:
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

        # Soccer: per-outcome SX Bet market hashes for targeted re-fetching.
        # Uses the provider's _SOCCER_LEAGUES so a new soccer league can't be
        # missed here — when "wc" was absent from a hardcoded copy of this
        # list, WC games stored only the bare sx_bet_market_hash (pointing at
        # the Tie market) and the stream wired draw odds into a team slot,
        # producing +188% phantom arbs (Qatar/Switzerland, 2026-06-10).
        if entry.get("league") in _SX_SOCCER_LEAGUES:
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
