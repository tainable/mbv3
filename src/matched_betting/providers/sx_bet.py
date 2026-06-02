from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from matched_betting.config import SxBetSettings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.models import OddsRecord, ProviderPayload, utc_now_iso
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.base import GameContext, OddsProvider


LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "wnba": "basketball",
    "mlb": "baseball",
    "mlb_spread": "baseball",
    "mlb_totals": "baseball",
    "kbo": "baseball",
    "nhl": "ice_hockey",
    "ucl": "soccer",
    "epl": "soccer",
    "uel": "soccer",
    "seria": "soccer",
    "laliga": "soccer",
    "mls": "soccer",
    "mls_spread": "soccer",
    "mls_totals": "soccer",
    "ipl": "cricket",
    "veikkausliiga": "soccer",
}

# SX Bet league IDs (from GET /leagues)
_LEAGUE_IDS: dict[str, int | None] = {
    "nba": 1,
    "wnba": 1384,
    "mlb": 171,
    "mlb_spread": 171,
    "mlb_totals": 171,
    "kbo": 1389,
    "nhl": 3,
    "ucl": 30,
    "epl": 29,
    "uel": 31,
    "seria": 1113,
    "laliga": 1114,
    "mls": 1115,
    "mls_spread": 1115,
    "mls_totals": 1115,
    "ipl": 1192,
    "veikkausliiga": 1626,
}

# Leagues that use binary Yes/No markets per outcome rather than a moneyline
_SOCCER_LEAGUES: frozenset[str] = frozenset({"ucl", "epl", "uel", "seria", "laliga", "mls", "veikkausliiga"})

# Cricket leagues: two-way match winner markets (no draw, no overtime)
_CRICKET_LEAGUES: frozenset[str] = frozenset({"ipl"})

# SX Bet stores percentageOdds as an integer representing probability * 10^20
_ODDS_SCALE = 10**20

# Market type 226 = "Moneyline Including Overtime" (two-way winner market)
_MONEYLINE_TYPE = 226

# Spread (run-line / handicap) market type per league.
# Baseball run-line = type 342; MLS goal-line handicap = type 3.
_SPREAD_TYPE_BY_LEAGUE: dict[str, int] = {
    "mlb_spread": 342,
    "mls_spread": 3,
}

# Over/Under (totals) market type per league.
# Baseball totals = type 28; MLS goal totals = type 2.
_TOTALS_TYPE_BY_LEAGUE: dict[str, int] = {
    "mlb_totals": 28,
    "mls_totals": 2,
}

_SOCCER_RESULT_TYPE = 1

# Spread leagues (run-line for baseball, goal-line handicap for soccer)
_SPREAD_LEAGUES: frozenset[str] = frozenset({"mlb_spread", "mls_spread"})

# Totals leagues (over/under)
_TOTALS_LEAGUES: frozenset[str] = frozenset({"mlb_totals", "mls_totals"})

