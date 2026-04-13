# matched_betting

Odds ingestion and aggregation system for matched betting on NBA, MLB, UCL, and EPL games.

Fetches live odds from Matchbook, Smarkets, Polymarket, and SX Bet, normalises them into a unified schema, matches records for the same game across providers using a canonical event ID, and writes both a full odds record file and a best-odds comparison table.

## Project layout

```
matched_betting/
├── run.py                          # Launcher (adds src/ to sys.path)
├── arb_finder.py                   # Arbitrage finder (sure bets and back-lay arbs)
├── find_smarkets_event_ids.py      # Dev utility: discover Smarkets competition IDs
├── find_sx_bet_league_ids.py       # Dev utility: discover SX Bet league IDs
├── src/matched_betting/
│   ├── __main__.py                 # Enables python -m matched_betting
│   ├── cli.py                      # Argument parsing and orchestration
│   ├── config.py                   # Settings loaded from .env
│   ├── http.py                     # HTTP client with retry logic
│   ├── models.py                   # OddsRecord and ProviderPayload dataclasses
│   ├── normalization.py            # Team name alias dictionaries and normalizer
│   ├── event_matching.py           # Canonical event matching across providers
│   ├── market_matching.py          # Moneyline filtering and canonical bet grouping
│   ├── debug.py                    # Optional stderr debug logger
│   └── providers/
│       ├── base.py                 # Abstract OddsProvider interface
│       ├── registry.py             # Provider factory
│       ├── matchbook.py            # Matchbook authenticated API adapter
│       ├── smarkets.py             # Smarkets authenticated API adapter
│       ├── polymarket.py           # Polymarket public API adapter
│       └── sx_bet.py               # SX Bet public API adapter (P2P, no credentials required)
└── tests/
    ├── test_event_matching.py
    ├── test_game_filtering.py
    └── test_models.py
```

## Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

No additional packages are required — the project uses only the Python standard library.

## Configuration

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Available env vars:

```
# Output
MATCHED_BETTING_OUTPUT_PATH=outputs/latest_odds.json

# Matchbook (authenticated)
MATCHBOOK_USERNAME=
MATCHBOOK_PASSWORD=
MATCHBOOK_BASE_URL=https://api.matchbook.com

# Smarkets (authenticated)
SMARKETS_USERNAME=
SMARKETS_PASSWORD=
SMARKETS_API_TOKEN=
SMARKETS_BASE_URL=https://api.smarkets.com

# Polymarket (public, no credentials required)
POLYMARKET_GAMMA_BASE_URL=https://gamma-api.polymarket.com
POLYMARKET_CLOB_BASE_URL=https://clob.polymarket.com

# SX Bet (public, no credentials required)
SX_BET_BASE_URL=https://api.sx.bet
SX_BET_BASE_TOKEN=0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B
```

Providers without credentials will be skipped with a warning rather than crashing.

## Running

```bash
python run.py
```

The `run.py` launcher inserts `src/` onto `sys.path`, so no install step is required.

### Run modes

| Mode flag | Description |
|---|---|
| _(none)_ | **Full fetch** — discover all markets from scratch, write all outputs |
| `--update` | **Update** — re-fetch odds only for market IDs already in the market index; prunes IDs that return no odds |
| `--reuse` | **Reuse** — re-emit the previous `_aggregated_games.json` and `_all_odds.json` without making any network calls |
| `--index-only` | **Index only** — run provider discovery and write the market index, skip odds outputs |

### CLI flags

| Flag | Description |
|---|---|
| `--leagues nba mlb ucl epl` | Leagues to fetch (default: all four) |
| `--nba` | Shortcut for `--leagues nba` |
| `--mlb` | Shortcut for `--leagues mlb` |
| `--ucl` | Shortcut for `--leagues ucl` |
| `--epl` | Shortcut for `--leagues epl` |
| `--providers matchbook smarkets polymarket sx_bet` | Providers to query (default: all four) |
| `--out PATH` | Override the output file path stem |
| `--debug` | Print progress messages to stderr |
| `--multi-provider` | With `--update`: skip games covered by only one provider (filters out far-future games only listed on Polymarket) |

Examples:

