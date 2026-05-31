from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from matched_betting.config import AzuroSettings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.models import OddsRecord, ProviderPayload, utc_now_iso
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.base import GameContext, OddsProvider


# ── Azuro Backend API notes ────────────────────────────────────────────────────
#
# The data-feed subgraphs were deprecated in 2025 and no longer receive new
# games. All feed data is fetched from the Backend REST API:
#   https://api.onchainfeed.org/api/v1/public
#
# Relevant endpoints:
#   GET  /market-manager/games-by-filters   upcoming prematch games by league
#   POST /market-manager/conditions-by-game-ids  conditions + odds for a batch
#   POST /market-manager/condition-batch    targeted fetch by conditionId
#
# Odds field: "odds" (was "currentOdds" in the subgraph).
# Pool-cap fields (maxOutcomePotentialLoss, potentialLoss) are not exposed by
# the API; max_stake_usdc is omitted from metadata (arb_finder cap lines blank).
#
# Outcome IDs are globally fixed across all Azuro deployments:
#
#   NBA  — marketId=19, "Match Winner incl. OT"
#           6983 = Team 1 wins,  6984 = Team 2 wins
#
#   Soccer — marketId=1, "Full Time Result" (1X2)
#           29 = Home (Team 1),  30 = Draw,  31 = Away (Team 2)
#
#   IPL  — marketId=19, "Match Winner"
#           7039 = Team 1 wins,  7040 = Team 2 wins
#
#   NHL  — marketId=19, "Match Winner incl. OT/SO"
#           16694 = Team 1 wins,  16695 = Team 2 wins  (unverified; no current games)
#
#   MLB  — marketId=19, "Match Winner"
#           7031 = Team 1 wins,  7032 = Team 2 wins
#           (NB: 6979/6980 are soccer Draw-no-bet IDs — wrong in old code)

_PER_PAGE = 50


# ── Per-league configuration ───────────────────────────────────────────────────

@dataclass(frozen=True)
class _LeagueConfig:
    """All Azuro-specific parameters for one league."""
    sport_slug: str               # Azuro sport slug
    league_slug: str              # Azuro league slug
    sport: str                    # Internal sport name for OddsRecord
    moneyline_ids: frozenset[int] # Fixed global outcomeIds for the moneyline
    slot_map: dict[int, str]      # outcomeId → "team1" | "draw" | "team2"
    market_type: str              # "two_way" | "three_way"
    market_key: str               # marketId-gamePeriodId-gameTypeId (for metadata)
    market_label: str             # Human-readable market name (for metadata)


# ── Basketball ─────────────────────────────────────────────────────────────────
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
    market_label="Full Time Result",
)

_UCL = _LeagueConfig(
    sport_slug="football",
    league_slug="uefa-champions-league",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="Full Time Result",
)

_UEL = _LeagueConfig(
    sport_slug="football",
    league_slug="uefa-europa-league",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="Full Time Result",
)

_SERIA = _LeagueConfig(
    sport_slug="football",
    league_slug="serie-a",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="Full Time Result",
)

_LALIGA = _LeagueConfig(
    sport_slug="football",
    league_slug="la-liga",
    sport="football",
    moneyline_ids=_SOCCER_ML_IDS,
    slot_map=_SOCCER_SLOT_MAP,
    market_type="three_way",
    market_key="1-1-1",
    market_label="Full Time Result",
)

