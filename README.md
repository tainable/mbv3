# matched_betting

Odds ingestion and arbitrage detection system for matched betting on NBA, MLB, UCL, and EPL games.

Fetches live odds from Matchbook, Smarkets, Polymarket, and SX Bet, normalises them into a unified schema, matches records for the same game across providers, and scans for sure bets and back-lay arbs (after commission).

The system runs as a two-stage pipeline:

1. **`ids.py`** — discover active games and save their market IDs to a JSON index.
2. **`scan.py`** — read the IDs index, fetch live odds game-by-game with all providers in parallel, and report arbs as they are found.

## Project layout

```
mbv1/
├── ids.py                          # Stage 1: discover active games → outputs/active_game_ids.json
├── scan.py                         # Stage 2: live scan, per-game parallel fetch + arb detection
├── arb_finder.py                   # Standalone arb finder (reads a pre-built aggregated games JSON)
├── run.py                          # Legacy launcher (full fetch → JSON outputs)
├── find_smarkets_event_ids.py      # Dev utility: discover Smarkets competition IDs
├── find_sx_bet_league_ids.py       # Dev utility: discover SX Bet league IDs
├── src/matched_betting/
│   ├── __main__.py                 # Enables python -m matched_betting
│   ├── cli.py                      # Argument parsing and orchestration (used by run.py)
│   ├── config.py                   # Settings and CommissionSettings loaded from .env
│   ├── calculator.py               # Pure arb maths: commission helpers, find_sure_bets, find_back_lay_arbs
│   ├── aggregation.py              # Builds per-game aggregated odds payload from OddsRecord objects
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

# Commission rates
# Set SMARKETS_ZERO_COMMISSION=false once the 60-day intro period ends
SMARKETS_ZERO_COMMISSION=true
MATCHBOOK_COMMISSION=0.02
SMARKETS_COMMISSION=0.02
SX_BET_COMMISSION=0.00
```

Providers without credentials will be skipped with a warning rather than crashing.

## Running the pipeline

### Stage 1 — discover active games

```bash
python ids.py
```

This fetches all markets from the configured providers in parallel, matches them to canonical games, and writes `outputs/active_game_ids.json`. By default Smarkets is excluded.

```bash
python ids.py --leagues nba epl            # specific leagues only
python ids.py --providers matchbook polymarket sx_bet smarkets
python ids.py --out outputs/my_ids.json
python ids.py --debug
```

### Stage 2 — scan live odds

```bash
python scan.py
```

Reads the IDs JSON, iterates games one at a time. For each game, all providers are fetched simultaneously, the arb calculator runs immediately, and any arbs are printed before moving on to the next game.

```bash
python scan.py --ids outputs/active_game_ids.json
python scan.py --leagues nba epl
python scan.py --providers matchbook polymarket sx_bet
python scan.py --min-profit 0.5            # only show arbs ≥ 0.5% profit
python scan.py --debug
```

### Standalone arb finder

`arb_finder.py` reads a pre-built aggregated games JSON and reports arbs without re-fetching. It also re-fetches live odds for each identified opportunity to confirm the arb is still valid.

```bash
python arb_finder.py
python arb_finder.py --input outputs/latest_odds_aggregated_games.json
python arb_finder.py --min-profit 0.5
python arb_finder.py --no-refresh
```

### Legacy full-fetch runner

`run.py` performs a full discovery run and writes JSON output files without live scanning.

```bash
python run.py
python run.py --update          # re-fetch known market IDs only (fast)
python run.py --leagues nba
python run.py --debug
```

## Commission

Commission rates are configured in `.env` and apply to all calculations in both the pipeline and the standalone arb finder. To switch off the Smarkets zero-commission period, set:

```
SMARKETS_ZERO_COMMISSION=false
```

| Provider | Default rate |
|---|---|
| Matchbook | 2% on net winnings |
| Smarkets | 0% (zero-commission period) or 2% standard |
| SX Bet | 0% |
| Polymarket | Dynamic: `0.0075 × 4 × p × (1 − p)` on stake |

