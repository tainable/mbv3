from __future__ import annotations

from typing import Any

from matched_betting.config import MatchbookSettings
from matched_betting.http import HttpClient
from matched_betting.debug import DebugLogger
from matched_betting.models import OddsRecord, ProviderPayload, utc_now_iso
from matched_betting.providers.base import OddsProvider, ProviderNotReadyError


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

    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        if not (self.settings.username and self.settings.password):
            raise ProviderNotReadyError(
                "Matchbook credentials are missing. Add MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD to .env."
            )

        retrieved_at = utc_now_iso()
        self.debug(f"{self.name}: logging in")
        self._login()
        self.debug(f"{self.name}: login successful")

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

    def _login(self) -> str:
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
        return str(token)

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
            return any(
                (tag.get("url-name") == "mlb") or (str(tag.get("name", "")).upper() == "MLB")
                for tag in meta_tags
            )
        return False

    def _event_to_records(
        self,
        event: dict[str, Any],
        league: str,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        records: list[OddsRecord] = []
        open_markets = [market for market in event.get("markets", []) if market.get("status") == "open"]
        self.debug(
            f"{self.name}: event_id={event.get('id')} has {len(open_markets)} open markets"
        )
        for market_index, market in enumerate(open_markets, start=1):
            self.debug(
                f"{self.name}: event_id={event.get('id')} market {market_index}/{len(open_markets)} "
                f"'{market.get('name', 'unknown')}'"
            )
            market_name = str(market.get("name") or market.get("market-type") or "Unknown market")
            market_type = str(market.get("market-type") or market.get("type") or "unknown")
            for runner in market.get("runners", []):
                for price_index, price in enumerate(runner.get("prices", [])):
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
                            selection_name=str(runner.get("name") or "Unknown selection"),
                            selection_side=str(price.get("side") or "back"),
                            decimal_odds=float(decimal_odds),
                            implied_probability=round(1 / float(decimal_odds), 6),
                            currency=str(price.get("currency") or "GBP"),
                            source_market_id=str(market.get("id")),
                            source_event_id=str(event.get("id")),
                            retrieved_at=retrieved_at,
                            metadata={
                                "market_status": market.get("status"),
                                "price_level": price_index,
                                "available_amount": price.get("available-amount"),
                                "handicap": runner.get("handicap", market.get("handicap")),
                                "exchange_type": price.get("exchange-type"),
                                "last_price_update_time": runner.get("last-price-update-time"),
                            },
                        )
                    )
        return records


LEAGUE_SPORT_IDS = {
    "nba": 4,
    "mlb": 3,
}

LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "mlb": "baseball",
}
