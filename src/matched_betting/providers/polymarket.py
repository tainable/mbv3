from __future__ import annotations

import itertools
import json
from dataclasses import replace as _dc_replace
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
    "mlb_spread": "baseball",
    "mlb_totals": "baseball",
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
}

# Leagues that use Yes/No binary markets per outcome (soccer-style)
_SOCCER_LEAGUES: frozenset[str] = frozenset({"ucl", "epl", "uel", "seria", "laliga", "mls"})

# Game markets live in /events (not /markets); map league → Polymarket tag_slug
_LEAGUE_EVENT_TAGS: dict[str, str] = {
    "epl": "premier-league",
    "ucl": "champions-league",
    "uel": "uel",
    "seria": "sea",
    "laliga": "la-liga",
    "nba": "nba",
    "mlb": "mlb",
    "mlb_spread": "mlb",
    "mlb_totals": "mlb",
    "nhl": "nhl",
    "ipl": "indian-premier-league",
    # MLS: tag slug is "mls"; base events (1x2) and -more-markets events (spread/totals)
    "mls": "mls",
    "mls_spread": "mls",
    "mls_totals": "mls",
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
        retrieved_at = utc_now_iso()

        records: list[OddsRecord] = []
        warnings: list[str] = []

        half_segments = {"1h", "2h", "1st-half", "2nd-half", "first-half", "second-half", "halftime"}

        candidate_markets: list[tuple[dict[str, Any], str]] = []
        _seen_ipl_pairs: set[frozenset] = set()
        _seen_market_ids: set[str] = set()
        total_seen = 0
        for market in itertools.chain(self._stream_markets(), self._stream_event_markets(leagues)):
            mid = str(market.get("id", ""))
            if mid and mid in _seen_market_ids:
                continue
            if mid:
                _seen_market_ids.add(mid)
            total_seen += 1
            league = self._infer_league(market, team_index)

            split_slug = market['slug'].split("-")

            # Skip 1st/2nd half markets (e.g. nba-bos-lal-1h)
            if any(seg in half_segments for seg in split_slug):
                continue

            # NBA moneyline slugs are exactly: nba-{team1}-{team2}-{yyyy}-{mm}-{dd} (6 parts).
            # Any extra segments indicate props/alternates (e.g. team-to-score-first, odd-even).
            _nba_non_moneyline = {"spread", "total", "over", "under", "cover", "ats"}
            if "nba" in leagues and "nba" in split_slug[0]:
                if len(split_slug) == 6 and not any(seg in _nba_non_moneyline for seg in split_slug):
                    if split_slug[1] in ['atl','phx','lal','ind','chi','phi',
                    'okc','bos','mia','cle','sas','mem','was','uta',
                    'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:

                        if split_slug[2] in ['atl','phx','lal','ind','chi','phi',
                    'okc','bos','mia','cle','sas','mem','was','uta',
                    'hou','min','mil','por','bkn','gsw','dal','den','tor','lac','nop','det','nyk','cha','sac','orl']:
                            league = 'nba'
                            candidate_markets.append((market, league))

            # MLB moneyline slugs: mlb-{team1}-{team2}-{yyyy}-{mm}-{dd} (6 parts).
            # Doubleheaders may add a game number suffix (7 parts, e.g. -2). 8+ = props/alternates.
            _mlb_non_moneyline = {"spread", "total", "over", "under", "runline", "run-line"}
            if "mlb" in leagues and "mlb" in split_slug[0]:
                if len(split_slug) <= 7 and not any(seg in _mlb_non_moneyline for seg in split_slug):
                    if split_slug[1] in ['cws','mil','wsh','chc','min','bal',
                    'cin','bos','laa','hou','det','sd','tex','phi',
                    'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:

                        if split_slug[2] in ['cws','mil','wsh','chc','min','bal',
                    'cin','bos','laa','hou','det','sd','tex','phi',
                    'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                            league = 'mlb'
                            candidate_markets.append((market, league))

            _mlb_run_line_keywords = {"spread"}
            if "mlb_spread" in leagues and split_slug[0] == "mlb":
                if any(seg in _mlb_run_line_keywords for seg in split_slug):
                    if split_slug[1] in ['cws','mil','wsh','chc','min','bal',
                    'cin','bos','laa','hou','det','sd','tex','phi',
                    'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                        if split_slug[2] in ['cws','mil','wsh','chc','min','bal',
                    'cin','bos','laa','hou','det','sd','tex','phi',
                    'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                            candidate_markets.append((market, 'mlb_spread'))

            _mlb_totals_keywords = {"total"}
            if "mlb_totals" in leagues and split_slug[0] == "mlb":
                if any(seg in _mlb_totals_keywords for seg in split_slug):
                    if len(split_slug) >= 3 and split_slug[1] in ['cws','mil','wsh','chc','min','bal',
                    'cin','bos','laa','hou','det','sd','tex','phi',
                    'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                        if split_slug[2] in ['cws','mil','wsh','chc','min','bal',
                    'cin','bos','laa','hou','det','sd','tex','phi',
                    'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']:
                            candidate_markets.append((market, 'mlb_totals'))

            # NHL moneyline slugs: nhl-{team1}-{team2}-{yyyy}-{mm}-{dd} (6 parts).
            _nhl_non_moneyline = {"spread", "total", "over", "under", "puck-line", "puckline"}
            if "nhl" in leagues and "nhl" in split_slug[0]:
                if len(split_slug) == 6 and not any(seg in _nhl_non_moneyline for seg in split_slug):
                    nhl_slugs = team_index.get("nhl", [])
                    if split_slug[1] in nhl_slugs and split_slug[2] in nhl_slugs:
                        league = 'nhl'
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

            uel_teams = team_index.get("uel", [])
            if "uel" in leagues and uel_teams:
                # Match head-to-head UEL slugs: uel-{team1}-{team2}-{date}-{team_or_draw}
                if split_slug[0] == "uel" and len(split_slug) >= 3:
                    if split_slug[1] in uel_teams and split_slug[2] in uel_teams:
                        if split_slug[-1] in uel_teams or split_slug[-1] == "draw":
                            candidate_markets.append((market, "uel"))

            seria_teams = team_index.get("seria", [])
            if "seria" in leagues and seria_teams:
                # Match head-to-head Serie A slugs: sea-{team1}-{team2}-{date}-{team_or_draw}
                if split_slug[0] == "sea" and len(split_slug) >= 3:
                    if split_slug[1] in seria_teams and split_slug[2] in seria_teams:
                        if split_slug[-1] in seria_teams or split_slug[-1] == "draw":
                            candidate_markets.append((market, "seria"))

            laliga_teams = team_index.get("laliga", [])
            if "laliga" in leagues and laliga_teams:
                # Match head-to-head La Liga slugs: lal-{team1}-{team2}-{date}-{team_or_draw}
                if split_slug[0] == "lal" and len(split_slug) >= 3:
                    if split_slug[1] in laliga_teams and split_slug[2] in laliga_teams:
                        if split_slug[-1] in laliga_teams or split_slug[-1] == "draw":
                            candidate_markets.append((market, "laliga"))

            # MLS — all three sub-leagues share tag "mls"; slug prefix is always "mls"
            _mls_teams = (
                team_index.get("mls") or team_index.get("mls_spread") or team_index.get("mls_totals") or set()
            )
            if _mls_teams and split_slug[0] == "mls" and len(split_slug) >= 3:
                c1, c2 = split_slug[1], split_slug[2]
                if c1 in _mls_teams and c2 in _mls_teams:
                    # 1x2: mls-{t1}-{t2}-{yyyy}-{mm}-{dd}-{team_or_draw}  (7 parts)
                    if "mls" in leagues and len(split_slug) == 7:
                        if split_slug[6] in _mls_teams or split_slug[6] == "draw":
                            candidate_markets.append((market, "mls"))
                    # Spread: mls-{t1}-{t2}-{yyyy}-{mm}-{dd}-spread-{home|away}-{Xpt5}  (9 parts)
                    if "mls_spread" in leagues and "spread" in split_slug:
                        candidate_markets.append((market, "mls_spread"))
                    # Totals: mls-{t1}-{t2}-{yyyy}-{mm}-{dd}-total-{Xpt5}  (8 parts)
                    if "mls_totals" in leagues and "total" in split_slug:
                        candidate_markets.append((market, "mls_totals"))

            _cricket_non_moneyline = {"innings", "runs", "wickets", "fours", "sixes", "total", "over", "under"}
            if "ipl" in leagues and split_slug[0] == "cricipl":
                if not any(seg in _cricket_non_moneyline for seg in split_slug):
                    ipl_slugs = team_index.get("ipl", [])
                    # Slug format: cricipl-{team1}-{team2} or cricipl-{team1}-{team2}-{date}
                    if len(split_slug) >= 3 and split_slug[1] in ipl_slugs and split_slug[2] in ipl_slugs:
                        pair = frozenset({split_slug[1], split_slug[2]})
                        if pair not in _seen_ipl_pairs:
                            _seen_ipl_pairs.add(pair)
                            candidate_markets.append((market, "ipl"))

        
        self.debug(f"{self.name}: scanned {total_seen} open markets, found {len(candidate_markets)} candidates after league filtering")


        for market_index, (market, league) in enumerate(candidate_markets, start=1):
            self.debug(
                f"{self.name}: market {market_index}/{len(candidate_markets)} id={market.get('id', 'unknown')} league={league}"
            )
            #print(market['question'])
            try:
                market_records, market_warnings = self._market_to_records(market, league, retrieved_at)
                records.extend(market_records)
                warnings.extend(market_warnings)
                self.debug(
                    f"{self.name}: {league} market {market.get('id', 'unknown')} -> {len(market_records)} records"
                )
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
                            #print(slot_records)
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
            elif league in ("mlb_totals", "mls_totals"):
                market_id = game.get("polymarket_market_id")
                over_token = game.get("polymarket_over_clob_token_id")
                under_token = game.get("polymarket_under_clob_token_id")
                total_line = game.get("total_line")
                for ou_name, clob_token_id in [("over", over_token), ("under", under_token)]:
                    if clob_token_id:
                        self.debug(f"{self.name}: CLOB totals direct token={clob_token_id} outcome={ou_name!r}")
                        try:
                            slot_records = self._fetch_clob_records_by_token(
                                clob_token_id, market_id or "", league, ou_name,
                                "two_way", event_name, event_start, retrieved_at,
                            )
                            if total_line is not None:
                                slot_records = [
                                    _dc_replace(r, metadata={**(r.metadata or {}), "total_line": total_line})
                                    for r in slot_records
                                ]
                            records.extend(slot_records)
                        except Exception as exc:
                            warnings.append(f"Skipped Polymarket CLOB token {clob_token_id}: {exc}")
                if not over_token and not under_token and market_id:
                    self.debug(f"{self.name}: CLOB totals gamma-lookup market_id={market_id}")
                    try:
                        game_records = self._fetch_clob_moneyline_records(
                            market_id, league, event_name, event_start, retrieved_at,
                        )
                        records.extend(game_records)
                    except Exception as exc:
                        warnings.append(f"Skipped Polymarket CLOB market {market_id}: {exc}")

            else:
                # NBA / MLB — two-way markets; per-slot token IDs stored alongside market ID
                market_type = "two_way"
                market_id = game.get("polymarket_market_id")
                team1_name = normalize_team_name(team1, league)
                team2_name = normalize_team_name(team2, league)
                team1_token = game.get("polymarket_team1_clob_token_id")
                team2_token = game.get("polymarket_team2_clob_token_id")

                if team1_token and team2_token:
                    # Both token IDs stored — use direct CLOB path, one call per outcome.
                    for outcome_name, clob_token_id, slot_market_id in [
                        (team1_name, team1_token, game.get("polymarket_team1_market_id") or market_id),
                        (team2_name, team2_token, game.get("polymarket_team2_market_id") or market_id),
                    ]:
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
                    # Only one token stored (or none) — Gamma resolves both token IDs at once.
                    # This handles games from before both tokens were stored (e.g. the IPL
                    # "Indian Premier League" prefix bug that left team1 token null).
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
        # Only emit a lay record for three-way (soccer) markets where buying the NO
        # token genuinely covers multiple outcomes (draw + loss).  For two-way markets
        # (NBA/NHL/IPL) the NO token is identical to backing the other team — treat it
        # as a back bet in the surebet detector, not a lay.
        if best_bid and 0 < best_bid < 1 and market_type != "two_way":
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

            elif league in ("mlb", "mlb_spread", "mlb_totals"):
                index[league] = ['cws','mil','wsh','chc','min','bal',
                'cin','bos','laa','hou','det','sd','tex','phi',
                'tb','stl','ari','lad','cle','sea','nyy','sf','oak','tor','col','mia','kc','atl', 'pit','nym']

            elif league == "nhl":

                index[league] = [
                    'min','stl','buf','chi','col','edm',
                    'lak','sea','wpg','vgk','las','mon','phi',
                    'nj','bos','car','nyi','wsh','cbj',
                'ana','cal','utah','pit',
                'sj','sjs','nsh','van','dal','buf','nyr','tb',
                'det','fla','tor','ott']

            elif league == "ucl":
                index[league] = ['rma1','bay1','spo1','ars','psg1','liv1','fcb1','atm1']

            elif league == "epl":

                index[league] = [
                    'ars','che','liv','mac','mun','tot','new','ast',
                    'bri','wes','wol','cry','ful','bre','eve','bou','not',
                    'bur','lee','sun',  # promoted: Burnley, Leeds, Sunderland
                ]

            elif league == "uel":

                # Add/remove as the competition progresses each season.
                index[league] = [
                    'not', 'por1',
                    'cel4', 'scf',
                    'ast', 'ast4', 'bol',
                    'bet1', 'scb',
                ]

            elif league == "seria":
                # Polymarket slug prefix: sea-{team1}-{team2}-{date}-{team_or_draw}

                index[league] = [
                    'udi', 'par', 'laz', 'nap', 'rom',
                    'ata', 'cre', 'tor', 'ver', 'mil',
                    'gen', 'pis', 'juv', 'bol', 'lec',
                    'fio', 'int', 'cag', 'com', 'sas',
                ]
            elif league == "laliga":
                index[league] = [
                    'ray','rso','mad','bil','ovi','elc','osa','sev',
                    'vil','cel','esp','lev','gir','mal','val','ala',
                    'bar','rea','bet','get'
                ]

            elif league in ("mls", "mls_spread", "mls_totals"):
                # Polymarket slug codes for all 30 MLS clubs (2025-26 season)
                index[league] = [
                    'atl', 'aus', 'chi', 'clb', 'clt', 'col', 'dal', 'dcu', 'fcc', 'hou',
                    'laf', 'lag', 'mia', 'mim', 'min', 'nas', 'ner', 'nyc', 'nyr', 'orl',
                    'phi', 'por', 'rsl', 'sdg', 'sea', 'sje', 'skc', 'stl', 'tor', 'vwh',
                ]

            elif league == "ipl":
                # Polymarket uses short lower-case team codes in IPL slugs.
                # Format: cricipl-{team1}-{team2}[-{date}]

                index[league] = [
                    'mum', 'che', 'roy', 'kol', 'del',
                    'pun',  'sun', 'luc', 'guj','raj'
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

    def _stream_markets(self):
        """Yield markets one at a time from the Gamma API without accumulating them all in memory."""
        offset = 0
        batch_size = 100  # Gamma API caps responses at 100 per page

        while True:
            self.debug(f"{self.name}: requesting markets offset={offset} limit={batch_size} (API cap)")
            try:
                batch = self.http_client.get_json(
                    f"{self.settings.gamma_base_url}/markets",
                    params={"limit": batch_size, "offset": offset, "closed": "false", "active": "true"},
                )
            except RuntimeError as exc:
                if "422" in str(exc):
                    # Gamma API returns 422 when offset exceeds its maximum allowed value.
                    self.debug(f"{self.name}: offset={offset} hit Gamma pagination limit — stopping")
                    break
                raise
            if not isinstance(batch, list) or not batch:
                break
            self.debug(f"{self.name}: received {len(batch)} markets at offset={offset}")
            yield from batch
            if len(batch) < batch_size:
                break
            offset += batch_size

    def _stream_event_markets(self, leagues: list[str]):
        """Yield child markets from /events for all supported leagues.

        Game markets are stored as events in the Gamma API, not in /markets.
        Each event's child markets are yielded with the parent event injected
        under 'events' so _market_to_records can read event_start and source_event_id.
        """
        seen_tags: set[str] = set()
        for league in leagues:
            tag = _LEAGUE_EVENT_TAGS.get(league)
            if not tag or tag in seen_tags:
                continue
            seen_tags.add(tag)
            offset = 0
            batch_size = 100
            while True:
                self.debug(f"{self.name}: requesting events tag={tag} offset={offset} limit={batch_size}")
                try:
                    events_batch = self.http_client.get_json(
                        f"{self.settings.gamma_base_url}/events",
                        params={"limit": batch_size, "offset": offset, "closed": "false", "active": "true", "tag_slug": tag},
                    )
                except RuntimeError as exc:
                    if "422" in str(exc):
                        self.debug(f"{self.name}: events tag={tag} offset={offset} hit pagination limit — stopping")
                        break
                    raise
                if not isinstance(events_batch, list) or not events_batch:
                    break
                self.debug(f"{self.name}: received {len(events_batch)} events (tag={tag}) at offset={offset}")
                for event in events_batch:
                    child_markets = event.get("markets") or []
                    event_ctx = {
                        "id": event.get("id"),
                        "title": event.get("title"),
                        "startTime": event.get("startTime") or event.get("endDate"),
                        "endDate": event.get("endDate"),
                    }
                    for child in child_markets:
                        yield {**child, "events": [event_ctx]}
                if len(events_batch) < batch_size:
                    break
                offset += batch_size

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
        if "nhl" in question or "hockey" in question:
            return "nhl"
        if "ucl" in question:
            return "ucl"
        if "premier league" in question or "epl" in question:
            return "epl"
        if "uel" in question or "europa league" in question:
            return "uel"
        if "ipl" in question or "cricket" in question or "cricipl" in question:
            return "ipl"

        return None

    def _market_to_records(
        self,
        market: dict[str, Any],
        league: str,
        retrieved_at: str,
    ) -> tuple[list[OddsRecord], list[str]]:
        outcomes = _parse_stringified_json_list(market.get("outcomes"))
        clob_token_ids = _parse_stringified_json_list(market.get("clobTokenIds"))

        method_warnings: list[str] = []
        _spread_value: float | None = None
        _spread_favourite: str | None = None
        _total_line_value: float | None = None
        if league in ("mlb_spread", "mls_spread"):
            _spread_value, _spread_found = _extract_spread(market)
            _spread_favourite = _extract_spread_favourite(market)
            if not _spread_found:
                slug = market.get("slug", "unknown")
                market_id = market.get("id", "unknown")
                method_warnings.append(
                    f"{league} market {market_id} ({slug}): spread not found in market data, defaulted to -1.5"
                )
        elif league in ("mlb_totals", "mls_totals"):
            _total_line_value = _extract_total_line_pm(market)

        lay_probs: list[float | None] = []
        token_liq_usd: list[float | None] = []  # per-token liquidity; empty = use market aggregate

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

            # bestAsk/bestBid in the Gamma API always track outcomes[0] (the first
            # token), regardless of groupItemTitle. Assigning by groupItemTitle caused
            # swapped prices when groupItemTitle matched outcomes[1] (observed on IPL).
            back_probs = [
                best_ask,
                (1.0 - best_bid) if best_bid is not None else None,
            ]
            lay_probs = [None, None]

            # Fetch per-token ask-side depth from CLOB so each outcome gets its own
            # liquidity figure. Gamma's liquidityNum is an aggregate and identical for
            # both outcomes, which hides the real per-side imbalance.
            token_liq_usd = [None, None]
            for _i, _token in enumerate(clob_token_ids[:2]):
                if _token:
                    try:
                        _, _, _ask_size = self._fetch_clob_book(_token)
                        token_liq_usd[_i] = _ask_size * 2  # matches CLOB-path convention
                    except Exception:
                        pass
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
            # For US sports, Polymarket slugs follow "{sport}-{away_slug}-{home_slug}[-extra]"
            # (away team first). Build "away at home" so _parse_teams assigns home correctly,
            # matching the ordering used by SX Bet (which lists home first as teamOneName).
            # Only use slug ordering when normalization resolves the codes to full names —
            # if the code isn't in the alias table it returns unchanged, signalling a miss.
            _SLUG_ORDERED_LEAGUES = frozenset({"mlb", "mlb_spread", "mlb_totals", "nba", "nhl"})
            slug = market.get("slug", "")
            slug_parts = slug.split("-")
            event_name_set = False
            if league in _SLUG_ORDERED_LEAGUES and len(slug_parts) >= 3:
                away_from_slug = normalize_team_name(slug_parts[1], league)
                home_from_slug = normalize_team_name(slug_parts[2], league)
                if away_from_slug != slug_parts[1] and home_from_slug != slug_parts[2]:
                    event_name = f"{away_from_slug} at {home_from_slug}"
                    event_name_set = True
            if not event_name_set:
                title = event.get("title") or ""
                # Strip trailing event-type suffixes added by Polymarket to more-markets events
                # e.g. "Inter Miami CF vs. Philadelphia Union - More Markets" → "..."
                _title_suffixes = (
                    " - More Markets", " - Halftime Result", " - Exact Score",
                    " - Player Props", " - Total Corners",
                )
                for _sfx in _title_suffixes:
                    if title.endswith(_sfx):
                        title = title[: -len(_sfx)].strip()
                        break
                _separators = (" vs ", " vs. ", " v ", " at ", " @ ")
                if any(sep in title.lower() for sep in _separators):
                    event_name = title
                elif len(outcomes) == 2:
                    t0 = normalize_team_name(str(outcomes[0]), league)
                    t1 = normalize_team_name(str(outcomes[1]), league)
                    event_name = f"{t0} vs {t1}"
                else:
                    event_name = (
                        market.get("groupItemTitle")
                        or market.get("question")
                        or "Unknown event"
                    )

        event_start = event.get("startTime") or event.get("endDate")

        if league in _SOCCER_LEAGUES:
            # For soccer, market_name should describe the game so each record is self-describing
            # alongside selection_name (which identifies the specific team/draw outcome)
            market_name = event_name
        elif league == "mlb_spread":
            market_name = "Run Line"
        elif league == "mlb_totals":
            market_name = "Total Runs"
        elif league == "mls_spread":
            market_name = "Goal Line"
        elif league == "mls_totals":
            market_name = "Total Goals"
        else:
            market_name = market.get("groupItemTitle") or market.get("question") or "Unknown market"

        # For single-outcome Yes/No remapped markets, infer_market_type would return "multi_way"
        # (only 1 outcome left). UCL markets are three-way (home/draw/away); all other
        # single-outcome remapped markets fall back to two_way for the moneyline filter.
        if league in ("mlb_spread", "mlb_totals", "mls_spread", "mls_totals"):
            # These are always two-outcome markets; _infer_market_type would return
            # "handicap" or "total" from slug keywords, which the moneyline filter would drop.
            market_type = "two_way"
        elif len(outcomes) == 1:
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
                "spread": _spread_value if league in ("mlb_spread", "mls_spread") else None,
                "spread_favourite": _spread_favourite if league in ("mlb_spread", "mls_spread") else None,
                "total_line": _total_line_value if league in ("mlb_totals", "mls_totals") else None,
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
            per_token_liq = token_liq_usd[i] if i < len(token_liq_usd) else None
            record_liq = per_token_liq if per_token_liq is not None else shared["metadata"]["liquidity_usd"]
            record_kw = {**shared, "metadata": {**shared["metadata"], "clob_token_id": clob_token, "liquidity_usd": record_liq}}

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
        return records, method_warnings

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


def _extract_total_line_pm(market: dict) -> float | None:
    """Extract total line from a Polymarket totals market."""
    import re as _re
    raw = market.get("line")
    if raw is not None:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    for field in ("slug", "question", "groupItemTitle"):
        text = str(market.get(field) or "")
        m = _re.search(r'(\d+)pt(\d+)', text)
        if m:
            return float(f"{m.group(1)}.{m.group(2)}")
        m = _re.search(r'[Oo]/[Uu]\s*(\d+(?:\.\d+)?)', text)
        if m:
            return float(m.group(1))
        m = _re.search(r'total[- _](\d+(?:\.\d+)?)', text, _re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None


def _extract_spread_favourite(market: dict) -> str | None:
    """Return the team name at the spread value in a run-line market, or None.

    Polymarket formats the question as 'Spread: {Team Name} ({value})', where the
    named team is always the one at the spread (e.g. -1.5).  Falls back to checking
    outcomes for embedded spread markers (e.g. 'NYY -1.5').
    """
    import re as _re

    # Primary: "Spread: Boston Red Sox (-1.5)" → "Boston Red Sox"
    question = str(market.get("question") or "")
    m = _re.match(r"^Spread:\s*(.+?)\s*\(", question)
    if m:
        return m.group(1).strip()

    # Fallback: outcome strings that embed the spread, e.g. ["NYY -1.5", "BOS +1.5"]
    outcomes = _parse_stringified_json_list(market.get("outcomes"))
    for outcome in outcomes:
        out_str = str(outcome)
        if _re.search(r"[-−]\d+\.5", out_str):
            return _re.sub(r"\s*[-−]\d+\.5.*$", "", out_str).strip() or None

    return None


def _extract_spread(market: dict) -> tuple[float, bool]:
    """Return (spread, found). found=False means no explicit line was detected and -1.5 is the default."""
    import re as _re
    for field in ("question", "slug", "groupItemTitle"):
        text = str(market.get(field) or "")
        m = _re.search(r"([+-]?\d+\.5)", text)
        if m:
            return float(m.group(1)), True
    return -1.5, False


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