```bash
# Full discovery run
python run.py

# Update only (fast, uses cached market IDs)
python run.py --update

# Refresh the market index without collecting odds
python run.py --index-only

# Single provider
python run.py --providers sx_bet

# Multiple providers, single league
python run.py --providers polymarket sx_bet --nba

# Custom output path
python run.py --out outputs/nba_odds.json

# Debug mode
python run.py --debug
```

## Output

Each run writes up to three files derived from the output path stem:

| File | Contents |
|---|---|
| `*_all_odds.json` | All normalised `OddsRecord` objects with `canonical_event_id` |
| `*_aggregated_games.json` | Best decimal odds per team per provider, one entry per game |
| `*_market_index.json` | Persistent index of known market IDs grouped by league and game |

A summary is also printed to stdout:

```json
{
  "leagues": ["nba", "mlb"],
  "providers_requested": ["matchbook", "smarkets", "polymarket", "sx_bet"],
  "record_count": 84,
  "aggregated_game_count": 14,
  "warnings": [],
  "all_odds_output_path": "outputs/latest_odds_all_odds.json",
  "aggregated_games_output_path": "outputs/latest_odds_aggregated_games.json"
}
```

### Odds record schema

All records share this shape regardless of source:

```json
{
  "provider": "polymarket",
  "sport": "basketball",
  "league": "nba",
  "event_name": "Lakers vs Celtics",
  "event_start": "2026-03-21T19:30:00Z",
  "market_name": "Moneyline",
  "market_type": "two_way",
  "selection_name": "Lakers",
  "selection_side": "back",
  "decimal_odds": 2.14,
  "implied_probability": 0.4673,
  "currency": "USD",
  "source_market_id": "12345",
  "source_event_id": "abcde",
  "retrieved_at": "2026-03-21T19:30:00Z",
  "metadata": {},
  "canonical_event_id": "nba|20260321T1930|los angeles celtics|los angeles lakers"
}
```

### Aggregated game schema

```json
{
  "team1": "Los Angeles Lakers",
  "team2": "Boston Celtics",
  "date_time": "2026-03-21T19:30:00Z",
  "league": "nba",
  "sport": "basketball",
  "market_type": "two_way",
  "polymarket_market_id": "abc123",
  "matchbook_event_id": "456",
  "smarkets_market_id": "789",
  "sx_bet_market_hash": "0xabc",
  "polymarket_team1_back_odds": 2.14,
  "polymarket_team1_lay_odds": null,
  "polymarket_team2_back_odds": 1.74,
  "polymarket_team2_lay_odds": null,
  "matchbook_team1_back_odds": 2.10,
  "matchbook_team1_lay_odds": 2.12,
  "matchbook_team2_back_odds": 1.80,
  "matchbook_team2_lay_odds": 1.82,
  "smarkets_team1_back_odds": null,
  "smarkets_team1_lay_odds": null,
  "smarkets_team2_back_odds": null,
  "smarkets_team2_lay_odds": null,
  "sx_bet_team1_back_odds": 2.08,
  "sx_bet_team1_lay_odds": null,
  "sx_bet_team2_back_odds": 1.82,
  "sx_bet_team2_lay_odds": null
}
```

`null` means the provider had no matching record for that outcome/side. Each provider stores the best (highest for back, lowest for lay) decimal odds seen across its records for the game. Three-way markets (e.g. football with draw) include additional `*_draw_back_odds` and `*_draw_lay_odds` fields. Market IDs are stored per provider to enable targeted odds re-fetching.

## Developer utilities

Two standalone scripts help discover the ID values you need to configure new leagues or debug provider mappings. Both read credentials from `.env` via the same `load_settings` path used by the main tool.

### find_smarkets_event_ids.py

Walks the Smarkets event hierarchy to find the root competition IDs referenced by the Smarkets provider.

```bash
# List all top-level sports
python find_smarkets_event_ids.py

# Search for competitions by name (fastest)
python find_smarkets_event_ids.py --name "premier"

# Walk ancestry from a known match event ID up to its root
python find_smarkets_event_ids.py --ancestors 12345678

# List children of a parent event ID
python find_smarkets_event_ids.py --parent 1234567

# Recursively search children for a keyword (up to --depth levels deep)
python find_smarkets_event_ids.py --parent 1234567 --search "premier" --depth 3
```