## Output

### IDs file (`outputs/active_game_ids.json`)

```json
{
  "generated_at": "2026-04-13T10:00:00Z",
  "leagues": ["nba", "mlb", "ucl", "epl"],
  "providers": ["matchbook", "polymarket", "sx_bet"],
  "game_count": 12,
  "games": [...]
}
```

### Aggregated game schema

Each game entry in the IDs file (and in the legacy `*_aggregated_games.json`) looks like:

```json
{
  "team1": "Los Angeles Lakers",
  "team2": "Boston Celtics",
  "date_time": "2026-04-13T19:30:00Z",
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

`null` means the provider had no record for that outcome/side. Three-way markets (e.g. football with draw) include additional `*_draw_*` fields. Market IDs are stored per provider to enable targeted re-fetching in Stage 2.

## How it works

### Pipeline architecture

```
ids.py
    ├── load_settings (config.py)              # reads .env, including commission rates
    ├── calculator.configure(commission)        # applies commission to arb calculator
    ├── ThreadPoolExecutor                      # parallel provider fetches
    │     ├── MatchbookProvider.fetch_odds
    │     ├── PolymarketProvider.fetch_odds
    │     └── SxBetProvider.fetch_odds
    ├── is_game_win_loss_record filter          # market_matching.py
    ├── match_records_to_canonical_events       # event_matching.py
    ├── build_aggregated_games_payload          # aggregation.py
    └── write outputs/active_game_ids.json

scan.py
    ├── load_settings (config.py)
    ├── calculator.configure(commission)
    ├── build_provider_registry
    └── for each game:
          ├── ThreadPoolExecutor               # all providers in parallel for this game
          │     ├── MatchbookProvider.fetch_odds_by_ids
          │     ├── PolymarketProvider.fetch_odds_by_ids
          │     └── SxBetProvider.fetch_odds_by_ids
          ├── match_records_to_canonical_events
          ├── build_aggregated_games_payload
          ├── calculator.find_sure_bets
          └── calculator.find_back_lay_arbs    # print arbs immediately
