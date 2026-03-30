from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decimal_from_probability(probability: float) -> float:
    if probability == None:
        return None
    
    elif probability <= 0 or probability > 1:
        raise ValueError(f"Probability must be in (0, 1], got {probability!r}")
    return round(1 / probability, 6)


@dataclass(frozen=True)
class OddsRecord:
    provider: str
    sport: str
    league: str
    event_name: str
    event_start: str | None
    market_name: str
    market_type: str
    selection_name: str
    selection_side: str
    decimal_odds: float
    implied_probability: float
    currency: str
    source_market_id: str
    source_event_id: str | None
    retrieved_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProviderPayload:
    provider: str
    records: list[OddsRecord]
    warnings: list[str] = field(default_factory=list)
