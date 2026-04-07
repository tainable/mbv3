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
    "ucl": "soccer",
}

# SX Bet league IDs (from GET /leagues)
_LEAGUE_IDS: dict[str, int] = {
    "nba": 1,
    "mlb": 171,
    "ucl": 30, 
}

# SX Bet stores percentageOdds as an integer representing probability * 10^20
_ODDS_SCALE = 10**20

# Market type 226 = "Moneyline Including Overtime" (two-way winner market)
_MONEYLINE_TYPE = 226

# TODO: verify SX Bet market type for soccer match result (Yes/No per outcome)
_SOCCER_RESULT_TYPE = 1


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

        if "ucl" in leagues:
            self._log_soccer_leagues()

        for league in leagues:
            league_id = _LEAGUE_IDS.get(league)
            if league_id is None:
                warnings.append(f"{self.name}: no league ID found for {league}")
                continue

            self.debug(f"{self.name}: fetching {league} markets (leagueId={league_id})")
            markets = self._fetch_markets(league_id, league)
            self.debug(f"{self.name}: found {len(markets)} {league} moneyline markets")

            if not markets:
                continue

            market_hashes = [m["marketHash"] for m in markets]
            hash_to_market = {m["marketHash"]: m for m in markets}

            self.debug(f"{self.name}: fetching best odds for {len(market_hashes)} markets")
            best_odds_map = self._fetch_best_odds(market_hashes)
            self.debug(f"{self.name}: best odds returned for {len(best_odds_map)} markets")

            self.debug(f"{self.name}: fetching order liquidity for {len(market_hashes)} markets")
            avail_map = self._fetch_available(market_hashes)

            if league == "ucl":
                ucl_records, ucl_warnings = self._process_ucl_markets(
                    hash_to_market, best_odds_map, retrieved_at
                )
                records.extend(ucl_records)
                warnings.extend(ucl_warnings)
            else:
                for market_hash, market in hash_to_market.items():
                    best = best_odds_map.get(market_hash)

                    if best is None:
                        self.debug(f"{self.name}: no best odds for market {market_hash}, skipping")
                        continue
                    try:
                        market_records = self._market_to_records(
                            market, best, league, retrieved_at,
                            avail=avail_map.get(market_hash),
                        )
                        records.extend(market_records)
                        self.debug(
                            f"{self.name}: market {market_hash} -> {len(market_records)} records"
                        )
                    except Exception as exc:
                        warnings.append(f"Skipped SX Bet market {market_hash}: {exc}")
                        self.debug(f"{self.name}: skipped market {market_hash}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def _log_soccer_leagues(self) -> None:
        try:
            raw = self.http_client.get_json(
                f"{self.settings.base_url}/leagues/active",
            )
            leagues = raw.get("data", {}).get("leagues", []) if isinstance(raw.get("data"), dict) else []
            self.debug(f"{self.name}: all SX Bet leagues ({len(leagues)} total):")
            for l in leagues:
                self.debug(f"{self.name}:   {l}")
        except Exception as exc:
            self.debug(f"{self.name}: could not fetch leagues list: {exc}")

    def _fetch_markets(self, league_id: int, league: str) -> list[dict[str, Any]]:
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

        if league == "ucl":
            type_counts: dict[Any, int] = {}
            for m in all_markets:
                t = m.get("type")
                type_counts[t] = type_counts.get(t, 0) + 1
            self.debug(f"{self.name}: UCL market types found across {len(all_markets)} markets:")
            for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                sample = next((m.get("marketName") or m.get("label") or m.get("type") for m in all_markets if m.get("type") == t), "")
                self.debug(f"{self.name}:   type={t}  count={count}  example={sample!r}")

        market_type = _SOCCER_RESULT_TYPE if league == "ucl" else _MONEYLINE_TYPE
        filtered = [
            m for m in all_markets
            if m.get("type") == market_type and m.get("leagueId") == league_id
        ]
        self.debug(f"{self.name}: {len(all_markets)} total markets, {len(filtered)} after filtering for type {market_type} and leagueId {league_id}")
        return filtered

    def _process_ucl_markets(
        self,
        hash_to_market: dict[str, dict[str, Any]],
        best_odds_map: dict[str, dict[str, Any]],
        retrieved_at: str,
    ) -> tuple[list[OddsRecord], list[str]]:
        """Handle UCL Yes/No binary markets (one market per outcome: home win, away win, draw).

        SX Bet creates a separate market for each outcome rather than a single 3-way market.
        teamOneName is the outcome (e.g. "Real Madrid", "Draw"), teamTwoName is "No" or similar.
        Markets for the same fixture share a sportXEventId.
        """
        records: list[OddsRecord] = []
        warnings: list[str] = []

        # Debug: dump raw market structure so we can identify the correct grouping key
        for market in list(hash_to_market.values())[:5]:
            self.debug(
                f"{self.name}: UCL market sample: sportXEventId={market.get('sportXEventId')!r}"
                f" teamOne={market.get('teamOneName')!r} teamTwo={market.get('teamTwoName')!r}"
                f" gameTime={market.get('gameTime')} outcomeOneName={market.get('outcomeOneName')!r}"
            )

        # Group markets by fixture. Use sportXEventId if present; fall back to
        # gameTime + teamOneName + teamTwoName as a composite key for soccer markets
        # where sportXEventId may be null.
        event_groups: dict[str, list[dict[str, Any]]] = {}
        for market in hash_to_market.values():
            sport_event_id = market.get("sportXEventId")
            if sport_event_id:
                group_key = str(sport_event_id)
            else:
                group_key = f"{market.get('gameTime')}|{market.get('teamOneName')}|{market.get('teamTwoName')}"
            event_groups.setdefault(group_key, []).append(market)

        for event_id, event_markets in event_groups.items():
            self.debug(f"{self.name}: UCL event {event_id} has {len(event_markets)} markets:")
            for m in event_markets:
                self.debug(
                    f"{self.name}:   hash={m.get('marketHash')} teamOne={m.get('teamOneName')!r}"
                    f" teamTwo={m.get('teamTwoName')!r} marketName={m.get('marketName')!r}"
                    f" type={m.get('type')} label={m.get('label')!r}"
                    f" outcomeOneName={m.get('outcomeOneName')!r} outcomeTwoName={m.get('outcomeTwoName')!r}"
                )

            _draw_variants = {"draw", "tie"}

            # event_name: use teamOneName/teamTwoName from the first market — these should
            # be the home/away team names, consistent across all outcome markets in the group.
            first = event_markets[0]
            home_name = first.get("teamOneName") or ""
            away_name = first.get("teamTwoName") or ""
            if home_name and away_name:
                event_name = f"{home_name} vs {away_name}"
            else:
                event_name = first.get("marketName") or f"UCL event {event_id}"

            game_time = first.get("gameTime")
            event_start = _unix_to_iso(game_time) if game_time else None

            for market in event_markets:
                market_hash = market["marketHash"]
                best = best_odds_map.get(market_hash)
                if best is None:
                    self.debug(f"{self.name}: no best odds for UCL market {market_hash}, skipping")
                    continue

                outcome_name = market.get("outcomeOneName", "")
                if not outcome_name:
                    warnings.append(f"Skipped SX Bet UCL market {market_hash}: missing outcome name")
                    continue

                if outcome_name.lower() in _draw_variants:
                    outcome_name = "draw"

                # Back "outcomeOne": taker prob = 1 - maker prob from outcomeTwo
                # Lay  "outcomeOne": taker prob = 1 - maker prob from outcomeOne
                outcome_one_data = best.get("outcomeOne", {})
                outcome_two_data = best.get("outcomeTwo", {})

                shared = dict(
                    provider=self.name,
                    sport="soccer",
                    league="ucl",
                    event_name=event_name,
                    event_start=event_start,
                    market_name="Match Result",
                    market_type="three_way",
                    selection_name=outcome_name,
                    currency="USD",
                    source_market_id=market_hash,
                    source_event_id=str(event_id) if event_id != "unknown" else None,
                    retrieved_at=retrieved_at,
                    metadata={
                        "market_hash": market_hash,
                        "game_time": game_time,
                        "league_id": market.get("leagueId"),
                        "market_type": market.get("type"),
                    },
                )

                for side, maker_data in (("back", outcome_two_data), ("lay", outcome_one_data)):
                    raw_pct = maker_data.get("percentageOdds")
                    if raw_pct is None:
                        continue
                    try:
                        maker_prob = int(raw_pct) / _ODDS_SCALE
                    except (TypeError, ValueError):
                        continue

                    if side == "back":
                        # Taker backs Yes: their probability = 1 - No-maker probability
                        prob = 1.0 - maker_prob
                    else:
                        # Lay odds = what the backer gets = 1 / Yes-maker probability.
                        # The Yes-maker probability IS the implied probability of the outcome.
                        prob = maker_prob

                    if not (0.0 < prob < 1.0):
                        continue
                    records.append(
                        OddsRecord(
                            **shared,
                            selection_side=side,
                            decimal_odds=round(1.0 / prob, 6),
                            implied_probability=round(prob, 6),
                        )
                    )

        return records, warnings

    def _fetch_available(self, market_hashes: list[str]) -> dict[str, dict[str, float]]:
        """Return {marketHash: {outcome_one_avail_usd, outcome_two_avail_usd}} taker stakes."""
        try:
            raw = self.http_client.get_json(
                f"{self.settings.base_url}/orders",
                params={
                    "marketHashes": ",".join(market_hashes),
                    "baseToken": self.settings.base_token,
                },
            )
            orders = raw.get("data", [])
        except Exception:
            return {}

        result: dict[str, dict[str, float]] = {}
        for order in orders:
            h = order.get("marketHash")
            if not h:
                continue
            try:
                maker_avail = (int(order["totalBetSize"]) - int(order["fillAmount"])) / 1e6
                pct = int(order["percentageOdds"]) / 1e20
                if pct <= 0 or pct >= 1:
                    continue
                taker_avail = maker_avail * (1 - pct) / pct
                entry = result.setdefault(h, {"outcome_one_avail_usd": 0.0, "outcome_two_avail_usd": 0.0})
                # isMakerBettingOutcomeOne=False → taker backs outcomeOne
                if not order.get("isMakerBettingOutcomeOne"):
                    entry["outcome_one_avail_usd"] += taker_avail
                else:
                    entry["outcome_two_avail_usd"] += taker_avail
            except Exception:
                continue
        return result

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
        avail: dict[str, float] | None = None,
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

        # avail keys: outcome_one_avail_usd (taker backing outcomeOne = team_one)
        #             outcome_two_avail_usd (taker backing outcomeTwo = team_two)
        avail_one = avail.get("outcome_one_avail_usd") if avail else None
        avail_two = avail.get("outcome_two_avail_usd") if avail else None

        records: list[OddsRecord] = []
        for i, (team_name, maker_data) in enumerate(outcomes):
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
            avail_usd = avail_one if i == 0 else avail_two
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
                        "available_usd": round(avail_usd, 2) if avail_usd is not None else None,
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
