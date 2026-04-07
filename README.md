# matched_betting

Odds ingestion and aggregation system for matched betting on NBA and MLB games.

Fetches live odds from Matchbook, Smarkets, Polymarket, and SX Bet, normalises them into a unified schema, matches records for the same game across providers using a canonical event ID, and writes both a full odds record file and a best-odds comparison table.

## Project layout

```
matched_betting/
├── run.py                          # Launcher (adds src/ to sys.path)
├── arb_finder.py                   # Arbitrage finder (sure bets and back-lay arbs)
├── src/matched_betting/
│   ├── __main__.py                 # Enables python -m matched_betting
│   ├── cli.py                      # Argument parsing and orchestration
│   ├── config.py                   # Settings loaded from .env
│   ├── http.py                     # HTTP client with retry logic
│   ├── models.py                   # OddsRecord and ProviderPayload dataclasses
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

### CLI flags

| Flag | Description |
|---|---|
| `--leagues nba mlb` | Leagues to fetch (default: both) |
| `--nba` | Shortcut for `--leagues nba` |
| `--mlb` | Shortcut for `--leagues mlb` |
| `--providers matchbook smarkets polymarket sx_bet` | Providers to query (default: all four) |
| `--out PATH` | Override the output file path |
| `--debug` | Print progress messages to stderr |

Examples:

```bash
# Single provider
python run.py --providers sx_bet

# Multiple providers
python run.py --providers polymarket sx_bet

# Single league
python run.py --nba

# Custom output path
python run.py --out outputs/nba_odds.json

# Debug mode (progress to stderr, JSON to stdout)
python run.py --debug
```

## Output

Each run writes two files derived from the output path stem:

| File | Contents |
|---|---|
| `*_all_odds.json` | All normalised `OddsRecord` objects with `canonical_event_id` |
| `*_aggregated_games.json` | Best decimal odds per team per provider, one entry per game |

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

**Canonical event matching** (`event_matching.py`): team names are parsed from each provider's event description, normalised through a league-specific alias dictionary (e.g. "Cavs" → "Cleveland Cavaliers"), then grouped by league, team pair, and start time within a ±30-minute tolerance. The resulting canonical event ID takes the form `league|YYYYMMDDTHHMM|away_team|home_team`.

**Odds normalisation**: Polymarket prices are converted from outcome probability to decimal odds using `1 / p`. Smarkets integer prices are first converted via `price / 10000` then the same formula. Matchbook prices are already decimal. SX Bet stores odds as scaled integers (`maker_probability × 10²⁰`); taker decimal odds are computed as `1 / (1 - maker_probability)` using the cross-referenced outcome (team one's odds derive from the best maker orders on outcome two, and vice versa).

**Filtering** (`market_matching.py`): only moneyline back bets with two identified teams are included in the output. For SX Bet specifically, only markets with `type == 226` (Moneyline Including Overtime) are fetched. Exchange-specific concepts (lay depth, commissions, order book levels) are preserved in `metadata` for future use.

**Parallel ingestion**: all configured providers are queried concurrently via `ThreadPoolExecutor`. Provider failures are captured as warnings and do not abort the run.