```

### Step-by-step data flow

**1. Configuration** (`config.py`)

`load_settings()` reads a `.env` file from the project root and populates frozen `Settings` dataclasses including `CommissionSettings`. Missing credentials cause a provider to raise `ProviderNotReadyError` at fetch time, caught and reported as a warning.

**2. Provider fetching** (`providers/`)

Each provider implements `OddsProvider` (`base.py`) with two methods:

- `fetch_odds(leagues)` — full discovery: paginates the provider API for all markets in the requested leagues.
- `fetch_odds_by_ids(game_contexts, leagues)` — targeted fetch: uses stored market/event IDs to re-fetch only known markets.

All providers for a given stage are queried concurrently via `ThreadPoolExecutor`. Each returns a `ProviderPayload` containing a flat list of `OddsRecord` objects.

**3. Data model** (`models.py`)

Every record shares the same frozen `OddsRecord` dataclass: provider, sport, league, event/market identity, selection name/side, decimal odds, implied probability, and a `metadata` dict for provider-specific raw fields.

**4. Odds normalisation** (per provider)

- **Matchbook**: prices already decimal; lay depth in `metadata`.
- **Smarkets**: integer prices (`0`–`10000`) → divide by 10000 → invert to decimal.
- **Polymarket**: token IDs resolved from Gamma API; best ask/bid from CLOB order book; prices converted `1 / p`.
- **SX Bet**: odds as scaled integers (`maker_probability × 10²⁰`); taker decimal = `1 / (1 − maker_probability)`.

**5. Team name normalisation** (`normalization.py`)

`normalize_team_name(name, league)` lowercases, strips non-alphanumeric chars, collapses whitespace, then looks up the result in a per-league `TEAM_ALIASES` dictionary.

**6. Event matching** (`event_matching.py`)

Groups records from all providers for the same real-world game: parse team names from the event description, normalise via aliases, match to an existing group (same league, same unordered team pair, start time within ±30 min), or create a new group. Canonical event ID: `league|YYYYMMDDTHHMM|home_team|away_team`.

**7. Market filtering** (`market_matching.py`)

`is_game_win_loss_record()` keeps only two-way/three-way moneyline records where the selection is one of the two identified teams, `"draw"`, or `"tie"`.

**8. Aggregation** (`aggregation.py`)

Filtered records are grouped by canonical event ID. For each game the best decimal odds per team per provider are computed (highest for back, lowest for lay) and written into a flat dict. Market IDs from each provider are stored alongside odds.

**9. Fee-adjusted effective odds** (`calculator.py`)

Before any arb comparison, each provider's raw decimal odds are converted to *effective* odds that represent what you actually keep after fees. The adjustment differs by provider.

**Matchbook and Smarkets — flat commission on net winnings**

Commission is charged only on profit, not on the returned stake.

```
eff_back = 1 + (odds − 1) × (1 − c)
eff_lay  = 1 + (odds − 1) / (1 − c)
```

Example — Matchbook back odds 3.00, c = 0.02:
- Net winnings per £1 stake = 2.00; after 2% commission: 2.00 × 0.98 = 1.96
- Effective back odds = **2.96**

The lay formula is the inverse: commission makes laying more expensive, so effective lay odds are higher than quoted.

**SX Bet — zero commission**

```
eff_back = odds
eff_lay  = odds
```

No adjustment needed; raw odds are used directly.

**Polymarket — dynamic fee on stake**

Polymarket's sports book charges a percentage of the *total stake* (not just winnings). The rate scales with how close to 50/50 the market is:

```
fee_rate = 0.0075 × 4 × p × (1 − p)      where p = 1 / odds
```

This peaks at 0.75% when p = 0.5 (odds = 2.00) and falls off toward zero for heavy favourites or long shots. Effective odds:

```
eff_back = odds − fee_rate
eff_lay  = 1 + (odds − 1) / (1 − fee_rate)
```

Note the back formula deducts the fee directly from decimal odds (not just from profit), because the fee applies to the full stake.

| Odds | p | Polymarket fee | Effective back |
|------|---|---|---|
| 2.00 | 0.500 | 0.75% | 1.9925 |
| 3.00 | 0.333 | 0.67% | 2.9933 |
| 5.00 | 0.200 | 0.48% | 4.9952 |
| 10.00 | 0.100 | 0.27% | 9.9973 |

**How the comparison works**

`_best_back(game, slot)` iterates every provider's back-odds field for that outcome, computes `eff_back` for each, and returns the provider with the highest effective back odds. `_best_lay(game, slot)` does the same for lay, picking the lowest effective lay odds. Those two values — not the raw quoted odds — are what the arb detectors compare.

**10. Arb detection** (`calculator.py`)

- `find_sure_bets()` — back-back(-back) across providers: net margin = sum of `1 / eff_back_odds` per leg. If margin < 1, it is a sure bet.
- `find_back_lay_arbs()` — per outcome: if `eff_back_odds > eff_lay_odds` across providers, it is a back-lay arb.

## Developer utilities

### find_smarkets_event_ids.py

Walks the Smarkets event hierarchy to find root competition IDs.

```bash
python find_smarkets_event_ids.py --name "premier"
python find_smarkets_event_ids.py --ancestors 12345678
python find_smarkets_event_ids.py --parent 1234567 --search "premier" --depth 3
```

### find_sx_bet_league_ids.py

Queries SX Bet `/leagues/active` to find league IDs. No credentials required.

```bash
python find_sx_bet_league_ids.py --search "premier"
```

## TODO

- **Investigate liquidity numbers** — verify that `*_back_avail` / `*_lay_avail` figures (Matchbook order depth, Smarkets contract liquidity, SX Bet taker-available, Polymarket CLOB size) are computed and converted to GBP consistently.

- **Investigate SX Bet further** — `type == 1` for soccer markets has not been validated against live EPL/UCL data. The back/lay probability derivation from P2P maker orders needs end-to-end verification once live soccer markets are available.
