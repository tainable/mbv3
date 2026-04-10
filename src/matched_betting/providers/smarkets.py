from __future__ import annotations

import time
import random 

from typing import Any
from urllib.error import HTTPError

from matched_betting.config import SmarketsSettings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.models import OddsRecord, ProviderPayload, decimal_from_probability, utc_now_iso
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.base import GameContext, OddsProvider


class SmarketsProvider(OddsProvider):
    name = "smarkets"

    def __init__(
        self,
        settings: SmarketsSettings,
        http_client: HttpClient,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        super().__init__(debug_logger)
        self.settings = settings
        self.http_client = http_client

    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        retrieved_at = utc_now_iso()
        contract_cache: dict[str, dict[str, Any]] = {}
        quotes_cache: dict[str, dict[str, Any]] = {}

        records: list[OddsRecord] = []
        warnings: list[str] = []

        for league in leagues:
            competition_id = LEAGUE_ROOT_EVENT_IDS.get(league)
            if competition_id is None:
                warnings.append(f"No Smarkets root event ID configured for {league} — skipping")
                self.debug(f"{self.name}: skipping {league} (no root event ID configured)")
                continue
            try:
                self.debug(f"{self.name}: fetching child events for {league} (event-id={competition_id})")
                game_events = self._fetch_children(competition_id)
                self.debug(f"{self.name}: fetched {len(game_events)} child events for {league}")
            except Exception as exc:
                warnings.append(f"Failed to fetch Smarkets {league} events: {exc}")
                self.debug(f"{self.name}: failed to fetch {league} events: {exc}")
                continue

            bettable_events = [event for event in game_events if event.get("bettable")]
            self.debug(f"{self.name}: {league} has {len(bettable_events)} bettable events to process")
            for event_index, event in enumerate(bettable_events, start=1):
                self.debug(
                    f"{self.name}: {league} event {event_index}/{len(bettable_events)} '{event.get('name', 'unknown')}'"
                )
                try:
                    event_records, event_warnings = self._event_to_records(
                        event,
                        league,
                        retrieved_at,
                        contract_cache,
                        quotes_cache,
                    )
                    records.extend(event_records)
                    warnings.extend(event_warnings)
                    self.debug(
                        f"{self.name}: {league} event '{event.get('name', 'unknown')}' -> {len(event_records)} records"
                    )
                except Exception as exc:
                    warnings.append(f"Skipped Smarkets event {event.get('id')}: {exc}")
                    self.debug(f"{self.name}: skipped event {event.get('id')}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def fetch_odds_by_ids(
        self,
        game_contexts: list[GameContext],
        leagues: list[str],
    ) -> ProviderPayload:
        retrieved_at = utc_now_iso()
        contract_cache: dict[str, dict[str, Any]] = {}
        quotes_cache: dict[str, dict[str, Any]] = {}
        records: list[OddsRecord] = []
        warnings: list[str] = []

        for game in game_contexts:
            market_id = game.get("smarkets_market_id")
            league = game.get("league")
            if not market_id or league not in leagues:
                continue
            event_name = f"{game.get('team1', '')} vs {game.get('team2', '')}"
            event_start = game.get("date_time")
            self.debug(f"{self.name}: targeted fetch market_id={market_id} league={league}")
            try:
                market_records, market_warnings = self._targeted_market_to_records(
                    market_id=market_id,
                    event_name=event_name,
                    event_start=event_start,
                    league=league,
                    retrieved_at=retrieved_at,
                    contract_cache=contract_cache,
                    quotes_cache=quotes_cache,
                )
                records.extend(market_records)
                warnings.extend(market_warnings)
                self.debug(
                    f"{self.name}: targeted fetch market_id={market_id} -> {len(market_records)} records"
                )
            except Exception as exc:
                warnings.append(f"Skipped Smarkets market {market_id}: {exc}")
                self.debug(f"{self.name}: targeted fetch: skipped market {market_id}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def _targeted_market_to_records(
        self,
        market_id: str,
        event_name: str,
        event_start: str | None,
        league: str,
        retrieved_at: str,
        contract_cache: dict[str, dict[str, Any]],
        quotes_cache: dict[str, dict[str, Any]],
    ) -> tuple[list[OddsRecord], list[str]]:
        """Fetch contracts and quotes for a single known market ID and build OddsRecords.

        Skips the event-level API call entirely.  Event name and start are
        reconstructed from the game context passed by the caller.
        """
        records: list[OddsRecord] = []
        warnings: list[str] = []

        try:
            contracts_payload = self._get_market_contracts(market_id, contract_cache)
            quotes_payload = self._get_market_quotes(market_id, quotes_cache)
        except HTTPError as exc:
            warnings.append(f"Skipped Smarkets market {market_id}: HTTP {exc.code}")
            return records, warnings
        except Exception as exc:
            warnings.append(f"Skipped Smarkets market {market_id}: {exc}")
            return records, warnings

        contract_map = {
            str(c["id"]): c for c in contracts_payload.get("contracts", [])
        }
        self.debug(
            f"{self.name}: targeted market_id={market_id} -> "
            f"{len(contract_map)} contracts, {len(quotes_payload)} quote books"
        )

        for contract_id, quote in quotes_payload.items():
            contract = contract_map.get(str(contract_id))
            if not contract:
                continue
            for side, ladder_key in (("back", "offers"), ("lay", "bids")):
                levels = quote.get(ladder_key, [])
                self.debug(
                    f"{self.name}: contract={contract.get('name')!r} side={side}"
                    f" levels={len(levels)}"
                    f" {'-> taking best' if levels else '-> skipped (no liquidity)'}"
                )
                if not levels:
                    continue
                best = levels[0]
                probability = _smarkets_probability(best["price"])
                records.append(
                    OddsRecord(
                        provider=self.name,
                        sport=LEAGUE_TO_SPORT[league],
                        league=league,
                        event_name=event_name,
                        event_start=event_start,
                        market_name="Match Winner",
                        market_type="three_way" if league in ("ucl", "epl") else "two_way",
                        selection_name=normalize_team_name(
                            str(contract.get("name") or "Unknown selection"), league
                        ),
                        selection_side=side,
                        decimal_odds=decimal_from_probability(probability),
                        implied_probability=round(probability, 6),
                        currency="GBP",
                        source_market_id=market_id,
                        source_event_id=market_id,
                        retrieved_at=retrieved_at,
                        metadata={
                            "contract_id": str(contract_id),
                            "raw_price": best.get("price"),
                            "raw_quantity": best.get("quantity"),
                        },
                    )
                )
        return records, warnings

    def _fetch_children(self, parent_id: int) -> list[dict[str, Any]]:
        self.debug(f"{self.name}: requesting event children for parent_id={parent_id}")
        payload = self.http_client.get_json(
            f"{self.settings.base_url}/v3/events/",
            params={"parent_id": parent_id, "limit": 200},
            headers={"Accept": "application/json"},
        )
        return list(payload.get("events", []))

    def _event_to_records(
        self,
        event: dict[str, Any],
        league: str,
        retrieved_at: str,
        contract_cache: dict[str, dict[str, Any]],
        quotes_cache: dict[str, dict[str, Any]],
    ) -> tuple[list[OddsRecord], list[str]]:
        self.debug(f"{self.name}: fetching markets for event_id={event['id']}")
        markets_payload = self.http_client.get_json(
            f"{self.settings.base_url}/v3/events/{event['id']}/markets/",
            headers={"Accept": "application/json"},
        )
        self.debug(
            f"{self.name}: event_id={event['id']} returned {len(markets_payload.get('markets', []))} markets"
        )
        records: list[OddsRecord] = []
        warnings: list[str] = []

        open_markets = [market for market in markets_payload.get("markets", []) if market.get("state") == "open"]
        core_markets = [market for market in open_markets if _is_core_market(market)]
        self.debug(
            f"{self.name}: event_id={event['id']} has {len(open_markets)} open markets, "
            f"{len(core_markets)} core markets after filtering"
        )
        for market_index, market in enumerate(core_markets, start=1):
            self.debug(
                f"{self.name}: event_id={event['id']} market {market_index}/{len(core_markets)} "
                f"'{market.get('name', 'unknown')}'"
            )

            try:
                self.debug(f"{self.name}: fetching contracts and quotes for market_id={market['id']}")
                contracts_payload = self._get_market_contracts(str(market["id"]), contract_cache)
                quotes_payload = self._get_market_quotes(str(market["id"]), quotes_cache)
            except HTTPError as exc:
                warnings.append(
                    f"Skipped Smarkets market {market.get('id')} for event {event.get('id')}: HTTP {exc.code}"
                )
                self.debug(f"{self.name}: skipped market {market.get('id')} with HTTP {exc.code}")
                continue
            except Exception as exc:
                warnings.append(
                    f"Skipped Smarkets market {market.get('id')} for event {event.get('id')}: {exc}"
                )
                self.debug(f"{self.name}: skipped market {market.get('id')}: {exc}")
                continue

            contract_map = {
                str(contract["id"]): contract for contract in contracts_payload.get("contracts", [])
            }
            self.debug(
                f"{self.name}: market_id={market['id']} -> {len(contract_map)} contracts, {len(quotes_payload)} quote books"
            )

            quoted_ids = {str(k) for k in quotes_payload.keys()}
            for cid, contract in contract_map.items():
                if cid not in quoted_ids:
                    self.debug(
                        f"{self.name}: contract {cid} ({contract.get('name')!r}) has no quote entry — skipped"
                    )

            for contract_id, quote in quotes_payload.items():
                contract = contract_map.get(str(contract_id))
                if not contract:
                    continue

                for side, ladder_key in (("back", "offers"), ("lay", "bids")):
                    levels = quote.get(ladder_key, [])
                    self.debug(
                        f"{self.name}: contract={contract.get('name')!r} side={side}"
                        f" levels={len(levels)} {'-> taking best' if levels else '-> skipped (no liquidity)'}"
                    )
                    if not levels:
                        continue
                    # Take only the best price: first offer (highest decimal odds) for back,
                    # first bid (lowest decimal odds) for lay.
                    best = levels[0]
                    probability = _smarkets_probability(best["price"])
                    records.append(
                        OddsRecord(
                            provider=self.name,
                            sport=LEAGUE_TO_SPORT[league],
                            league=league,
                            event_name=str(event.get("name") or "Unknown event"),
                            event_start=event.get("start_datetime"),
                            market_name=str(market.get("name") or "Unknown market"),
                            market_type="three_way" if league in ("ucl", "epl") else "two_way",
                            selection_name=normalize_team_name(str(contract.get("name") or "Unknown selection"), league),
                            selection_side=side,
                            decimal_odds=decimal_from_probability(probability),
                            implied_probability=round(probability, 6),
                            currency="GBP",
                            source_market_id=str(market.get("id")),
                            source_event_id=str(event.get("id")),
                            retrieved_at=retrieved_at,
                            metadata={
                                "contract_id": str(contract_id),
                                "raw_price": best.get("price"),
                                "raw_quantity": best.get("quantity"),
                                "contract_type": contract.get("contract_type", {}).get("name"),
                                "market_category": market.get("category"),
                                "market_param": market.get("market_type", {}).get("param"),
                            },
                        )
                    )

        return records, warnings

    def _get_market_contracts(
        self,
        market_id: str,
        cache: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if market_id in cache:
            self.debug(f"{self.name}: contracts cache hit for market_id={market_id}")
            return cache[market_id]
        payload = self.http_client.get_json(
            f"{self.settings.base_url}/v3/markets/{market_id}/contracts/",
            headers={"Accept": "application/json"},
        )
        cache[market_id] = payload
        self._pace_market_requests()
        return payload

    def _get_market_quotes(
        self,
        market_id: str,
        cache: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if market_id in cache:
            self.debug(f"{self.name}: quotes cache hit for market_id={market_id}")
            return cache[market_id]
        payload = self.http_client.get_json(
            f"{self.settings.base_url}/v3/markets/{market_id}/quotes/",
            headers={"Accept": "application/json"},
        )
        cache[market_id] = payload
        self._pace_market_requests()
        return payload

    def _pace_market_requests(self) -> None:
        delay = 0.25+(random.random())*0.05
        time.sleep(delay)


LEAGUE_ROOT_EVENT_IDS = {
    "nba": 19694311,
    "mlb": 13240353,
    "ucl": 25363462,
    "epl": 25508311,
}

LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "mlb": "baseball",
    "ucl": "soccer",
    "epl": "soccer",
}


def _smarkets_probability(raw_price: int | float) -> float:
    return float(raw_price) / 10000.0


_MONEYLINE_MARKET_NAMES = {
    "winner (incl. overtime)",
    "winner (including overtime)",
    "match winner",
    "winner",  # Smarkets UCL match winner market
    "full-time result"
}


def _is_core_market(market: dict[str, Any]) -> bool:
    market_name = str(market.get("name") or "").lower().strip()

    return market_name in _MONEYLINE_MARKET_NAMES
