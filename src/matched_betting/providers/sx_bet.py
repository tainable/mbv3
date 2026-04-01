from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from matched_betting.config import SxBetSettings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.models import OddsRecord, ProviderPayload, utc_now_iso
from matched_betting.providers.base import OddsProvider


LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "mlb": "baseball",
}

# SX Bet league IDs (from GET /leagues)
_LEAGUE_IDS: dict[str, int] = {
    "nba": 1,
    "mlb": 171,
}

# SX Bet stores percentageOdds as an integer representing probability * 10^20
_ODDS_SCALE = 10**20

# Market type 226 = "Moneyline Including Overtime" (two-way winner market)
_MONEYLINE_TYPE = 226


class SxBetProvider(OddsProvider):
    name = "sx_bet"

    def __init__(
        self,
        settings: SxBetSettings,
        http_client: HttpClient,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        super().__init__(debug_logger)
        self.settings = settings
        self.http_client = http_client

    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        records: list[OddsRecord] = []
        warnings: list[str] = []
        retrieved_at = utc_now_iso()

        for league in leagues:
            league_id = _LEAGUE_IDS.get(league)
            if league_id is None:
                warnings.append(f"{self.name}: no league ID found for {league}")
                continue

            self.debug(f"{self.name}: fetching {league} markets (leagueId={league_id})")
            markets = self._fetch_markets(league_id)
            self.debug(f"{self.name}: found {len(markets)} {league} moneyline markets")

            if not markets:
                continue

            market_hashes = [m["marketHash"] for m in markets]
            hash_to_market = {m["marketHash"]: m for m in markets}

            self.debug(f"{self.name}: fetching best odds for {len(market_hashes)} markets")
            best_odds_map = self._fetch_best_odds(market_hashes)
            self.debug(f"{self.name}: best odds returned for {len(best_odds_map)} markets")

            for market_hash, market in hash_to_market.items():
                best = best_odds_map.get(market_hash)

                if best is None:
                    self.debug(f"{self.name}: no best odds for market {market_hash}, skipping")
                    continue
                try:
                    market_records = self._market_to_records(market, best, league, retrieved_at)
                    records.extend(market_records)
                    self.debug(
                        f"{self.name}: market {market_hash} -> {len(market_records)} records"
                    )
                except Exception as exc:
                    warnings.append(f"Skipped SX Bet market {market_hash}: {exc}")
                    self.debug(f"{self.name}: skipped market {market_hash}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def _fetch_markets(self, league_id: int) -> list[dict[str, Any]]:
        all_markets: list[dict[str, Any]] = []
        params: dict[str, str] = {"leagueId": str(league_id)}

        while True:
            raw = self.http_client.get_json(
                f"{self.settings.base_url}/markets/active",
                params=params,
            )
            data = raw.get("data", {})
            page_markets = data.get("markets", []) if isinstance(data, dict) else []
            all_markets.extend(page_markets)

            next_key = data.get("nextKey") if isinstance(data, dict) else None
            if not next_key:
                break
            params = {"leagueId": str(league_id), "paginationKey": next_key}

        # Keep only type 226 markets (Moneyline Including Overtime)
        moneyline = [m for m in all_markets if m.get("type") == _MONEYLINE_TYPE]
        self.debug(f"{self.name}: {len(all_markets)} total markets, {len(moneyline)} moneyline (type {_MONEYLINE_TYPE})")
        return moneyline

    def _fetch_best_odds(self, market_hashes: list[str]) -> dict[str, dict[str, Any]]:
        """Fetch best available odds by market hash.

        Returns a dict keyed by marketHash, each value having 'outcomeOne' and
        'outcomeTwo' sub-dicts with 'percentageOdds' strings.
        """
        raw = self.http_client.get_json(
            f"{self.settings.base_url}/orders/odds/best",
            params={
                "marketHashes": ",".join(market_hashes),
                "baseToken": self.settings.base_token,
            },
        )
        best_odds_list: list[dict[str, Any]] = (
            raw.get("data", {}).get("bestOdds", [])
        )
        return {entry["marketHash"]: entry for entry in best_odds_list if "marketHash" in entry}

    def _market_to_records(
        self,
        market: dict[str, Any],
        best: dict[str, Any],
        league: str,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        market_hash = market["marketHash"]
        team_one: str = market["teamOneName"]
        team_two: str = market["teamTwoName"]
        game_time = market.get("gameTime")
        event_start = _unix_to_iso(game_time) if game_time else None
        event_name = f"{team_one} vs. {team_two}"
        sport = LEAGUE_TO_SPORT[league]
        source_event_id = (
            str(market["sportXEventId"]) if market.get("sportXEventId") else None
        )

        # In SX Bet's P2P model, percentageOdds is the maker's probability * 10^20.
        # The taker backing team_one is matched against makers betting on team_two,
        # so taker implied probability = 1 - (outcomeTwo.percentageOdds / scale),
        # and vice versa for team_two.
        outcome_one_data = best.get("outcomeOne", {})
        outcome_two_data = best.get("outcomeTwo", {})

        outcomes = [
            (team_one, outcome_two_data),  # team one's taker odds come from outcomeTwo maker orders
            (team_two, outcome_one_data),  # team two's taker odds come from outcomeOne maker orders
        ]

        records: list[OddsRecord] = []
        for team_name, maker_data in outcomes:
            raw_pct = maker_data.get("percentageOdds")
            if raw_pct is None:
                continue
            try:
                maker_prob = int(raw_pct) / _ODDS_SCALE
            except (TypeError, ValueError):
                continue

            taker_prob = 1.0 - maker_prob
            if not (0.0 < taker_prob < 1.0):
                continue

            decimal_odds = 1.0 / taker_prob
            records.append(
                OddsRecord(
                    provider=self.name,
                    sport=sport,
                    league=league,
                    event_name=event_name,
                    event_start=event_start,
                    market_name="Moneyline Incl. OT",
                    market_type="money_line",
                    selection_name=team_name,
                    selection_side="back",
                    decimal_odds=round(decimal_odds, 6),
                    implied_probability=round(taker_prob, 6),
                    currency="USD",
                    source_market_id=market_hash,
                    source_event_id=source_event_id,
                    retrieved_at=retrieved_at,
                    metadata={
                        "market_hash": market_hash,
                        "game_time": game_time,
                        "league_id": market.get("leagueId"),
                        "market_type": _MONEYLINE_TYPE,
                    },
                )
            )
        return records


def _unix_to_iso(ts: int | float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
