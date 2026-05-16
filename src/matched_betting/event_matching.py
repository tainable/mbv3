from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from matched_betting.debug import DebugLogger, noop_debug
from matched_betting.models import OddsRecord
from matched_betting.normalization import TEAM_ALIASES, normalize_team_name  # noqa: F401 (re-exported)


TEAM_SPLIT_PATTERNS = [
    (" at ", True),
    (" @ ", True),
    (" vs. ", False),
    (" vs ", False),
    (" v ", False),
]


@dataclass(frozen=True)
class EventIdentity:
    league: str
    sport: str
    home_team: str | None
    away_team: str | None
    order_known: bool
    start_time: datetime | None
    display_name: str
    spread: float | None = None


@dataclass
class CanonicalEventGroup:
    canonical_event_id: str
    league: str
    sport: str
    home_team: str | None
    away_team: str | None
    event_start: str | None
    source_event_names: list[str]
    providers: list[str]
    record_count: int
    provider_count: int
    records: list[int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_event_id": self.canonical_event_id,
            "league": self.league,
            "sport": self.sport,
            "home_team": self.home_team,
            "away_team": self.away_team,
            "event_start": self.event_start,
            "source_event_names": self.source_event_names,
            "providers": self.providers,
            "record_count": self.record_count,
            "provider_count": self.provider_count,
        }


def match_records_to_canonical_events(
    records: list[OddsRecord],
    *,
    start_tolerance_minutes: int = 30,
    debug_logger: DebugLogger | None = None,
) -> tuple[dict[int, str], list[CanonicalEventGroup]]:
    debug = debug_logger or noop_debug
    assignment: dict[int, str] = {}
    groups: list[_MutableGroup] = []

    for index, record in enumerate(records):
        identity = infer_event_identity(record)
        group = _find_group(groups, identity, start_tolerance_minutes)
        if group is None:
            group = _MutableGroup.from_identity(identity)
            groups.append(group)
            debug(
                f"matcher: created canonical event {group.canonical_event_id} from '{record.event_name}'"
            )
        group.add_record(index, record, identity)
        debug(f"matcher: added record {index} to canonical event {group.canonical_event_id}")
        
        assignment[index] = group.canonical_event_id

    finalized = [group.to_public() for group in groups]
    debug(f"matcher: built {len(finalized)} canonical events from {len(records)} records")
    return assignment, finalized


def infer_event_identity(record: OddsRecord) -> EventIdentity:
    home_team, away_team, order_known = _parse_teams(record.event_name, record.league)
    spread: float | None = None
    raw_spread = (record.metadata or {}).get("spread")
    if raw_spread is not None:
        try:
            spread = float(raw_spread)
        except (TypeError, ValueError):
            pass

    # Normalise spread to the home-team's perspective.
    # spread_favourite names the team at the negative end of the spread (the market favourite).
    # Canonical convention: negative = home team must cover (home favoured),
    #                       positive = away team must cover (home is the underdog).
    if spread is not None and home_team and away_team:
        spread_favourite_raw = (record.metadata or {}).get("spread_favourite")
        if spread_favourite_raw:
            favourite_norm = normalize_team_name(str(spread_favourite_raw), record.league)
            if favourite_norm == home_team:
                spread = -abs(spread)
            elif favourite_norm == away_team:
                spread = abs(spread)

    return EventIdentity(
        league=record.league,
        sport=record.sport,
        home_team=home_team,
        away_team=away_team,
        order_known=order_known,
        start_time=_parse_datetime(record.event_start),
        display_name=record.event_name,
        spread=spread,
    )


def _parse_teams(event_name: str, league: str) -> tuple[str | None, str | None, bool]:
    lowered = event_name.strip()
    for separator, ordered in TEAM_SPLIT_PATTERNS:
        if separator in lowered.lower():
            left, right = _split_case_insensitive(lowered, separator)
            if ordered:
                away = normalize_team_name(left, league)
                home = normalize_team_name(right, league)
                return home, away, True
            home = normalize_team_name(left, league)
            away = normalize_team_name(right, league)
            return home, away, False
    return None, None, False


