from __future__ import annotations

from matched_betting.config import Settings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.providers.base import OddsProvider
from matched_betting.providers.matchbook import MatchbookProvider
from matched_betting.providers.polymarket import PolymarketProvider
from matched_betting.providers.smarkets import SmarketsProvider
from matched_betting.providers.sx_bet import SxBetProvider


def build_provider_registry(
    settings: Settings,
    http_client: HttpClient,
    debug_logger: DebugLogger | None = None,
) -> dict[str, OddsProvider]:
    return {
        "matchbook": MatchbookProvider(settings.matchbook, http_client, debug_logger),
        "smarkets": SmarketsProvider(settings.smarkets, http_client, debug_logger),
        "polymarket": PolymarketProvider(settings.polymarket, http_client, debug_logger),
        "sx_bet": SxBetProvider(settings.sx_bet, http_client, debug_logger),
    }
