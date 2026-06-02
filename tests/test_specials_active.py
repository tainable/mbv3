"""
Unit tests for specials_active.is_active().

Exercises every rule type and every edge case:
  - no active config (always active)
  - cutoff: before / after
  - cutoff: missing or bad resolves_by (fail-closed)
  - blackout: now outside / inside window
  - blackout: malformed window (fail-closed)
  - dynamic callable: not blocked / blocked / raises / bad path
  - now=None uses real clock (smoke test only)
"""
from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for p in (PROJECT_ROOT / "src", PROJECT_ROOT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from specials_active import is_active

# Fixed reference time used in every test that needs a deterministic "now"
_NOW = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)


def _ev(**kwargs):
    """Build a minimal event dict.  Defaults: resolves_by 24 h from _NOW."""
    base = {
        "key":         "test_event",
        "resolves_by": "2026-06-16T12:00:00Z",   # 24 h from _NOW
    }
    base.update(kwargs)
    return base


class TestNoActiveConfig(unittest.TestCase):
    def test_no_active_key_is_always_active(self):
        r = is_active(_ev(), now=_NOW)
        self.assertTrue(r["active"])
        self.assertIsNone(r["blocked_by"])

    def test_empty_active_dict_is_active(self):
        r = is_active(_ev(active={}), now=_NOW)
        self.assertTrue(r["active"])

    def test_now_none_uses_real_clock(self):
        # Smoke test only — just check the shape, not the value.
        r = is_active(_ev())
        self.assertIn("active", r)
        self.assertIn("reason", r)
        self.assertIn("blocked_by", r)


class TestCutoffRule(unittest.TestCase):
    def _ev_cutoff(self, minutes: int, resolves_by: str | None = None):
        base = _ev()
        if resolves_by is not None:
            base["resolves_by"] = resolves_by
        base["active"] = {"cutoff_minutes_before_resolution": minutes}
        return base

    def test_before_cutoff_is_active(self):
        # cutoff = 60 min → cutoff_at = 2026-06-16T11:00Z, now = 12:00 prev day
        r = is_active(self._ev_cutoff(60), now=_NOW)
        self.assertTrue(r["active"])

    def test_exactly_at_cutoff_is_inactive(self):
        # resolves_by 12:00 + cutoff 60 min → cutoff_at 11:00
        # now = 11:00 → inactive (now >= cutoff_at)
        now = datetime(2026, 6, 16, 11, 0, 0, tzinfo=timezone.utc)
        r = is_active(self._ev_cutoff(60), now=now)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "cutoff_minutes_before_resolution")

    def test_after_cutoff_is_inactive(self):
        # now = 11:30 (30 min inside cutoff window)
        now = datetime(2026, 6, 16, 11, 30, 0, tzinfo=timezone.utc)
        r = is_active(self._ev_cutoff(60), now=now)
        self.assertFalse(r["active"])

    def test_missing_resolves_by_fails_closed(self):
        ev = {"key": "test", "active": {"cutoff_minutes_before_resolution": 60}}
        r = is_active(ev, now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "cutoff_minutes_before_resolution")

    def test_bad_resolves_by_fails_closed(self):
        r = is_active(self._ev_cutoff(60, resolves_by="not-a-date"), now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "cutoff_minutes_before_resolution")

    def test_zero_cutoff_inactive_at_resolution(self):
        now = datetime(2026, 6, 16, 12, 0, 0, tzinfo=timezone.utc)
        r = is_active(self._ev_cutoff(0), now=now)
        self.assertFalse(r["active"])


