"""
event_log.py
------------
Append-only file logging for arb detection, provider warnings, and crashes.

Files written to outputs/:
  arb_log.jsonl   — every profitable arb detected (profit_pct > 0), one JSON per line
  stream.db       — SQLite: per-game odds snapshots from stream.py (stream_ticks table)
  warnings.log    — provider warnings and soft fetch failures
  crashes.log     — unhandled exceptions with full traceback
"""
from __future__ import annotations

import json
import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path

from matched_betting.leagues import TOTALS_LEAGUES


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _outputs_dir(project_root: Path) -> Path:
    d = project_root / "outputs"
    d.mkdir(exist_ok=True)
    return d


def log_arb(arb_type: str, arb: dict, project_root: Path,
            source: str | None = None) -> None:
    """Append one profitable arb to outputs/arb_log.jsonl.

    source: optional tag to distinguish origin ("stream", "scan", etc.).
            Written to the entry only when provided, so existing records
            without the field remain valid.
    """
    if arb_type == "sure_bet":
        providers = sorted({
            p for p in (
                arb.get("team1_back_provider"),
                arb.get("team2_back_provider"),
                arb.get("draw_back_provider"),
            ) if p
        })
        entry: dict = {
            "timestamp": _now_iso(),
            "arb_type": "sure_bet",
            "league": arb.get("league"),
            "game_team1": arb.get("game_team1"),
            "game_team2": arb.get("game_team2"),
            "team1": arb.get("team1"),
            "team2": arb.get("team2"),
            "date_time": arb.get("date_time"),
            "total_line": arb.get("total_line"),
            "spread": arb.get("spread"),
            "team1_back_odds": arb.get("team1_back_odds"),
            "team1_back_provider": arb.get("team1_back_provider"),
            "team2_back_odds": arb.get("team2_back_odds"),
            "team2_back_provider": arb.get("team2_back_provider"),
            "providers": providers,
            "profit_pct": arb.get("profit_pct"),
            "profit_24h_pct": arb.get("profit_24h_pct"),
        }
    elif arb_type == "back_lay":
        entry = {
            "timestamp": _now_iso(),
            "arb_type": "back_lay",
            "league": arb.get("league"),
            "game_team1": arb.get("team1"),
            "game_team2": arb.get("team2"),
            "team1": arb.get("team1"),
            "team2": arb.get("team2"),
            "date_time": arb.get("date_time"),
            "outcome": arb.get("arb_outcome"),
            "back_odds": arb.get("back_odds"),
            "back_provider": arb.get("back_provider"),
            "lay_odds": arb.get("lay_odds"),
            "lay_provider": arb.get("lay_provider"),
            "providers": sorted({arb.get("back_provider"), arb.get("lay_provider")} - {None}),
            "profit_pct": arb.get("profit_pct"),
            "profit_24h_pct": arb.get("profit_24h_pct"),
        }
    elif arb_type == "kbo":
        entry = {
            "timestamp": _now_iso(),
            "arb_type": "kbo",
            "league": "kbo",
            "team1": arb.get("team1"),
            "team2": arb.get("team2"),
            "providers": ["polymarket", "sx_bet"],
            "profit_pct": arb.get("profit_pct"),
            "profit_24h_pct": arb.get("profit_24h_pct"),
        }
    else:
        return

    if source:
        entry["source"] = source

    path = _outputs_dir(project_root) / "arb_log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


# ---------------------------------------------------------------------------
# Stream tick DB  (outputs/stream.db)
# ---------------------------------------------------------------------------

# Ordered list of odds columns — matches the CREATE TABLE declaration below.
_STREAM_ODDS_COLS: list[str] = [
    "polymarket_team1_back_odds", "polymarket_team2_back_odds", "polymarket_draw_back_odds",
    "polymarket_team1_lay_odds",  "polymarket_team2_lay_odds",  "polymarket_draw_lay_odds",
    "sx_bet_team1_back_odds",     "sx_bet_team2_back_odds",
    "sx_bet_team1_lay_odds",      "sx_bet_team2_lay_odds",
    "matchbook_team1_back_odds",  "matchbook_team1_lay_odds",
    "matchbook_team2_back_odds",  "matchbook_team2_lay_odds",
    "matchbook_draw_back_odds",   "matchbook_draw_lay_odds",
    "smarkets_team1_back_odds",   "smarkets_team2_back_odds",
    "azuro_team1_back_odds",      "azuro_team2_back_odds",
]

