"""
mb_auth.py
----------
Shared Matchbook authentication helpers used by bet.py and portfolio.py.
"""
from __future__ import annotations

import time

from matched_betting.http import HttpClient

# Cache: (base_url, username) -> (token, expires_monotonic)
# Matchbook sessions last well beyond 10 minutes; we refresh proactively.
_token_cache: dict[tuple[str, str], tuple[str, float]] = {}
_TOKEN_TTL_S = 600  # 10 minutes


def mb_login(http: HttpClient, base_url: str, username: str, password: str) -> str:
    """Return a valid Matchbook session token, reusing a cached one if fresh."""
    key    = (base_url, username)
    cached = _token_cache.get(key)
    if cached and time.monotonic() < cached[1]:
        return cached[0]

    resp  = http.post_json(
        f"{base_url}/bpapi/rest/security/session",
        payload={"username": username, "password": password},
        headers={"Accept": "application/json"},
    )
    token = resp.get("session-token")
    if not token:
        raise RuntimeError(f"Login failed: {resp}")
    token = str(token)
    _token_cache[key] = (token, time.monotonic() + _TOKEN_TTL_S)
    return token


def mb_invalidate_token(base_url: str, username: str) -> None:
    """Force a fresh login on the next mb_login call (e.g. after a 401)."""
    _token_cache.pop((base_url, username), None)


def mb_best_price(prices: list[dict], side: str) -> float | None:
    """Return the best available decimal odds for the given side from a prices list.

    For back: returns the highest available price.
    For lay:  returns the lowest available price.
    """
    vals = [
        float(p.get("decimal-odds") or p.get("odds") or 0)
        for p in prices
        if p.get("side") == side and (p.get("decimal-odds") or p.get("odds"))
    ]
    if not vals:
        return None
    return max(vals) if side == "back" else min(vals)