# Max hashes per API call — SX Bet returns 414 with long query strings
_BATCH_SIZE = 20


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

        if (any(lg in leagues for lg in _SOCCER_LEAGUES)
                or any(lg in leagues for lg in _CRICKET_LEAGUES)
                or any(lg in leagues for lg in _SPREAD_LEAGUES)):
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

            if league in _SOCCER_LEAGUES:
                soccer_records, soccer_warnings = self._process_soccer_markets(
                    hash_to_market, best_odds_map, avail_map, league, retrieved_at
                )
                records.extend(soccer_records)
                warnings.extend(soccer_warnings)
            else:
                for market_hash, market in hash_to_market.items():
                    best = best_odds_map.get(market_hash)

                    if best is None:
                        self.debug(f"{self.name}: no best odds for market {market_hash}, skipping")
                        continue
                    try:
                        market_records, market_warnings = self._market_to_records(
                            market, best, league, retrieved_at,
                            avail=avail_map.get(market_hash),
                        )
                        records.extend(market_records)
                        warnings.extend(market_warnings)
                        self.debug(
                            f"{self.name}: market {market_hash} -> {len(market_records)} records"
                        )
                    except Exception as exc:
                        warnings.append(f"Skipped SX Bet market {market_hash}: {exc}")
                        self.debug(f"{self.name}: skipped market {market_hash}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def fetch_odds_by_ids(
        self,
        game_contexts: list[GameContext],
        leagues: list[str],
    ) -> ProviderPayload:
        records: list[OddsRecord] = []
        warnings: list[str] = []
        retrieved_at = utc_now_iso()

        non_soccer_games = [
            g for g in game_contexts
            if g.get("league") in leagues
            and g.get("league") not in _SOCCER_LEAGUES
            and g.get("sx_bet_market_hash")
        ]
        soccer_leagues_in_scope = [
            lg for lg in _SOCCER_LEAGUES
            if lg in leagues
            and any(
                g.get("league") == lg and g.get("sx_bet_market_hash")
                for g in game_contexts
            )
        ]

        if non_soccer_games:
            all_hashes = [g["sx_bet_market_hash"] for g in non_soccer_games]
            self.debug(f"{self.name}: targeted fetch: batching {len(all_hashes)} market hashes")
            best_odds_map = self._fetch_best_odds(all_hashes)
            avail_map = self._fetch_available(all_hashes)

            for game in non_soccer_games:
                market_hash = game["sx_bet_market_hash"]
                league = game["league"]
                best = best_odds_map.get(market_hash)
                if best is None:
                    self.debug(f"{self.name}: no best odds for {market_hash}, skipping")
                    continue

                game_time_iso = game.get("date_time")
                game_time_unix: int | None = None
                if game_time_iso:
                    try:
                        game_time_unix = int(
                            datetime.fromisoformat(
                                game_time_iso.replace("Z", "+00:00")
                            ).timestamp()
                        )
                    except (ValueError, AttributeError):
                        pass

                # SX Bet's outcomeOne/outcomeTwo correspond to the original teamOneName/teamTwoName
                # from the market, which may differ from the canonical team1/team2 ordering.
                # Use the stored sx_bet_outcome_one_team to reconstruct the correct order.
                stored_outcome_one = game.get("sx_bet_outcome_one_team")
                team1_raw = game.get("team1") or ""
                team2_raw = game.get("team2") or ""
                if stored_outcome_one and stored_outcome_one == normalize_team_name(team2_raw, league):
                    sx_team_one, sx_team_two = team2_raw, team1_raw
                else:
                    sx_team_one, sx_team_two = team1_raw, team2_raw

                synthetic_market: dict[str, Any] = {
                    "marketHash": market_hash,
                    "teamOneName": sx_team_one,
                    "teamTwoName": sx_team_two,
                    "gameTime": game_time_unix,
                    "sportXEventId": None,
                    "leagueId": _LEAGUE_IDS.get(league),
                }

                # Pass stored spread so _market_to_records doesn't default to -1.5.
                # Canonical spread is home-team-perspective; SX Bet expects teamOne-perspective.
                # If teamOne is the away team (team2), negate to convert.
                stored_spread = game.get("spread")
                if stored_spread is not None and league in _SPREAD_LEAGUES:
                    raw_spread = -stored_spread if sx_team_one == team2_raw else stored_spread
                    synthetic_market["spread"] = raw_spread
                if league in _TOTALS_LEAGUES:
                    synthetic_market["line"] = game.get("total_line")
                try:
                    market_records, market_warnings = self._market_to_records(
                        synthetic_market, best, league, retrieved_at,
                        avail=avail_map.get(market_hash),
                    )
                    records.extend(market_records)
                    warnings.extend(market_warnings)
                    self.debug(
                        f"{self.name}: targeted fetch hash={market_hash} -> {len(market_records)} records"
                    )
                except Exception as exc:
                    warnings.append(f"Skipped SX Bet market {market_hash}: {exc}")
                    self.debug(f"{self.name}: targeted fetch: skipped market {market_hash}: {exc}")

        for soccer_league in soccer_leagues_in_scope:
            league_id = _LEAGUE_IDS.get(soccer_league)
            if league_id is None:
                warnings.append(f"{self.name}: no league ID for {soccer_league}, skipping targeted fetch")
                continue

            soccer_games = [
                g for g in game_contexts
                if g.get("league") == soccer_league and g.get("sx_bet_market_hash")
            ]

            # Partition into games that have stored per-outcome hashes (targeted)
            # and those that don't (fall back to full league scan).
            targeted_games = [
                g for g in soccer_games
                if g.get("sx_bet_team1_market_hash") and g.get("sx_bet_team2_market_hash")
            ]
            fallback_games = [g for g in soccer_games if g not in targeted_games]

            if targeted_games:
                self.debug(
                    f"{self.name}: targeted fetch: {soccer_league} using stored slot hashes"
                    f" for {len(targeted_games)} games"
                )
                for game in targeted_games:
                    slot_hashes: dict[str, str] = {}
                    for slot in ("team1", "draw", "team2"):
                        h = game.get(f"sx_bet_{slot}_market_hash")
                        if h:
                            slot_hashes[slot] = h

                    all_hashes = list(slot_hashes.values())
                    best_odds_map = self._fetch_best_odds(all_hashes)
                    avail_map = self._fetch_available(all_hashes)

                    team1_name = normalize_team_name(game.get("team1") or "", soccer_league)
                    team2_name = normalize_team_name(game.get("team2") or "", soccer_league)
                    slot_outcome_names = {"team1": team1_name, "draw": "draw", "team2": team2_name}

                    game_time_iso = game.get("date_time")
                    game_time_unix: int | None = None
                    if game_time_iso:
                        try:
                            game_time_unix = int(
                                datetime.fromisoformat(
                                    game_time_iso.replace("Z", "+00:00")
                                ).timestamp()
                            )
                        except (ValueError, AttributeError):
                            pass
                    event_start = _unix_to_iso(game_time_unix) if game_time_unix else None
                    event_name = f"{team1_name} vs {team2_name}"

                    for slot, market_hash in slot_hashes.items():
                        best = best_odds_map.get(market_hash)
                        if best is None:
                            continue
                        outcome_name = slot_outcome_names[slot]
                        avail = avail_map.get(market_hash)
                        avail_by_side = {
                            "back": round(avail["outcome_one_avail_usd"], 2) if avail else None,
                            "lay":  round(avail["outcome_two_avail_usd"], 2) if avail else None,
                        }
                        outcome_one_data = best.get("outcomeOne", {})
                        outcome_two_data = best.get("outcomeTwo", {})
                        for side, maker_data in (("back", outcome_two_data), ("lay", outcome_one_data)):
                            raw_pct = maker_data.get("percentageOdds")
                            if raw_pct is None:
                                continue
                            try:
                                maker_prob = int(raw_pct) / _ODDS_SCALE
                            except (TypeError, ValueError):
                                continue
                            prob = (1.0 - maker_prob) if side == "back" else maker_prob
                            if not (0.0 < prob < 1.0):
                                continue
                            records.append(
                                OddsRecord(
                                    provider=self.name,
                                    sport="soccer",
                                    league=soccer_league,
                                    event_name=event_name,
                                    event_start=event_start,
                                    market_name="Match Result",
                                    market_type="three_way",
                                    selection_name=outcome_name,
                                    selection_side=side,
                                    decimal_odds=round(1.0 / prob, 6),
                                    implied_probability=round(prob, 6),
                                    currency="USD",
                                    source_market_id=market_hash,
                                    source_event_id=None,
                                    retrieved_at=retrieved_at,
                                    metadata={
                                        "market_hash": market_hash,
                                        "game_time": game_time_unix,
                                        "league_id": league_id,
                                        "market_type": _SOCCER_RESULT_TYPE,
                                        "available_usd": avail_by_side[side],
                                    },
                                )
                            )

            if fallback_games:
                self.debug(
                    f"{self.name}: targeted fetch: {soccer_league} falling back to full league scan"
                    f" for {len(fallback_games)} games (no stored slot hashes)"
                )
                soccer_markets = self._fetch_markets(league_id, soccer_league)
                if soccer_markets:
                    market_hashes = [m["marketHash"] for m in soccer_markets]
                    hash_to_market = {m["marketHash"]: m for m in soccer_markets}
                    best_odds_map = self._fetch_best_odds(market_hashes)
                    avail_map = self._fetch_available(market_hashes)
                    soccer_records, soccer_warnings = self._process_soccer_markets(
                        hash_to_market, best_odds_map, avail_map, soccer_league, retrieved_at
                    )
                    records.extend(soccer_records)
                    warnings.extend(soccer_warnings)

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

        if league in _SOCCER_LEAGUES or league in _CRICKET_LEAGUES:
            type_counts: dict[Any, int] = {}
            for m in all_markets:
                t = m.get("type")
                type_counts[t] = type_counts.get(t, 0) + 1
            self.debug(f"{self.name}: {league.upper()} market types found across {len(all_markets)} markets:")
            for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                sample = next((m.get("marketName") or m.get("label") or m.get("type") for m in all_markets if m.get("type") == t), "")
                self.debug(f"{self.name}:   type={t}  count={count}  example={sample!r}")

        if league in _TOTALS_LEAGUES:
            totals_type = _TOTALS_TYPE_BY_LEAGUE.get(league)
            type_counts_t: dict[Any, int] = {}
            for m in all_markets:
                t = m.get("type")
                type_counts_t[t] = type_counts_t.get(t, 0) + 1
            self.debug(
                f"{self.name}: {league.upper()} market types for leagueId={league_id}: "
                + str({t: c for t, c in sorted(type_counts_t.items(), key=lambda x: -x[1])})
            )
            filtered = [
                m for m in all_markets
                if m.get("leagueId") == league_id
                and m.get("type") == totals_type
            ]
            if not filtered:
                self.debug(
                    f"{self.name}: {league}: 0 markets matched type {totals_type}; "
                    f"check _TOTALS_TYPE_BY_LEAGUE[{league!r}]"
                )
        elif league in _SPREAD_LEAGUES:
            spread_type = _SPREAD_TYPE_BY_LEAGUE.get(league)
            # Log all market types available for this league so we can identify the
            # correct spread type if _SPREAD_TYPE_BY_LEAGUE turns out to be wrong.
            type_counts: dict[Any, int] = {}
            for m in all_markets:
                t = m.get("type")
                type_counts[t] = type_counts.get(t, 0) + 1
            self.debug(
                f"{self.name}: {league.upper()} market types found across {len(all_markets)} markets "
                f"for leagueId={league_id}:"
            )
            for t, count in sorted(type_counts.items(), key=lambda x: -x[1]):
                sample = next(
                    (m.get("marketName") or m.get("label") or str(m.get("type"))
                     for m in all_markets if m.get("type") == t),
                    "",
                )
                self.debug(f"{self.name}:   type={t}  count={count}  example={sample!r}")

            # Spread markets filtered by per-league type ID.
            # Do NOT fall back to all two-team markets if this type is absent — that would
            # silently use moneyline (or other) markets and produce impossible spread odds.
            filtered = [
                m for m in all_markets
                if m.get("leagueId") == league_id
                and m.get("teamOneName")
                and m.get("teamTwoName")
                and m.get("type") == spread_type
            ]
            if not filtered:
                all_league_markets = [m for m in all_markets if m.get("leagueId") == league_id]
                self.debug(
                    f"{self.name}: {league}: 0 markets matched type {spread_type}; "
                    f"{len(all_league_markets)} total markets for leagueId={league_id}. "
                    f"Update _SPREAD_TYPE_BY_LEAGUE[{league!r}] based on the type counts above."
                )
        elif league in _CRICKET_LEAGUES:
            # Cricket match-winner markets are two-way (no draw, no overtime).
            # SX Bet may use type 226 or a sport-specific type; filter by leagueId
            # and require both teamOneName and teamTwoName to identify match-winner
            # markets without depending on a potentially-wrong type constant.
            # If leagueId 1192 is wrong, run find_sx_bet_league_ids.py --search cricket
            # to discover the correct ID and update _LEAGUE_IDS["ipl"].
            filtered = [
                m for m in all_markets
                if m.get("leagueId") == league_id
                and m.get("teamOneName")
                and m.get("teamTwoName")
                and m.get("type") == _MONEYLINE_TYPE  # type 226; update if SX Bet uses a different type for cricket
            ]
            if not filtered:
                # Fall back to any two-team market for this league (drop type filter)
                # so we can at least see what's available in debug output.
                all_league_markets = [m for m in all_markets if m.get("leagueId") == league_id]
                self.debug(
                    f"{self.name}: IPL: 0 markets matched type {_MONEYLINE_TYPE}; "
                    f"{len(all_league_markets)} total markets for leagueId={league_id}. "
                    f"If 0 total, league ID may be wrong — run find_sx_bet_league_ids.py --search cricket"
                )
                filtered = [
                    m for m in all_league_markets
                    if m.get("teamOneName") and m.get("teamTwoName")
                ]
                self.debug(f"{self.name}: IPL fallback: {len(filtered)} two-team markets without type filter")
        elif league in _SOCCER_LEAGUES:
            market_type = _SOCCER_RESULT_TYPE
            filtered = [
                m for m in all_markets
                if m.get("type") == market_type and m.get("leagueId") == league_id
            ]
        else:
            market_type = _MONEYLINE_TYPE
            filtered = [
                m for m in all_markets
                if m.get("type") == market_type and m.get("leagueId") == league_id
            ]

        self.debug(f"{self.name}: {len(all_markets)} total markets, {len(filtered)} after filtering for leagueId {league_id}")
        return filtered

    def _process_soccer_markets(
        self,
        hash_to_market: dict[str, dict[str, Any]],
        best_odds_map: dict[str, dict[str, Any]],
        avail_map: dict[str, dict[str, float]],
        league: str,
        retrieved_at: str,
    ) -> tuple[list[OddsRecord], list[str]]:
        """Handle soccer Yes/No binary markets (one market per outcome: home win, away win, draw).

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
                outcome_name = normalize_team_name(outcome_name, league)

                # Back "outcomeOne": taker prob = 1 - maker prob from outcomeTwo
                # Lay  "outcomeOne": taker prob = 1 - maker prob from outcomeOne
                outcome_one_data = best.get("outcomeOne", {})
                outcome_two_data = best.get("outcomeTwo", {})

                avail = avail_map.get(market_hash)
                # back of outcomeOne → taker is betting on outcomeOne → outcome_one_avail_usd
                # lay  of outcomeOne → taker is betting on outcomeTwo → outcome_two_avail_usd
                avail_by_side = {
                    "back": round(avail["outcome_one_avail_usd"], 2) if avail else None,
                    "lay":  round(avail["outcome_two_avail_usd"], 2) if avail else None,
                }

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
                            provider=self.name,
                            sport="soccer",
                            league=league,
                            event_name=event_name,
                            event_start=event_start,
                            market_name="Match Result",
                            market_type="three_way",
                            selection_name=outcome_name,
                            selection_side=side,
                            decimal_odds=round(1.0 / prob, 6),
                            implied_probability=round(prob, 6),
                            currency="USD",
                            source_market_id=market_hash,
                            source_event_id=str(event_id) if event_id != "unknown" else None,
                            retrieved_at=retrieved_at,
                            metadata={
                                "market_hash": market_hash,
                                "game_time": game_time,
                                "league_id": market.get("leagueId"),
                                "market_type": market.get("type"),
                                "available_usd": avail_by_side[side],
                            },
                        )
                    )

        return records, warnings

    def _fetch_available(self, market_hashes: list[str]) -> dict[str, dict[str, float]]:
        """Return {marketHash: {outcome_one_avail_usd, outcome_two_avail_usd}} taker stakes.

        Sends hashes in batches of _BATCH_SIZE to avoid HTTP 414.
        """
        result: dict[str, dict[str, float]] = {}
        for i in range(0, len(market_hashes), _BATCH_SIZE):
            batch = market_hashes[i : i + _BATCH_SIZE]
            try:
                raw = self.http_client.get_json(
                    f"{self.settings.base_url}/orders",
                    params={
                        "marketHashes": ",".join(batch),
                        "baseToken": self.settings.base_token,
                    },
                )
                orders = raw.get("data", [])
            except Exception:
                continue
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
        'outcomeTwo' sub-dicts with 'percentageOdds' strings.  Sends hashes in
        batches of _BATCH_SIZE to avoid HTTP 414.
        """
        result: dict[str, dict[str, Any]] = {}
        for i in range(0, len(market_hashes), _BATCH_SIZE):
            batch = market_hashes[i : i + _BATCH_SIZE]
            raw = self.http_client.get_json(
                f"{self.settings.base_url}/orders/odds/best",
                params={
                    "marketHashes": ",".join(batch),
                    "baseToken": self.settings.base_token,
                },
            )
            best_odds_list: list[dict[str, Any]] = raw.get("data", {}).get("bestOdds", [])
            for entry in best_odds_list:
                if "marketHash" in entry:
                    result[entry["marketHash"]] = entry
        return result

    def _market_to_records(
        self,
        market: dict[str, Any],
        best: dict[str, Any],
        league: str,
        retrieved_at: str,
        avail: dict[str, float] | None = None,
    ) -> tuple[list[OddsRecord], list[str]]:
        market_hash = market["marketHash"]
        game_time = market.get("gameTime")
        event_start = _unix_to_iso(game_time) if game_time else None
        source_event_id = (
            str(market["sportXEventId"]) if market.get("sportXEventId") else None
        )

        if league in _TOTALS_LEAGUES:
            # Totals (over/under) market: teamOneName/teamTwoName are the actual teams;
            # outcomeOneName = "Over X.X", outcomeTwoName = "Under X.X".
            team_one = normalize_team_name(market.get("teamOneName", ""), league)
            team_two = normalize_team_name(market.get("teamTwoName", ""), league)
            event_name = f"{team_one} vs. {team_two}"
            sport = LEAGUE_TO_SPORT[league]
            try:
                total_line = float(market.get("line") or 0) or None
            except (TypeError, ValueError):
                total_line = None

            outcome_one_data = best.get("outcomeOne", {})
            outcome_two_data = best.get("outcomeTwo", {})
            avail_one = avail.get("outcome_one_avail_usd") if avail else None
            avail_two = avail.get("outcome_two_avail_usd") if avail else None

            records: list[OddsRecord] = []
            # outcomeOne = Over; taker backing Over uses outcomeTwo maker orders
            # outcomeTwo = Under; taker backing Under uses outcomeOne maker orders
            for ou_name, maker_data, avail_usd in [
                ("over",  outcome_two_data, avail_one),
                ("under", outcome_one_data, avail_two),
            ]:
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
                records.append(
                    OddsRecord(
                        provider=self.name,
                        sport=sport,
                        league=league,
                        event_name=event_name,
                        event_start=event_start,
                        market_name="Total Runs",
                        market_type="two_way",
                        selection_name=ou_name,
                        selection_side="back",
                        decimal_odds=round(1.0 / taker_prob, 6),
                        implied_probability=round(taker_prob, 6),
                        currency="USD",
                        source_market_id=market_hash,
                        source_event_id=source_event_id,
                        retrieved_at=retrieved_at,
                        metadata={
                            "market_hash": market_hash,
                            "game_time": game_time,
                            "league_id": market.get("leagueId"),
                            "market_type": _TOTALS_TYPE_BY_LEAGUE.get(league),
                            "total_line": total_line,
                            "available_usd": round(avail_usd, 2) if avail_usd is not None else None,
                            "outcome_one_team": "over",
                        },
                    )
                )
            return records, []

        team_one: str = normalize_team_name(market["teamOneName"], league)
        team_two: str = normalize_team_name(market["teamTwoName"], league)
        event_name = f"{team_one} vs. {team_two}"
        sport = LEAGUE_TO_SPORT[league]
        market_display_name = "Run Line" if league in _SPREAD_LEAGUES else "Moneyline Incl. OT"

        method_warnings: list[str] = []
        spread: float | None = None
        spread_favourite: str | None = None
        if league in _SPREAD_LEAGUES:
            raw = market.get("spread") or market.get("line")
            
            try:
                spread = float(raw)

            except (TypeError, ValueError):
                spread = None
                method_warnings.append(
                    f"sx_bet {league} market {market.get('marketHash', 'unknown')}: "
                    "spread not found in market data"
                )
            # teamOne is the side at -1.5 when spread < 0; teamTwo when spread > 0.
            # Storing this lets event_matching.py apply the home-perspective flip
            # the same way it does for Polymarket records.
            if spread is not None:
                spread_favourite = team_one if spread <= 0 else team_two

            # For MLS spread, only accept half-ball lines (0.5, 1.5, 2.5 …).
            # Integers (1.0, 2.0 …) can push; quarter-balls (0.25, 0.75 …)
            # split the stake across two adjacent lines.  Both are non-standard
            # and Polymarket only offers half-ball lines anyway.
            # A value is half-ball iff abs(val % 1 - 0.5) < 0.1, i.e. the
            # fractional part is close to 0.5 — same logic as _is_halfball_handicap
            # in matchbook.py.
            if league == "mls_spread" and spread is not None:
                val = abs(spread)
                if abs(val - round(val)) <= 0.4:  # not a X.5 value
                    self.debug(
                        f"{self.name}: skipping mls_spread market {market.get('marketHash', 'unknown')} "
                        f"— non-half-ball spread={spread}"
                    )
                    return [], []

        # In SX Bet's P2P model, percentageOdds is the maker's probability * 10^20.
        # The taker backing team_one is matched against makers betting on team_two,
        # so taker implied probability = 1 - (outcomeTwo.percentageOdds / scale),
        # and vice versa for team_two.
        if league in _SPREAD_LEAGUES:
            self.debug(
                f"{self.name}: spread market {market_hash} raw best={best!r} "
                f"teamOne={market.get('teamOneName')!r} teamTwo={market.get('teamTwoName')!r} "
                f"type={market.get('type')!r}"
            )
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
                    market_name=market_display_name,
                    market_type="two_way",
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
                        "market_type": _SPREAD_TYPE_BY_LEAGUE.get(league, _MONEYLINE_TYPE) if league in _SPREAD_LEAGUES else _MONEYLINE_TYPE,
                        "spread": spread,
                        "spread_favourite": spread_favourite,
                        "available_usd": round(avail_usd, 2) if avail_usd is not None else None,
                        # Stored so update mode can reconstruct the correct synthetic
                        # market order without a round-trip to the SX Bet API.
                        "outcome_one_team": team_one,
                    },
                )
            )
        # Sanity check: in any binary two-way market the implied-probability sum must be
        # >= 1.0 (market has positive margin for the book) and realistically <= 1.30.
        # Sums below 0.80 mean we're reading the wrong market type or wrong formula.
        if len(records) == 2 and league in _SPREAD_LEAGUES:
            prob_sum = records[0].implied_probability + records[1].implied_probability
            if prob_sum < 0.80:
                method_warnings.append(
                    f"sx_bet {league} market {market_hash}: impossible implied-prob sum "
                    f"{prob_sum:.2%} (expected ≥80%); market type may be wrong. "
                    f"Check _SPREAD_TYPE_BY_LEAGUE[{league!r}] ({_SPREAD_TYPE_BY_LEAGUE.get(league)}) vs debug output. Skipping."
                )
                self.debug(
                    f"{self.name}: SKIPPED {market_hash} — implied-prob sum {prob_sum:.2%}, "
                    f"type={market.get('type')!r}"
                )
                return [], method_warnings
        return records, method_warnings


def _unix_to_iso(ts: int | float) -> str:
    return (
        datetime.fromtimestamp(ts, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
