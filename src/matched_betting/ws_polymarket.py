"""
ws_polymarket.py
----------------
Asyncio WebSocket client for the Polymarket CLOB market channel.

Endpoint: wss://ws-subscriptions-clob.polymarket.com/ws/market
Protocol: custom JSON — subscribe with asset token IDs, receive price_change events.
Auth:      none (public channel)
Proxy:     optional SOCKS5 (routes through VPN bridge at 127.0.0.1:1082)

On each price_change event the best_bid / best_ask fields are used to derive
back odds and written directly into the shared OddsCache.

  back_odds = 1.0 / best_ask   (you pay ask price to back the outcome)

A PING must be sent every 10 seconds or the server closes the connection.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any

from matched_betting.odds_cache import OddsCache

log = logging.getLogger(__name__)

_PING_INTERVAL_S = 10.0
_RECONNECT_DELAY_S = 5.0
_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class PolymarketWSClient:
    """Subscribe to Polymarket CLOB price updates for a fixed set of token IDs.

    token_map: {token_id -> (game_id, slot)}
        slot is "team1", "team2", "draw", "over", or "under"
    """

    def __init__(
        self,
        cache: OddsCache,
        token_map: dict[str, tuple[str, str]],
        proxy_url: str | None = None,
        ws_url: str = _WS_URL,
    ) -> None:
        self.cache     = cache
        self.token_map = token_map
        self.proxy_url = proxy_url
        self.ws_url    = ws_url
        self._running  = False

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect, subscribe, and receive indefinitely. Reconnects on error."""
        self._running = True
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("polymarket ws: %s — reconnecting in %.0fs", exc, _RECONNECT_DELAY_S)
                await asyncio.sleep(_RECONNECT_DELAY_S)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _session(self) -> None:
        import websockets

        ws = await _open_ws(self.ws_url, self.proxy_url)
        log.info("polymarket ws: connected (%d tokens)", len(self.token_map))
        try:
            sub = json.dumps({
                "type":        "market",
                "assets_ids":  list(self.token_map.keys()),
                "initial_dump": True,
            })
            await ws.send(sub)

            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    self._handle(raw)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass
        finally:
            await ws.close()
            log.info("polymarket ws: disconnected")

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(_PING_INTERVAL_S)
            try:
                await ws.send("PING")
            except Exception:
                break

    def _handle(self, raw: str) -> None:
        if raw == "PONG":
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return

        if msg.get("event_type") != "price_change":
            return

        for pc in msg.get("price_changes", []):
            token_id = pc.get("asset_id")
            mapping  = self.token_map.get(token_id)
            if not mapping:
                continue
            gid, slot = mapping

            ask_str = pc.get("best_ask")
            bid_str = pc.get("best_bid")

            try:
                best_ask = float(ask_str) if ask_str else None
                best_bid = float(bid_str) if bid_str else None
            except (ValueError, TypeError):
                continue

            # Back = buy YES token at ask price; back odds = 1 / ask
            back_odds = (1.0 / best_ask) if best_ask and 0.0 < best_ask < 1.0 else None
            # Lay = sell YES token; lay odds derived from bid (what the market will pay)
            lay_odds  = (1.0 / best_bid) if best_bid and 0.0 < best_bid < 1.0 else None

            self.cache.update_back_and_lay_odds(gid, "polymarket", slot, back_odds, lay_odds)
            log.debug("polymarket ws: %s %s back=%.4f lay=%.4f",
                      gid, slot,
                      back_odds or 0.0, lay_odds or 0.0)


# ---------------------------------------------------------------------------
# Helper: build token_map from the games payload
# ---------------------------------------------------------------------------

def build_token_map(games: list[dict]) -> dict[str, tuple[str, str]]:
    """Return {token_id: (game_id, slot)} for all Polymarket tokens in the payload."""
    from matched_betting.odds_cache import game_id as _gid

    token_map: dict[str, tuple[str, str]] = {}
    slot_keys = [
        ("polymarket_team1_clob_token_id",  "team1"),
        ("polymarket_team2_clob_token_id",  "team2"),
        ("polymarket_draw_clob_token_id",   "draw"),
        ("polymarket_over_clob_token_id",   "over"),
        ("polymarket_under_clob_token_id",  "under"),
    ]
    for game in games:
        gid = _gid(game)
        for key, slot in slot_keys:
            token_id = game.get(key)
            if token_id:
                token_map[token_id] = (gid, slot)
    return token_map


# ---------------------------------------------------------------------------
# SOCKS5 proxy helper
# ---------------------------------------------------------------------------

async def _open_ws(uri: str, proxy_url: str | None):
    import websockets
    from urllib.parse import urlparse

    if not proxy_url:
        return await websockets.connect(uri)

    parsed    = urlparse(uri)
    dest_host = parsed.hostname
    dest_port = parsed.port or (443 if parsed.scheme == "wss" else 80)

    from python_socks.async_.asyncio import Proxy
    proxy = Proxy.from_url(proxy_url)
    raw_sock = await proxy.connect(dest_host=dest_host, dest_port=dest_port)

    ssl_ctx = ssl.create_default_context() if parsed.scheme == "wss" else None
    return await websockets.connect(uri, sock=raw_sock, ssl=ssl_ctx)