def _split_case_insensitive(value: str, separator: str) -> tuple[str, str]:
    pattern = re.compile(re.escape(separator), re.IGNORECASE)
    parts = pattern.split(value, maxsplit=1)
    if len(parts) != 2:
        return value, ""
    return parts[0].strip(), parts[1].strip()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _find_group(
    groups: list["_MutableGroup"],
    identity: EventIdentity,
    start_tolerance_minutes: int,
) -> "_MutableGroup | None":
    for group in groups:
        if group.matches(identity, start_tolerance_minutes):
            return group
    return None


class _MutableGroup:
    def __init__(
        self,
        *,
        canonical_event_id: str,
        league: str,
        sport: str,
        home_team: str | None,
        away_team: str | None,
        order_known: bool,
        start_time: datetime | None,
        display_name: str,
        spread: float | None = None,
    ) -> None:
        self.canonical_event_id = canonical_event_id
        self.league = league
        self.sport = sport
        self.home_team = home_team
        self.away_team = away_team
        self.order_known = order_known
        self.start_time = start_time
        self.spread = spread
        self.source_event_names: set[str] = {display_name}
        self.providers: set[str] = set()
        self.records: list[int] = []

    @classmethod
    def from_identity(cls, identity: EventIdentity) -> "_MutableGroup":
        return cls(
            canonical_event_id=_build_canonical_event_id(identity),
            league=identity.league,
            sport=identity.sport,
            home_team=identity.home_team,
            away_team=identity.away_team,
            order_known=identity.order_known,
            start_time=identity.start_time,
            display_name=identity.display_name,
            spread=identity.spread,
        )

    def matches(self, identity: EventIdentity, tolerance_minutes: int) -> bool:
        if self.league != identity.league:
            return False
        if self.spread is not None and identity.spread is not None and self.spread != identity.spread:
            return False

        if self.home_team and self.away_team and identity.home_team and identity.away_team:
            if {self.home_team, self.away_team} != {identity.home_team, identity.away_team}:
                return False
        else:
            if self.source_event_names.isdisjoint({identity.display_name}):
                return False

        if self.start_time and identity.start_time:
            delta = abs((self.start_time - identity.start_time).total_seconds()) / 60
            if delta > tolerance_minutes:
                return False

        return True

    def add_record(self, index: int, record: OddsRecord, identity: EventIdentity) -> None:
        self.records.append(index)
        self.providers.add(record.provider)
        self.source_event_names.add(record.event_name)
        if self.start_time is None and identity.start_time is not None:
            self.start_time = identity.start_time
        if identity.home_team and identity.away_team:
            if self.home_team is None or self.away_team is None:
                self.home_team = identity.home_team
                self.away_team = identity.away_team
                self.order_known = identity.order_known
            elif identity.order_known and not self.order_known:
                # A record with a known ordering (e.g. "away at home") is more
                # authoritative than an assumed one (e.g. "home vs away").
                # Upgrade so aggregation uses the correct home/away slots.
                self.home_team = identity.home_team
                self.away_team = identity.away_team
                self.order_known = True

    def to_public(self) -> CanonicalEventGroup:
        event_start = None
        if self.start_time is not None:
            event_start = self.start_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return CanonicalEventGroup(
            canonical_event_id=self.canonical_event_id,
            league=self.league,
            sport=self.sport,
            home_team=self.home_team,
            away_team=self.away_team,
            event_start=event_start,
            source_event_names=sorted(self.source_event_names),
            providers=sorted(self.providers),
            record_count=len(self.records),
            provider_count=len(self.providers),
            records=list(self.records),
        )


def _build_canonical_event_id(identity: EventIdentity) -> str:
    start_key = "unknown-start"
    if identity.start_time is not None:
        start_key = identity.start_time.strftime("%Y%m%dT%H%M")
    home = identity.home_team or "unknown-home"
    away = identity.away_team or "unknown-away"
    spread_key = f"|spread{identity.spread:+.1f}" if identity.spread is not None else ""
    return f"{identity.league}|{start_key}|{home}|{away}{spread_key}"
