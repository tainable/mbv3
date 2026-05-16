from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from matched_betting.config import AzuroSettings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.models import OddsRecord, ProviderPayload, utc_now_iso
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.base import GameContext, OddsProvider


# ── Azuro v3 data-feed schema notes ───────────────────────────────────────────
#
# - conditionTypeId does NOT exist in v3. Market type is identified by
#   outcomeId lookup against the Azuro dictionaries outcomes.json.
# - state: Active  (not status: Created)
# - currentOdds is a BigDecimal string (e.g. "1.72") — no 1e12 scaling.
# - Outcome IDs are globally fixed across all Azuro deployments.
#
# Moneyline outcomeId reference (from Azuro dictionaries outcomes.json):
#
#   NBA  — marketId=19, gamePeriodId=76, gameTypeId=76 "Match Winner incl. OT"
#           6983 = Team 1 wins,  6984 = Team 2 wins
#
#   EPL/UCL/UEL — marketId=1, gamePeriodId=1, gameTypeId=1 "1X2 Full Time"
#           29 = Home (Team 1),  30 = Draw,  31 = Away (Team 2)
#
#   IPL  — marketId=19, gamePeriodId=76, gameTypeId=83 "Match Winner"
#           7039 = Team 1 wins,  7040 = Team 2 wins
#
#   NHL  — marketId=19, gamePeriodId=853, gameTypeId=1 "Match Winner incl. OT/SO"
#           16694 = Team 1 wins,  16695 = Team 2 wins
#
#   MLB  — marketId=19, gamePeriodId=1, gameTypeId=1 "Match Winner"
#           6979 = Team 1 wins,  6980 = Team 2 wins
#
# Note: NHL and MLB slugs are unverified — those leagues may not be live on
# the Polygon subgraph. The provider will return 0 records gracefully if so.

# ── GraphQL queries ────────────────────────────────────────────────────────────

# Single parameterised query used for every league.
# $moneylineIds filters conditions server-side to only the target market.
_GQL_FETCH_GAMES = """
query FetchGames(
  $sportSlug:    String!
  $leagueSlug:   String!
  $after:        BigInt!
  $moneylineIds: [String!]!
) {
  games(
    where: {
      sport_:  { slug: $sportSlug }
      league_: { slug: $leagueSlug }
      startsAt_gt: $after
    }
    orderBy: startsAt
    orderDirection: asc
    first: 200
  ) {
    gameId
    startsAt
    participants { name }
    conditions(
      where: {
        state: Active
        outcomesIds_contains: $moneylineIds
      }
    ) {
      conditionId
      state
      maxOutcomePotentialLoss
      outcomes(orderBy: sortOrder) {
        outcomeId
        currentOdds
        sortOrder
        potentialLoss
      }
    }
  }
}
"""

_GQL_FETCH_CONDITION = """
query FetchCondition($conditionId: String!) {
  condition(id: $conditionId) {
    conditionId
    state
    maxOutcomePotentialLoss
    outcomes(orderBy: sortOrder) {
      outcomeId
      currentOdds
      sortOrder
      potentialLoss
    }
    game {
      gameId
      startsAt
      participants { name }
    }
  }
}
"""

# ── Per-league configuration ───────────────────────────────────────────────────

@dataclass(frozen=True)
class _LeagueConfig:
    """All Azuro-specific parameters for one league."""
    sport_slug: str               # Azuro subgraph sport slug
    league_slug: str              # Azuro subgraph league slug
    sport: str                    # Internal sport name for OddsRecord
    moneyline_ids: frozenset[int] # Fixed global outcomeIds for the moneyline
    slot_map: dict[int, str]      # outcomeId → "team1" | "draw" | "team2"
    market_type: str              # "two_way" | "three_way"
    market_key: str               # marketId-gamePeriodId-gameTypeId (for metadata)
    market_label: str             # Human-readable market name (for metadata)


# ── Basketball ─────────────────────────────────────────────────────────────────
# marketId=19, gamePeriodId=76, gameTypeId=76 = "Match Winner incl. OT"
_NBA = _LeagueConfig(
    sport_slug="basketball",
    league_slug="nba",
    sport="basketball",
    moneyline_ids=frozenset({6983, 6984}),
    slot_map={6983: "team1", 6984: "team2"},
    market_type="two_way",
    market_key="19-76-76",
    market_label="Match Winner incl. OT",
)

