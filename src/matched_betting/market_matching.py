from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from matched_betting.debug import DebugLogger, noop_debug
from matched_betting.event_matching import CanonicalEventGroup, infer_event_identity, normalize_team_name
from matched_betting.models import OddsRecord


TOKEN_TITLE_OVERRIDES = {
    "la": "LA",
    "okc": "OKC",
    "mlb": "MLB",
    "nba": "NBA",
}


@dataclass(frozen=True)
class BetIdentity:
    canonical_event_id: str
    market_family: str
    market_scope: str
    line: str | None
    normalized_selection: str
    display_market_name: str
    display_selection_name: str


@dataclass
class CanonicalBetGroup:
    canonical_bet_id: str
    canonical_event_id: str
    canonical_bet_name: str
    market_family: str
    market_scope: str
    line: str | None
    selection_name: str
    providers: list[str]
    record_count: int
    source_market_names: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "canonical_bet_id": self.canonical_bet_id,
            "canonical_event_id": self.canonical_event_id,
            "canonical_bet_name": self.canonical_bet_name,
            "market_family": self.market_family,
            "market_scope": self.market_scope,
            "line": self.line,
            "selection_name": self.selection_name,
            "providers": self.providers,
            "record_count": self.record_count,
            "source_market_names": self.source_market_names,
        }


def match_records_to_canonical_bets(
    records: list[OddsRecord],
    canonical_event_assignment: dict[int, str],
    canonical_events: list[CanonicalEventGroup],
    *,
    debug_logger: DebugLogger | None = None,
) -> tuple[dict[int, str], list[CanonicalBetGroup]]:
    debug = debug_logger or noop_debug
    events_by_id = {event.canonical_event_id: event for event in canonical_events}

    assignment: dict[int, str] = {}
    groups: dict[str, _MutableBetGroup] = {}

    for index, record in enumerate(records):
        canonical_event_id = canonical_event_assignment[index]
        event_group = events_by_id[canonical_event_id]
        identity = infer_bet_identity(record, canonical_event_id, event_group)
        canonical_bet_id = _build_canonical_bet_id(identity)
        if canonical_bet_id not in groups:
            groups[canonical_bet_id] = _MutableBetGroup.from_identity(identity, event_group)
            debug(
                f"bet-matcher: created canonical bet {canonical_bet_id} from "
                f"'{record.market_name}' / '{record.selection_name}'"
            )
        groups[canonical_bet_id].add_record(record)
        assignment[index] = canonical_bet_id

    finalized = [group.to_public() for group in groups.values()]
    finalized.sort(key=lambda item: (item.canonical_event_id, item.canonical_bet_name))
    debug(f"bet-matcher: built {len(finalized)} canonical bets from {len(records)} records")
    return assignment, finalized


def is_game_win_loss_record(record: OddsRecord) -> bool:
    if not _is_moneyline_market(record.market_name.lower(), record.market_type.lower()):
        return False
    selection = normalize_team_name(record.selection_name, record.league)
    if selection in ("draw", "tie"):
        return True
    identity = infer_event_identity(record)
    if not identity.home_team or not identity.away_team:
        return False
    return selection in {identity.home_team, identity.away_team}


def infer_bet_identity(
    record: OddsRecord,
    canonical_event_id: str,
    event_group: CanonicalEventGroup,
) -> BetIdentity:
    market_name = record.market_name.strip()
    selection_name = record.selection_name.strip()
    market_type = record.market_type.lower()
    market_name_lower = market_name.lower()

    if _is_moneyline_market(market_name_lower, market_type):
        normalized_selection = normalize_team_name(selection_name, record.league)
        display_selection = _pretty_name(normalized_selection)
        return BetIdentity(
            canonical_event_id=canonical_event_id,
            market_family="moneyline",
            market_scope="match",
            line=None,
            normalized_selection=normalized_selection,
            display_market_name="Moneyline",
            display_selection_name=display_selection,
        )

    if "handicap" in market_type or "handicap" in market_name_lower:
        handicap = _extract_handicap(record)
        normalized_selection = _extract_selection_team(record, event_group)
        display_selection = _pretty_name(normalized_selection)
        line = _format_line(handicap)
        market_display = f"Handicap {line}" if line is not None else "Handicap"
        return BetIdentity(
            canonical_event_id=canonical_event_id,
            market_family="handicap",
            market_scope="match",
            line=line,
            normalized_selection=f"{normalized_selection}|{line or 'unknown-line'}",
            display_market_name=market_display,
            display_selection_name=display_selection,
        )

    if "total" in market_type or "total" in market_name_lower or "over/under" in market_name_lower:
        side = _extract_total_side(selection_name)
        line = _extract_total_line(record)
        display_selection = side.title() if side else _pretty_name(selection_name)
        market_display = f"Total {line}" if line is not None else "Total"
        return BetIdentity(
            canonical_event_id=canonical_event_id,
            market_family="total",
            market_scope="match",
            line=line,
            normalized_selection=f"{side or selection_name.lower()}|{line or 'unknown-line'}",
            display_market_name=market_display,
            display_selection_name=display_selection,
        )

    normalized_selection = _simple_normalize(selection_name)
    return BetIdentity(
        canonical_event_id=canonical_event_id,
        market_family="other",
        market_scope="match",
        line=None,
        normalized_selection=f"{_simple_normalize(market_name)}|{normalized_selection}",
        display_market_name=market_name,
        display_selection_name=selection_name,
    )


