# matched_betting

Odds ingestion and arbitrage detection system for matched betting on NBA, WNBA, MLB (moneyline, spread, totals), KBO, UCL, EPL, UEL, NHL, IPL, Serie A, La Liga, Veikkausliiga, and MLS (moneyline, spread, totals) games.

Fetches live odds from Matchbook, Smarkets, Polymarket, SX Bet, and Azuro, normalises them into a unified schema, matches records for the same game across providers, and scans for sure bets, back-lay arbs, and KBO tie-aware arbs (after commission).

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
├── vpn_proxy_bridge.py             # SOCKS5 bridge: 127.0.0.1:1082 → upstream via Mullvad tunnel
├── specials_scan.py                # Specials scanner: read-only eval of one-off prediction markets
├── specials_place.py               # Specials placement: executes strategies with live confirmation
├── specials_close.py               # Specials close-out: sends closing orders for open specials positions
├── specials_active.py              # List active/pending specials positions
├── specials_registry.py            # Registry of named specials events and their strategies
├── specials.py                     # Core specials orchestrator (fetching, evaluation, snapshots)
├── specials_strategies.py          # Strategy definitions for specials (back/lay, arb, threshold)
├── specials_size.py                # Kelly sizing for specials legs
├── matchbook_bet.py                # Dev utility: direct Matchbook order placement for testing
├── polymarket_bet.py               # Dev utility: direct Polymarket CLOB order placement for testing
├── pm_geo_check.py                 # Dev utility: verify Polymarket geo-access via VPN bridge
├── find_smarkets_event_ids.py      # Dev utility: discover Smarkets competition IDs
├── find_sx_bet_league_ids.py       # Dev utility: discover SX Bet league IDs
├── src/matched_betting/
│   ├── __main__.py                 # Enables python -m matched_betting
│   ├── cli.py                      # Argument parsing and orchestration (used by run.py)
│   ├── config.py                   # Settings and CommissionSettings loaded from .env
│   ├── calculator.py               # Pure arb maths: commission helpers, find_sure_bets, find_back_lay_arbs, find_kbo_tie_aware_arbs
│   ├── kelly.py                    # Profit-scaled bet sizing (Kelly-inspired bankroll fraction)
│   ├── notifier.py                 # Webhook alert delivery (ntfy.sh, Telegram, Discord, Slack, generic) + heartbeat
│   ├── rebalancer.py               # Post-bet balance monitor: alerts when USDC drops below threshold
│   ├── aggregation.py              # Builds per-game aggregated odds payload from OddsRecord objects
│   ├── event_log.py                # Append-only structured logging: arb_log.jsonl, warnings.log, crashes.log
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
│       └── azuro.py                # Azuro decentralised protocol adapter (Polygon REST API)
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

Polymarket and SX Bet require a non-blocked IP for trading. The pipeline routes their traffic through a local SOCKS5 bridge (`vpn_proxy_bridge.py`) while keeping Matchbook, Smarkets, and Azuro on a direct connection.

**Hard constraints — do not violate:**
- **Matchbook must never be routed through the VPN.** Matchbook suspends accounts that connect from a proxy or VPN IP. It always uses a direct connection regardless of `VPN_PROXY_URL`.
- **`svchost.exe` must always be in the Mullvad split-tunnel excluded list.** `svchost.exe` hosts the RDP server (TermService) and Azure VM heartbeat services. If it enters the VPN tunnel the RDP session drops and the VM becomes inaccessible.

**How it works:**

`vpn_proxy_bridge.py` is a lightweight SOCKS5 server that listens on `127.0.0.1:1082`. The bridge process runs as `pythonw.exe` inside the Mullvad VPN tunnel. When `scan.py` (running as `python.exe`, excluded from the tunnel) makes a Polymarket or SX Bet request through `socks5h://127.0.0.1:1082`, the upstream connection is created by `pythonw.exe` and exits through the Mullvad relay — never through the raw Azure IP. Matchbook requests use a plain `HttpClient()` with no proxy.

**Starting the bridge:**

Use `menu.py` (the recommended path) or `start_vpn_bridge.bat` (standalone). Both ensure the bridge is correctly set up. Do **not** start the bridge manually with `pythonw vpn_proxy_bridge.py` unless it is run from an interactive terminal that is not a descendant of an excluded process (see the WFP note below).

```
python menu.py          # use the VPN menu → [v] → (Re)start bridge
```

or:

```
start_vpn_bridge.bat    # run from an interactive CMD window
```

Then set `VPN_PROXY_URL` in `.env`:

```
VPN_PROXY_URL=socks5h://127.0.0.1:1082
```