# ── Football / Soccer ──────────────────────────────────────────────────────────
# marketId=1, gamePeriodId=1, gameTypeId=1 = "1X2 Full Time"
# outcomeId 29 = Home (sortOrder 0), 30 = Draw (sortOrder 1), 31 = Away (sortOrder 2)
_SOCCER_ML_IDS   = frozenset({29, 30, 31})
_SOCCER_SLOT_MAP = {29: "team1", 30: "draw", 31: "team2"}

_EPL = _LeagueConfig(
    sport_slug="football",
    league_slug="premier-league",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="1X2 Full Time",
)

_UCL = _LeagueConfig(
    sport_slug="football",
    league_slug="uefa-champions-league",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="1X2 Full Time",
)

_UEL = _LeagueConfig(
    sport_slug="football",
    league_slug="uefa-europa-league",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="1X2 Full Time",
)

_SERIA = _LeagueConfig(
    sport_slug="football",
    league_slug="serie-a",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="1X2 Full Time",
)

# TODO: verify the Azuro subgraph league slug for La Liga
_LALIGA = _LeagueConfig(
    sport_slug="football",
    league_slug="la-liga",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="1X2 Full Time",
)

# ── Cricket ────────────────────────────────────────────────────────────────────
# marketId=19, gamePeriodId=76, gameTypeId=83 = "Match Winner"
# Azuro league slug "premier-league" under sport "cricket" = IPL
_IPL = _LeagueConfig(
    sport_slug="cricket",
    league_slug="premier-league",
    sport="cricket",
    moneyline_ids=frozenset({7039, 7040}),
    slot_map={7039: "team1", 7040: "team2"},
    market_type="two_way",
    market_key="19-76-83",
    market_label="Match Winner",
)

# ── Ice Hockey ─────────────────────────────────────────────────────────────────
# marketId=19, gamePeriodId=853, gameTypeId=1 = "Match Winner incl. OT/SO"
# NOTE: NHL has not been observed in the Polygon subgraph; the provider will
# return 0 records gracefully if the league slug is wrong or unavailable.
_NHL = _LeagueConfig(
    sport_slug="ice-hockey",
    league_slug="nhl",
    sport="ice-hockey",
    moneyline_ids=frozenset({16694, 16695}),
    slot_map={16694: "team1", 16695: "team2"},
    market_type="two_way",
    market_key="19-853-1",
    market_label="Match Winner incl. OT/SO",
)

# ── Baseball ───────────────────────────────────────────────────────────────────
# marketId=19, gamePeriodId=1, gameTypeId=1 = "Match Winner"
# NOTE: MLB has not been observed in the Polygon subgraph; as above, the
# provider returns 0 records gracefully if unavailable.
_MLB = _LeagueConfig(
    sport_slug="baseball",
    league_slug="mlb",
    sport="baseball",
    moneyline_ids=frozenset({6979, 6980}),
    slot_map={6979: "team1", 6980: "team2"},
    market_type="two_way",
    market_key="19-1-1",
    market_label="Match Winner",
)

# Master lookup: internal league key → config
_LEAGUE_CONFIGS: dict[str, _LeagueConfig] = {
    "nba": _NBA,
    "epl": _EPL,
    "ucl": _UCL,
    "uel": _UEL,
    "seria": _SERIA,
    "laliga": _LALIGA,
    "ipl": _IPL,
    "nhl": _NHL,
    "mlb": _MLB,
}


