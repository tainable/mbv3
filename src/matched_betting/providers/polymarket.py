from __future__ import annotations

import json
from typing import Any

from matched_betting.config import PolymarketSettings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.models import OddsRecord, ProviderPayload, decimal_from_probability, utc_now_iso
from matched_betting.providers.base import OddsProvider


LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "mlb": "baseball",
}


class PolymarketProvider(OddsProvider):
    name = "polymarket"

    def __init__(
        self,
        settings: PolymarketSettings,
        http_client: HttpClient,
        debug_logger: DebugLogger | None = None,
    ) -> None:
        super().__init__(debug_logger)
        self.settings = settings
        self.http_client = http_client

    def fetch_odds(self, leagues: list[str]) -> ProviderPayload:
        self.debug(f"{self.name}: loading team index for {', '.join(leagues)}")
        team_index = self._load_team_index(leagues)
        self.debug(f"{self.name}: loading open markets")
        all_markets = self._load_markets()
        self.debug(f"{self.name}: fetched {len(all_markets)} open markets")
        retrieved_at = utc_now_iso()

        #print("all_markets:",all_markets)
        records: list[OddsRecord] = []
        warnings: list[str] = []

        candidate_markets: list[tuple[dict[str, Any], str]] = []
        for market in all_markets:
            league = self._infer_league(market, team_index)
            
            split_slug = market['slug'].split("-")

            if "nba" in split_slug[0]:
                if split_slug[1] in ['atl','phx','lal','ind','chi','phi', 
                'okc','bos','mia','cle','sas','mem','was','uta',
                'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:
                    
                    if split_slug[2] in ['atl','phx','lal','ind','chi','phi', 
                'okc','bos','mia','cle','sas','mem','was','uta',
                'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:
                        league = 'nba'
                        candidate_markets.append((market, league))

            if "mlb" in split_slug[0]:
                if split_slug[1] in ['cws','mil','wsh','chc','min','bal', 
                'cin','bos','laa','hou','det','sd','tex','phi',
                'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                    
                    if split_slug[2] in ['cws','mil','wsh','chc','min','bal', 
                'cin','bos','laa','hou','det','sd','tex','phi',
                'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                        league = 'mlb'
                        candidate_markets.append((market, league))
        
        self.debug(f"{self.name}: found {len(candidate_markets)} candidate markets after league filtering")

        for market_index, (market, league) in enumerate(candidate_markets, start=1):
            self.debug(
                f"{self.name}: market {market_index}/{len(candidate_markets)} id={market.get('id', 'unknown')} league={league}"
            )
            print(market['question'])
            try:
                market_records = self._market_to_records(market, league, retrieved_at)
                records.extend(market_records)
                self.debug(
                    f"{self.name}: {league} market {market.get('id', 'unknown')} -> {len(market_records)} records"
                
                )
                
            except Exception as exc:
                market_id = str(market.get("id", "unknown"))
                warnings.append(f"Skipped Polymarket market {market_id}: {exc}")
                self.debug(f"{self.name}: skipped market {market_id}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def _load_team_index(self, leagues: list[str]) -> dict[str, set[str]]:
        index: dict[str, set[str]] = {league: set() for league in leagues}
        for league in leagues:
            self.debug(f"{self.name}: requesting teams for {league}")
            
            if league == "nba":
                index[league] = ['atl','phx','lal','ind','chi','phi', 
                'okc','bos','mia','cle','sas','mem','was','uta',
                'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']
                return index
            
            elif league == "mlb":
                index[league] = ['cws','mil','wsh','chc','min','bal', 
                'cin','bos','laa','hou','det','sd','tex','phi',
                'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']
                return index    
            
            else:
                raw_teams = self.http_client.get_json(
                f"{self.settings.gamma_base_url}/teams",
                params={"league": league.upper(), "limit": 500},
            )
            for team in raw_teams:
                team_id = str(team.get("id", "")).strip()
                if team_id:
                    index[league].add(team_id)
            self.debug(f"{self.name}: indexed {len(index[league])} teams for {league}")
        return index

    def _load_markets(self) -> list[dict[str, Any]]:
        offset = 0
        markets: list[dict[str, Any]] = []
        batch_size = 500

        while True:
            self.debug(f"{self.name}: requesting markets offset={offset} limit={batch_size}")
            batch = self.http_client.get_json(
                f"{self.settings.gamma_base_url}/markets",
                params={"limit": batch_size, "offset": offset, "closed": "false"},
            )
            if not isinstance(batch, list) or not batch:
                break
            markets.extend(batch)
            self.debug(f"{self.name}: received {len(batch)} markets at offset={offset}")
            if len(batch) < batch_size:
                break
            offset += batch_size

        return markets

    def _infer_league(
        self,
        market: dict[str, Any],
        team_index: dict[str, set[str]],
    ) -> str | None:
        team_a = str(market.get("outcomes")[0] or "").strip()
        team_b = str(market.get("outcomes")[1] or "").strip()
        for league, known_ids in team_index.items():
            if team_a in known_ids or team_b in known_ids:
                return league

        events = market.get("events") or []
        for event in events:
            title = " ".join(
                str(event.get(key, "")).lower()
                for key in ("title", "subtitle", "category", "subcategory")
            )
            if "nba" in title:
                return "nba"
            if "mlb" in title or "major league baseball" in title:
                return "mlb"

        question = str(market.get("question") or "").lower()
        if "nba" in question:
            return "nba"
        if "mlb" in question:
            return "mlb"

        return None

    def _market_to_records(
        self,
        market: dict[str, Any],
        league: str,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        #print(_parse_stringified_json_list(market))
        outcomes = _parse_stringified_json_list(market.get("outcomes"))
        
        #prices = [float(item) for item in _parse_stringified_json_list(market.get("outcomePrices"))]
        try: 
            bestAsk = float(market.get("bestAsk"))
        except:
            bestAsk = None

        try: 
            bestBid = 1-float(market.get("bestBid"))
        except:
            bestBid = None

        
        
        prices= [bestAsk, bestBid]

        

        if not outcomes or not prices or len(outcomes) != len(prices):
            raise ValueError("missing or mismatched outcomes/outcomePrices")

        event = (market.get("events") or [{}])[0]
        event_name = (
            event.get("title")
            or market.get("groupItemTitle")
            or market.get("question")
            or "Unknown event"
        )

        if "mlb" in event.get("slug"):
            event_start = event.get("startTime")
        else:
            event_start = event.get("endDate")

        market_name = market.get("groupItemTitle") or market.get("question") or "Unknown market"
        market_type = self._infer_market_type(outcomes)

        records: list[OddsRecord] = []
        for outcome, probability in zip(outcomes, prices, strict=True):
            #if probability <= 0:
            #    continue
            try:
                iprob = round(probability, 6)
            except:
                iprob = None

            records.append(
                OddsRecord(
                    provider=self.name,
                    sport=LEAGUE_TO_SPORT[league],
                    league=league,
                    event_name=str(event_name),
                    event_start=str(event_start) if event_start else None,
                    market_name=str(market_name),
                    market_type=market_type,
                    selection_name=str(outcome),
                    selection_side="back",
                    decimal_odds=decimal_from_probability(probability),
                    
                    implied_probability=iprob,
                    currency="USD",
                    source_market_id=str(market.get("id")),
                    source_event_id=str(event.get("id")) if event.get("id") is not None else None,
                    retrieved_at=retrieved_at,
                    metadata={
                        "question": market.get("question"),
                        "slug": market.get("slug"),
                        "market_type_raw": market.get("marketType"),
                        "sports_market_type_raw": market.get("sportsMarketType"),
                    },
                )
            )

        return records

    @staticmethod
    def _infer_market_type(outcomes: list[str]) -> str:
        if len(outcomes) == 2:
            return "two_way"
        if len(outcomes) == 3:
            return "three_way"
        return "multi_way"


def _parse_stringified_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return parsed
    raise ValueError(f"Cannot parse list value: {value!r}")