# ── Cricket ────────────────────────────────────────────────────────────────────
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
_MLB = _LeagueConfig(
    sport_slug="baseball",
    league_slug="mlb",
    sport="baseball",
    moneyline_ids=frozenset({7031, 7032}),
    slot_map={7031: "team1", 7032: "team2"},
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

        for league_key in requested:
            cfg = _LEAGUE_CONFIGS[league_key]
            self.debug(f"{self.name}: fetching {league_key} ({cfg.sport_slug}/{cfg.league_slug})")

            try:
                games = self._fetch_games(cfg.sport_slug, cfg.league_slug)
            except Exception as exc:
                warnings.append(f"Azuro: {league_key} games fetch failed: {exc}")
                continue

            if not games:
                self.debug(f"{self.name}: {league_key} — 0 upcoming games")
                continue

            self.debug(f"{self.name}: {len(games)} upcoming {league_key} games")

            # Index participant names and start times by gameId for condition lookup
            game_info: dict[str, dict[str, Any]] = {
                g["gameId"]: {
                    "participants": [p["name"] for p in g.get("participants", [])],
                    "startsAt": g.get("startsAt"),
                }
                for g in games
            }

            try:
                conditions = self._fetch_conditions_by_game_ids(list(game_info))
            except Exception as exc:
                warnings.append(f"Azuro: {league_key} conditions fetch failed: {exc}")
                continue

            self.debug(f"{self.name}: {len(conditions)} conditions for {league_key}")

            for condition in conditions:
                if condition.get("state") != "Active":
                    continue

                outcome_ids = {int(o.get("outcomeId", 0)) for o in condition.get("outcomes", [])}
                if not outcome_ids.issuperset(cfg.moneyline_ids):
                    continue

                game_id = (condition.get("game") or {}).get("gameId")
                info = game_info.get(game_id, {})
                participant_names = info.get("participants", [])
                event_name = _build_event_name(participant_names)
                event_start = _parse_starts_at(info.get("startsAt"))

                try:
                    cond_records = self._condition_to_records(
                        condition, participant_names, league_key, cfg,
                        event_name, event_start, retrieved_at,
                    )
                    records.extend(cond_records)
                    self.debug(
                        f"{self.name}: condition {condition.get('conditionId')} "
                        f"-> {len(cond_records)} records"
                    )
                except Exception as exc:
                    warnings.append(
                        f"Azuro: skipped condition {condition.get('conditionId')}: {exc}"
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

            self.debug(f"{self.name}: targeted fetch {league_key} conditionId={condition_id}")
            try:
                resp = self._api_post(
                    "market-manager/condition-batch",
                    {"conditionIds": [condition_id], "environment": self.settings.environment},
                )
            except Exception as exc:
                warnings.append(f"Azuro: condition {condition_id} fetch failed: {exc}")
                continue

            conditions = resp.get("conditions", [])
            if not conditions:
                warnings.append(f"Azuro: condition {condition_id} not found")
                continue

            condition = conditions[0]
            if condition.get("state") != "Active":
                self.debug(
                    f"{self.name}: condition {condition_id} "
                    f"state={condition.get('state')!r} — skipping"
                )
                continue

            outcome_ids = {int(o.get("outcomeId", 0)) for o in condition.get("outcomes", [])}
            if not outcome_ids.issuperset(cfg.moneyline_ids):
                self.debug(
                    f"{self.name}: condition {condition_id} outcomeIds={outcome_ids} "
                    f"— not the expected moneyline for {league_key}, skipping"
                )
                continue

            # Participant names come from the aggregated game context (already normalised)
            participant_names = [
                game.get("team1") or "participant_0",
                game.get("team2") or "participant_1",
            ]
            event_name = _build_event_name(participant_names)
            event_start = game.get("date_time")

            try:
                cond_records = self._condition_to_records(
                    condition, participant_names, league_key, cfg,
                    event_name, event_start, retrieved_at,
                )
                records.extend(cond_records)
                self.debug(
                    f"{self.name}: targeted fetch conditionId={condition_id} "
                    f"-> {len(cond_records)} records"
                )
            except Exception as exc:
                warnings.append(f"Azuro: skipped condition {condition_id}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _fetch_games(self, sport_slug: str, league_slug: str) -> list[dict[str, Any]]:
        """Fetch all upcoming prematch games for a sport/league, paginating as needed."""
        all_games: list[dict[str, Any]] = []
        page = 1
        while True:
            resp = self._api_get(
                "market-manager/games-by-filters",
                params={
                    "gameState": "Prematch",
                    "environment": self.settings.environment,
                    "sportSlug": sport_slug,
                    "leagueSlug": league_slug,
                    "orderBy": "startsAt",
                    "orderDirection": "asc",
                    "page": page,
                    "perPage": _PER_PAGE,
                },
            )
            games = resp.get("games", [])
            all_games.extend(games)
            if len(games) < _PER_PAGE or page >= resp.get("totalPages", 1):
                break
            page += 1
        return all_games

    def _fetch_conditions_by_game_ids(self, game_ids: list[str]) -> list[dict[str, Any]]:
        resp = self._api_post(
            "market-manager/conditions-by-game-ids",
            {"gameIds": game_ids, "environment": self.settings.environment},
        )
        return resp.get("conditions", [])

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

        for o in condition.get("outcomes", []):
            oid = int(o.get("outcomeId", 0))
            slot = cfg.slot_map.get(oid)
            if slot is None:
                continue

            raw_odds = o.get("odds")
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
                raw_name = participant_names[0] if participant_names else "participant_0"
                selection_name = normalize_team_name(raw_name, league_key)
            else:
                raw_name = (
                    participant_names[1]
                    if len(participant_names) > 1
                    else "participant_1"
                )
                selection_name = normalize_team_name(raw_name, league_key)

            self.debug(
                f"{self.name}: outcome {oid} slot={slot} "
                f"-> selection={selection_name!r} odds={decimal_odds}"
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
                    },
                )
            )
        return records

    def _api_get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.http_client.get_json(
            f"{self.settings.api_url}/{path}",
            params=params,
        )

    def _api_post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.http_client.post_json(
            f"{self.settings.api_url}/{path}",
            payload=payload,
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
