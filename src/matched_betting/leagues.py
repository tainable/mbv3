"""
leagues.py
----------
Shared league-class constants.

Every league-specific branch (slot naming, market resolvers, arb-type
exclusions) must use these sets instead of inline tuples. Three separate
phantom-arb / wrong-market bugs (2026-06-10: WC Tie-market slot mapping,
wc_spread Matchbook moneyline fallback, wc_totals SX slot mismatch) came
from hardcoded copies of these lists missing a newly added league.

When adding a league variant, update it here once.
"""
from __future__ import annotations

# Handicap / run-line leagues: two-way markets keyed by a signed line.
SPREAD_LEAGUES: frozenset[str] = frozenset({"mlb_spread", "mls_spread", "wc_spread"})

# Over/Under leagues: two-way markets using "over"/"under" slots instead of teams.
TOTALS_LEAGUES: frozenset[str] = frozenset({"mlb_totals", "mls_totals", "wc_totals"})