class AzuroProvider(OddsProvider):
    name = "azuro"

    def __init__(
        self,
        settings: AzuroSettings,
        http_client: HttpClient,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        super().__init__(debug_logger)
        self.settings = settings
        self.http_client = http_client

    # ── Public interface ───────────────────────────────────────────────────────

    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        requested = [lg for lg in leagues if lg in _LEAGUE_CONFIGS]
        if not requested:
            return ProviderPayload(provider=self.name, records=[], warnings=[])

        retrieved_at = utc_now_iso()
        records: list[OddsRecord] = []
        warnings: list[str] = []
        now_unix = str(int(time.time()))

        for league_key in requested:
            cfg = _LEAGUE_CONFIGS[league_key]
            self.debug(
                f"{self.name}: fetching {league_key} "
                f"({cfg.sport_slug}/{cfg.league_slug}) after unix={now_unix}"
            )
            try:
                response = self._gql(
                    _GQL_FETCH_GAMES,
                    {
                        "sportSlug":    cfg.sport_slug,
                        "leagueSlug":   cfg.league_slug,
                        "after":        now_unix,
                        "moneylineIds": [str(oid) for oid in sorted(cfg.moneyline_ids)],
                    },
                )
            except Exception as exc:
                warnings.append(f"Azuro: {league_key} fetch failed: {exc}")
                continue

            gql_errors = response.get("errors")
            if gql_errors:
                warnings.append(f"Azuro: {league_key} GraphQL errors: {gql_errors}")
                continue

            games = (response.get("data") or {}).get("games", [])
            self.debug(f"{self.name}: {len(games)} {league_key} games returned from subgraph")

            for game in games:
                try:
                    game_records = self._game_to_records(game, league_key, cfg, retrieved_at)
                    records.extend(game_records)
                    self.debug(
                        f"{self.name}: game {game.get('gameId')} -> {len(game_records)} records"
                    )
                except Exception as exc:
                    warnings.append(
                        f"Azuro: skipped {league_key} game {game.get('gameId')}: {exc}"
                    )

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def fetch_odds_by_ids(
        self,
        game_contexts: list[GameContext],
        leagues: list[str],
    ) -> ProviderPayload:
        supported = {lg for lg in leagues if lg in _LEAGUE_CONFIGS}
        if not supported:
            return ProviderPayload(provider=self.name, records=[], warnings=[])

        retrieved_at = utc_now_iso()
        records: list[OddsRecord] = []
        warnings: list[str] = []

        for game in game_contexts:
            condition_id = game.get("azuro_condition_id")
            league_key = game.get("league")
            if not condition_id or league_key not in supported:
                continue
            cfg = _LEAGUE_CONFIGS[league_key]

            self.debug(
                f"{self.name}: targeted fetch {league_key} conditionId={condition_id}"
            )
            try:
                response = self._gql(_GQL_FETCH_CONDITION, {"conditionId": condition_id})
                gql_errors = response.get("errors")
                if gql_errors:
                    warnings.append(
                        f"Azuro: GraphQL errors for {condition_id}: {gql_errors}"
                    )
                    continue
                condition = (response.get("data") or {}).get("condition")
                if not condition:
                    warnings.append(
                        f"Azuro: condition {condition_id} not found in subgraph"
                    )
                    continue
                if condition.get("state") != "Active":
                    self.debug(
                        f"{self.name}: condition {condition_id} "
                        f"state={condition.get('state')!r} — skipping"
                    )
                    continue
                # Verify the condition is still the expected moneyline market
                outcome_ids = {
                    int(o.get("outcomeId", 0))
                    for o in condition.get("outcomes", [])
                }
                if not outcome_ids.issuperset(cfg.moneyline_ids):
                    self.debug(
                        f"{self.name}: condition {condition_id} outcomeIds={outcome_ids} "
                        f"— not the expected moneyline for {league_key}, skipping"
                    )
                    continue
                game_data = condition.get("game") or {}
                participant_names = _extract_participant_names(
                    game_data.get("participants", [])
                )
                event_name = _build_event_name(participant_names)
                event_start = _parse_starts_at(game_data.get("startsAt"))
                condition_records = self._condition_to_records(
                    condition, participant_names, league_key, cfg,
                    event_name, event_start, retrieved_at,
                )
                records.extend(condition_records)
                self.debug(
                    f"{self.name}: targeted fetch conditionId={condition_id} "
                    f"-> {len(condition_records)} records"
                )
            except Exception as exc:
                warnings.append(
                    f"Azuro: skipped condition {condition_id}: {exc}"
                )

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _game_to_records(
        self,
        game: dict[str, Any],
        league_key: str,
        cfg: _LeagueConfig,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        participant_names = _extract_participant_names(game.get("participants", []))
        event_name = _build_event_name(participant_names)
        event_start = _parse_starts_at(game.get("startsAt"))
        conditions = game.get("conditions", [])
        self.debug(
            f"{self.name}: game {game.get('gameId')} event_name={event_name!r} "
            f"start={event_start} moneyline_conditions={len(conditions)}"
        )

        records: list[OddsRecord] = []
        for condition in conditions:
            outcome_ids = {
                int(o.get("outcomeId", 0))
                for o in condition.get("outcomes", [])
            }
            if not outcome_ids.issuperset(cfg.moneyline_ids):
                # GQL filter should prevent this, but guard defensively
                self.debug(
                    f"{self.name}: skipping condition {condition.get('conditionId')} "
                    f"outcomeIds={outcome_ids} (not the expected moneyline)"
                )
                continue
            records.extend(
                self._condition_to_records(
                    condition, participant_names, league_key, cfg,
                    event_name, event_start, retrieved_at,
                )
            )
        return records

    def _condition_to_records(
        self,
        condition: dict[str, Any],
        participant_names: list[str],
        league_key: str,
        cfg: _LeagueConfig,
        event_name: str,
        event_start: str | None,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        condition_id = str(condition.get("conditionId"))
        records: list[OddsRecord] = []

        # Per-outcome pool cap (max profit available to a single bettor per side).
        # Remaining capacity = cap − already allocated losses.
        try:
            max_outcome_cap = float(condition.get("maxOutcomePotentialLoss") or 0)
        except (TypeError, ValueError):
            max_outcome_cap = 0.0

        for o in condition.get("outcomes", []):
            oid = int(o.get("outcomeId", 0))
            slot = cfg.slot_map.get(oid)
            if slot is None:
                continue

            raw_odds = o.get("currentOdds")
            if raw_odds is None:
                continue
            try:
                decimal_odds = float(raw_odds)
            except (TypeError, ValueError):
                continue
            if decimal_odds <= 1.0:
                self.debug(
                    f"{self.name}: skipping outcome {oid} decimal_odds={decimal_odds} (<=1.0)"
                )
                continue

            if slot == "draw":
                selection_name = "draw"
            elif slot == "team1":
                raw_name = (
                    participant_names[0]
                    if participant_names
                    else "participant_0"
                )
                selection_name = normalize_team_name(raw_name, league_key)
            else:  # team2
                raw_name = (
                    participant_names[1]
                    if len(participant_names) > 1
                    else "participant_1"
                )
                selection_name = normalize_team_name(raw_name, league_key)

            # Max stake = remaining win capacity / (odds − 1)
            try:
                already_allocated = float(o.get("potentialLoss") or 0)
            except (TypeError, ValueError):
                already_allocated = 0.0
            remaining_win = max(0.0, max_outcome_cap - already_allocated)
            max_stake_usdc = (
                round(remaining_win / (decimal_odds - 1), 2)
                if decimal_odds > 1.0 and remaining_win > 0
                else 0.0
            )

            self.debug(
                f"{self.name}: outcome {oid} slot={slot} "
                f"-> selection={selection_name!r} odds={decimal_odds} "
                f"max_stake_usdc={max_stake_usdc}"
            )

            records.append(
                OddsRecord(
                    provider=self.name,
                    sport=cfg.sport,
                    league=league_key,
                    event_name=event_name,
                    event_start=event_start,
                    market_name="moneyline",
                    market_type=cfg.market_type,
                    selection_name=selection_name,
                    selection_side="back",
                    decimal_odds=decimal_odds,
                    implied_probability=round(1 / decimal_odds, 6),
                    currency="USDC",
                    source_market_id=condition_id,
                    source_event_id=condition_id,
                    retrieved_at=retrieved_at,
                    metadata={
                        "outcome_id": oid,
                        "market_key": cfg.market_key,
                        "market_name": cfg.market_label,
                        "max_stake_usdc": max_stake_usdc,
                    },
                )
            )
        return records

    def _gql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        return self.http_client.post_json(
            self.settings.subgraph_url,
            payload={"query": query, "variables": variables},
        )


# ── Module-level helpers ───────────────────────────────────────────────────────

def _extract_participant_names(participants: list[dict[str, Any]]) -> list[str]:
    return [p.get("name") or f"participant_{i}" for i, p in enumerate(participants)]


def _build_event_name(participant_names: list[str]) -> str:
    if len(participant_names) >= 2:
        return f"{participant_names[0]} vs {participant_names[1]}"
    return " vs ".join(participant_names) if participant_names else "Unknown event"


def _parse_starts_at(starts_at: Any) -> str | None:
    """Convert an Azuro BigInt Unix timestamp to an ISO-8601 string."""
    if starts_at is None:
        return None
    try:
        return datetime.fromtimestamp(int(starts_at), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return str(starts_at)