class TestBlackoutWindowRule(unittest.TestCase):
    _WINDOW = {"start": "2026-06-15T10:00:00Z", "end": "2026-06-15T14:00:00Z"}

    def _ev_bw(self, *windows):
        return _ev(active={"blackout_windows": list(windows)})

    def test_before_window_is_active(self):
        now = datetime(2026, 6, 15, 9, 59, 59, tzinfo=timezone.utc)
        r = is_active(self._ev_bw(self._WINDOW), now=now)
        self.assertTrue(r["active"])

    def test_inside_window_is_inactive(self):
        # _NOW = 12:00, window 10:00–14:00
        r = is_active(self._ev_bw(self._WINDOW), now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "blackout_windows")
        self.assertIn("10:00", r["reason"])

    def test_at_window_start_is_inactive(self):
        now = datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
        r = is_active(self._ev_bw(self._WINDOW), now=now)
        self.assertFalse(r["active"])

    def test_at_window_end_is_active(self):
        # End is exclusive (half-open interval)
        now = datetime(2026, 6, 15, 14, 0, 0, tzinfo=timezone.utc)
        r = is_active(self._ev_bw(self._WINDOW), now=now)
        self.assertTrue(r["active"])

    def test_after_window_is_active(self):
        now = datetime(2026, 6, 15, 15, 0, 0, tzinfo=timezone.utc)
        r = is_active(self._ev_bw(self._WINDOW), now=now)
        self.assertTrue(r["active"])

    def test_multiple_windows_first_match_blocks(self):
        w2 = {"start": "2026-06-16T08:00:00Z", "end": "2026-06-16T10:00:00Z"}
        r = is_active(self._ev_bw(self._WINDOW, w2), now=_NOW)
        self.assertFalse(r["active"])

    def test_multiple_windows_second_match_blocks(self):
        w1 = {"start": "2026-06-14T00:00:00Z", "end": "2026-06-14T23:59:00Z"}
        w2 = {"start": "2026-06-15T10:00:00Z", "end": "2026-06-15T14:00:00Z"}
        r = is_active(self._ev_bw(w1, w2), now=_NOW)
        self.assertFalse(r["active"])

    def test_malformed_window_missing_start_fails_closed(self):
        r = is_active(self._ev_bw({"end": "2026-06-15T14:00:00Z"}), now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "blackout_windows")
        self.assertIn("malformed", r["reason"])

    def test_malformed_window_bad_end_fails_closed(self):
        r = is_active(self._ev_bw({"start": "2026-06-15T10:00:00Z", "end": "not-a-date"}), now=_NOW)
        self.assertFalse(r["active"])

    def test_empty_windows_list_is_active(self):
        r = is_active(_ev(active={"blackout_windows": []}), now=_NOW)
        self.assertTrue(r["active"])


class TestDynamicCallableRule(unittest.TestCase):
    """Inject synthetic modules via sys.modules so no real files are needed."""

    def _inject(self, module_name: str, func_name: str, func):
        mod = types.ModuleType(module_name)
        setattr(mod, func_name, func)
        sys.modules[module_name] = mod
        self.addCleanup(sys.modules.pop, module_name, None)

    def _ev_dyn(self, dotted_path: str):
        return _ev(active={"dynamic_blackout_callable": dotted_path})

    def test_callable_not_blocked_is_active(self):
        self._inject("mymod", "check", lambda: {"blocked": False, "reason": "all clear"})
        r = is_active(self._ev_dyn("mymod.check"), now=_NOW)
        self.assertTrue(r["active"])

    def test_callable_blocked_is_inactive(self):
        self._inject("mymod", "check", lambda: {"blocked": True, "reason": "player on court"})
        r = is_active(self._ev_dyn("mymod.check"), now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "dynamic_blackout_callable")
        self.assertIn("player on court", r["reason"])

    def test_callable_raises_fails_closed(self):
        self._inject("mymod", "check", lambda: (_ for _ in ()).throw(RuntimeError("API down")))
        r = is_active(self._ev_dyn("mymod.check"), now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "dynamic_blackout_callable")
        self.assertIn("RuntimeError", r["reason"])

    def test_callable_import_error_fails_closed(self):
        r = is_active(self._ev_dyn("nonexistent_module.check"), now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "dynamic_blackout_callable")

    def test_bad_dotted_path_no_dot_fails_closed(self):
        r = is_active(self._ev_dyn("nodot"), now=_NOW)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "dynamic_blackout_callable")

    def test_callable_blocked_true_without_reason_still_inactive(self):
        self._inject("mymod", "check", lambda: {"blocked": True})
        r = is_active(self._ev_dyn("mymod.check"), now=_NOW)
        self.assertFalse(r["active"])


class TestRuleOrdering(unittest.TestCase):
    """Cutoff fires before blackout; blackout fires before dynamic."""

    def test_cutoff_takes_priority_over_passing_blackout(self):
        # now is PAST resolution → cutoff fires; window would not fire
        now = datetime(2026, 6, 16, 13, 0, 0, tzinfo=timezone.utc)
        ev = _ev(active={
            "cutoff_minutes_before_resolution": 60,        # cutoff_at=11:00
            "blackout_windows": [
                {"start": "2026-06-17T00:00:00Z", "end": "2026-06-17T12:00:00Z"},
            ],
        })
        r = is_active(ev, now=now)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "cutoff_minutes_before_resolution")

    def test_blackout_fires_when_cutoff_passes(self):
        # now is before cutoff but inside a blackout window
        now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        ev = _ev(active={
            "cutoff_minutes_before_resolution": 60,        # cutoff_at = 2026-06-16T11:00Z — not hit
            "blackout_windows": [
                {"start": "2026-06-15T10:00:00Z", "end": "2026-06-15T14:00:00Z"},
            ],
        })
        r = is_active(ev, now=now)
        self.assertFalse(r["active"])
        self.assertEqual(r["blocked_by"], "blackout_windows")


if __name__ == "__main__":
    unittest.main()
