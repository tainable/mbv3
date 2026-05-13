"""
azuro_test.py
-------------
Standalone diagnostic tool: query the Azuro Protocol subgraph for active NBA
markets and verify slugs, market types, team names, odds, and liquidity.

NBA moneyline identification (from Azuro dictionaries outcomes.json):
  marketId=19, gamePeriodId=76, gameTypeId=76  (key: 19-76-76 = "Match Winner incl. OT")
    outcomeId 6983  = "Game Winner of match Point 1"  -> Team 1 wins
    outcomeId 6984  = "Game Winner of match Point 2"  -> Team 2 wins

Liquidity fields (all in USDC, 1 USDC = 1 USD):
  maxConditionPotentialLoss  - total pool the house can lose on the condition
  maxOutcomePotentialLoss    - max profit a bettor can take on one outcome
  turnover                   - amount actually bet so far on the condition
  margin                     - built-in bookmaker edge (e.g. 0.07 = 7%)

USD/GBP rate is fetched live from open.er-api.com (no key required).

Schema note (v3 data-feed):
  - conditionTypeId does NOT exist. Market type comes from outcomeId lookup.
  - currentOdds is already a BigDecimal string (e.g. "1.72").
  - Active conditions: state: Active  (not status: Created).
  - NBA league slug is "nba"  (not "us-basketball-nba").

Usage:
    python azuro_test.py                  # NBA moneyline games with liquidity
    python azuro_test.py --all-markets    # show all conditions, not just moneyline
    python azuro_test.py --discover       # list ALL sports/leagues
    python azuro_test.py --url gnosis     # try Gnosis Chain endpoint
    python azuro_test.py --raw            # dump raw JSON for first game
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests", file=sys.stderr)
    sys.exit(1)

# -- Known subgraph URLs --------------------------------------------------------
KNOWN_URLS: dict[str, str] = {
    "polygon":     "https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon",
    "gnosis":      "https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-gnosis",
    "polygon-api": "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-polygon-v3",
    "gnosis-api":  "https://thegraph.azuro.org/subgraphs/name/azuro-protocol/azuro-api-gnosis-v3",
}
DEFAULT_URL = KNOWN_URLS["polygon"]

# -- Default NBA slugs ---------------------------------------------------------
DEFAULT_SPORT_SLUG  = "basketball"
DEFAULT_LEAGUE_SLUG = "nba"

# -- Moneyline outcomeId constants (from Azuro dictionaries outcomes.json) ------
# marketId=19, gamePeriodId=76, gameTypeId=76  =  "Match Winner incl. OT"
_MONEYLINE_TEAM1 = 6983   # "Game Winner of match Point 1" = Team 1 wins
_MONEYLINE_TEAM2 = 6984   # "Game Winner of match Point 2" = Team 2 wins
_MONEYLINE_IDS   = frozenset({_MONEYLINE_TEAM1, _MONEYLINE_TEAM2})

# All known NBA market keys (marketId-gamePeriodId-gameTypeId) for labelling
_NBA_MARKET_NAMES: dict[str, str] = {
    "19-76-76":  "Match Winner incl. OT  [MONEYLINE]",
    "1-76-76":   "Full Time Result (1X2)",
    "3-76-76":   "Handicap incl. OT",
    "4-76-76":   "Total Points incl. OT",
    "7-76-76":   "Team Individual Total incl. OT",
    "14-76-76":  "Total Odd/Even",
    "15-76-76":  "Team Individual Total Odd/Even",
    "19-50-76":  "1st Half Winner",
    "19-53-76":  "1st Quarter Winner",
    "19-58-76":  "2nd Quarter Winner",
    "19-62-76":  "3rd Quarter Winner",
    "19-65-76":  "4th Quarter Winner",
    "4-50-76":   "1st Half Total",
    "4-53-76":   "1st Quarter Total",
    "4-58-76":   "2nd Quarter Total",
    "4-62-76":   "3rd Quarter Total",
}

# -- GraphQL queries (v3 schema) ------------------------------------------------

_GQL_DISCOVER = """
query Discover($after: BigInt!) {
  games(
    where: { startsAt_gt: $after }
    orderBy: startsAt
    orderDirection: asc
    first: 200
  ) {
    gameId
    startsAt
    totalTurnover
    sport  { slug name }
    league { slug name }
    participants { name }
    conditions(where: { state: Active }, first: 1) {
      conditionId
      state
      turnover
      maxConditionPotentialLoss
      maxOutcomePotentialLoss
      margin
      outcomes(orderBy: sortOrder) {
        outcomeId
        currentOdds
        sortOrder
        potentialLoss
      }
    }
  }
}
"""

# Moneyline only -- filtered server-side by outcomeIds_contains
_GQL_NBA_MONEYLINE = """
query NBAMoneyline($sportSlug: String!, $leagueSlug: String!, $after: BigInt!) {
  games(
    where: {
      sport_:  { slug: $sportSlug }
      league_: { slug: $leagueSlug }
      startsAt_gt: $after
    }
    orderBy: startsAt
    orderDirection: asc
    first: 200
  ) {
    gameId
    startsAt
    totalTurnover
    sport  { slug name }
    league { slug name }
    participants { name }
    conditions(
      where: {
        state: Active
        outcomesIds_contains: ["6983", "6984"]
      }
    ) {
      conditionId
      state
      turnover
      maxConditionPotentialLoss
      maxOutcomePotentialLoss
      margin
      outcomes(orderBy: sortOrder) {
        outcomeId
        currentOdds
        sortOrder
        potentialLoss
      }
    }
  }
}
"""

# All markets -- for inspection / debugging
_GQL_NBA_ALL = """
query NBAAll($sportSlug: String!, $leagueSlug: String!, $after: BigInt!) {
  games(
    where: {
      sport_:  { slug: $sportSlug }
      league_: { slug: $leagueSlug }
      startsAt_gt: $after
    }
    orderBy: startsAt
    orderDirection: asc
    first: 200
  ) {
    gameId
    startsAt
    totalTurnover
    sport  { slug name }
    league { slug name }
    participants { name }
    conditions(where: { state: Active }) {
      conditionId
      state
      turnover
      maxConditionPotentialLoss
      maxOutcomePotentialLoss
      margin
      outcomes(orderBy: sortOrder) {
        outcomeId
        currentOdds
        sortOrder
        potentialLoss
      }
    }
  }
}
"""


# -- HTTP helpers ---------------------------------------------------------------

def _gql(url: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    resp = requests.post(
        url,
        json={"query": query, "variables": variables},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_usd_gbp_rate() -> float | None:
    """Fetch live USD/GBP rate from open.er-api.com (free, no key)."""
    try:
        r = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        return rates.get("GBP")
    except Exception:
        return None


# -- Formatting helpers ---------------------------------------------------------

def _fmt_ts(unix: Any) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(unix), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(unix)


def _fmt_odds(val: Any) -> str:
    try:
        return f"{float(val):.4f}"
    except Exception:
        return str(val)


def _usdc(val: Any) -> float:
    """Parse a USDC BigDecimal value (already decimal, 1 USDC = 1 USD)."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _fmt_gbp(usdc_val: Any, rate: float | None) -> str:
    """Format a USDC value as 'GBP X.XX  (USDC Y.YY)'."""
    usd = _usdc(usdc_val)
    usdc_s = f"USDC {usd:,.2f}"
    if rate and usd > 0:
        gbp = usd * rate
        return f"GBP {gbp:,.2f}  ({usdc_s})"
    return usdc_s


