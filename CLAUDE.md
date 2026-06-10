# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run the full odds scan (all leagues, all providers)
python -m matched_betting

# Run for specific leagues or providers
python -m matched_betting --nba
python -m matched_betting --mlb
python -m matched_betting --leagues nba nhl --providers polymarket sx_bet

# Targeted re-fetch using stored market IDs (faster; falls back to full scan if no index)
python -m matched_betting --update

# Re-emit the last output without network calls
python -m matched_betting --reuse

# Update market ID index only (no odds output files written)
python -m matched_betting --index-only

# Run with verbose debug output
python -m matched_betting --debug

# Run tests
python -m unittest discover -s tests

# Run a single test file
python -m unittest tests.test_event_matching

# Run a single test case
python -m unittest tests.test_event_matching.EventMatchingTests.test_team_alias_normalization
```

The package root is `src/`; tests prepend it to `sys.path` manually, so no install step is needed. All network credentials live in `.env`.

## Architecture Overview

This is a **matched-betting odds aggregator** that fetches moneyline (and some spread/totals) markets from multiple providers, normalises them into a canonical format, cross-matches events across providers, and emits JSON outputs for downstream analysis.

### Data flow

```
providers/*  →  OddsRecord list  →  event_matching  →  aggregation  →  JSON outputs
                                      (canonical events)   (per-game odds table)
```

1. **Each provider** implements `OddsProvider` (`providers/base.py`) with two methods:
   - `fetch_odds(leagues)` — full discovery, iterates paginated APIs, returns `ProviderPayload`.
   - `fetch_odds_by_ids(game_contexts, leagues)` — targeted refresh using market IDs stored from a prior run (`--update` mode).

2. **`cli.py`** orchestrates everything: builds the provider registry, runs fetches in parallel via `ThreadPoolExecutor`, pipes records through `event_matching` and `aggregation`, and writes three output files:
   - `*_all_odds.json` — flat list of every `OddsRecord`.
   - `*_aggregated_games.json` — one entry per canonical game, with best odds per provider per selection.
   - `*_market_index.json` — additive index mapping league → game → provider IDs, used by `--update` mode.

3. **`event_matching.py`** groups `OddsRecord`s into `CanonicalEventGroup`s by matching on normalised team names + start time (±30 min tolerance). The canonical event ID encodes `league|starttime|home|away`.

4. **`normalization.py`** contains `TEAM_ALIASES` (a per-league dict) and `normalize_team_name()` which lowercases, strips non-alphanumeric chars, and applies aliases. **When adding a new league, add its team aliases here.**

5. **`market_matching.py`** further groups records within a canonical event into `CanonicalBetGroup`s (e.g. moneyline vs. spread vs. totals) and implements `is_game_win_loss_record()`, the filter that drops non-moneyline records before aggregation.

### Adding a new league

Every new league requires touching these files (in order):

| File | What to add |
|------|-------------|
| `normalization.py` | `TEAM_ALIASES["<league>"]` dict — keys are already-normalised provider variants, values are canonical names |
| `providers/matchbook.py` | `LEAGUE_SPORT_IDS["<league>"]`, `LEAGUE_TO_SPORT["<league>"]`, and a branch in `_event_matches_league()` to filter by meta-tag |
| `providers/sx_bet.py` | `LEAGUE_TO_SPORT["<league>"]` and `_LEAGUE_IDS["<league>"]` (discover via `find_sx_bet_league_ids.py`) |
| `providers/polymarket.py` | `LEAGUE_TO_SPORT["<league>"]`, `_LEAGUE_EVENT_TAGS["<league>"]`, slug detection block in `fetch_odds()`, and team slug list in `_load_team_index()` |
| `cli.py` | Add league to `DEFAULT_LEAGUES`, `ALL_LEAGUES`, `_KNOWN_LEAGUE_ORDER`, and optionally add a `--<league>` shortcut argument |

For a **two-way moneyline** league (like NBA/NHL/MLB), `market_type` should be `"two_way"`. Soccer leagues use `"three_way"` and need separate handling in `_SOCCER_LEAGUES` frozensets.

### Provider-specific notes

- **Matchbook** — Requires login (`MATCHBOOK_USERNAME`, `MATCHBOOK_PASSWORD`). Has a rate limiter (`_throttle()`, default 400 req/min; account hard cap 700/min). Events are fetched by `sport_id` then filtered by `meta-tag url-name`. The `find_matchbook_league_tags.py` script helps discover correct tag names.
- **Polymarket** — Requires VPN proxy (Montreal relay) via `VPN_PROXY_URL`. Markets are discovered by slug pattern matching against hardcoded team-slug lists. Update mode uses CLOB token IDs stored in `_market_index.json` to skip Gamma API lookups.
- **SX Bet** — Also VPN-proxied. Uses `leagueId` + market `type` integer to filter. `find_sx_bet_league_ids.py` discovers league IDs. Moneyline = type 226. Soccer uses one market per outcome (home/draw/away) grouped by `sportXEventId`.
- **Smarkets / Azuro** — Largely unused in active runs; smarkets has rate-limit retry logic in `cli.py`.

### Key constants in `cli.py`

- `DEFAULT_LEAGUES` / `ALL_LEAGUES` — the set of valid league strings (must match across all providers).
- `_KNOWN_LEAGUE_ORDER` — ordering used in the market index JSON output.

### Output schema (`_aggregated_games.json`)

Each entry in `aggregated_games` contains: `league`, `sport`, `team1`, `team2`, `date_time`, canonical event/bet IDs, per-provider best odds keyed by selection name (`back`/`lay`), and stored market IDs for all providers.

### Discovery helper scripts

The root-level `_discover_*.py` and `find_*.py` scripts are one-off debugging tools for discovering API IDs and verifying market structures — they are not part of the production pipeline.