_STREAM_DDL = """
CREATE TABLE IF NOT EXISTS stream_ticks (
    id         INTEGER PRIMARY KEY,
    timestamp  TEXT    NOT NULL,
    game_team1 TEXT,
    game_team2 TEXT,
    league     TEXT,
    date_time  TEXT,
    spread     REAL,
    total_line REAL,

    -- Polymarket
    polymarket_team1_back_odds  REAL,
    polymarket_team2_back_odds  REAL,
    polymarket_draw_back_odds   REAL,
    polymarket_team1_lay_odds   REAL,
    polymarket_team2_lay_odds   REAL,
    polymarket_draw_lay_odds    REAL,

    -- SX Bet
    sx_bet_team1_back_odds      REAL,
    sx_bet_team2_back_odds      REAL,
    sx_bet_team1_lay_odds       REAL,
    sx_bet_team2_lay_odds       REAL,

    -- Matchbook
    matchbook_team1_back_odds   REAL,
    matchbook_team1_lay_odds    REAL,
    matchbook_team2_back_odds   REAL,
    matchbook_team2_lay_odds    REAL,
    matchbook_draw_back_odds    REAL,
    matchbook_draw_lay_odds     REAL,

    -- Smarkets
    smarkets_team1_back_odds    REAL,
    smarkets_team2_back_odds    REAL,

    -- Azuro
    azuro_team1_back_odds       REAL,
    azuro_team2_back_odds       REAL,

    -- Arb detection result for this tick
    arb_type   TEXT,
    profit_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_st_timestamp
    ON stream_ticks(timestamp);
CREATE INDEX IF NOT EXISTS idx_st_game
    ON stream_ticks(league, game_team1, game_team2);
CREATE INDEX IF NOT EXISTS idx_st_arb
    ON stream_ticks(arb_type, profit_pct)
    WHERE arb_type IS NOT NULL;
"""

# One cached connection per DB path (stream.py is the sole writer, single thread).
_stream_db_cache: dict[str, sqlite3.Connection] = {}

# Precomputed INSERT statement (same for every call).
_STREAM_INSERT_COLS = (
    ["timestamp", "game_team1", "game_team2", "league", "date_time", "spread", "total_line"]
    + _STREAM_ODDS_COLS
    + ["arb_type", "profit_pct"]
)
_STREAM_INSERT_SQL = (
    f"INSERT INTO stream_ticks ({', '.join(_STREAM_INSERT_COLS)}) "
    f"VALUES ({', '.join('?' * len(_STREAM_INSERT_COLS))})"
)


def _get_stream_db(project_root: Path) -> sqlite3.Connection:
    db_path = _outputs_dir(project_root) / "stream.db"
    key = str(db_path)
    if key not in _stream_db_cache:
        conn = sqlite3.connect(key, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_STREAM_DDL)
        _stream_db_cache[key] = conn
    return _stream_db_cache[key]


def log_stream_tick(
    game: dict,
    arb_type: str | None,
    profit_pct: float | None,
    project_root: Path,
) -> None:
    """Insert one game-state snapshot into outputs/stream.db (stream_ticks table).

    Called for every game the stream arb detector evaluates, regardless of
    whether an arb was found.  arb_type / profit_pct are NULL when no arb
    was detected so that near-miss ticks are still captured for analysis.

    Useful queries:
      -- odds time series for one game
      SELECT timestamp, polymarket_team1_back_odds, sx_bet_team1_back_odds
      FROM stream_ticks
      WHERE game_team1 = 'Seattle Mariners' AND game_team2 = 'New York Mets'
      ORDER BY timestamp;

      -- all arb detections
      SELECT * FROM stream_ticks WHERE arb_type IS NOT NULL ORDER BY timestamp;

      -- profit distribution by league
      SELECT league, COUNT(*), AVG(profit_pct), MAX(profit_pct)
      FROM stream_ticks WHERE arb_type IS NOT NULL GROUP BY league;
    """
    # Totals leagues store odds under "over"/"under" keys rather than
    # "team1"/"team2". Remap so the fixed DB columns are always populated.
    is_totals = game.get("league") in TOTALS_LEAGUES
    def _get_odds(col: str):
        val = game.get(col)
        if val is None and is_totals:
            col = col.replace("_team1_", "_over_").replace("_team2_", "_under_")
            val = game.get(col)
        return val

    row = (
        [_now_iso(), game.get("team1"), game.get("team2"), game.get("league"),
         game.get("date_time"), game.get("spread"), game.get("total_line")]
        + [_get_odds(col) for col in _STREAM_ODDS_COLS]
        + [arb_type, profit_pct]
    )
    conn = _get_stream_db(project_root)
    conn.execute(_STREAM_INSERT_SQL, row)
    conn.commit()


def log_warning(source: str, message: str, project_root: Path) -> None:
    """Append one warning line to outputs/warnings.log."""
    path = _outputs_dir(project_root) / "warnings.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{_now_iso()}  [{source}]  {message}\n")


def log_crash(source: str, exc: BaseException, project_root: Path, context: str = "") -> None:
    """Append one crash block to outputs/crashes.log."""
    path = _outputs_dir(project_root) / "crashes.log"
    tb = traceback.format_exc()
    ctx = f"  {context}" if context else ""
    with path.open("a", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"{_now_iso()}  {source}{ctx}\n")
        f.write(f"{type(exc).__name__}: {exc}\n")
        f.write(tb + "\n")