# -- Discovery mode -------------------------------------------------------------

def run_discover(url: str, dump_raw: bool, gbp_rate: float | None) -> None:
    print(f"Subgraph: {url}")
    print(f"Query:    discovery (all sports, first 200 future games)\n")

    now = str(int(time.time()))
    result = _gql(url, _GQL_DISCOVER, {"after": now})

    errors = result.get("errors")
    if errors:
        print(f"GraphQL errors:\n{json.dumps(errors, indent=2)}")
        return

    games: list[dict] = (result.get("data") or {}).get("games", [])
    print(f"Total games returned: {len(games)}")

    if not games:
        print("\nNo future games found. Possible causes:")
        print("  * Wrong subgraph URL (try --url gnosis)")
        print("  * Off-season")
        return

    if dump_raw:
        print("\n-- Raw JSON (first game) --")
        print(json.dumps(games[0], indent=2))
        print()

    sport_league: dict[str, dict[str, int]] = {}
    for g in games:
        s = (g.get("sport")  or {}).get("slug", "unknown")
        l = (g.get("league") or {}).get("slug", "unknown")
        sport_league.setdefault(s, {}).setdefault(l, 0)
        sport_league[s][l] += 1

    print("-- Sports & Leagues ------------------------------------------------")
    for sport_slug, leagues in sorted(sport_league.items()):
        total = sum(leagues.values())
        print(f"  sport={sport_slug!r}  ({total} games)")
        for league_slug, count in sorted(leagues.items()):
            print(f"    league={league_slug!r}  {count} game(s)")

    shown: set[str] = set()
    print("\n-- Sample game per sport -------------------------------------------")
    for g in games:
        s = (g.get("sport") or {}).get("slug", "unknown")
        if s in shown:
            continue
        shown.add(s)
        _print_game(g, gbp_rate, all_markets=False)


