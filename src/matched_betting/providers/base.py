from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from matched_betting.debug import DebugLogger, noop_debug
from matched_betting.models import ProviderPayload

# A single entry from _aggregated_games.json, passed to targeted fetch methods.
GameContext = dict[str, Any]


class ProviderNotReadyError(RuntimeError):
    """Raised when a provider cannot run with the current local configuration."""


class OddsProvider(ABC):
    name: str

    def __init__(self, debug_logger: DebugLogger | None = None) -> None:
        self.debug = debug_logger or noop_debug

    @abstractmethod
    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        raise NotImplementedError

    def fetch_odds_by_ids(
        self,
        game_contexts: list[GameContext],
        leagues: list[str],
    ) -> ProviderPayload:
        """Targeted fetch using stored market/event IDs from a previous run.

        Each provider overrides this with a direct per-market API call, skipping
        full league discovery and pagination.  This default falls back to full
        discovery so providers that have not yet implemented targeted fetch still
        work correctly.
        """
        return self.fetch_odds(leagues)
