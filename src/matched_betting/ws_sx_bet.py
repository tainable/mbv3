"""
ws_sx_bet.py
------------
Asyncio WebSocket client for SX Bet real-time order book updates.

Uses the Centrifugo v4 JSON protocol over wss://realtime.sx.bet/connection/websocket.
Subscribes to one `order_book:market_{marketHash}` channel per game (per-market
subscriptions, not the noisy best_odds:global channel).

Auth flow:
  1. POST /user/realtime-token/api-key with x-api-key header → get Centrifugo token
  2. Connect WS, send {"id":1, "connect": {"token": TOKEN}}
  3. For each market hash: send {"id":N, "subscribe": {"channel": "order_book:market_HASH"}}
  4. Handle push publications; server pings answered with empty frame {}

Order book mechanics:
  SX Bet is a P2P exchange. Orders have:
    - percentageOdds: maker's probability * 10^20 (string)
    - isMakerBettingOutcomeOne: bool
    - totalBetSize / fillAmount: USDC with 6 decimal places (strings)
    - status: "ACTIVE" | "CANCELLED" | "FILLED"

  Best back odds for outcomeOne = 1 / (1 - best_maker_prob_for_non_o1_makers)
  Best back odds for outcomeTwo = 1 / (1 - best_maker_prob_for_o1_makers)

  The slot mapping (outcomeOne → team1 or team2) is stored in the game dict as
  sx_bet_outcome_one_team (the normalised team name that is outcomeOne).
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from typing import Any

import requests

from matched_betting.odds_cache import OddsCache

log = logging.getLogger(__name__)

_RECONNECT_DELAY_S  = 5.0
_PING_REPLY_TIMEOUT = 30.0   # if no server frame in this long, reconnect
_ODDS_SCALE         = 10 ** 20


# ---------------------------------------------------------------------------
# Auth token fetch
# ---------------------------------------------------------------------------

def fetch_realtime_token(api_key: str, token_url: str, proxy_url: str | None = None) -> str:
    """Exchange an API key for a short-lived Centrifugo JWT."""
    proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None
    resp = requests.get(
        token_url,
        headers={"x-api-key": api_key},
        proxies=proxies,
        timeout=10,
    )
    resp.raise_for_status()
    token = resp.json().get("data", {}).get("token")
    if not token:
        raise ValueError(f"No token in response: {resp.text[:200]}")
    return token


# ---------------------------------------------------------------------------
# Order book: track active orders per market hash, compute best odds
# ---------------------------------------------------------------------------

class _OrderBook:
    """In-memory order book for one SX Bet market."""

    def __init__(self) -> None:
        # order_hash -> order dict
        self._orders: dict[str, dict[str, Any]] = {}

    def apply(self, orders: list[dict[str, Any]]) -> None:
        """Apply a list of order updates (initial snapshot or delta)."""
        for order in orders:
            h = order.get("orderHash")
            if not h:
                continue
            status = (order.get("status") or "").upper()
            if status in ("CANCELLED", "FILLED"):
                self._orders.pop(h, None)
            else:
                self._orders[h] = order

    def best_odds(self) -> tuple[float | None, float | None]:
        """Return (best_back_outcome_one, best_back_outcome_two) as decimal odds.

        outcomeOne backers are matched against makers where isMakerBettingOutcomeOne=False.
        outcomeTwo backers are matched against makers where isMakerBettingOutcomeOne=True.
        """
        o1_probs: list[float] = []
        o2_probs: list[float] = []

        for order in self._orders.values():
            raw_pct = order.get("percentageOdds")
            if raw_pct is None:
                continue
            try:
                maker_prob = int(raw_pct) / _ODDS_SCALE
            except (TypeError, ValueError):
                continue
            if not (0.0 < maker_prob < 1.0):
                continue

            # Only count orders with remaining liquidity
            try:
                remaining = int(order.get("totalBetSize", 0)) - int(order.get("fillAmount", 0))
            except (TypeError, ValueError):
                remaining = 1
            if remaining <= 0:
                continue

            if order.get("isMakerBettingOutcomeOne"):
                o2_probs.append(maker_prob)
            else:
                o1_probs.append(maker_prob)

        # Taker's implied probability = 1 - best (highest) maker probability
        o1_odds = round(1.0 / (1.0 - max(o1_probs)), 6) if o1_probs else None
        o2_odds = round(1.0 / (1.0 - max(o2_probs)), 6) if o2_probs else None
        return o1_odds, o2_odds

    def total_liquidity(self) -> tuple[float, float]:
        """Return (outcome_one_avail_usd, outcome_two_avail_usd) taker stakes."""
        o1_avail = 0.0
        o2_avail = 0.0
        for order in self._orders.values():
            try:
                raw_pct   = order.get("percentageOdds")
                maker_prob = int(raw_pct) / _ODDS_SCALE if raw_pct else 0.0
                if not (0.0 < maker_prob < 1.0):
                    continue
                maker_avail = (
                    int(order.get("totalBetSize", 0)) - int(order.get("fillAmount", 0))
                ) / 1e6
                taker_avail = maker_avail * (1.0 - maker_prob) / maker_prob
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if order.get("isMakerBettingOutcomeOne"):
                o2_avail += taker_avail
            else:
                o1_avail += taker_avail
        return round(o1_avail, 2), round(o2_avail, 2)


# ---------------------------------------------------------------------------
# Per-market subscription context
# ---------------------------------------------------------------------------

class _MarketCtx:
    """Everything stream.py needs to map a market hash back to the odds cache."""

    __slots__ = ("game_id", "o1_slot", "o2_slot", "book")

    def __init__(self, game_id: str, o1_slot: str, o2_slot: str) -> None:
        self.game_id = game_id
        self.o1_slot = o1_slot   # "team1" or "team2"
        self.o2_slot = o2_slot   # the other one
        self.book    = _OrderBook()


# ---------------------------------------------------------------------------
# Main WS client
# ---------------------------------------------------------------------------

class SxBetWSClient:
    """Subscribe to per-market order book channels on SX Bet via Centrifugo.

    market_map: {market_hash -> _MarketCtx}
    """

    def __init__(
        self,
        cache: OddsCache,
        market_map: dict[str, _MarketCtx],
        api_key: str,
        token_url: str = "https://api.sx.bet/user/realtime-token/api-key",
        ws_url: str = "wss://realtime.sx.bet/connection/websocket",
        proxy_url: str | None = None,
    ) -> None:
        self.cache      = cache
        self.market_map = market_map
        self.api_key    = api_key
        self.token_url  = token_url
        self.ws_url     = ws_url
        self.proxy_url  = proxy_url
        self._running   = False

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self._session()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log.error("sx_bet ws: %s — reconnecting in %.0fs", exc, _RECONNECT_DELAY_S)
                await asyncio.sleep(_RECONNECT_DELAY_S)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Session: connect → subscribe all markets → receive loop
    # ------------------------------------------------------------------

    async def _session(self) -> None:
        token = await asyncio.get_event_loop().run_in_executor(
            None, fetch_realtime_token, self.api_key, self.token_url, self.proxy_url
        )

        ws = await _open_ws(self.ws_url, self.proxy_url)
        log.info("sx_bet ws: connected (%d markets)", len(self.market_map))
        try:
            await self._centrifugo_connect(ws, token)
            await self._subscribe_all(ws)
            await self._receive_loop(ws)
        finally:
            await ws.close()
            log.info("sx_bet ws: disconnected")

    # ------------------------------------------------------------------
    # Centrifugo protocol
    # ------------------------------------------------------------------

    async def _centrifugo_connect(self, ws: Any, token: str) -> None:
        await ws.send(json.dumps({"id": 1, "connect": {"token": token, "name": "matched_betting"}}))
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
        if "error" in reply:
            raise ConnectionError(f"Centrifugo connect error: {reply['error']}")
        log.debug("sx_bet ws: centrifugo connected — client=%s",
                  reply.get("connect", {}).get("client", "?"))

    async def _subscribe_all(self, ws: Any) -> None:
        for req_id, market_hash in enumerate(self.market_map, start=2):
            channel = f"order_book:market_{market_hash}"
            await ws.send(json.dumps({"id": req_id, "subscribe": {"channel": channel}}))

        # Drain the subscribe acknowledgements and apply any initial snapshots
        pending = len(self.market_map)
        deadline = asyncio.get_event_loop().time() + 15.0
        while pending > 0:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                log.warning("sx_bet ws: timed out waiting for %d subscribe acks", pending)
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            msg = json.loads(raw)
            # Subscribe ack contains initial history in msg["subscribe"]["data"]
            if "subscribe" in msg:
                sub_data = msg.get("subscribe", {})
                history  = sub_data.get("data") or []
                # history is a list of pub objects; each pub has {"data": {"orders": [...]}}
                for pub in history:
                    self._apply_pub(pub)
                pending -= 1
            # Could also receive early publications during subscribe drain — handle them too
            elif "push" in msg:
                self._handle_push(msg["push"])

        log.info("sx_bet ws: subscribed to %d markets", len(self.market_map))

    async def _receive_loop(self, ws: Any) -> None:
        async for raw in ws:
            if not raw:
                continue
            msg = json.loads(raw)
            # Server keepalive ping: empty JSON object {}
            if not msg:
                await ws.send("{}")
                continue
            if "push" in msg:
                self._handle_push(msg["push"])

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_push(self, push: dict[str, Any]) -> None:
        channel = push.get("channel", "")
        pub     = push.get("pub") or {}
        if not channel.startswith("order_book:market_"):
            return
        market_hash = channel[len("order_book:market_"):]
        self._apply_pub(pub, market_hash=market_hash)

    def _apply_pub(self, pub: dict[str, Any], market_hash: str | None = None) -> None:
        data = pub.get("data") or {}
        orders = data.get("orders") or data.get("order") or []
        if isinstance(orders, dict):
            orders = [orders]
        if not orders:
            return

        # Infer market_hash from orders if not provided (initial snapshot path)
        if market_hash is None:
            market_hash = orders[0].get("marketHash") if orders else None
        if not market_hash:
            return

        ctx = self.market_map.get(market_hash)
        if ctx is None:
            return

        ctx.book.apply(orders)
        o1_odds, o2_odds = ctx.book.best_odds()
        o1_avail, o2_avail = ctx.book.total_liquidity()

        self.cache.update_back_odds(ctx.game_id, "sx_bet", ctx.o1_slot, o1_odds, o1_avail)
        self.cache.update_back_odds(ctx.game_id, "sx_bet", ctx.o2_slot, o2_odds, o2_avail)

        log.debug("sx_bet ws: %s %s=%.4f %s=%.4f",
                  ctx.game_id,
                  ctx.o1_slot, o1_odds or 0.0,
                  ctx.o2_slot, o2_odds or 0.0)


# ---------------------------------------------------------------------------
# Helper: build market_map from the games payload
# ---------------------------------------------------------------------------

def build_market_map(games: list[dict]) -> dict[str, _MarketCtx]:
    """Return {market_hash: _MarketCtx} for all SX Bet markets in the payload.

    Handles both two-way markets (sx_bet_market_hash) and soccer per-outcome
    markets (sx_bet_team1_market_hash etc.).
    """
    from matched_betting.normalization import normalize_team_name
    from matched_betting.odds_cache import game_id as _gid

    market_map: dict[str, _MarketCtx] = {}

    for game in games:
        gid    = _gid(game)
        league = game.get("league", "")

        # Two-way market (NBA, MLB, WNBA, KBO, NHL …)
        mh = game.get("sx_bet_market_hash")
        if mh:
            # outcomeOne team is stored as the normalised name; compare to determine slots
            o1_team_norm = game.get("sx_bet_outcome_one_team") or ""
            t1_norm      = normalize_team_name(game.get("team1") or "", league)
            if o1_team_norm and o1_team_norm == t1_norm:
                o1_slot, o2_slot = "team1", "team2"
            else:
                o1_slot, o2_slot = "team2", "team1"
            market_map[mh] = _MarketCtx(gid, o1_slot, o2_slot)

        # Soccer per-outcome binary markets (team1-win market, draw market, team2-win market)
        for slot, key in (
            ("team1", "sx_bet_team1_market_hash"),
            ("draw",  "sx_bet_draw_market_hash"),
            ("team2", "sx_bet_team2_market_hash"),
        ):
            h = game.get(key)
            if h:
                # For binary YES/NO markets: outcomeOne = YES (the named outcome)
                # Back the outcome → matched against NO makers (isMakerBettingOutcomeOne=False)
                # o1_slot = the named outcome, o2_slot = "no" (ignored for arb purposes)
                market_map[h] = _MarketCtx(gid, slot, f"{slot}_no")

    return market_map


# ---------------------------------------------------------------------------
# SOCKS5 proxy helper (shared with ws_polymarket)
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
    url      = proxy_url.replace("socks5h://", "socks5://")
    proxy    = Proxy.from_url(url)
    raw_sock = await proxy.connect(dest_host=dest_host, dest_port=dest_port)
    ssl_ctx  = ssl.create_default_context() if parsed.scheme == "wss" else None
    return await websockets.connect(uri, sock=raw_sock, ssl=ssl_ctx)
