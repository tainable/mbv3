from __future__ import annotations

from matched_betting.config import Settings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.providers.base import OddsProvider
from matched_betting.providers.azuro import AzuroProvider
from matched_betting.providers.matchbook import MatchbookProvider
from matched_betting.providers.polymarket import PolymarketProvider
from matched_betting.providers.smarkets import SmarketsProvider
from matched_betting.providers.sx_bet import SxBetProvider


def build_provider_registry(
    settings: Settings,
    http_client: HttpClient,
    debug_logger: DebugLogger | None = None,
    proxied_http_client: HttpClient | None = None,
) -> dict[str, OddsProvider]:
    # Providers that require a VPN/proxy use proxied_http_client when supplied.
    # Matchbook, Smarkets, and Azuro always use the direct client.
    vpn_client = proxied_http_client if proxied_http_client is not None else http_client
    return {
        "matchbook": MatchbookProvider(settings.matchbook, http_client, debug_logger),
        "smarkets":  SmarketsProvider(settings.smarkets,  http_client, debug_logger),
        "polymarket": PolymarketProvider(settings.polymarket, vpn_client, debug_logger),
        "sx_bet":    SxBetProvider(settings.sx_bet,    vpn_client, debug_logger),
        "azuro":     AzuroProvider(settings.azuro,     http_client, debug_logger),
    }
