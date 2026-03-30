from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from matched_betting.event_matching import match_records_to_canonical_events, normalize_team_name
from matched_betting.models import OddsRecord


class EventMatchingTests(unittest.TestCase):
    def test_team_alias_normalization(self) -> None:
        self.assertEqual(normalize_team_name("Cavs", "nba"), "cleveland cavaliers")
        self.assertEqual(normalize_team_name("D-Backs", "mlb"), "arizona diamondbacks")

    def test_matching_groups_like_for_like_events(self) -> None:
        records = [
            OddsRecord(
                provider="matchbook",
                sport="basketball",
                league="nba",
                event_name="Cleveland Cavaliers at New Orleans Pelicans",
                event_start="2026-03-21T23:00:00Z",
                market_name="Moneyline",
                market_type="money_line",
                selection_name="Cleveland Cavaliers",
                selection_side="back",
                decimal_odds=1.9,
                implied_probability=0.526316,
                currency="GBP",
                source_market_id="1",
                source_event_id="100",
                retrieved_at="2026-03-21T19:00:00Z",
            ),
            OddsRecord(
                provider="smarkets",
                sport="basketball",
                league="nba",
                event_name="Cavs at Pelicans",
                event_start="2026-03-21T23:05:00Z",
                market_name="Winner",
                market_type="WINNER_2_WAY",
                selection_name="Cavs",
                selection_side="back",
                decimal_odds=1.88,
                implied_probability=0.531915,
                currency="GBP",
                source_market_id="2",
                source_event_id="200",
                retrieved_at="2026-03-21T19:00:00Z",
            ),
        ]

        assignment, groups = match_records_to_canonical_events(records)

        self.assertEqual(len(groups), 1)
        self.assertEqual(assignment[0], assignment[1])
        self.assertEqual(groups[0].home_team, "new orleans pelicans")
        self.assertEqual(groups[0].away_team, "cleveland cavaliers")


if __name__ == "__main__":
    unittest.main()
