"""
odds_cache.py
-------------
Thread-safe in-memory store for the latest aggregated odds per game.

Data is stored in the same dict format produced by aggregation.py, so WS
updates can feed directly into calculator.py without any conversion layer.

Write path: WS clients and pollers call update_back_odds() / update_back_and_lay_odds()
Read path:  arb detector calls pop_dirty() to get games that changed since last check
"""
from __future__ import annotations

import threading
import time
from typing import Any


class OddsCache:

    def __init__(self, max_provider_staleness_s: float = 90.0) -> None:
        self._lock = threading.RLock()
        # game_id -> aggregated game dict (same schema as aggregation.py output)
        self._games: dict[str, dict[str, Any]] = {}
        # (game_id, provider) -> monotonic time of last update
        self._fetch_times: dict[tuple[str, str], float] = {}
        # game IDs that need an arb check on the next detector tick
        self._dirty: set[str] = set()
        self.max_provider_staleness_s = max_provider_staleness_s

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def seed(self, games_payload: list[dict[str, Any]]) -> None:
        """Initialise from the aggregated games list (active_game_ids.json).

        Does NOT mark games dirty — Matchbook fetches are only triggered
        by real WS price updates, not by the initial seed.
        """
        with self._lock:
            for game in games_payload:
                gid = game_id(game)
                self._games[gid] = dict(game)

    # ------------------------------------------------------------------
    # Writes (called from WS threads and poll threads)
    # ------------------------------------------------------------------

    def update_back_odds(
        self,
        gid: str,
        provider: str,
        slot: str,
        back_odds: float | None,
        avail_usd: float | None = None,
    ) -> bool:
        """Update one provider's back odds for one outcome slot.

        slot is one of: "team1", "team2", "draw", "over", "under"
        Returns False if the game_id is not in the cache (stale subscription).
        """
        with self._lock:
            g = self._games.get(gid)
            if g is None:
                return False
            g[f"{provider}_{slot}_back_odds"] = back_odds
            if avail_usd is not None:
                g[f"{provider}_{slot}_back_avail"] = avail_usd
            self._fetch_times[(gid, provider)] = time.monotonic()
            self._dirty.add(gid)
            return True

    def update_back_and_lay_odds(
        self,
        gid: str,
        provider: str,
        slot: str,
        back_odds: float | None,
        lay_odds: float | None,
        avail_usd: float | None = None,
    ) -> bool:
        with self._lock:
            g = self._games.get(gid)
            if g is None:
                return False
            g[f"{provider}_{slot}_back_odds"] = back_odds
            g[f"{provider}_{slot}_lay_odds"] = lay_odds
            if avail_usd is not None:
                g[f"{provider}_{slot}_back_avail"] = avail_usd
            self._fetch_times[(gid, provider)] = time.monotonic()
            self._dirty.add(gid)
            return True

    # ------------------------------------------------------------------
    # Reads (called from the arb detector)
    # ------------------------------------------------------------------

    def pop_dirty(self) -> list[dict[str, Any]]:
        """Return copies of all games updated since the last call, then clear the set."""
        with self._lock:
            result = [
                dict(self._games[gid])
                for gid in self._dirty
                if gid in self._games
            ]
            self._dirty.clear()
            return result

    def all_games(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(g) for g in self._games.values()]

    def game_ids(self) -> list[str]:
        with self._lock:
            return list(self._games.keys())

    # ------------------------------------------------------------------
    # Staleness checks
    # ------------------------------------------------------------------

    def provider_age_s(self, gid: str, provider: str) -> float | None:
        """Seconds since this provider's odds were last updated. None = never."""
        t = self._fetch_times.get((gid, provider))
        return None if t is None else time.monotonic() - t

    def is_stale(self, gid: str, provider: str) -> bool:
        age = self.provider_age_s(gid, provider)
        return age is None or age > self.max_provider_staleness_s


# ---------------------------------------------------------------------------
# Stable game ID (must be consistent across cache lifetime)
# ---------------------------------------------------------------------------

def game_id(game: dict[str, Any]) -> str:
    """Derive a stable string key from a game dict.

    Uses league + date (YYYY-MM-DD) + team names so the ID survives restarts
    and is human-readable in logs.
    """
    league = game.get("league", "")
    date   = (game.get("date_time") or "")[:10]
    team1  = (game.get("team1") or "").lower()
    team2  = (game.get("team2") or "").lower()
    return f"{league}|{date}|{team1}|{team2}"
