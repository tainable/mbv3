from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from matched_betting.market_matching import is_game_win_loss_record
from matched_betting.models import OddsRecord


class GameFilteringTests(unittest.TestCase):
    def test_moneyline_back_is_kept(self) -> None:
        record = OddsRecord(
            provider="matchbook",
            sport="basketball",
            league="nba",
            event_name="Cleveland Cavaliers at New Orleans Pelicans",
            event_start="2026-03-21T23:00:00Z",
            market_name="Moneyline",
            market_type="two_way",
            selection_name="cleveland cavaliers",
            selection_side="back",
            decimal_odds=1.9,
            implied_probability=0.526316,
            currency="GBP",
            source_market_id="1",
            source_event_id="100",
            retrieved_at="2026-03-21T19:00:00Z",
        )
        self.assertTrue(is_game_win_loss_record(record))

    def test_total_is_not_kept(self) -> None:
        record = OddsRecord(
            provider="matchbook",
            sport="basketball",
            league="nba",
            event_name="Cleveland Cavaliers at New Orleans Pelicans",
            event_start="2026-03-21T23:00:00Z",
            market_name="Total",
            market_type="total",
            selection_name="Over 221.5",
            selection_side="back",
            decimal_odds=1.9,
            implied_probability=0.526316,
            currency="GBP",
            source_market_id="2",
            source_event_id="100",
            retrieved_at="2026-03-21T19:00:00Z",
        )
        self.assertFalse(is_game_win_loss_record(record))


if __name__ == "__main__":
    unittest.main()
