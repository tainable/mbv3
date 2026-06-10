"""Tests for the core arb maths in calculator.py."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from matched_betting import calculator


def _zero_commission():
    """Configure all commissions to zero for predictable test arithmetic."""
    cfg = SimpleNamespace(
        matchbook=0.0, smarkets=0.0, sx_bet=0.0,
        smarkets_zero_commission_period=True,
    )
    calculator.configure(cfg)


def _standard_commission():
    """Configure realistic commissions: MB 2%, others zero."""
    cfg = SimpleNamespace(
        matchbook=0.02, smarkets=0.02, sx_bet=0.0,
        smarkets_zero_commission_period=False,
    )
    calculator.configure(cfg)


def _game(team1_back=None, team2_back=None, team1_lay=None, team2_lay=None,
          league="nba", market_type="two_way"):
    """Build a minimal game dict for calculator tests."""
    g: dict = {
        "team1": "Team A", "team2": "Team B",
        "league": league, "market_type": market_type,
        "date_time": "2099-01-01T00:00:00Z",  # far future avoids profit_24h clamping
    }
    if team1_back is not None:
        provider, odds = team1_back
        g[f"{provider}_team1_back_odds"] = odds
    if team2_back is not None:
        provider, odds = team2_back
        g[f"{provider}_team2_back_odds"] = odds
    if team1_lay is not None:
        provider, odds = team1_lay
        g[f"{provider}_team1_lay_odds"] = odds
    if team2_lay is not None:
        provider, odds = team2_lay
        g[f"{provider}_team2_lay_odds"] = odds
    return g


class EffectiveOddsTests(unittest.TestCase):
    def setUp(self):
        _zero_commission()

    def test_zero_commission_back_is_identity(self):
        self.assertAlmostEqual(calculator._eff_back_odds(2.0, "sx_bet"), 2.0)
        self.assertAlmostEqual(calculator._eff_back_odds(2.0, "azuro"), 2.0)

    def test_matchbook_back_commission_reduces_odds(self):
        _standard_commission()
        # 2% commission on net winnings: eff = 1 + (odds-1) * 0.98
        self.assertAlmostEqual(calculator._eff_back_odds(3.0, "matchbook"), 2.96, places=6)
        self.assertAlmostEqual(calculator._eff_back_odds(2.0, "matchbook"), 1.98, places=6)

    def test_matchbook_lay_commission_raises_cost(self):
        _standard_commission()
        # eff_lay = 1 + (odds-1) / 0.98 — commission makes laying more expensive
        eff = calculator._eff_lay_odds(2.0, "matchbook")
        self.assertGreater(eff, 2.0)
        self.assertAlmostEqual(eff, 1.0 + 1.0 / 0.98, places=6)

    def test_polymarket_back_fee_peaks_at_evens(self):
        # Peak fee = 0.0075 when p = 0.5 (odds = 2.0)
        eff = calculator._eff_back_odds(2.0, "polymarket")
        self.assertAlmostEqual(eff, 1.9925, places=4)

    def test_polymarket_fee_falls_for_longshots(self):
        # At odds 10 (p=0.1): fee = 0.0075*4*0.1*0.9 = 0.0027
        # eff = 1 + (10-1) * (1-0.0027) = 1 + 9*0.9973 = 9.9757
        eff = calculator._eff_back_odds(10.0, "polymarket")
        self.assertAlmostEqual(eff, 9.9757, places=3)
        # Fee rate is lower than at evens — longshots attract less fee
        eff_evens = calculator._eff_back_odds(2.0, "polymarket")
        self.assertGreater(eff / 10.0, eff_evens / 2.0)


class FindSureBetsTests(unittest.TestCase):
    def setUp(self):
        _zero_commission()

    def test_cross_provider_arb_detected(self):
        # 1/2.1 + 1/2.1 = 0.952 < 1 → sure bet across PM and SX
        g = _game(team1_back=("polymarket", 2.1), team2_back=("sx_bet", 2.1))
        result = calculator.find_sure_bets([g])
        self.assertEqual(len(result), 1)
        self.assertGreater(result[0]["profit_pct"], 0)

    def test_negative_margin_no_arb(self):
        # 1/1.9 + 1/1.9 = 1.05 > 1 → no sure bet
        g = _game(team1_back=("polymarket", 1.9), team2_back=("sx_bet", 1.9))
        result = calculator.find_sure_bets([g])
        self.assertEqual(len(result), 0)

    def test_same_provider_rejected(self):
        # Both legs from Polymarket — not a cross-provider arb
        g = _game(team1_back=("polymarket", 2.5), team2_back=("polymarket", 2.5))
        result = calculator.find_sure_bets([g])
        self.assertEqual(len(result), 0)

    def test_kbo_excluded(self):
        g = _game(team1_back=("polymarket", 2.1), team2_back=("sx_bet", 2.1), league="kbo")
        result = calculator.find_sure_bets([g])
        self.assertEqual(len(result), 0)

    def test_commission_eliminates_marginal_arb(self):
        # Raw margin is slightly profitable but commission kills it
        _standard_commission()
        # MB back at 2.02 + PM back at 2.02 — marginal without fees
        # With 2% MB commission: eff_back_mb = 1 + 1.02*0.98 = 1.9996
        # With PM fee at p≈0.495: fee≈0.0075, eff_back_pm ≈ 2.0047
        # net_margin ≈ 1/1.9996 + 1/2.0047 ≈ 0.5001 + 0.4988 = 0.9989 < 1 → still arb
        # Use tighter odds to eliminate it:
        g = _game(team1_back=("matchbook", 1.99), team2_back=("sx_bet", 1.99))
        # raw: 1/1.99 + 1/1.99 = 1.005 > 1 → no arb even before commission
        result = calculator.find_sure_bets([g])
        self.assertEqual(len(result), 0)

    def test_min_profit_filter(self):
        # Arb exists but below threshold
        g = _game(team1_back=("polymarket", 2.05), team2_back=("sx_bet", 2.05))
        all_arbs   = calculator.find_sure_bets([g], min_profit_pct=0.0)
        high_arbs  = calculator.find_sure_bets([g], min_profit_pct=50.0)
        self.assertGreater(len(all_arbs), 0)
        self.assertEqual(len(high_arbs), 0)

    def test_result_sorted_by_profit(self):
        g1 = _game(team1_back=("polymarket", 2.1), team2_back=("sx_bet", 2.1))
        g2 = _game(team1_back=("polymarket", 3.0), team2_back=("sx_bet", 3.0))
        result = calculator.find_sure_bets([g1, g2])
        self.assertEqual(len(result), 2)
        self.assertGreaterEqual(result[0]["profit_pct"], result[1]["profit_pct"])


class FindBackLayArbsTests(unittest.TestCase):
    def setUp(self):
        _zero_commission()

    def test_arb_detected_when_back_exceeds_lay(self):
        # Back at 2.5 (SX, zero commission), Lay at 2.3 (SX, zero commission)
        # eff_back 2.5 > eff_lay 2.3 → arb
        g = _game(team1_back=("sx_bet", 2.5), team1_lay=("sx_bet", 2.3))
        # Same provider for back and lay on sx_bet — but only one provider each for back/lay
        # Actually sx_bet back and sx_bet lay are separate fields; different side, same provider → rejected
        # Need different providers:
        g = {
            "team1": "A", "team2": "B", "league": "nba", "market_type": "two_way",
            "date_time": "2099-01-01T00:00:00Z",
            "polymarket_team1_back_odds": 2.5,
            "matchbook_team1_lay_odds": 2.3,
        }
        result = calculator.find_back_lay_arbs([g])
        self.assertEqual(len(result), 1)
        self.assertGreater(result[0]["profit_pct"], 0)

    def test_no_arb_when_lay_exceeds_back(self):
        g = {
            "team1": "A", "team2": "B", "league": "nba", "market_type": "two_way",
            "date_time": "2099-01-01T00:00:00Z",
            "polymarket_team1_back_odds": 2.0,
            "matchbook_team1_lay_odds": 2.5,  # lay > back → no arb
        }
        result = calculator.find_back_lay_arbs([g])
        self.assertEqual(len(result), 0)

    def test_commission_eliminates_marginal_back_lay(self):
        # Without commission: SX back 2.02 > MB lay 2.01 looks like an arb
        # With MB 2% commission: eff_lay = 1 + 1.01/0.98 = 2.0306 > eff_back 2.02 → no arb
        _standard_commission()
        g = {
            "team1": "A", "team2": "B", "league": "nba", "market_type": "two_way",
            "date_time": "2099-01-01T00:00:00Z",
            "sx_bet_team1_back_odds": 2.02,
            "matchbook_team1_lay_odds": 2.01,
        }
        result = calculator.find_back_lay_arbs([g])
        self.assertEqual(len(result), 0)

    def test_mlb_spread_excluded(self):
        g = {
            "team1": "A", "team2": "B", "league": "mlb_spread", "market_type": "two_way",
            "date_time": "2099-01-01T00:00:00Z",
            "polymarket_team1_back_odds": 2.5,
            "matchbook_team1_lay_odds": 2.3,
        }
        result = calculator.find_back_lay_arbs([g])
        self.assertEqual(len(result), 0)

    def test_kbo_excluded(self):
        g = {
            "team1": "A", "team2": "B", "league": "kbo", "market_type": "two_way",
            "date_time": "2099-01-01T00:00:00Z",
            "polymarket_team1_back_odds": 2.5,
            "matchbook_team1_lay_odds": 2.3,
        }
        result = calculator.find_back_lay_arbs([g])
        self.assertEqual(len(result), 0)

    def test_no_arb_when_only_one_provider(self):
        # Only SX back, no lay providers → no arb
        g = _game(team1_back=("sx_bet", 2.5))
        result = calculator.find_back_lay_arbs([g])
        self.assertEqual(len(result), 0)


if __name__ == "__main__":
    unittest.main()
