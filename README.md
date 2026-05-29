# matched_betting

Odds ingestion and arbitrage detection system for matched betting on NBA, WNBA, MLB (moneyline, spread, totals), UCL, EPL, UEL, NHL, IPL, Serie A, La Liga, and MLS (moneyline, spread, totals) games.

Fetches live odds from Matchbook, Smarkets, Polymarket, SX Bet, and Azuro, normalises them into a unified schema, matches records for the same game across providers, and scans for sure bets and back-lay arbs (after commission).

The system runs as a two-stage pipeline:

1. **`ids.py`** — discover active games and save their market IDs to a JSON index.
2. **`scan.py`** — read the IDs index, fetch live odds game-by-game with all providers in parallel, and report arbs as they are found.

## Project layout

```
mbv2-Default/
├── ids.py                          # Stage 1: discover active games → outputs/active_game_ids.json
├── scan.py                         # Stage 2: live scan, per-game parallel fetch + arb detection
├── bet_executor.py                 # Auto-bet orchestration: leg ordering, sizing, placement, alerts
├── bet.py                          # Per-platform bet placement functions (Matchbook, Polymarket, SX Bet)
├── arb_finder.py                   # Standalone arb finder (reads a pre-built aggregated games JSON)
├── run.py                          # Legacy launcher (full fetch → JSON outputs)
├── portfolio.py                    # Wallet balance and active bet monitor
├── watch_bet.py                    # Live watcher: polls scan output and monitors active bet status
├── analyse_bets.py                 # Post-hoc bet analysis and P&L reporting
├── menu.py                         # Interactive CLI menu for common pipeline operations
├── vpn_proxy_bridge.py             # SOCKS5 bridge: 127.0.0.1:1081 → upstream via Mullvad tunnel
├── matchbook_bet.py                # Dev utility: direct Matchbook order placement for testing
├── polymarket_bet.py               # Dev utility: direct Polymarket CLOB order placement for testing
├── pm_geo_check.py                 # Dev utility: verify Polymarket geo-access via VPN bridge
├── find_smarkets_event_ids.py      # Dev utility: discover Smarkets competition IDs
├── find_sx_bet_league_ids.py       # Dev utility: discover SX Bet league IDs
├── src/matched_betting/
│   ├── __main__.py                 # Enables python -m matched_betting
│   ├── cli.py                      # Argument parsing and orchestration (used by run.py)
│   ├── config.py                   # Settings and CommissionSettings loaded from .env
│   ├── calculator.py               # Pure arb maths: commission helpers, find_sure_bets, find_back_lay_arbs
│   ├── kelly.py                    # Profit-scaled bet sizing (Kelly-inspired bankroll fraction)
│   ├── notifier.py                 # Webhook alert delivery (ntfy.sh, Telegram, Discord, Slack, generic)
│   ├── rebalancer.py               # Post-bet balance monitor: alerts when USDC drops below threshold
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
│       ├── polymarket.py           # Polymarket public API adapter (CLOB v2)
│       ├── sx_bet.py               # SX Bet public API adapter (P2P, no credentials required)
│       └── azuro.py                # Azuro decentralised protocol adapter (Polygon subgraph)
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

Install the package and its dependencies:

```bash
pip install -e .
```

The only non-stdlib dependency is `requests` (used by the HTTP client and the notifier).

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

# Polymarket (public, no credentials required for read; private key required for betting)
POLYMARKET_GAMMA_BASE_URL=https://gamma-api.polymarket.com
POLYMARKET_CLOB_BASE_URL=https://clob.polymarket.com
POLYMARKET_PRIVATE_KEY=        # EVM private key for signing CLOB orders
POLYGON_RPC_URL=               # Polygon JSON-RPC endpoint (for on-chain reads)

# SX Bet (public, no credentials required)
SX_BET_BASE_URL=https://api.sx.bet
SX_BET_BASE_TOKEN=0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B
# Block explorer for on-chain USDC balance queries (portfolio.py).
# Leave blank to use the default: https://explorerl2.sx.technology/api
SX_EXPLORER_URL=

# Azuro (public, no credentials required)
AZURO_SUBGRAPH_URL=https://thegraph-1.onchainfeed.org/subgraphs/name/azuro-protocol/azuro-data-feed-polygon

# VPN proxy (optional — see VPN routing section below)
VPN_PROXY_URL=

# Alerts — set ALERT_WEBHOOK_URL to enable push notifications
ALERT_WEBHOOK_URL=             # ntfy.sh, Telegram, Discord, Slack, or generic webhook
ALERT_ENABLED=true

# Commission rates
# Set SMARKETS_ZERO_COMMISSION=false once the 60-day intro period ends
SMARKETS_ZERO_COMMISSION=true
MATCHBOOK_COMMISSION=0.02
SMARKETS_COMMISSION=0.02
SX_BET_COMMISSION=0.00

# Kelly bet sizing (used with --auto-bet when KELLY_ENABLED=true)
KELLY_ENABLED=true
KELLY_LOW_PROFIT=0.2           # % profit that maps to KELLY_LOW_FRACTION of bankroll
KELLY_HIGH_PROFIT=1.5          # % profit that maps to KELLY_HIGH_FRACTION of bankroll
KELLY_LOW_FRACTION=0.10
KELLY_HIGH_FRACTION=0.25
MAX_STAKE_USDC=200             # hard cap per arb regardless of Kelly output
MIN_STAKE_USDC=5               # skip arbs that would size below this
MIN_BANKROLL_USDC=20           # minimum computed bankroll before Kelly kicks in
# SX Bet / Polymarket arbs use a steeper 3-anchor piecewise curve (bridge fee breakeven at kink)
SX_PM_KINK_PROFIT=0.44         # kink point = bridge fee threshold
SX_PM_KINK_FRACTION=0.15       # fraction at kink
SX_PM_HIGH_FRACTION=0.40       # fraction at KELLY_HIGH_PROFIT (vs 0.25 for other arbs)

# Rebalancer — alerts when a platform USDC balance falls below MIN_BALANCE_USDC
REBALANCER_ENABLED=true
MIN_BALANCE_USDC=10
```

