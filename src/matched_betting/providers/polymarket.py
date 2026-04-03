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
    "ucl": "soccer",
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

        half_segments = {"1h", "2h", "1st-half", "2nd-half", "first-half", "second-half", "halftime"}

        candidate_markets: list[tuple[dict[str, Any], str]] = []
        for market in all_markets:
            league = self._infer_league(market, team_index)

            split_slug = market['slug'].split("-")

            # Skip 1st/2nd half markets (e.g. nba-bos-lal-1h)
            if any(seg in half_segments for seg in split_slug):
                continue

            if "nba" in leagues and "nba" in split_slug[0]:
                if split_slug[1] in ['atl','phx','lal','ind','chi','phi',
                'okc','bos','mia','cle','sas','mem','was','uta',
                'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:

                    if split_slug[2] in ['atl','phx','lal','ind','chi','phi',
                'okc','bos','mia','cle','sas','mem','was','uta',
                'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:
                        league = 'nba'
                        candidate_markets.append((market, league))

            if "mlb" in leagues and "mlb" in split_slug[0]:
                if split_slug[1] in ['cws','mil','wsh','chc','min','bal',
                'cin','bos','laa','hou','det','sd','tex','phi',
                'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:

                    if split_slug[2] in ['cws','mil','wsh','chc','min','bal',
                'cin','bos','laa','hou','det','sd','tex','phi',
                'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                        league = 'mlb'
                        candidate_markets.append((market, league))

            ucl_teams = team_index.get("ucl", [])
            if "ucl" in leagues and ucl_teams:
                slug = market.get("slug", "")
                # Match head-to-head UCL slugs: ucl-{team1}-{team2}-{date}
                # Markets may not exist until close to match day (QF first legs April 8-9)
                if split_slug[0] == "ucl" and len(split_slug) >= 3:
                    if split_slug[1] in ucl_teams and split_slug[2] in ucl_teams:
                        if split_slug[-1] in ucl_teams or split_slug[-1] == "draw":
                            candidate_markets.append((market, "ucl"))
        
        self.debug(f"{self.name}: found {len(candidate_markets)} candidate markets after league filtering")

        for market_index, (market, league) in enumerate(candidate_markets, start=1):
            self.debug(
                f"{self.name}: market {market_index}/{len(candidate_markets)} id={market.get('id', 'unknown')} league={league}"
            )
            #print(market['question'])
            try:
                market_records = self._market_to_records(market, league, retrieved_at)
                records.extend(market_records)
                self.debug(
                    f"{self.name}: {league} market {market.get('id', 'unknown')} -> {len(market_records)} records"
                
                )
                #print(market_records)
                
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

            elif league == "mlb":
                index[league] = ['cws','mil','wsh','chc','min','bal',
                'cin','bos','laa','hou','det','sd','tex','phi',
                'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']

            elif league == "ucl":
                index[league] = ['rma1','bay1','spo1','ars','psg1','liv1','fcb1','atm1']

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
        if "ucl" in question:
            return "ucl"

        return None

    def _market_to_records(
        self,
        market: dict[str, Any],
        league: str,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        outcomes = _parse_stringified_json_list(market.get("outcomes"))

        lay_probs: list[float | None] = []

        if len(outcomes) == 3:
            # Three-way market: use outcomePrices for all three outcomes
            prices_raw = _parse_stringified_json_list(market.get("outcomePrices"))
            if not prices_raw or len(prices_raw) != 3:
                raise ValueError("three-way market missing outcomePrices")
            back_probs = [float(p) for p in prices_raw]
            lay_probs = [None] * 3
        elif len(outcomes) == 2:
            try:
                ask = float(market.get("bestAsk"))
            except (TypeError, ValueError):
                ask = None
            try:
                bid_comp = 1-float(market.get("bestBid"))
            except (TypeError, ValueError):
                bid_comp = None
            back_probs = [ask, bid_comp]
            lay_probs = [None, None]
        else:
            raise ValueError(f"unexpected outcome count: {len(outcomes)}")

        if not back_probs or len(back_probs) != len(outcomes) or any(p is None for p in back_probs):
            raise ValueError("missing or mismatched outcomes/prices")

        event = (market.get("events") or [{}])[0]

        # Polymarket UCL markets are typically Yes/No binary markets per outcome.
        # "Yes" = this team/outcome wins; remap to the meaningful name from groupItemTitle.
        # "No" is dropped — its probability is the complement and is emitted as a lay record.
        if league == "ucl" and {o.lower() for o in outcomes} <= {"yes", "no"}:
            group_title = market.get("groupItemTitle")
            if not group_title:
                raise ValueError("UCL Yes/No market missing groupItemTitle for outcome name")
            yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "yes"), None)
            if yes_idx is None:
                raise ValueError("UCL Yes/No market has no 'Yes' outcome")
            outcomes = [group_title]
            try:
                back_probs = [float(market.get("bestBid"))]
            except (TypeError, ValueError):
                raise ValueError("UCL Yes/No market missing bestBid for back price")
            try:
                lay_probs = [1 - float(market.get("bestAsk"))]
            except (TypeError, ValueError):
                raise ValueError("UCL Yes/No market missing bestAsk for lay price")

        if league == "ucl":
            # Build event name from the two team outcomes (skip draw/no-draw outcomes)
            _draw_variants = {"draw", "tie", "no draw", "not draw"}
            non_draw = [o for o in outcomes if o.lower() not in _draw_variants]
            if len(non_draw) >= 2:
                event_name = f"{non_draw[0]} vs {non_draw[1]}"
            else:
                # Single-outcome market (team or draw): use parent event title for match context
                event_name = (
                    event.get("title")
                    or market.get("question")
                    or f"{outcomes[0]} vs {outcomes[-1]}"
                )
        else:
            event_name = (
                event.get("title")
                or market.get("groupItemTitle")
                or market.get("question")
                or "Unknown event"
            )

        if league in ("mlb", "ucl"):
            event_start = event.get("startTime")
        else:
            event_start = event.get("endDate")

        if league == "ucl":
            # For soccer, market_name should describe the game so each record is self-describing
            # alongside selection_name (which identifies the specific team/draw outcome)
            market_name = event_name
        else:
            market_name = market.get("groupItemTitle") or market.get("question") or "Unknown market"

        # For single-outcome Yes/No remapped markets, infer_market_type would return "multi_way"
        # (only 1 outcome left). Force two_way so the moneyline filter accepts these records.
        if len(outcomes) == 1:
            market_type = "two_way"
        else:
            market_type = self._infer_market_type(
                outcomes,
                slug=market.get("slug", ""),
                question=market.get("question", ""),
                sports_market_type=market.get("sportsMarketType", ""),
            )

        shared = dict(
            provider=self.name,
            sport=LEAGUE_TO_SPORT[league],
            league=league,
            event_name=str(event_name),
            event_start=str(event_start) if event_start else None,
            market_name=str(market_name),
            market_type=market_type,
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

        records: list[OddsRecord] = []
        for i, (outcome, back_prob) in enumerate(zip(outcomes, back_probs)):
            try:
                iprob = round(back_prob, 6)
            except (TypeError, ValueError):
                iprob = None

            records.append(
                OddsRecord(
                    **shared,
                    selection_name=str(outcome),
                    selection_side="back",
                    decimal_odds=decimal_from_probability(back_prob),
                    implied_probability=iprob,
                )
            )

            # Lay record: buying "No" on Polymarket is equivalent to a lay bet.
            # Use independent lay_probs when available (e.g. Yes/No markets where
            # back=bestBid_Yes and lay=1-bestAsk_Yes to properly account for spread).
            explicit_lay = lay_probs[i] if lay_probs and i < len(lay_probs) else None
            lay_prob = explicit_lay if explicit_lay is not None else 1 - back_prob
            if 0 < lay_prob < 1:
                records.append(
                    OddsRecord(
                        **shared,
                        selection_name=str(outcome),
                        selection_side="lay",
                        decimal_odds=decimal_from_probability(lay_prob),
                        implied_probability=round(lay_prob, 6),
                    )
                )

        return records

    @staticmethod
    def _infer_market_type(
        outcomes: list[str],
        slug: str = "",
        question: str = "",
        sports_market_type: str = "",
    ) -> str:
        combined = f"{slug.lower()} {question.lower()} {sports_market_type.lower()}"
        if any(kw in combined for kw in ("spread", "cover", "handicap", "ats", "run-line", "runline")):
            return "handicap"
        if any(kw in combined for kw in ("over-under", "over/under", "total", "runs-over", "runs-under")):
            return "total"
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

