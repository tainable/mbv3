from __future__ import annotations

import re
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

        if any(lg in leagues for lg in ("ucl", "epl", "uel", "ipl", "seria", "laliga")):
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
                # For spread markets a single Matchbook event holds multiple run-line
                # markets (e.g. both the +1.5 and -1.5 side). Filter to the specific
                # line stored in the game context so only one canonical spread group
                # is produced downstream.
                if league == "mlb_spread":
                    context_favourite = game.get("spread_favourite")
                    if context_favourite is not None:
                        before = len(event_records)
                        event_records = [
                            r for r in event_records
                            if (r.metadata or {}).get("spread_favourite") == context_favourite
                        ]
                        self.debug(
                            f"{self.name}: targeted fetch event_id={event_id} "
                            f"spread filter '{context_favourite}': {before} -> {len(event_records)} records"
                        )
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
        if league in ("mlb", "mlb_spread"):
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
        if league == "laliga":
            return any(tag.get("url-name") == "spain-la-liga" for tag in meta_tags)
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

        if league == "mlb_spread":
            run_line_markets = [m for m in open_markets if _is_run_line_market(m)]
            self.debug(
                f"{self.name}: event_id={event.get('id')} has {len(open_markets)} open markets, "
                f"{len(run_line_markets)} run-line after filtering"
            )
            if not run_line_markets and open_markets:
                names = sorted({str(m.get("name") or "").lower().strip() for m in open_markets})
                self.debug(
                    f"{self.name}: event_id={event.get('id')} — no run-line match; "
                    f"available market names: {names}"
                )
            for market in run_line_markets:
                market_name = str(market.get("name") or "Unknown market")
                runners = market.get("runners", [])

                # Log all runners with their handicap and odds so we can verify
                # we're picking up the right line (standard MLB run line is ±1.5).
                for runner in runners:
                    hcap = runner.get("handicap")
                    back_prices = [p for p in runner.get("prices", []) if str(p.get("side") or "back") == "back"]
                    best_back = max(
                        (p.get("decimal-odds") or p.get("odds") for p in back_prices if p.get("decimal-odds") or p.get("odds")),
                        default=None,
                    )
                    self.debug(
                        f"{self.name}: event_id={event.get('id')} runner={runner.get('name')!r} "
                        f"handicap={hcap} best_back={best_back}"
                    )

                # Only accept the standard MLB run line (±1.5).
                # Matchbook's "Handicap" market may include alternative lines (±0.5, ±2.5, etc.).
                run_line_runners = [
                    r for r in runners
                    if _is_run_line_handicap(r.get("handicap"))
                ]
                if not run_line_runners:
                    all_hcaps = [r.get("handicap") for r in runners]
                    self.debug(
                        f"{self.name}: event_id={event.get('id')} — no ±1.5 runners found; "
                        f"handicaps present: {all_hcaps}"
                    )
                    continue

                # Derive spread value and favourite from runner handicaps.
                # Matchbook may store the handicap as an unsigned value, encoding
                # the direction only in the runner name (e.g. "Texas Rangers -1.5").
                # _runner_is_favourite checks the handicap field sign first, then
                # falls back to scanning the runner name for a leading minus.
                spread_value: float | None = None
                spread_favourite: str | None = None
                for runner in run_line_runners:
                    hcap = runner.get("handicap")
                    if hcap is not None and spread_value is None:
                        try:
                            spread_value = abs(float(hcap))
                        except (TypeError, ValueError):
                            pass
                    if spread_favourite is None and _runner_is_favourite(runner):
                        spread_favourite = normalize_team_name(
                            _strip_handicap_suffix(str(runner.get("name") or "")), league
                        )

                for runner in run_line_runners:
                    best_by_side = _best_prices_per_side(runner.get("prices", []))
                    for side, price in best_by_side.items():
                        decimal_odds = price.get("decimal-odds") or price.get("odds")
                        if decimal_odds is None:
                            continue
                        records.append(
                            OddsRecord(
                                provider=self.name,
                                sport="baseball",
                                league=league,
                                event_name=str(event.get("name") or "Unknown event"),
                                event_start=event.get("start"),
                                market_name=market_name,
                                market_type="two_way",
                                selection_name=normalize_team_name(
                                    _strip_handicap_suffix(str(runner.get("name") or "Unknown selection")), league
                                ),
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
                                    "handicap": runner.get("handicap"),
                                    "spread": spread_value,
                                    "spread_favourite": spread_favourite,
                                    "exchange_type": price.get("exchange-type"),
                                    "last_price_update_time": runner.get("last-price-update-time"),
                                },
                            )
                        )
            return records

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
            market_type = "three_way" if league in ("ucl", "epl", "uel", "seria", "laliga") else "two_way"
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

_RUN_LINE_MARKET_NAMES = {
    "run line",
    "runline",
    "run-line",
    "handicap",
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


def _is_run_line_market(market: dict) -> bool:
    name = str(market.get("name") or "").lower().strip()
    return name in _RUN_LINE_MARKET_NAMES


_HANDICAP_SUFFIX_RE = re.compile(r"\s*\(?[+-]?\d+(?:\.\d+)?\)?\s*$")

# Standard MLB run line is always ±1.5. Filter out any alternative lines.
_MLB_RUN_LINE_VALUE = 1.5


def _strip_handicap_suffix(name: str) -> str:
    return _HANDICAP_SUFFIX_RE.sub("", name).strip()


def _is_run_line_handicap(handicap: object) -> bool:
    """Return True if the runner's handicap is the standard ±1.5 MLB run line."""
    if handicap is None:
        return False
    try:
        return abs(abs(float(handicap)) - _MLB_RUN_LINE_VALUE) < 0.01
    except (TypeError, ValueError):
        return False


def _runner_is_favourite(runner: dict) -> bool:
    """Return True if this runner is the spread favourite (negative handicap).

    Matchbook may store the handicap as an unsigned value and encode the
    direction in the runner name instead (e.g. "Texas Rangers -1.5").
    Check the structured field first, then fall back to the name.
    """
    hcap = runner.get("handicap")
    if hcap is not None:
        try:
            return float(hcap) < 0
        except (TypeError, ValueError):
            pass
    name = str(runner.get("name") or "")
    return bool(re.search(r"[\s(]-\d+(?:\.\d+)?", name))


LEAGUE_SPORT_IDS = {
    "nba": 4,
    "mlb": 3,
    "mlb_spread": 3,  # Same sport ID as mlb (baseball); events are filtered by run-line market name
    "nhl": 6,
    "ucl": 15,
    "epl": 15,  # Same sport ID as UCL (soccer)
    "uel": 15,  # Same sport ID (soccer)
    "seria": 15,  # Same sport ID (soccer)
    "laliga": 15,  # Same sport ID (soccer)
    "ipl": 110,  # Cricket — sport ID 110 covers all cricket; filtered by meta-tag below
}

LEAGUE_TO_SPORT = {
    "nba": "basketball",
    "mlb": "baseball",
    "mlb_spread": "baseball",
    "nhl": "ice_hockey",
    "ucl": "soccer",
    "epl": "soccer",
    "uel": "soccer",
    "seria": "soccer",
    "laliga": "soccer",
    "ipl": "cricket",
}