Providers without credentials will be skipped with a warning rather than crashing.

### VPN / Proxy routing

Polymarket and SX Bet require a non-blocked IP for trading. The pipeline routes their traffic through a local SOCKS5 bridge (`vpn_proxy_bridge.py`) while keeping Matchbook, Smarkets, and Azuro on a direct connection. **Matchbook must never be routed through the VPN** — it will suspend accounts that connect from a proxy or VPN IP.

**How it works:**

`vpn_proxy_bridge.py` is a lightweight SOCKS5 server that listens on `127.0.0.1:1081`. It runs as `pythonw.exe`, which sits inside the Mullvad VPN tunnel. When scan.py (running as `python.exe`, excluded from the tunnel) makes a request through the bridge, the upstream TCP connection is created by `pythonw.exe` and exits through the Mullvad relay — never through the raw Azure IP.

Start the bridge before running the pipeline:

```
pythonw vpn_proxy_bridge.py
```

Then set `VPN_PROXY_URL` in `.env`:

```
VPN_PROXY_URL=socks5h://127.0.0.1:1081
```

The `socks5h` scheme sends hostnames to the bridge for resolution, preventing DNS leaks.

**Mullvad relay:** Polymarket blocks trading from both US and Swedish IPs. Set the relay to Canada before starting:

```
mullvad relay set location ca
```

Montreal relays (`ca-mtr-*`) work reliably. Verify with:

```
mullvad status
```

**Split-tunnel exclusion:** Only `python.exe` (and `svchost.exe`) should be in Mullvad's split-tunnel exclusion list. `pythonw.exe` must remain inside the tunnel so the bridge's upstream connections exit via Mullvad.

**Provider routing:**

| Provider | Connection |
|---|---|
| Matchbook | Direct (always — VPN would get the account suspended) |
| Smarkets | Direct (always) |
| Azuro | Direct (always) |
| Polymarket | Via bridge if `VPN_PROXY_URL` is set, otherwise direct |
| SX Bet | Via bridge if `VPN_PROXY_URL` is set, otherwise direct |

