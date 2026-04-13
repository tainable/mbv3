from __future__ import annotations

import json
from typing import Any

from matched_betting.config import PolymarketSettings
from matched_betting.debug import DebugLogger
from matched_betting.http import HttpClient
from matched_betting.models import OddsRecord, ProviderPayload, decimal_from_probability, utc_now_iso
from matched_betting.normalization import normalize_team_name
from matched_betting.providers.base import GameContext, OddsProvider



LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "mlb": "baseball",
    "ucl": "soccer",
    "epl": "soccer",
}

# Leagues that use Yes/No binary markets per outcome (soccer-style)
_SOCCER_LEAGUES: frozenset[str] = frozenset({"ucl", "epl"})


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

            _nba_non_moneyline = {"spread", "total", "over", "under", "cover", "ats"}
            if "nba" in leagues and "nba" in split_slug[0]:
                if not any(seg in _nba_non_moneyline for seg in split_slug):
                    if split_slug[1] in ['atl','phx','lal','ind','chi','phi',
                    'okc','bos','mia','cle','sas','mem','was','uta',
                    'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:

                        if split_slug[2] in ['atl','phx','lal','ind','chi','phi',
                    'okc','bos','mia','cle','sas','mem','was','uta',
                    'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:
                            league = 'nba'
                            candidate_markets.append((market, league))

            _mlb_non_moneyline = {"spread", "total", "over", "under", "runline", "run-line"}
            if "mlb" in leagues and "mlb" in split_slug[0]:
                if not any(seg in _mlb_non_moneyline for seg in split_slug):
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

                # Match head-to-head UCL slugs: ucl-{team1}-{team2}-{date}
                # Markets may not exist until close to match day
                if split_slug[0] == "ucl" and len(split_slug) >= 3:
                    if split_slug[1] in ucl_teams and split_slug[2] in ucl_teams:
                        if split_slug[-1] in ucl_teams or split_slug[-1] == "draw":
                            candidate_markets.append((market, "ucl"))

            epl_teams = team_index.get("epl", [])
            if "epl" in leagues and epl_teams:
                # Match head-to-head EPL slugs: epl-{team1}-{team2}-{date}-{team_or_draw}
                if split_slug[0] == "epl" and len(split_slug) >= 3:
                    if split_slug[1] in epl_teams and split_slug[2] in epl_teams:
                        if split_slug[-1] in epl_teams or split_slug[-1] == "draw":
                            candidate_markets.append((market, "epl"))

        
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
                slug = str(market.get("slug", "unknown"))
                warnings.append(f"Skipped Polymarket market {market_id} ({slug}): {exc}")
                self.debug(f"{self.name}: skipped market {market_id} ({slug}): {exc}")
        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def fetch_odds_by_ids(
        self,
        game_contexts: list[GameContext],
        leagues: list[str],
    ) -> ProviderPayload:
        """Targeted refresh using stored CLOB token IDs where available.

        For each game, prefers reading the per-slot CLOB token IDs stored in
        the aggregated games output (polymarket_team1_clob_token_id etc.) and
        hitting the CLOB book API directly, skipping the intermediate Gamma
        lookup.  Falls back to a Gamma API call when stored token IDs are
        absent (e.g. on first run after a full fetch that pre-dates this change).

        This mirrors the EPL per-slot approach for all leagues:
          - Soccer (EPL/UCL): team1 / draw / team2 slots, market_type three_way
          - NBA/MLB:          team1 / team2 slots,         market_type two_way
        """
        retrieved_at = utc_now_iso()
        records: list[OddsRecord] = []
        warnings: list[str] = []

        for game in game_contexts:
            league = game.get("league")
            if league not in leagues:
                continue

            team1 = game.get("team1") or ""
            team2 = game.get("team2") or ""
            event_name = f"{team1} vs {team2}" if team1 and team2 else "Unknown event"
            event_start = game.get("date_time")

            if league in _SOCCER_LEAGUES:
                market_type = "three_way"
                slots: list[tuple[str, str | None, str | None]] = [
                    (
                        normalize_team_name(team1, league),
                        game.get("polymarket_team1_clob_token_id"),
                        game.get("polymarket_team1_market_id"),
                    ),
                    (
                        "draw",
                        game.get("polymarket_draw_clob_token_id"),
                        game.get("polymarket_draw_market_id"),
                    ),
                    (
                        normalize_team_name(team2, league),
                        game.get("polymarket_team2_clob_token_id"),
                        game.get("polymarket_team2_market_id"),
                    ),
                ]
                for outcome_name, clob_token_id, market_id in slots:
                    if not outcome_name:
                        continue
                    if clob_token_id:
                        self.debug(f"{self.name}: CLOB direct token={clob_token_id} outcome={outcome_name!r}")
                        try:
                            slot_records = self._fetch_clob_records_by_token(
                                clob_token_id, market_id or "", league, outcome_name,
                                market_type, event_name, event_start, retrieved_at,
                            )
                            records.extend(slot_records)
                            self.debug(f"{self.name}: CLOB token={clob_token_id} -> {len(slot_records)} records")
                        except Exception as exc:
                            warnings.append(f"Skipped Polymarket CLOB token {clob_token_id}: {exc}")
                            self.debug(f"{self.name}: CLOB: skipped token {clob_token_id}: {exc}")
                    elif market_id:
                        # Fallback: resolve token ID via Gamma (older aggregated games output)
                        self.debug(f"{self.name}: CLOB gamma-lookup market_id={market_id} outcome={outcome_name!r}")
                        try:
                            slot_records = self._fetch_clob_binary_records(
                                market_id, league, outcome_name, event_name, event_start, retrieved_at,
                            )
                            records.extend(slot_records)
                            self.debug(f"{self.name}: CLOB market_id={market_id} -> {len(slot_records)} records")
                        except Exception as exc:
                            warnings.append(f"Skipped Polymarket CLOB market {market_id}: {exc}")
                            self.debug(f"{self.name}: CLOB: skipped market {market_id}: {exc}")
            else:
                # NBA / MLB — two-way markets; per-slot token IDs stored alongside market ID
                market_type = "two_way"
                market_id = game.get("polymarket_market_id")
                team1_name = normalize_team_name(team1, league)
                team2_name = normalize_team_name(team2, league)
                team1_token = game.get("polymarket_team1_clob_token_id")
                team2_token = game.get("polymarket_team2_clob_token_id")

                if team1_token or team2_token:
                    # Use stored CLOB token IDs directly, one book call per outcome
                    for outcome_name, clob_token_id, slot_market_id in [
                        (team1_name, team1_token, game.get("polymarket_team1_market_id") or market_id),
                        (team2_name, team2_token, game.get("polymarket_team2_market_id") or market_id),
                    ]:
                        if not clob_token_id:
                            continue
                        self.debug(f"{self.name}: CLOB direct token={clob_token_id} outcome={outcome_name!r}")
                        try:
                            slot_records = self._fetch_clob_records_by_token(
                                clob_token_id, slot_market_id or "", league, outcome_name,
                                market_type, event_name, event_start, retrieved_at,
                            )
                            records.extend(slot_records)
                            self.debug(f"{self.name}: CLOB token={clob_token_id} -> {len(slot_records)} records")
                        except Exception as exc:
                            warnings.append(f"Skipped Polymarket CLOB token {clob_token_id}: {exc}")
                            self.debug(f"{self.name}: CLOB: skipped token {clob_token_id}: {exc}")
                elif market_id:
                    # Fallback: resolve token IDs via Gamma (older aggregated games output)
                    self.debug(f"{self.name}: CLOB gamma-lookup market_id={market_id} league={league}")
                    try:
                        game_records = self._fetch_clob_moneyline_records(
                            market_id, league, event_name, event_start, retrieved_at,
                        )
                        records.extend(game_records)
                        self.debug(f"{self.name}: CLOB market_id={market_id} -> {len(game_records)} records")
                    except Exception as exc:
                        warnings.append(f"Skipped Polymarket CLOB market {market_id}: {exc}")
                        self.debug(f"{self.name}: CLOB: skipped market {market_id}: {exc}")

        return ProviderPayload(provider=self.name, records=records, warnings=warnings)

    def _fetch_clob_records_by_token(
        self,
        clob_token_id: str,
        source_market_id: str,
        league: str,
        outcome_name: str,
        market_type: str,
        event_name: str,
        event_start: str | None,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        """Fetch a single CLOB book using a known token ID, skipping the Gamma lookup.

        Used in update mode when the token ID is already stored in the aggregated
        games output, avoiding a round-trip to the Gamma API per outcome slot.
        """
        best_ask, best_bid, total_ask_size = self._fetch_clob_book(clob_token_id)
        shared: dict = dict(
            provider=self.name,
            sport=LEAGUE_TO_SPORT[league],
            league=league,
            event_name=event_name,
            event_start=event_start,
            market_name=event_name,
            market_type=market_type,
            selection_name=outcome_name,
            currency="USD",
            source_market_id=source_market_id,
            source_event_id=None,
            retrieved_at=retrieved_at,
            metadata={"token_id": clob_token_id, "liquidity_usd": total_ask_size * 2},
        )
        result: list[OddsRecord] = []
        if best_ask and 0 < best_ask < 1:
            result.append(OddsRecord(
                **shared,
                selection_side="back",
                decimal_odds=decimal_from_probability(best_ask),
                implied_probability=round(best_ask, 6),
            ))
        if best_bid and 0 < best_bid < 1:
            result.append(OddsRecord(
                **shared,
                selection_side="lay",
                decimal_odds=decimal_from_probability(best_bid),
                implied_probability=round(best_bid, 6),
            ))
        return result

    def _fetch_clob_book(self, token_id: str) -> tuple[float | None, float | None, float]:
        """Return (best_ask, best_bid, total_ask_size_usd) for a CLOB token."""
        book = self.http_client.get_json(
            f"{self.settings.clob_base_url}/book",
            params={"token_id": token_id},
        )
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        best_ask = float(asks[-1]["price"]) if asks else None
        best_bid = float(bids[-1]["price"]) if bids else None
        total_ask_size = sum(float(a.get("size", 0)) for a in asks)
        return best_ask, best_bid, total_ask_size

    def _fetch_gamma_token_ids(self, market_id: str) -> tuple[list[str], list[str]]:
        """Return (outcomes, clob_token_ids) for a Gamma numeric market ID."""
        raw = self.http_client.get_json(
            f"{self.settings.gamma_base_url}/markets",
            params={"id": market_id},
        )
        market_data = raw[0] if isinstance(raw, list) else raw
        outcomes = _parse_stringified_json_list(market_data.get("outcomes"))
        clob_token_ids = _parse_stringified_json_list(market_data.get("clobTokenIds"))
        return outcomes, clob_token_ids

    def _fetch_clob_binary_records(
        self,
        market_id: str,
        league: str,
        outcome_name: str,
        event_name: str,
        event_start: str | None,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        """Fetch a soccer Yes/No binary market from CLOB and return OddsRecords.

        outcome_name is already the meaningful label (team name or 'draw');
        the Yes token maps to that outcome.
        """
        outcomes, clob_token_ids = self._fetch_gamma_token_ids(market_id)
        yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "yes"), None)
        if yes_idx is None or yes_idx >= len(clob_token_ids):
            raise ValueError(f"Market {market_id} has no Yes token in Gamma clobTokenIds")
        token_id = clob_token_ids[yes_idx]

        best_ask, best_bid, total_ask_size = self._fetch_clob_book(token_id)

        shared: dict = dict(
            provider=self.name,
            sport=LEAGUE_TO_SPORT[league],
            league=league,
            event_name=event_name,
            event_start=event_start,
            market_name=event_name,
            market_type="three_way",
            selection_name=outcome_name,
            currency="USD",
            source_market_id=market_id,
            source_event_id=None,
            retrieved_at=retrieved_at,
            metadata={"token_id": token_id, "liquidity_usd": total_ask_size * 2},
        )
        records: list[OddsRecord] = []
        if best_ask and 0 < best_ask < 1:
            records.append(OddsRecord(
                **shared,
                selection_side="back",
                decimal_odds=decimal_from_probability(best_ask),
                implied_probability=round(best_ask, 6),
            ))
        if best_bid and 0 < best_bid < 1:
            records.append(OddsRecord(
                **shared,
                selection_side="lay",
                decimal_odds=decimal_from_probability(best_bid),
                implied_probability=round(best_bid, 6),
            ))
        return records

    def _fetch_clob_moneyline_records(
        self,
        market_id: str,
        league: str,
        event_name: str,
        event_start: str | None,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        """Fetch a two-way moneyline market (NBA/MLB) from CLOB and return OddsRecords."""
        outcomes, clob_token_ids = self._fetch_gamma_token_ids(market_id)
        if not clob_token_ids:
            raise ValueError(f"Market {market_id} has no clobTokenIds in Gamma")

        records: list[OddsRecord] = []
        for outcome_raw, token_id in zip(outcomes, clob_token_ids):
            if not token_id or not outcome_raw:
                continue
            outcome_name = normalize_team_name(outcome_raw, league)

            best_ask, _, total_ask_size = self._fetch_clob_book(token_id)
            if not best_ask or not (0 < best_ask < 1):
                continue

            records.append(OddsRecord(
                provider=self.name,
                sport=LEAGUE_TO_SPORT[league],
                league=league,
                event_name=event_name,
                event_start=event_start,
                market_name=event_name,
                market_type="two_way",
                selection_name=outcome_name,
                selection_side="back",
                decimal_odds=decimal_from_probability(best_ask),
                implied_probability=round(best_ask, 6),
                currency="USD",
                source_market_id=market_id,
                source_event_id=None,
                retrieved_at=retrieved_at,
                metadata={"token_id": token_id, "liquidity_usd": total_ask_size * 2},
            ))
        return records

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

            elif league == "epl":
                # ODO: verify exact Polymarket team slug codes for EPL
                index[league] = [
                    'ars','che','liv','mac','mun','tot','new','ast',
                    'bri','wes','wol','cry','ful','bre','eve','bou','not',
                    'bur','lee','sun',  # promoted: Burnley, Leeds, Sunderland
                ]

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
        if "premier league" in question or "epl" in question:
            return "epl"

        return None

    def _market_to_records(
        self,
        market: dict[str, Any],
        league: str,
        retrieved_at: str,
    ) -> list[OddsRecord]:
        outcomes = _parse_stringified_json_list(market.get("outcomes"))
        clob_token_ids = _parse_stringified_json_list(market.get("clobTokenIds"))

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
                best_ask = float(market.get("bestAsk"))
            except (TypeError, ValueError):
                best_ask = None
            try:
                best_bid = float(market.get("bestBid"))
            except (TypeError, ValueError):
                best_bid = None

            # bestAsk/bestBid refer to the YES (primary) outcome of this market,
            # which Polymarket identifies via groupItemTitle. Find which outcomes
            # index that team sits at and assign prices accordingly.
            yes_idx = _yes_outcome_index(outcomes, market.get("groupItemTitle"), league)
            no_idx = 1 - yes_idx
            back_probs = [None, None]
            back_probs[yes_idx] = best_ask
            back_probs[no_idx] = (1.0 - best_bid) if best_bid is not None else None
            lay_probs = [None, None]
        else:
            raise ValueError(f"unexpected outcome count: {len(outcomes)}")

        if not back_probs or len(back_probs) != len(outcomes) or any(p is None for p in back_probs):
            raise ValueError("missing or mismatched outcomes/prices")

        event = (market.get("events") or [{}])[0]

        # Polymarket soccer markets (UCL/EPL) are typically Yes/No binary markets per outcome.
        # "Yes" = this team/outcome wins; remap to the meaningful name from groupItemTitle.
        # "No" is dropped — its probability is the complement and is emitted as a lay record.
        if league in _SOCCER_LEAGUES and {o.lower() for o in outcomes} <= {"yes", "no"}:
            group_title = market.get("groupItemTitle")
            if not group_title:
                raise ValueError("UCL Yes/No market missing groupItemTitle for outcome name")
            yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "yes"), None)
            if yes_idx is None:
                raise ValueError("UCL Yes/No market has no 'Yes' outcome")
            yes_token = clob_token_ids[yes_idx] if yes_idx < len(clob_token_ids) else None
            clob_token_ids = [yes_token] if yes_token else []
            outcomes = [group_title]
            try:
                back_probs = [float(market.get("bestAsk"))]
            except (TypeError, ValueError):
                raise ValueError("UCL Yes/No market missing bestAsk for back price")
            try:
                lay_probs = [float(market.get("bestBid"))]
            except (TypeError, ValueError):
                raise ValueError("UCL Yes/No market missing bestBid for lay price")

        if league in _SOCCER_LEAGUES:
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
            title = event.get("title") or ""
            _separators = (" vs ", " vs. ", " v ", " at ", " @ ")
            if any(sep in title.lower() for sep in _separators):
                event_name = title
            elif len(outcomes) == 2:
                # Build a parseable "X vs Y" from normalized outcomes so canonical
                # matching works even when the event title is absent or unparseable.
                t0 = normalize_team_name(str(outcomes[0]), league)
                t1 = normalize_team_name(str(outcomes[1]), league)
                event_name = f"{t0} vs {t1}"
            else:
                event_name = (
                    market.get("groupItemTitle")
                    or market.get("question")
                    or "Unknown event"
                )

        if league in ("mlb", "ucl", "epl"):
            event_start = event.get("startTime")
        else:
            event_start = event.get("endDate")

        if league in _SOCCER_LEAGUES:
            # For soccer, market_name should describe the game so each record is self-describing
            # alongside selection_name (which identifies the specific team/draw outcome)
            market_name = event_name
        else:
            market_name = market.get("groupItemTitle") or market.get("question") or "Unknown market"

        # For single-outcome Yes/No remapped markets, infer_market_type would return "multi_way"
        # (only 1 outcome left). UCL markets are three-way (home/draw/away); all other
        # single-outcome remapped markets fall back to two_way for the moneyline filter.
        if len(outcomes) == 1:
            market_type = "three_way" if league in _SOCCER_LEAGUES else "two_way"
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
                "liquidity_usd": market.get("liquidityNum"),
            },
        )

        records: list[OddsRecord] = []

        for i, (outcome, back_prob) in enumerate(zip(outcomes, back_probs)):

            if "draw" in outcome.lower():
                outcome = "draw"
            outcome = normalize_team_name(str(outcome), league)
            try:
                iprob = round(back_prob, 6)
            except (TypeError, ValueError):
                iprob = None

            clob_token = clob_token_ids[i] if i < len(clob_token_ids) else None
            record_kw = {**shared, "metadata": {**shared["metadata"], "clob_token_id": clob_token}}

            records.append(
                OddsRecord(
                    **record_kw,
                    selection_name=str(outcome),
                    selection_side="back",
                    decimal_odds=decimal_from_probability(back_prob),
                    implied_probability=iprob,
                )
            )

            # Lay record: only emit when we have an explicit lay price (e.g. UCL Yes/No
            # markets where bestBid gives the layer's implied probability).
            # For two-way markets (NBA/MLB), "laying" one side is just backing the other —
            # there is no independent exchange lay, so we skip it.
            explicit_lay = lay_probs[i] if lay_probs and i < len(lay_probs) else None
            if explicit_lay is not None and 0 < explicit_lay < 1:
                records.append(
                    OddsRecord(
                        **record_kw,
                        selection_name=str(outcome),
                        selection_side="lay",
                        decimal_odds=decimal_from_probability(explicit_lay),
                        implied_probability=round(explicit_lay, 6),
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


def _yes_outcome_index(outcomes: list[str], group_item_title: str | None, league: str) -> int:
    """Return the outcomes index that bestAsk/bestBid refer to.

    On Polymarket, bestAsk and bestBid track the YES token for the market's
    primary outcome, which groupItemTitle names. If groupItemTitle matches
    outcomes[1] (not outcomes[0]), the prices must be assigned in reverse.
    Falls back to 0 if groupItemTitle is absent or doesn't match either outcome.
    """
    if not group_item_title:
        return 0
    normalized_title = normalize_team_name(group_item_title, league)
    for i, outcome in enumerate(outcomes):
        if normalize_team_name(str(outcome), league) == normalized_title:
            return i
    return 0


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