# -- NBA mode ------------------------------------------------------------------

def run_nba(
    url: str,
    sport_slug: str,
    league_slug: str,
    dump_raw: bool,
    gbp_rate: float | None,
    all_markets: bool,
) -> None:
    print(f"Subgraph:    {url}")
    print(f"sport slug:  {sport_slug!r}")
    print(f"league slug: {league_slug!r}")
    print(f"mode:        {'all markets' if all_markets else 'moneyline only (outcomeIds 6983/6984)'}\n")

    now = str(int(time.time()))
    query = _GQL_NBA_ALL if all_markets else _GQL_NBA_MONEYLINE
    result = _gql(url, query, {
        "sportSlug":  sport_slug,
        "leagueSlug": league_slug,
        "after":      now,
    })

    errors = result.get("errors")
    if errors:
        print(f"GraphQL errors:\n{json.dumps(errors, indent=2)}")
        return

    games: list[dict] = (result.get("data") or {}).get("games", [])
    print(f"Games returned: {len(games)}")

    if not games:
        print("\nNo games found. Run --discover to see available slugs.")
        return

    if dump_raw:
        print("\n-- Raw JSON (first game) --")
        print(json.dumps(games[0], indent=2))
        print()

    print(f"-- {sport_slug.upper()} / {league_slug.upper()} Games ({len(games)}) "
          f"------------------------------")
    for g in games:
        _print_game(g, gbp_rate, all_markets=all_markets)


# -- Game printer --------------------------------------------------------------

def _market_label(outcomes: list[dict]) -> str:
    """Derive a human-readable market label from outcomeIds via the dictionary."""
    import requests as _req
    # Load once per process (cached after first call)
    if not hasattr(_market_label, "_cache"):
        try:
            raw = _req.get(
                "https://raw.githubusercontent.com/Azuro-protocol/dictionaries/main/dictionaries/outcomes.json",
                timeout=10,
            ).text
            import json as _json
            _market_label._cache = _json.loads(raw)
        except Exception:
            _market_label._cache = {}

    d = _market_label._cache
    for o in outcomes:
        entry = d.get(str(o.get("outcomeId", "")))
        if entry:
            key = f"{entry['marketId']}-{entry['gamePeriodId']}-{entry['gameTypeId']}"
            return _NBA_MARKET_NAMES.get(key, key)
    return "(unknown market)"


