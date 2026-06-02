"""
specials_active.py
------------------
Window-checking logic for specials events.

`is_active(event_config, now)` decides whether the event should be scanned
at a given moment.  It supports three rule types, each independently checked:

  cutoff_minutes_before_resolution
      The event goes inactive this many minutes before its `resolves_by`
      timestamp.  Protects against betting into a result that is already
      known but not yet settled on the exchanges.

  blackout_windows
      A list of {start, end} ISO-8601 UTC intervals during which the event
      is paused.  For elections: the overnight count window.  For tournament
      outrights: specific match slots when a player is on court.

  dynamic_blackout_callable
      A dotted Python path (e.g. "my_checks.is_sinner_playing") to a zero-
      argument callable that returns {"blocked": bool, "reason": str}.  Used
      when the blackout condition cannot be expressed as a static schedule
      (e.g. "is the player currently in an active match?").  Fail-closed:
      any import error or exception is treated as blocked.

Rules are evaluated in order; the first blocking rule wins.  If the event
config has no "active" key, the event is considered always active.

Example registry entry (add under the event's top-level keys):

    "active": {
        "cutoff_minutes_before_resolution": 60,
        "blackout_windows": [
            {"start": "2026-07-14T21:00:00Z", "end": "2026-07-15T10:00:00Z"},
        ],
        # For Sinner outright: uncomment the line below and implement the callable.
        # "dynamic_blackout_callable": "sinner_checks.is_on_court",
    },
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone, timedelta
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(s: str | None, label: str) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp.  Returns None on any failure."""
    if not s:
        return None
    try:
        # Accept trailing Z or +00:00
        clean = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _inactive(blocked_by: str, reason: str) -> dict[str, Any]:
    return {"active": False, "reason": reason, "blocked_by": blocked_by}


def _active(reason: str = "all checks passed") -> dict[str, Any]:
    return {"active": True, "reason": reason, "blocked_by": None}


def is_active(event_config: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Return {active: bool, reason: str, blocked_by: str | None}.

    Parameters
    ----------
    event_config : the full event dict from specials_registry
    now          : UTC datetime to evaluate against; defaults to datetime.now(utc)
    """
    if now is None:
        now = _utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    active_cfg: dict[str, Any] | None = event_config.get("active")
    if not active_cfg:
        return _active("no active rules configured")

    # ------------------------------------------------------------------
    # Rule 1: cutoff before resolution
    # ------------------------------------------------------------------
    cutoff_minutes = active_cfg.get("cutoff_minutes_before_resolution")
    if cutoff_minutes is not None:
        resolves_by_raw = event_config.get("resolves_by")
        resolves_by = _parse_dt(resolves_by_raw, "resolves_by")
        if resolves_by is None:
            return _inactive(
                "cutoff_minutes_before_resolution",
                f"resolves_by is missing or unparseable ({resolves_by_raw!r}) — defaulting inactive",
            )
        cutoff_at = resolves_by - timedelta(minutes=float(cutoff_minutes))
        if now >= cutoff_at:
            return _inactive(
                "cutoff_minutes_before_resolution",
                f"within {cutoff_minutes}m of resolution "
                f"(cutoff_at={cutoff_at.isoformat()}, now={now.isoformat()})",
            )

    # ------------------------------------------------------------------
    # Rule 2: static blackout windows
    # ------------------------------------------------------------------
    for window in active_cfg.get("blackout_windows") or []:
        w_start = _parse_dt(window.get("start"), "window.start")
        w_end   = _parse_dt(window.get("end"),   "window.end")
        if w_start is None or w_end is None:
            # Malformed window — fail-closed
            return _inactive(
                "blackout_windows",
                f"malformed blackout window {window!r} — defaulting inactive",
            )
        if w_start <= now < w_end:
            return _inactive(
                "blackout_windows",
                f"inside blackout window {w_start.isoformat()} – {w_end.isoformat()}",
            )

    # ------------------------------------------------------------------
    # Rule 3: dynamic callable
    # ------------------------------------------------------------------
    callable_path = active_cfg.get("dynamic_blackout_callable")
    if callable_path:
        try:
            module_path, _, func_name = str(callable_path).rpartition(".")
            if not module_path or not func_name:
                raise ImportError(f"invalid dotted path {callable_path!r}")
            module = importlib.import_module(module_path)
            func = getattr(module, func_name)
            result = func()
            if result.get("blocked"):
                return _inactive(
                    "dynamic_blackout_callable",
                    result.get("reason") or f"{callable_path} returned blocked=True",
                )
        except Exception as exc:
            return _inactive(
                "dynamic_blackout_callable",
                f"{callable_path} raised {type(exc).__name__}: {exc} — defaulting inactive",
            )

    return _active()