### find_sx_bet_league_ids.py

Queries the SX Bet `/leagues/active` endpoint to find league IDs referenced by the SX Bet provider. No credentials required.

```bash
# List all active leagues
python find_sx_bet_league_ids.py

# Filter by keyword (case-insensitive)
python find_sx_bet_league_ids.py --search "premier"
python find_sx_bet_league_ids.py --search "england"
```

## Arbitrage finder

`arb_finder.py` reads the aggregated games output and identifies two types of opportunity:

- **Sure bets** — back the same outcome across different providers such that the sum of implied probabilities is below 1 (after commission).
- **Back-lay arbs** — back an outcome on one exchange and lay it on another when the effective back odds exceed the effective lay odds (after commission).

Both two-way (NBA/MLB moneyline) and three-way (e.g. football with draw) markets are supported.

### Commission assumptions

| Provider | Rate |
|---|---|
| Matchbook | 2% on net winnings |
| Smarkets | 0% during 60-day intro period, else 2% |
| SX Bet | 0% |
| Polymarket | Dynamic: `0.0075 × 4 × p × (1 − p)` on stake |

Update `SMARKETS_ZERO_COMMISSION_PERIOD` in `arb_finder.py` when the Smarkets intro period ends.

### Usage

```bash
python arb_finder.py
python arb_finder.py --input outputs/latest_odds_aggregated_games.json
python arb_finder.py --min-profit 0.5
python arb_finder.py --no-refresh
```

### CLI flags

| Flag | Description |
|---|---|
| `--input PATH` | Path to aggregated games JSON (default: `outputs/latest_odds_aggregated_games.json`) |
| `--min-profit PCT` | Minimum net profit % to report (default: 0.0) |
| `--no-refresh` | Skip the targeted odds re-fetch after identifying arbs |

After listing arbs, the finder re-fetches current odds for each identified opportunity directly from the provider APIs (using the stored market IDs) and reports whether each arb is still valid, showing the delta from the originally aggregated odds.

Max available liquidity is shown per leg in GBP. USD amounts (Polymarket) are converted via a live exchange rate from `open.er-api.com`, falling back to 0.79 if the request fails.

## How it works

### Architecture overview

```
run.py / python -m matched_betting
    └── cli.py (main)
            ├── load_settings (config.py)         # reads .env
            ├── build_provider_registry            # instantiates providers
            ├── ThreadPoolExecutor                 # parallel provider fetches
            │     ├── MatchbookProvider.fetch_odds
            │     ├── SmarketsProvider.fetch_odds
            │     ├── PolymarketProvider.fetch_odds
            │     └── SxBetProvider.fetch_odds
            │           └── each returns ProviderPayload(records=[OddsRecord, ...])
            ├── match_records_to_canonical_events  # event_matching.py
            ├── is_game_win_loss_record filter      # market_matching.py
            ├── aggregate_games                    # cli.py
            └── write JSON outputs
```

### Step-by-step data flow

**1. Configuration** (`config.py`)

`load_settings()` reads a `.env` file from the project root (using a zero-dependency parser — no `python-dotenv` required) and populates frozen `Settings` dataclasses for each provider. Missing credentials cause a provider to raise `ProviderNotReadyError` at fetch time, which is caught and recorded as a warning rather than crashing the run.

**2. Provider fetching** (`providers/`)

Each provider implements the `OddsProvider` abstract base class (`base.py`) with two methods:

- `fetch_odds(leagues)` — full discovery: paginates the provider API to find all markets for the requested leagues.
- `fetch_odds_by_ids(game_contexts, leagues)` — targeted fetch: uses stored market/event IDs from the market index to re-fetch only known markets. Defaults to `fetch_odds` if not overridden.

All providers are queried concurrently via `ThreadPoolExecutor`. Each provider returns a `ProviderPayload` containing a flat list of `OddsRecord` objects and any provider-level warnings. Individual provider failures are caught and appended as warnings without aborting the run.

**3. Data model** (`models.py`)

Every record across every provider shares the same frozen `OddsRecord` dataclass:

