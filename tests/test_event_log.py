"""Tests for event_log.py — specifically the SQLite stream tick writer."""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from matched_betting import event_log


class StreamTickDBTests(unittest.TestCase):
    def setUp(self):
        # Each test gets a fresh temp directory and a fresh DB connection.
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        (self.root / "outputs").mkdir()
        # Clear the module-level connection cache so each test creates its own DB.
        event_log._stream_db_cache.clear()

    def tearDown(self):
        # Explicitly close connections before cleanup — Windows locks open SQLite files.
        for conn in event_log._stream_db_cache.values():
            conn.close()
        event_log._stream_db_cache.clear()
        self._tmpdir.cleanup()

    def _read_rows(self) -> list[sqlite3.Row]:
        db_path = self.root / "outputs" / "stream.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM stream_ticks ORDER BY id").fetchall()
        conn.close()
        return rows

    def test_db_and_table_created_on_first_write(self):
        game = {"team1": "Seattle Mariners", "team2": "New York Mets",
                "league": "mlb", "date_time": "2026-06-03T01:40:00Z"}
        event_log.log_stream_tick(game, None, None, self.root)
        self.assertTrue((self.root / "outputs" / "stream.db").exists())

    def test_tick_with_arb_stored_correctly(self):
        game = {
            "team1": "Seattle Mariners", "team2": "New York Mets",
            "league": "mlb", "date_time": "2026-06-03T01:40:00Z",
            "spread": None, "total_line": None,
            "polymarket_team1_back_odds": 1.754386,
            "sx_bet_team2_back_odds": 2.346041,
        }
        event_log.log_stream_tick(game, "sure_bet", 0.1946, self.root)

        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]

        self.assertEqual(row["game_team1"], "Seattle Mariners")
        self.assertEqual(row["game_team2"], "New York Mets")
        self.assertEqual(row["league"], "mlb")
        self.assertEqual(row["arb_type"], "sure_bet")
        self.assertAlmostEqual(row["profit_pct"], 0.1946, places=4)
        self.assertAlmostEqual(row["polymarket_team1_back_odds"], 1.754386, places=4)
        self.assertAlmostEqual(row["sx_bet_team2_back_odds"], 2.346041, places=4)
        self.assertIsNone(row["matchbook_team1_back_odds"])  # not in game dict → NULL

    def test_tick_without_arb_has_null_arb_fields(self):
        game = {"team1": "A", "team2": "B", "league": "nba"}
        event_log.log_stream_tick(game, None, None, self.root)

        rows = self._read_rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["arb_type"])
        self.assertIsNone(rows[0]["profit_pct"])

    def test_multiple_ticks_accumulate(self):
        game = {"team1": "A", "team2": "B", "league": "nba"}
        for i in range(5):
            event_log.log_stream_tick(game, None, None, self.root)
        self.assertEqual(len(self._read_rows()), 5)

    def test_timestamp_is_written(self):
        game = {"team1": "A", "team2": "B", "league": "nba"}
        event_log.log_stream_tick(game, None, None, self.root)
        row = self._read_rows()[0]
        self.assertIsNotNone(row["timestamp"])
        self.assertIn("Z", row["timestamp"])  # ISO UTC format

    def test_indexes_exist(self):
        game = {"team1": "A", "team2": "B", "league": "nba"}
        event_log.log_stream_tick(game, None, None, self.root)
        db_path = self.root / "outputs" / "stream.db"
        conn = sqlite3.connect(str(db_path))
        indexes = {
            row[1] for row in conn.execute(
                "SELECT * FROM sqlite_master WHERE type='index' AND tbl_name='stream_ticks'"
            ).fetchall()
        }
        conn.close()
        self.assertIn("idx_st_timestamp", indexes)
        self.assertIn("idx_st_game", indexes)
        self.assertIn("idx_st_arb", indexes)

    def test_spread_and_total_line_stored(self):
        game = {"team1": "A", "team2": "B", "league": "mlb_spread",
                "spread": 1.5, "total_line": None}
        event_log.log_stream_tick(game, None, None, self.root)
        row = self._read_rows()[0]
        self.assertAlmostEqual(row["spread"], 1.5)
        self.assertIsNone(row["total_line"])


if __name__ == "__main__":
    unittest.main()
