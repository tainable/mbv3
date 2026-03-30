# matched_betting

Odds ingestion and aggregation system for matched betting on NBA and MLB games.

Fetches live odds from Matchbook, Smarkets, and Polymarket, normalises them into a unified schema, matches records for the same game across providers using a canonical event ID, and writes both a full odds record file and a best-odds comparison table.

## Project layout

```
matched_betting/
├── run.py                          # Launcher (adds src/ to sys.path)
├── src/matched_betting/
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
│       └── polymarket.py           # Polymarket public API adapter
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
| `--providers matchbook smarkets polymarket` | Providers to query (default: all three) |
| `--out PATH` | Override the output file path |
| `--debug` | Print progress messages to stderr |

Examples:

```bash
# Single provider
python run.py --providers polymarket

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
  "providers_requested": ["matchbook", "smarkets", "polymarket"],
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
  "polymarket_team1_odds": 2.14,
  "polymarket_team2_odds": 1.74,
  "matchbook_team1_odds": 2.10,
  "matchbook_team2_odds": 1.80,
  "smarkets_team1_odds": null,
  "smarkets_team2_odds": null
}
```

`null` means the provider had no matching record for that team. Odds shown are the best (highest) decimal odds seen across that provider's records for the game.

## How it works

**Canonical event matching** (`event_matching.py`): team names are parsed from each provider's event description, normalised through a league-specific alias dictionary (e.g. "Cavs" → "Cleveland Cavaliers"), then grouped by league, team pair, and start time within a ±30-minute tolerance. The resulting canonical event ID takes the form `league|YYYYMMDDTHHMM|away_team|home_team`.

**Odds normalisation**: Polymarket prices are converted from outcome probability to decimal odds using `1 / p`. Smarkets integer prices are first converted via `price / 10000` then the same formula. Matchbook prices are already decimal.

**Filtering** (`market_matching.py`): only moneyline back bets with two identified teams are included in the output. Exchange-specific concepts (lay depth, commissions, order book levels) are preserved in `metadata` for future use.

**Parallel ingestion**: all configured providers are queried concurrently via `ThreadPoolExecutor`. Provider failures are captured as warnings and do not abort the run.