- **Provider metadata**: `provider`, `sport`, `league`
- **Event identity**: `event_name`, `event_start`, `source_event_id`
- **Market identity**: `market_name`, `market_type`, `source_market_id`
- **Odds**: `selection_name`, `selection_side` (`back`/`lay`), `decimal_odds`, `implied_probability`
- **Extras**: `currency`, `retrieved_at`, `metadata` (provider-specific raw fields)

**4. Odds normalisation** (per provider)

Each provider adapter translates its native API format into the common schema:

- **Matchbook**: prices are already decimal; lay depth is stored in `metadata`.
- **Smarkets**: integer prices (`0`–`10000`) are divided by `10000` then inverted to decimal (`1 / p`).
- **Polymarket**: token IDs are resolved from the Gamma API (`/markets?id=…`) using `clobTokenIds` aligned with `outcomes`. Best ask is `asks[-1]` and best bid is `bids[-1]` from the CLOB order book. Prices are converted to decimal via `1 / p`.
- **SX Bet**: odds are stored as scaled integers (`maker_probability × 10²⁰`). Taker decimal odds are computed as `1 / (1 − maker_probability)`, cross-referencing outcomes (team one's odds are derived from the best maker orders on outcome two, and vice versa). Only markets with `type == 226` (Moneyline Including Overtime) are fetched.

**5. Team name normalisation** (`normalization.py`)

`normalize_team_name(name, league)` lowercases the input, strips non-alphanumeric characters to spaces, collapses whitespace, then looks up the result in a per-league `TEAM_ALIASES` dictionary. This handles common abbreviations ("Cavs" → "Cleveland Cavaliers"), diacritics stripped by provider encoding ("Bayern M nchen" → "Bayern Munich"), suffixes ("Arsenal FC" → "Arsenal"), and shorthand names ("Man Utd" → "Manchester United").

**6. Event matching** (`event_matching.py`)

`match_records_to_canonical_events()` groups records from all providers that refer to the same real-world game:

1. `infer_event_identity()` parses team names from the event description using separators like `" at "`, `" @ "`, `" vs "`, or `" v "` (ordered separators set away/home; unordered do not).
2. Each team name is normalised through the alias dictionary.
3. Records are matched to an existing `_MutableGroup` if they share the same league, the same unordered team pair, and a start time within ±30 minutes.
4. On no match, a new group is created.
5. The canonical event ID is built as `league|YYYYMMDDTHHMM|home_team|away_team`.

**7. Market filtering** (`market_matching.py`)

`is_game_win_loss_record()` keeps only records where:
- The market type is `two_way` or `three_way` (moneyline).
- The selection name is one of the two identified teams, `"draw"`, or `"tie"`.

Exchange-specific data (lay depth, order book levels, commissions) are preserved in each record's `metadata` dict for potential future use.

**8. Aggregation** (`cli.py`)

Filtered records are grouped by canonical event ID. For each game, the best decimal odds per team per provider are computed (highest for back, lowest for lay) and written into a flat dict — the aggregated game entry. Market IDs from each provider are stored alongside odds to enable `--update` mode targeted re-fetching.

**9. Market index** (`*_market_index.json`)

The index is an additive store keyed by `(team1, team2, date_time)`. Full and index-only runs append new entries; they never remove existing ones. Update-mode runs prune entries only when all stored provider IDs return no odds (indicating the market has closed).

## TODO

- **Investigate liquidity numbers** — the `*_back_avail` / `*_lay_avail` fields in the aggregated games output are partially populated. Verify that the available stake figures from each provider (Matchbook order depth, Smarkets contract liquidity, SX Bet taker-available calculation, Polymarket CLOB size) are computed and converted to a common currency (GBP) consistently, and surface them correctly in `arb_finder.py` per-leg output.

- **Investigate SX Bet further** — SX Bet returns `type == 1` for soccer markets but this has not been validated against real EPL/UCL data. The `_process_soccer_markets` grouping logic (using `sportXEventId` with a `gameTime|teamOne|teamTwo` fallback) and the back/lay probability derivation from P2P maker orders need end-to-end verification once live soccer markets are available on SX Bet. Rate-limit handling (HTTP 429 / Cloudflare error 1015) has been mitigated by batching all market-hash requests in update mode, but may still occur during full fetches with many leagues.