Before scanning, `scan.py` tests that the bridge is reachable via a TCP connect. If it is not, you are warned — requests to Polymarket and SX Bet will fail or expose your real IP if you proceed without the bridge running.

Leave `VPN_PROXY_URL` blank to disable proxying entirely.

## Running the pipeline

### Stage 1 — discover active games

```bash
python ids.py
```

This fetches all markets from the configured providers in parallel, matches them to canonical games, and writes `outputs/active_game_ids.json`. By default Smarkets and Azuro are excluded.

```bash
python ids.py --leagues nba epl            # specific leagues only
python ids.py --providers matchbook polymarket sx_bet smarkets azuro
python ids.py --out outputs/my_ids.json
python ids.py --debug
```

Supported leagues: `nba`, `wnba`, `mlb`, `mlb_spread`, `mlb_totals`, `ucl`, `epl`, `uel`, `nhl`, `ipl`, `seria`, `laliga`, `mls`, `mls_spread`, `mls_totals`.

### Stage 2 — scan live odds

```bash
python scan.py
```

Reads the IDs JSON, iterates games one at a time. For each game, all providers are fetched simultaneously, the arb calculator runs immediately, and any arbs are printed before moving on to the next game.

```bash
python scan.py --ids outputs/active_game_ids.json
python scan.py --leagues nba epl
python scan.py --providers matchbook polymarket sx_bet azuro
python scan.py --min-profit 0.5            # only show arbs ≥ 0.5% profit
python scan.py --show-odds                 # print back/lay odds table per game
python scan.py --azuro-cap                 # show max profit constrained by Azuro pool size
python scan.py --polymarket-debug          # detailed Polymarket diagnostics per game
python scan.py --debug
```

### Auto-betting

Pass `--auto-bet` to automatically place every arb found. Requires `--budget` (maximum stake per arb in USDC). Supported providers: Matchbook, Polymarket, SX Bet.

```bash
python scan.py --auto-bet --budget 50
python scan.py --auto-bet --budget 50 --bet-dry-run   # build and sign orders but do not submit
python scan.py --auto-bet --budget 50 --allow-topup   # enable incremental top-ups (see below)
```

When `KELLY_ENABLED=true` (the default), the actual stake is determined by a profit-scaled Kelly fraction of the current bankroll rather than `--budget` directly. `--budget` acts as a hard cap. Kelly sizing is skipped if any platform balance is unavailable or the computed bankroll falls below `MIN_BANKROLL_USDC`.

When both a sure bet and a back-lay arb are found on the same game, `--auto-bet` places only the **single highest-profit arb** across both lists. Lower-profit arbs for the same game are skipped and a count is printed.

#### Incremental top-ups (`--allow-topup`)

When `--allow-topup` is passed, if an arb is found on a game where a bet was already placed in the last 36 hours with **exactly the same leg structure** (same platform, side, and outcome on every leg), and the new profit is at least **0.1% higher** than the prior bet, an additional top-up stake is placed equal to:

```
top-up stake = Kelly(new_edge) - Kelly(prior_edge)   (evaluated at current bankroll)
```

This brings the total committed stake up to what Kelly would have sized at the higher edge from the start. If the delta falls below `MIN_STAKE_USDC` the top-up is skipped. Top-up bets are logged with status `PLACED_TOPUP` in `bet_log.jsonl`, and subsequent top-ups always delta against the highest previously committed edge for that game. Off by default — enable once bankroll is large enough for the deltas to clear the minimum stake threshold.

After each successful bet, the rebalancer checks USDC balances on Polymarket (Polygon) and SX Bet (SX Network). If either drops below `MIN_BALANCE_USDC`, an alert is sent via the configured webhook.

### Portfolio monitor

`portfolio.py` checks wallet balances and active bets across all three platforms concurrently. It is a standalone file — it does not depend on the scan pipeline.

```bash
python portfolio.py                  # summary — balances + counts
python portfolio.py --detail         # summary + every active bet/order/position
python portfolio.py --matchbook      # Matchbook only (full detail)
python portfolio.py --polymarket     # Polymarket only (full detail)
python portfolio.py --sx-bet         # SX Bet only (full detail)
```