def _print_game(g: dict, gbp_rate: float | None, all_markets: bool = False) -> None:
    game_id   = g.get("gameId", "?")
    starts    = _fmt_ts(g.get("startsAt"))
    sport     = (g.get("sport")  or {}).get("slug", "?")
    league    = (g.get("league") or {}).get("slug", "?")
    parts     = [p.get("name", "?") for p in (g.get("participants") or [])]
    teams     = " vs ".join(parts) if parts else "(no participants)"
    game_vol  = _fmt_gbp(g.get("totalTurnover"), gbp_rate)

    print(f"\n  gameId={game_id}  {starts}")
    print(f"  sport={sport!r}  league={league!r}")
    print(f"  {teams}")
    print(f"  Game total turnover:  {game_vol}")

    conditions = g.get("conditions") or []
    if not conditions:
        print("  (no Active conditions)")
        return

    print(f"  Conditions: {len(conditions)}")

    for idx, c in enumerate(conditions):
        cid      = c.get("conditionId", "?")
        outcomes = c.get("outcomes") or []
        oids     = {int(o.get("outcomeId", 0)) for o in outcomes}
        is_ml    = oids == _MONEYLINE_IDS

        label    = _market_label(outcomes) if all_markets else (
            "Match Winner incl. OT  [MONEYLINE]" if is_ml else "(other)"
        )

        margin_pct = ""
        try:
            margin_pct = f"  margin={float(c['margin'])*100:.1f}%"
        except Exception:
            pass

        pool_liq    = _fmt_gbp(c.get("maxConditionPotentialLoss"), gbp_rate)
        outcome_liq = _fmt_gbp(c.get("maxOutcomePotentialLoss"),  gbp_rate)
        vol         = _fmt_gbp(c.get("turnover"), gbp_rate)

        print(f"    [{idx}] {label}{margin_pct}")
        print(f"         conditionId={cid}")
        print(f"         Pool cap (max win total):      {pool_liq}")
        print(f"         Per-outcome cap (max win/side):{outcome_liq}")
        print(f"         Volume bet so far:             {vol}")

        for o in outcomes:
            oid    = o.get("outcomeId", "?")
            odds   = _fmt_odds(o.get("currentOdds"))
            sorder = o.get("sortOrder", "?")
            name   = parts[sorder] if isinstance(sorder, int) and sorder < len(parts) else f"outcome_{sorder}"
            ploss  = _fmt_gbp(o.get("potentialLoss"), gbp_rate)

            # Max stake = max_win / (odds - 1)
            max_stake_s = ""
            try:
                max_win  = _usdc(o.get("potentialLoss")) or _usdc(c.get("maxOutcomePotentialLoss"))
                dec_odds = float(o.get("currentOdds", 0))
                if dec_odds > 1.0 and max_win > 0:
                    max_stake = max_win / (dec_odds - 1)
                    if gbp_rate:
                        max_stake_s = f"  max_stake=GBP {max_stake * gbp_rate:,.2f}"
                    else:
                        max_stake_s = f"  max_stake=USDC {max_stake:,.2f}"
            except Exception:
                pass

            print(f"         outcome {oid}  sortOrder={sorder}  odds={odds}  "
                  f"potential_loss={ploss}{max_stake_s}  --> {name!r}")


# -- Entry point ---------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Azuro subgraph diagnostic: NBA games with liquidity in GBP."
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="List all sports and leagues in the subgraph (no filter).",
    )
    parser.add_argument(
        "--sport",
        default=DEFAULT_SPORT_SLUG,
        metavar="SLUG",
        help=f"Sport slug (default: {DEFAULT_SPORT_SLUG!r}).",
    )
    parser.add_argument(
        "--league",
        default=DEFAULT_LEAGUE_SLUG,
        metavar="SLUG",
        help=f"League slug (default: {DEFAULT_LEAGUE_SLUG!r}).",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        metavar="URL|NAME",
        help=(
            "Subgraph URL or named shortcut. "
            "Names: " + ", ".join(KNOWN_URLS.keys()) + ". "
            "Default: polygon."
        ),
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Dump raw JSON of the first game returned.",
    )
    parser.add_argument(
        "--all-markets",
        action="store_true",
        help="Show all Active conditions per game, not just the moneyline.",
    )
    args = parser.parse_args()

    url = KNOWN_URLS.get(args.url, args.url)

    # Fetch GBP rate up front
    print("Fetching USD/GBP rate...", end=" ", flush=True)
    gbp_rate = _fetch_usd_gbp_rate()
    if gbp_rate:
        print(f"1 USD = GBP {gbp_rate:.4f}")
    else:
        print("unavailable (showing USDC only)")
    print()

    try:
        if args.discover:
            run_discover(url, args.raw, gbp_rate)
        else:
            run_nba(url, args.sport, args.league, args.raw, gbp_rate, args.all_markets)
    except requests.HTTPError as exc:
        print(f"HTTP error: {exc}", file=sys.stderr)
        sys.exit(1)
    except requests.ConnectionError as exc:
        print(f"Connection error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
