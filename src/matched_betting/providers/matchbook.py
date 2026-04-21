from __future__ import annotations

from typing import Any

from matched_betting.config import MatchbookSettings
from matched_betting.http import HttpClient
from matched_betting.debug import DebugLogger
from matched_betting.models import OddsRecord, ProviderPayload, utc_now_iso
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.base import GameContext, OddsProvider, ProviderNotReadyError


class MatchbookProvider(OddsProvider):
    name = "matchbook"

    def __init__(
        self,
        settings: MatchbookSettings,
        http_client: HttpClient,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        super().__init__(debug_logger)
        self.settings = settings
        self.http_client = http_client
        self._session_token: str | None = None
        self._login_lock = __import__("threading").Lock()

    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        if not (self.settings.username and self.settings.password):
            raise ProviderNotReadyError(
                "Matchbook credentials are missing. Add MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD to .env."
            )

        retrieved_at = utc_now_iso()
        self._login()

        if any(lg in leagues for lg in ("ucl", "epl", "uel", "ipl", "seria")):
            self._log_available_sports()

        records: list[OddsRecord] = []
        warnings: list[str] = []

        for league in leagues:
            sport_id = LEAGUE_SPORT_IDS[league]
            self.debug(f"{self.name}: fetching events for {league} (sport-id={sport_id})")
            events = self._iter_events(sport_id=sport_id)
            self.debug(f"{self.name}: fetched {len(events)} raw events for {league}")
            league_events = [event for event in events if self._event_matches_league(event, league)]
            self.debug(f"{self.name}: {league} has {len(league_events)} matching events to process")
            matched_events = 0
            for event_index, event in enumerate(league_events, start=1):
                matched_events += 1
                self.debug(
                    f"{self.name}: {league} event {event_index}/{len(league_events)} '{event.get('name', 'unknown')}'"
                )
                try:
                    event_records = self._event_to_records(event, league, retrieved_at)
                    records.extend(event_records)
                    self.debug(
                        f"{self.name}: {league} event '{event.get('name', 'unknown')}' -> {len(event_records)} records"
                    )
                except Exception as exc:
                    warnings.append(f"Skipped Matchbook event {event.get('id')}: {exc}")
                    self.debug(f"{self.name}: skipped event {event.get('id')}: {exc}")
            self.debug(f"{self.name}: matched {matched_events} {league} events")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def fetch_odds_by_ids(
        self,
        game_contexts: list[GameContext],
        leagues: list[str],
    ) -> ProviderPayload:
        if not (self.settings.username and self.settings.password):
            raise ProviderNotReadyError(
                "Matchbook credentials are missing. Add MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD to .env."
            )
        retrieved_at = utc_now_iso()
        self._login()

        records: list[OddsRecord] = []
        warnings: list[str] = []

        for game in game_contexts:
            event_id = game.get("matchbook_event_id")
            league = game.get("league")
            if not event_id or league not in leagues:
                continue
            self.debug(f"{self.name}: targeted fetch event_id={event_id} league={league}")
            try:
                event_data = self.http_client.get_json(
                    f"{self.settings.base_url}/edge/rest/events/{event_id}",
                    headers={"Accept": "application/json"},
                )
                event_records = self._event_to_records(event_data, league, retrieved_at)
                records.extend(event_records)
                self.debug(
                    f"{self.name}: targeted fetch event_id={event_id} -> {len(event_records)} records"
                )
            except Exception as exc:
                warnings.append(f"Skipped Matchbook event {event_id}: {exc}")
                self.debug(f"{self.name}: targeted fetch: skipped event {event_id}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def _login(self) -> str:
        with self._login_lock:
            if self._session_token:
                return self._session_token
            response = self.http_client.post_json(
                f"{self.settings.base_url}/bpapi/rest/security/session",
                payload={
                    "username": self.settings.username,
                    "password": self.settings.password,
                },
                headers={"Accept": "application/json"},
            )
            token = response.get("session-token")
            if not token:
                raise ProviderNotReadyError("Matchbook login succeeded without a session token.")
            self._session_token = str(token)
            self.debug(f"{self.name}: login successful")
            return self._session_token

    def _log_available_sports(self) -> None:
        try:
            payload = self.http_client.get_json(
                f"{self.settings.base_url}/edge/rest/sports",
                headers={"Accept": "application/json"},
            )
            sports = payload.get("sports", [])
            self.debug(f"{self.name}: available sports ({len(sports)} total):")
            for sport in sports:
                self.debug(f"{self.name}:   id={sport.get('id')}  name={sport.get('name')!r}  url-name={sport.get('url-name')!r}")
        except Exception as exc:
            self.debug(f"{self.name}: could not fetch sports list: {exc}")

    def _iter_events(self, *, sport_id: int) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        offset = 0
        per_page = 50
        while True:
            self.debug(f"{self.name}: requesting events page offset={offset} per-page={per_page}")
            payload = self.http_client.get_json(
                f"{self.settings.base_url}/edge/rest/events",
                params={
                    "sport-ids": sport_id,
                    "per-page": per_page,
                    "offset": offset,
                },
                headers={"Accept": "application/json"},
            )
            batch = payload.get("events", [])
            if not batch:
                break
            events.extend(batch)
            self.debug(f"{self.name}: received {len(batch)} events at offset={offset}")
            offset += len(batch)
            if len(batch) < per_page:
                break
        return events

    def _event_matches_league(self, event: dict[str, Any], league: str) -> bool:
        meta_tags = event.get("meta-tags", [])
        if league == "nba":
            return any(tag.get("url-name") == "nba" for tag in meta_tags)
        if league == "mlb":
            # Sport ID 3 already scopes the API response to baseball events, so
            # trust it rather than relying on meta-tag values which vary by market.
            return True
        if league == "nhl":
            # Sport ID 6 scopes the response to ice hockey; trust it.
            return True
        if league == "ucl":
            # TODO: verify the exact url-name Matchbook uses for the Champions League
            return any(tag.get("url-name") in ("champions-league", "ucl", "uefa-champions-league") for tag in meta_tags)
        if league == "epl":
            # ODO: verify the exact url-name Matchbook uses for the Premier League
            return any(tag.get("url-name") in ("premier-league", "epl", "english-premier-league") for tag in meta_tags)
        if league == "uel":
            # TODO: verify the exact url-name Matchbook uses for the Europa League
            return any(tag.get("url-name") in ("europa-league", "uel", "uefa-europa-league") for tag in meta_tags)
        if league == "seria":
            # TODO: verify the exact url-name Matchbook uses for Serie A
            return any(tag.get("url-name") in ("serie-a", "seria", "italian-serie-a", "italy-serie-a") for tag in meta_tags)
        if league == "ipl":
            # Sport ID 110 covers all cricket; filter down to IPL specifically.
            # Try meta-tags first (multiple known url-name variants across seasons).
            if any(
                tag.get("url-name") in (
                    "ipl", "indian-premier-league",
                    "ipl-2025", "ipl-2026",
                    "cricket-ipl", "ipl-t20", "t20-ipl",
                )
                for tag in meta_tags
            ):
                return True
            # Fallback: meta-tag url-names vary; check event name directly.
            event_name_lower = str(event.get("name") or "").lower()
            return "ipl" in event_name_lower or "indian premier league" in event_name_lower
        return False

    def _event_to_records(
        self,
        event: dict[str, Any],
        league: str,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        records: list[OddsRecord] = []
        open_markets = [market for market in event.get("markets", []) if market.get("status") == "open"]
        moneyline_markets = [m for m in open_markets if _is_moneyline_market(m)]
        self.debug(
            f"{self.name}: event_id={event.get('id')} has {len(open_markets)} open markets, "
            f"{len(moneyline_markets)} moneyline after filtering"
        )
        for market_index, market in enumerate(moneyline_markets, start=1):
            self.debug(
                f"{self.name}: event_id={event.get('id')} market {market_index}/{len(open_markets)} "
                f"'{market.get('name', 'unknown')}'"
            )
            market_name = str(market.get("name") or market.get("market-type") or "Unknown market")
            market_type = "three_way" if league in ("ucl", "epl", "uel", "seria") else "two_way"
            for runner in market.get("runners", []):
                best_by_side = _best_prices_per_side(runner.get("prices", []))
                for side, price in best_by_side.items():
                    decimal_odds = price.get("decimal-odds") or price.get("odds")
                    if decimal_odds is None:
                        continue
                    records.append(
                        OddsRecord(
                            provider=self.name,
                            sport=LEAGUE_TO_SPORT[league],
                            league=league,
                            event_name=str(event.get("name") or "Unknown event"),
                            event_start=event.get("start"),
                            market_name=market_name,
                            market_type=market_type,
                            selection_name=normalize_team_name(str(runner.get("name") or "Unknown selection"), league),
                            selection_side=side,
                            decimal_odds=float(decimal_odds),
                            implied_probability=round(1 / float(decimal_odds), 6),
                            currency=str(price.get("currency") or "GBP"),
                            source_market_id=str(market.get("id")),
                            source_event_id=str(event.get("id")),
                            retrieved_at=retrieved_at,
                            metadata={
                                "market_status": market.get("status"),
                                "available_amount": price.get("available-amount"),
                                "handicap": runner.get("handicap", market.get("handicap")),
                                "exchange_type": price.get("exchange-type"),
                                "last_price_update_time": runner.get("last-price-update-time"),
                            },
                        )
                    )
        return records


_MONEYLINE_MARKET_NAMES = {
    "match odds",               # Soccer / UCL and cricket
    "match winner",             # Alternative soccer naming
    "money line",
    "moneyline",
    "winner (incl. overtime)",
    "winner (including overtime)",
    "to win the match",         # Cricket (Matchbook)
    "match result",             # Cricket alternative
}


def _best_prices_per_side(prices: list[dict]) -> dict[str, dict]:
    """Return the single best price per side from a Matchbook runner's price ladder.

    For back: highest decimal odds (best for the backer).
    For lay:  lowest decimal odds (cheapest lay, best for the backer on the other side).
    """
    best: dict[str, dict] = {}
    for price in prices:
        side = str(price.get("side") or "back")
        raw_odds = price.get("decimal-odds") or price.get("odds")
        if raw_odds is None:
            continue
        try:
            odds = float(raw_odds)
        except (TypeError, ValueError):
            continue
        current = best.get(side)
        if current is None:
            best[side] = price
        else:
            current_odds = float(current.get("decimal-odds") or current.get("odds") or 0)
            if side == "back" and odds > current_odds:
                best[side] = price
            elif side == "lay" and odds < current_odds:
                best[side] = price
    return best


def _is_moneyline_market(market: dict) -> bool:
    name = str(market.get("name") or "").lower().strip()
    return name in _MONEYLINE_MARKET_NAMES


LEAGUE_SPORT_IDS = {
    "nba": 4,
    "mlb": 3,
    "nhl": 6,
    "ucl": 15,
    "epl": 15,  # Same sport ID as UCL (soccer)
    "uel": 15,  # Same sport ID (soccer)
    "seria": 15,  # Same sport ID (soccer)
    "ipl": 110,  # Cricket — sport ID 110 covers all cricket; filtered by meta-tag below
}

LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "mlb": "baseball",
    "nhl": "ice_hockey",
    "ucl": "soccer",
    "epl": "soccer",
    "uel": "soccer",
    "seria": "soccer",
    "ipl": "cricket",
}