def _is_moneyline_market(market_name_lower: str, market_type: str) -> bool:
    return (
        "moneyline" in market_name_lower
        or "match odds" in market_name_lower
        or "match winner" in market_name_lower
        or "full-time result" in market_name_lower
        or market_name_lower.startswith("winner")
        or market_type in {"money_line", "winner_2_way", "winner_3_way", "one_x_two", "two_way", "three_way"}
    )


def _extract_handicap(record: OddsRecord) -> float | None:
    handicap = record.metadata.get("handicap")
    if handicap is not None:
        try:
            return float(handicap)
        except (TypeError, ValueError):
            pass

    market_param = record.metadata.get("market_param")
    if market_param is not None:
        try:
            return float(market_param)
        except (TypeError, ValueError):
            pass

    for text in (record.selection_name, record.market_name):
        match = re.search(r"([+-]?\d+(?:\.\d+)?)", text)
        if match:
            return float(match.group(1))
    return None


def _extract_selection_team(record: OddsRecord, event_group: CanonicalEventGroup) -> str:
    selection = normalize_team_name(record.selection_name, record.league)
    if selection and selection not in {"over", "under"}:
        return selection

    market_name_lower = record.market_name.lower()
    if event_group.home_team and event_group.home_team in market_name_lower:
        return event_group.home_team
    if event_group.away_team and event_group.away_team in market_name_lower:
        return event_group.away_team
    return selection


def _extract_total_side(selection_name: str) -> str | None:
    lowered = selection_name.lower()
    if "over" in lowered:
        return "over"
    if "under" in lowered:
        return "under"
    return None


def _extract_total_line(record: OddsRecord) -> str | None:
    handicap = record.metadata.get("handicap")
    if handicap is not None:
        return _format_line(float(handicap))

    market_param = record.metadata.get("market_param")
    if market_param is not None:
        try:
            return _format_line(float(market_param))
        except (TypeError, ValueError):
            pass

    for text in (record.selection_name, record.market_name):
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if match:
            return _format_line(float(match.group(1)))
    return None


def _build_canonical_bet_id(identity: BetIdentity) -> str:
    suffix = identity.normalized_selection
    return (
        f"{identity.canonical_event_id}|{identity.market_family}|{identity.market_scope}|"
        f"{identity.line or 'no-line'}|{suffix}"
    )


def _simple_normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", text.lower())).strip()


def _pretty_name(text: str) -> str:
    parts = text.split()
    titled: list[str] = []
    for part in parts:
        if part in TOKEN_TITLE_OVERRIDES:
            titled.append(TOKEN_TITLE_OVERRIDES[part])
        elif any(char.isdigit() for char in part):
            titled.append(part)
        else:
            titled.append(part.capitalize())
    return " ".join(titled)


def _format_line(value: float | None) -> str | None:
    if value is None:
        return None
    if value.is_integer():
        formatted = f"{int(value)}.0"
    else:
        formatted = f"{value:.1f}".rstrip("0").rstrip(".")
    if value > 0:
        return f"+{formatted}"
    if value == 0:
        return "0.0"
    return formatted


class _MutableBetGroup:
    def __init__(
        self,
        *,
        canonical_bet_id: str,
        canonical_event_id: str,
        canonical_bet_name: str,
        market_family: str,
        market_scope: str,
        line: str | None,
        selection_name: str,
    ) -> None:
        self.canonical_bet_id = canonical_bet_id
        self.canonical_event_id = canonical_event_id
        self.canonical_bet_name = canonical_bet_name
        self.market_family = market_family
        self.market_scope = market_scope
        self.line = line
        self.selection_name = selection_name
        self.providers: set[str] = set()
        self.source_market_names: set[str] = set()
        self.record_count = 0

    @classmethod
    def from_identity(
        cls,
        identity: BetIdentity,
        event_group: CanonicalEventGroup,
    ) -> "_MutableBetGroup":
        event_name = _display_event_name(event_group)
        canonical_bet_name = (
            f"{event_name} | {identity.display_market_name} | {identity.display_selection_name}"
        )
        return cls(
            canonical_bet_id=_build_canonical_bet_id(identity),
            canonical_event_id=identity.canonical_event_id,
            canonical_bet_name=canonical_bet_name,
            market_family=identity.market_family,
            market_scope=identity.market_scope,
            line=identity.line,
            selection_name=identity.display_selection_name,
        )

    def add_record(self, record: OddsRecord) -> None:
        self.record_count += 1
        self.providers.add(record.provider)
        self.source_market_names.add(record.market_name)

    def to_public(self) -> CanonicalBetGroup:
        return CanonicalBetGroup(
            canonical_bet_id=self.canonical_bet_id,
            canonical_event_id=self.canonical_event_id,
            canonical_bet_name=self.canonical_bet_name,
            market_family=self.market_family,
            market_scope=self.market_scope,
            line=self.line,
            selection_name=self.selection_name,
            providers=sorted(self.providers),
            record_count=self.record_count,
            source_market_names=sorted(self.source_market_names),
        )


def _display_event_name(event_group: CanonicalEventGroup) -> str:
    if event_group.away_team and event_group.home_team:
        return f"{_pretty_name(event_group.away_team)} at {_pretty_name(event_group.home_team)}"
    if event_group.source_event_names:
        return event_group.source_event_names[0]
    return event_group.canonical_event_id
