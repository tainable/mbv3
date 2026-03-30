from __future__ import annotations

from abc import ABC, abstractmethod

from matched_betting.debug import DebugLogger, noop_debug
from matched_betting.models import ProviderPayload


class ProviderNotReadyError(RuntimeError):
    """Raised when a provider cannot run with the current local configuration."""


class OddsProvider(ABC):
    name: str

    def __init__(self, debug_logger: DebugLogger | None = None) -> None:
        self.debug = debug_logger or noop_debug

    @abstractmethod
    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        raise NotImplementedError
