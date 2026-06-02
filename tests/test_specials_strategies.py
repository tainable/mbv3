"""
Unit tests for the three specials strategy evaluators.

Exercises every status branch the evaluators can return.  Catalog and
strategy dicts are constructed inline so the tests are independent of
specials_registry.SPECIALS_EVENTS state.
"""
from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
for p in (SRC_DIR, PROJECT_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

# Configure calculator with zero commission so the maths is easy to reason about
from matched_betting.calculator import configure as _cfg_calc
from matched_betting.config import CommissionSettings

_cfg_calc(CommissionSettings(matchbook=0.0, smarkets=0.0, sx_bet=0.0,
                             smarkets_zero_commission_period=True))

from specials_strategies import (
    evaluate,
    evaluate_cross_hedge,
    evaluate_same_provider_basket,
    evaluate_selective_basket,
)


# A tiny catalog used by basket tests.  Three Polymarket outcomes.
_CATALOG = {
    "key": "test_event",
    "providers": {
        "polymarket": {
            "market_id": "pm-1",
            "outcomes": {
                "burnham":   {"clob_token_id": "tok-b", "candidate": "Andy Burnham",   "party": "labour"},
                "smith":     {"clob_token_id": "tok-s", "candidate": "Joe Smith",      "party": "reform"},
                "loony":     {"clob_token_id": "tok-l", "candidate": "X Y Z",          "party": "monster_raving_loony"},
            },
        },
        "matchbook": {
            "event_id":  111,
            "market_id": 222,
            "outcomes": {
                "labour": {"runner_id": 1, "party": "labour"},
                "reform": {"runner_id": 2, "party": "reform"},
                "other":  {"runner_id": 3, "party": "other"},
            },
        },
    },
}


# Helper to build strategy dicts
def _strategy(stype, **kwargs):
    base = {"name": "test", "type": stype, "enabled": True, "alert_only": True}
    base.update(kwargs)
    return base


# ===========================================================================
# Dispatcher
# ===========================================================================

class DispatcherTests(unittest.TestCase):
    def test_disabled_strategy_returns_disabled(self):
        s = _strategy("cross_hedge", enabled=False,
                      back={"provider": "polymarket", "outcome": "burnham", "side": "yes"},
                      lay={"provider": "matchbook", "outcome": "labour", "side": "lay"})
        r = evaluate(s, {}, _CATALOG, None)
        self.assertEqual(r["status"], "disabled")
        self.assertFalse(r["enabled"])

    def test_unknown_type(self):
        s = _strategy("flux_capacitor")
        r = evaluate(s, {}, _CATALOG, None)
        self.assertEqual(r["status"], "unknown_strategy_type")


# ===========================================================================
# cross_hedge
# ===========================================================================

class CrossHedgeTests(unittest.TestCase):
    def _make(self, **overrides):
        s = _strategy(
            "cross_hedge",
            back={"provider": "polymarket", "outcome": "burnham", "side": "yes"},
            lay={"provider": "matchbook", "outcome": "labour", "side": "lay"},
            min_edge_pct=0.5,
            risk_class="candidate_replacement",
            gap_outcomes=["x"],
        )
        s.update(overrides)
        return s

    def test_no_back_price(self):
        prices = {("matchbook", "labour", "lay"): 2.00}
        r = evaluate_cross_hedge(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "no_price")
        self.assertEqual(r["missing_side"], "back")

    def test_no_lay_price(self):
        prices = {("polymarket", "burnham", "yes"): 2.00}
        r = evaluate_cross_hedge(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "no_price")
        self.assertEqual(r["missing_side"], "lay")

    def test_below_threshold(self):
        # back=2.00 PM (fee at p=0.5 = 0.75% → eff_back ≈ 1.9925)
        # lay=2.00 MB (zero commission) → eff_lay = 2.00
        # edge_pct = (1.9925/2.00 − 1) × 100 ≈ −0.375%, below 0.5% threshold
        prices = {("polymarket", "burnham", "yes"): 2.00,
                  ("matchbook",  "labour",  "lay"): 2.00}
        r = evaluate_cross_hedge(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "below_threshold")
        self.assertLess(r["edge_pct"], 0.5)  # below configured threshold

    def test_fires(self):
        # back=2.10 PM (p=0.476, fee ≈ 0.748% → eff_back ≈ 2.0918)
        # lay=2.00 MB → edge_pct ≈ 4.59% (well above 0.5% threshold)
        prices = {("polymarket", "burnham", "yes"): 2.10,
                  ("matchbook",  "labour",  "lay"): 2.00}
        r = evaluate_cross_hedge(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "fires")
        self.assertAlmostEqual(r["edge_pct"], 4.5884, places=3)
        # Direction info propagated
        self.assertEqual(r["back"]["provider"], "polymarket")
        self.assertEqual(r["lay"]["provider"], "matchbook")
        self.assertEqual(r["gap_outcomes"], ["x"])

    def test_degenerate_odds(self):
        prices = {("polymarket", "burnham", "yes"): 0.95,
                  ("matchbook",  "labour",  "lay"): 2.00}
        r = evaluate_cross_hedge(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "degenerate_odds")


# ===========================================================================
# same_provider_basket
# ===========================================================================

class SameProviderBasketTests(unittest.TestCase):
    def _make(self, **overrides):
        s = _strategy(
            "same_provider_basket",
            provider="polymarket",
            side="yes",
            include_outcomes="*",
            min_edge_pct=1.0,
        )
        s.update(overrides)
        return s

    def test_incomplete_catalog_refuses_to_fire(self):
        # Missing 'loony' price — catalog is incomplete for a '*' basket
        prices = {("polymarket", "burnham", "yes"): 3.00,
                  ("polymarket", "smith",   "yes"): 4.00}
        r = evaluate_same_provider_basket(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "incomplete_catalog")
        self.assertIn("loony", r["missing"])

    def test_basket_fires_when_sum_implied_below_1(self):
        # PM fees apply per outcome: at 3.00 fee ≈ 0.667%, at 4.00 fee ≈ 0.563%,
        # at 8.00 fee ≈ 0.328%.  Σ(1/eff) ≈ 0.7112 → edge_pct ≈ 40.6%
        prices = {("polymarket", "burnham", "yes"): 3.00,
                  ("polymarket", "smith",   "yes"): 4.00,
                  ("polymarket", "loony",   "yes"): 8.00}
        r = evaluate_same_provider_basket(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "fires")
        self.assertAlmostEqual(r["sum_implied"], 0.7112, places=3)
        self.assertGreater(r["edge_pct"], 1.0)

    def test_below_threshold_when_sum_implied_above_1(self):
        prices = {("polymarket", "burnham", "yes"): 2.00,
                  ("polymarket", "smith",   "yes"): 3.00,
                  ("polymarket", "loony",   "yes"): 5.00}
        # Σ = 0.5 + 0.333 + 0.2 = 1.0333 → no arb
        r = evaluate_same_provider_basket(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "below_threshold")
        self.assertLess(r["edge_pct"], 0)

    def test_subset_basket_fires(self):
        s = self._make(include_outcomes=["burnham", "smith"])
        prices = {("polymarket", "burnham", "yes"): 3.00,
                  ("polymarket", "smith",   "yes"): 4.00}
        r = evaluate_same_provider_basket(s, prices, _CATALOG, None)
        # Σ = 1/3 + 1/4 = 0.583 → edge_pct ≈ 71% conditional on basket winning
        self.assertEqual(r["status"], "fires")

    def test_empty_catalog(self):
        empty_cat = {"providers": {"polymarket": {"outcomes": {}}}}
        r = evaluate_same_provider_basket(self._make(), {}, empty_cat, None)
        self.assertEqual(r["status"], "empty_catalog")


# ===========================================================================
# selective_basket
# ===========================================================================

class SelectiveBasketTests(unittest.TestCase):
    def _make(self, **overrides):
        s = _strategy(
            "selective_basket",
            provider="polymarket",
            side="yes",
            include_outcomes=["burnham", "smith"],     # loony excluded
            min_edge_pct=2.0,
            max_excluded_prob=0.05,
        )
        s.update(overrides)
        return s

    def test_excluded_prob_exceeded_auto_suspends(self):
        # loony at 5.00 PM (fee 0.48% → eff ≈ 4.9808 → implied ≈ 0.2008)
        # well above 0.05 threshold
        prices = {("polymarket", "burnham", "yes"): 3.00,
                  ("polymarket", "smith",   "yes"): 4.00,
                  ("polymarket", "loony",   "yes"): 5.00}
        r = evaluate_selective_basket(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "excluded_prob_exceeded")
        self.assertAlmostEqual(r["excluded_prob"], 0.2008, places=3)
        self.assertEqual(r["threshold"], 0.05)

    def test_fires_when_tail_thin_and_edge_high(self):
        # loony at 50.00 → implied 0.02, under 0.05 threshold
        # Included: burnham 3.00 + smith 4.00 → Σincluded = 0.5833
        # edge_pct = ((1 - 0.02) / 0.5833 - 1) * 100 = 68.0%
        prices = {("polymarket", "burnham", "yes"): 3.00,
                  ("polymarket", "smith",   "yes"): 4.00,
                  ("polymarket", "loony",   "yes"): 50.00}
        r = evaluate_selective_basket(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "fires")
        self.assertAlmostEqual(r["excluded_prob"], 0.02, places=3)
        self.assertGreater(r["edge_pct"], 2.0)
        self.assertEqual(r["excluded"][0]["outcome"], "loony")

    def test_missing_excluded_price_refuses_to_fire(self):
        # No 'loony' price -> cannot verify excluded threshold
        prices = {("polymarket", "burnham", "yes"): 3.00,
                  ("polymarket", "smith",   "yes"): 4.00}
        r = evaluate_selective_basket(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "no_price_excluded")

    def test_missing_included_price(self):
        # Excluded priced but an included outcome missing
        prices = {("polymarket", "smith",   "yes"): 4.00,
                  ("polymarket", "loony",   "yes"): 50.00}
        r = evaluate_selective_basket(self._make(), prices, _CATALOG, None)
        self.assertEqual(r["status"], "no_price_included")

    def test_empty_include_list(self):
        s = self._make(include_outcomes=[])
        r = evaluate_selective_basket(s, {}, _CATALOG, None)
        self.assertEqual(r["status"], "empty_include_list")


if __name__ == "__main__":
    unittest.main()