**What each platform shows:**

| Platform | Balance source | Active bets |
|---|---|---|
| Matchbook | `/edge/rest/account` (GBP) | Open offers (unmatched/partial), matched (awaiting settlement), settled (recent) |
| Polymarket | CLOB USDC balance + MATIC gas | Open orders, active positions with PnL, recent trades |
| SX Bet | SX Network block explorer `tokenbalance` API (on-chain USDC) | Open maker orders with fill ratio, recent trades |

SX Bet's on-chain USDC balance is queried via `https://explorerl2.sx.technology/api` using the `tokenbalance` action against the USDC contract (`SX_BET_BASE_TOKEN`). Override the endpoint with `SX_EXPLORER_URL` in `.env` if needed.

**Cancel actions:**

```bash
python portfolio.py --cancel-mb OFFER_ID
python portfolio.py --cancel-pm                    # cancel all open Polymarket orders
python portfolio.py --cancel-pm --order-id ID      # cancel one Polymarket order
python portfolio.py --cancel-sx ORDER_HASH
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
| Azuro | 0% |

## Kelly bet sizing

When `KELLY_ENABLED=true`, `--auto-bet` sizes each stake as a profit-scaled fraction of the current bankroll:

```
bankroll = min(balances of the platforms involved in this arb) × 3
stake    = bankroll × fraction  (clamped to [MIN_STAKE_USDC, MAX_STAKE_USDC])
```

Only the platforms actually involved in the arb contribute to the bankroll calculation. The ×3 multiplier is fixed regardless of how many platforms are involved, so sizing is always anchored to the weakest leg. If any relevant balance is unavailable or the computed bankroll falls below `MIN_BANKROLL_USDC`, Kelly is skipped and `--budget` is used as the fixed stake.

#### Standard curve (all arbs except SX Bet / Polymarket)

The fraction is a clamped linear interpolation between two anchors:

```
fraction = linear interpolation between (KELLY_LOW_PROFIT,  KELLY_LOW_FRACTION)
                                      and (KELLY_HIGH_PROFIT, KELLY_HIGH_FRACTION)
```

Example with defaults — bankroll $100:

| Profit | Fraction | Stake |
|--------|----------|-------|
| 0.20%  | 10.0%    | $10.00 |
| 0.50%  | 13.5%    | $13.46 |
| 1.00%  | 19.2%    | $19.23 |
| 1.50%  | 25.0%    | $25.00 |

#### SX Bet / Polymarket arbs — kinked curve

SX Bet / Polymarket arbs use a steeper 3-anchor piecewise curve. The kink sits at the bridge fee breakeven point (`SX_PM_KINK_PROFIT`): below the kink the curve rises faster than the standard linear, and above it continues to a higher ceiling (`SX_PM_HIGH_FRACTION`).

```
fraction = piecewise linear over:
  (KELLY_LOW_PROFIT,      KELLY_LOW_FRACTION)    # 0.20% → 10%
  (SX_PM_KINK_PROFIT,     SX_PM_KINK_FRACTION)   # 0.44% → 15%  (bridge breakeven)
  (KELLY_HIGH_PROFIT,     SX_PM_HIGH_FRACTION)   # 1.50% → 40%