The `socks5h` scheme sends hostnames to the bridge for resolution, preventing DNS leaks.

**Mullvad relay:** Polymarket and SX Bet apply geo-restrictions at the IP level. Portugal relays (`pt-lis-wg-*`) are confirmed working. Set with:

```
mullvad relay set location pt lis
```

Verify with `mullvad status`. If a relay is blocked for trading, switch relays and use the VPN menu → [v] → [1] Check routing to confirm both platforms show OK before restarting the daemon.

**Split-tunnel configuration:**

Enable split tunneling and add exactly these two exclusions:

```
mullvad split-tunnel set on
mullvad split-tunnel app add "C:\Windows\System32\svchost.exe"
mullvad split-tunnel app add "C:\Program Files\Python312\python.exe"
```

`start_vpn_bridge.bat` runs these commands automatically. `pythonw.exe` must **not** be in the exclusion list — it must remain inside the tunnel so the bridge's upstream connections exit via Mullvad.

**WFP inheritance — why the bridge must be started carefully:**

Mullvad's Windows split-tunnel driver (WFP) propagates the excluded routing context transitively through the process tree, at least two levels deep. This has two implications:

1. `python.exe` (excluded) cannot spawn the bridge directly — the child `pythonw.exe` process would inherit the excluded context and receive WinError 10013 when trying to reach `10.64.0.1:1080` (Mullvad's internal SOCKS5, only reachable from inside the tunnel).

2. The Windows Task Scheduler (`mbv2-VpnProxyBridge` logon task) runs under `svchost.exe`, which is also excluded for RDP. Any bridge it spawns therefore also gets the excluded context. The logon task is disabled for this reason.

`menu.py` works around this by using `PROC_THREAD_ATTRIBUTE_PARENT_PROCESS` (Windows API via `ctypes`) to create the bridge as a direct child of `explorer.exe` (not excluded), giving it the correct tunnel routing context regardless of which process calls `_start_bridge()`. If the bridge is already running but failing (WinError 10013 after a Mullvad config change), the menu auto-kills and restarts it so the new process picks up the current tunnel state.

`start_vpn_bridge.bat`, when run from a standalone interactive CMD window (opened from the desktop or taskbar), starts `pythonw.exe` from `cmd.exe` whose parent is `explorer.exe` — the same clean context.

**Provider routing:**

| Provider | Connection |
|---|---|
| Matchbook | Direct — always. Never proxied (account ban risk). |
| Smarkets | Direct — always. |
| Azuro | Direct — always. |
| Polymarket | Via bridge if `VPN_PROXY_URL` is set, otherwise direct. |
| SX Bet | Via bridge if `VPN_PROXY_URL` is set, otherwise direct. |

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

Supported leagues: `nba`, `wnba`, `mlb`, `mlb_spread`, `mlb_totals`, `kbo`, `ucl`, `epl`, `uel`, `nhl`, `ipl`, `seria`, `laliga`, `mls`, `mls_spread`, `mls_totals`, `veikkausliiga`.

### Stage 2 — scan live odds

```bash
python scan.py
```

Reads the IDs JSON, iterates games one at a time. For each game, all providers are fetched simultaneously, the arb calculator runs immediately, and any arbs are printed before moving on to the next game. KBO games are evaluated separately with the tie-aware calculator.

```bash
python scan.py --ids outputs/active_game_ids.json
python scan.py --leagues nba epl
python scan.py --leagues kbo                       # KBO tie-aware arbs only
python scan.py --providers matchbook polymarket sx_bet azuro
python scan.py --min-profit 0.5            # only show arbs ≥ 0.5% profit
python scan.py --show-odds                 # print back/lay odds table per game
python scan.py --azuro-cap                 # show max profit constrained by Azuro pool size
python scan.py --polymarket-debug          # detailed Polymarket diagnostics per game
python scan.py --debug
```

The summary line at the end of each run shows counts for all three arb types:

```
  Games scanned:  24
  Sure bets:      1
  Back-lay arbs:  2
  KBO arbs:       1
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

### Specials pipeline

The specials pipeline handles one-off prediction market events (elections, award outcomes, non-recurring markets) that fall outside the sports game pipeline. Each event is registered in `specials_registry.py` with a catalog of provider market IDs and a list of strategies.

```bash
python specials_scan.py             # evaluate all registered events (read-only)
python specials_scan.py --event makerfield_by_election_2026
python specials_scan.py --list      # list all registered events
python specials_scan.py --debug

python specials_place.py            # place bets for all profitable strategies
python specials_place.py --event makerfield_by_election_2026 --dry-run

python specials_close.py            # send closing orders for open positions
python specials_active.py           # list active / pending specials positions
```

Specials do not go through `ids.py` or `scan.py` — they use provider market IDs from the registry directly. Outputs (snapshots and audit logs) are written to `outputs/specials_<event>.json` and `outputs/specials_<event>_evaluations.jsonl`.

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

### Event log (`outputs/`)

Three append-only log files are written during each scan run:

| File | Content |
|---|---|
| `arb_log.jsonl` | Every profitable arb detected (profit_pct > 0), one JSON object per line |
| `warnings.log` | Provider warnings and soft fetch failures (network timeouts, partial data) |
| `crashes.log` | Unhandled exceptions with full tracebacks from `_scan_game` |

`arb_log.jsonl` records every arb regardless of `--min-profit` — it is a complete audit trail independent of display filters.

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
          ├── calculator.find_sure_bets         (non-KBO games)
          ├── calculator.find_back_lay_arbs     (non-KBO games)
          ├── calculator.find_kbo_tie_aware_arbs (KBO games)
          ├── event_log.log_arb                 # append to arb_log.jsonl
          ├── PRINT ARB IMMEDIATELY (no batching)
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

Azuro is a decentralised protocol queried via its REST Backend API on Polygon. It provides back odds only (no lay side); `--azuro-cap` constrains displayed profit by the pool's maximum stake per outcome.

**3. Data model** (`models.py`)

Every record shares the same frozen `OddsRecord` dataclass: provider, sport, league, event/market identity, selection name/side, decimal odds, implied probability, and a `metadata` dict for provider-specific raw fields.

**4. Odds normalisation** (per provider)

- **Matchbook**: prices already decimal; lay depth in `metadata`.
- **Smarkets**: integer prices (`0`–`10000`) → divide by 10000 → invert to decimal.
- **Polymarket**: token IDs resolved from Gamma API; best ask/bid from CLOB v2 order book; prices converted `1 / p`.
- **SX Bet**: odds as scaled integers (`maker_probability × 10²⁰`); taker decimal = `1 / (1 − maker_probability)`.
- **Azuro**: `currentOdds` from the Polygon REST API is already a decimal string; used directly.

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
- `find_kbo_tie_aware_arbs()` — KBO-specific: back the underdog on Polymarket, back the favourite on SX Bet (see below).

KBO games are excluded from `find_sure_bets()` and `find_back_lay_arbs()` and handled exclusively by `find_kbo_tie_aware_arbs()`.

### KBO tie-aware arbs

KBO (Korean Baseball Organization) games have a structurally different tie resolution between providers:

- **Polymarket**: if a game ties, NO tokens resolve at 1.00 — both YES and NO tokens resolve at $0.50. An underdog token bought below $0.50 (odds > 2.00) therefore *gains* on a tie.
- **SX Bet**: ties are treated as void — stakes refunded, no gain or loss.

This asymmetry means backing the underdog on Polymarket and the favourite on SX Bet can be profitable even after accounting for the possibility of a tie. The calculator:

1. Identifies the underdog (team with higher decimal odds on Polymarket).
2. Calculates the two-way back-back margin using the underdog-on-Poly + favourite-on-SX direction only.
3. Reports `profit_pct` (net after fees, ignoring ties) and `tie_gain_pct` (additional gain as % of total staked if the game ties).

```
tie_gain_pct = (0.50 − 1/O_poly) / (1/O_poly + 1/O_sx) × 100
```

The odds table in `--show-odds` mode displays a `KBO arb` hint line showing the direction and margin for each KBO game.

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

- **Validate KBO coverage** — KBO tie-aware arbs are wired up for Polymarket and SX Bet but the tie-resolution logic has not been validated end-to-end against live KBO settlement data.

- **Validate Veikkausliiga coverage** — Veikkausliiga (Finnish football) is registered across providers but has not been validated against live match data.

- **Investigate liquidity numbers** — verify that `*_back_avail` / `*_lay_avail` figures (Matchbook order depth, Smarkets contract liquidity, SX Bet taker-available, Polymarket CLOB size) are computed and converted to GBP consistently.

- **Investigate SX Bet soccer markets** — `type == 1` for soccer markets has not been validated against live EPL/UCL data. The back/lay probability derivation from P2P maker orders needs end-to-end verification once live soccer markets are available.

- **Azuro NHL/MLB slugs** — league slugs for NHL and MLB on the Polygon REST API are unverified; those leagues may return 0 records gracefully until confirmed live.

- **Validate MLS coverage** — MLS moneyline, spread, and totals markets (`mls`, `mls_spread`, `mls_totals`) are wired up across Polymarket, Matchbook, and SX Bet but have not been validated end-to-end against live MLS data.
