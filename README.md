# matched_betting

Initial ingestion layer for a matched betting system.

This first version focuses on retrieving and normalizing NBA and MLB odds from:

- Matchbook
- Smarkets
- Polymarket

The codebase is intentionally modular so provider-specific logic stays isolated from the shared schema, CLI, and future comparison engine.

## Current state

- The project scaffold, virtual environment, config loading, CLI, normalized output schema, and tests are in place.
- The Matchbook adapter is implemented with session login and live event/market price ingestion.
- The Smarkets adapter is implemented with session login, event tree traversal, market contract discovery, and authenticated quote ladders.
- The Polymarket adapter is implemented against public documented endpoints.

## Project layout

```text
matched_betting/
  src/matched_betting/
    cli.py
    config.py
    http.py
    models.py
    providers/
      base.py
      matchbook.py
      polymarket.py
      registry.py
      smarkets.py
  tests/
```

## Setup

The dedicated virtual environment already lives at:

`/Users/guysemple/matched_betting/.venv`

To activate it:

```bash
source /Users/guysemple/matched_betting/.venv/bin/activate
```

## Configuration

Create an env file:

```bash
cp /Users/guysemple/matched_betting/.env.example /Users/guysemple/matched_betting/.env
```

Then add credentials as you provide them.

## Run

```bash
/Users/guysemple/matched_betting/.venv/bin/python /Users/guysemple/matched_betting/run.py --format json
```

Optional examples:

```bash
/Users/guysemple/matched_betting/.venv/bin/python /Users/guysemple/matched_betting/run.py --providers polymarket
/Users/guysemple/matched_betting/.venv/bin/python /Users/guysemple/matched_betting/run.py --nba
/Users/guysemple/matched_betting/.venv/bin/python /Users/guysemple/matched_betting/run.py --mlb
/Users/guysemple/matched_betting/.venv/bin/python /Users/guysemple/matched_betting/run.py --leagues nba
/Users/guysemple/matched_betting/.venv/bin/python /Users/guysemple/matched_betting/run.py --out /Users/guysemple/matched_betting/outputs/latest_odds.json
/Users/guysemple/matched_betting/.venv/bin/python /Users/guysemple/matched_betting/run.py --debug
```

The `run.py` launcher inserts `src/` onto `sys.path`, so no package install step is required.

`--debug` prints progress messages to `stderr` while the normal JSON output still goes to `stdout`.

Each run now matches records to the same real-world game via `canonical_event_id`.

By default the run writes two separate files:

- `outputs/latest_odds_records.json`
- `outputs/latest_odds_aggregated_games.json`

If you pass `--out /some/path/name.json`, those become:

- `/some/path/name_records.json`
- `/some/path/name_aggregated_games.json`

## Output schema

Each normalized record uses the same shape regardless of source:

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
  "metadata": {}
}
```

## Notes

- Polymarket prices are normalized from public outcome prices into decimal odds using `1 / probability`.
- Smarkets quote ladder prices are normalized from their integer probability format into decimal odds using `price / 10000` then `1 / probability`.
- Exchange-specific concepts such as lay/back depth, commissions, and order book levels are deliberately left in `metadata` for now and can be pulled into the shared schema later if needed.
- The next logical step after credential wiring is a comparison module that groups records by normalized event and market type.