```

Example with defaults — bankroll $100:

| Profit  | Fraction | Stake  | vs standard |
|---------|----------|--------|-------------|
| 0.20%   | 10.0%    | $10.00 | — |
| 0.30%   | 12.1%    | $12.08 | +$0.93 |
| 0.44% * | 15.0%    | $15.00 | +$2.23 |
| 0.50%   | 16.4%    | $16.42 | +$2.96 |
| 1.00%   | 28.2%    | $28.21 | +$8.98 |
| 1.50%   | 40.0%    | $40.00 | +$15.00 |

\* bridge fee breakeven — kink point

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
  "azuro_condition_id": "0xdef",
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
  "sx_bet_team2_lay_odds": null,
  "azuro_team1_back_odds": 2.05,
  "azuro_team1_lay_odds": null,
  "azuro_team2_back_odds": 1.85,
  "azuro_team2_lay_odds": null
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
    │     ├── SxBetProvider.fetch_odds
    │     └── AzuroProvider.fetch_odds
    ├── is_game_win_loss_record filter          # market_matching.py
    ├── match_records_to_canonical_events       # event_matching.py
    ├── build_aggregated_games_payload          # aggregation.py
    └── write outputs/active_game_ids.json

scan.py
    ├── load_settings (config.py)
    ├── calculator.configure(commission)
    ├── build_provider_registry (with proxied HttpClient for Polymarket/SX Bet)
    └── for each game:
          ├── ThreadPoolExecutor               # all providers in parallel for this game
          │     ├── MatchbookProvider.fetch_odds_by_ids
          │     ├── PolymarketProvider.fetch_odds_by_ids
          │     ├── SxBetProvider.fetch_odds_by_ids
          │     └── AzuroProvider.fetch_odds_by_ids
          ├── match_records_to_canonical_events
          ├── build_aggregated_games_payload
          ├── calculator.find_sure_bets
          ├── calculator.find_back_lay_arbs    # print arbs immediately
          └── (if --auto-bet) kelly sizing → place bets → rebalancer check

run.py  (legacy, no VPN proxy)
    ├── load_settings (config.py)
    ├── ThreadPoolExecutor                      # parallel full-discovery fetches
    ├── is_game_win_loss_record filter
    ├── match_records_to_canonical_events
    ├── build_aggregated_games_payload
    └── write outputs/latest_odds_{all_odds,aggregated_games,market_index}.json
        (--update mode uses stored market IDs from market_index for targeted re-fetch)
```

### Step-by-step data flow

**1. Configuration** (`config.py`)

`load_settings()` reads a `.env` file from the project root and populates frozen `Settings` dataclasses including `CommissionSettings`, `KellySettings`, and `RebalancerSettings`. Missing credentials cause a provider to raise `ProviderNotReadyError` at fetch time, caught and reported as a warning.

**2. Provider fetching** (`providers/`)

Each provider implements `OddsProvider` (`base.py`) with two methods:

- `fetch_odds(leagues)` — full discovery: paginates the provider API for all markets in the requested leagues.
- `fetch_odds_by_ids(game_contexts, leagues)` — targeted fetch: uses stored market/event IDs to re-fetch only known markets.

All providers for a given stage are queried concurrently via `ThreadPoolExecutor`. Each returns a `ProviderPayload` containing a flat list of `OddsRecord` objects.

Azuro is a decentralised protocol queried via a GraphQL subgraph on Polygon. It provides back odds only (no lay side); `--azuro-cap` constrains displayed profit by the pool's maximum stake per outcome.

**3. Data model** (`models.py`)

Every record shares the same frozen `OddsRecord` dataclass: provider, sport, league, event/market identity, selection name/side, decimal odds, implied probability, and a `metadata` dict for provider-specific raw fields.

**4. Odds normalisation** (per provider)

- **Matchbook**: prices already decimal; lay depth in `metadata`.
- **Smarkets**: integer prices (`0`–`10000`) → divide by 10000 → invert to decimal.
- **Polymarket**: token IDs resolved from Gamma API; best ask/bid from CLOB v2 order book; prices converted `1 / p`.
- **SX Bet**: odds as scaled integers (`maker_probability × 10²⁰`); taker decimal = `1 / (1 − maker_probability)`.
- **Azuro**: `currentOdds` from the Polygon subgraph is already a decimal string; used directly.

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

**SX Bet and Azuro — zero commission**

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

- **Investigate SX Bet soccer markets** — `type == 1` for soccer markets has not been validated against live EPL/UCL data. The back/lay probability derivation from P2P maker orders needs end-to-end verification once live soccer markets are available.

- **Azuro NHL/MLB slugs** — league slugs for NHL and MLB on the Polygon subgraph are unverified; those leagues may return 0 records gracefully until confirmed live.

- **Validate MLS coverage** — MLS moneyline, spread, and totals markets (`mls`, `mls_spread`, `mls_totals`) are wired up across Polymarket, Matchbook, and SX Bet but have not been validated end-to-end against live MLS data.
