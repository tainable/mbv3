"""
find_matchbook_league_tags.py
-----------------------------
Fetch all active soccer events from Matchbook (sport-id 15) and print the
meta-tag url-names attached to each event.  Use --search to filter by keyword.

Usage:
    python find_matchbook_league_tags.py
    python find_matchbook_league_tags.py --search "liga"
    python find_matchbook_league_tags.py --search "spain"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient


_SOCCER_SPORT_ID = 15


def _login(http: HttpClient, base_url: str, username: str, password: str) -> str:
    response = http.post_json(
        f"{base_url}/bpapi/rest/security/session",
        payload={"username": username, "password": password},
        headers={"Accept": "application/json"},
    )
    token = response.get("session-token")
    if not token:
        sys.exit("ERROR: Matchbook login failed — no session-token in response.")
    return str(token)


def _fetch_events(http: HttpClient, base_url: str, sport_id: int, token: str) -> list[dict]:
    events: list[dict] = []
    offset = 0
    per_page = 50
    while True:
        payload = http.get_json(
            f"{base_url}/edge/rest/events",
            params={"sport-ids": sport_id, "per-page": per_page, "offset": offset},
            headers={"Accept": "application/json", "session-token": token},
        )
        batch = payload.get("events", [])
        if not batch:
            break
        events.extend(batch)
        offset += len(batch)
        if len(batch) < per_page:
            break
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description="Find Matchbook meta-tag url-names for soccer leagues.")
    parser.add_argument("--search", type=str, default=None, help="Keyword to filter by (case-insensitive).")
    args = parser.parse_args()

    project_root = Path(__file__).parent
    settings = load_settings(project_root)
    mb = settings.matchbook

    if not (mb.username and mb.password):
        sys.exit("ERROR: MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD must be set in .env")

    http = HttpClient()

    print(f"Logging in to Matchbook as {mb.username!r} ...")
    token = _login(http, mb.base_url, mb.username, mb.password)

    print(f"Fetching soccer events (sport-id={_SOCCER_SPORT_ID}) ...")
    events = _fetch_events(http, mb.base_url, _SOCCER_SPORT_ID, token)
    print(f"Fetched {len(events)} events.\n")

    keyword = args.search.lower() if args.search else None

    # Collect all unique url-names and which events carry them
    tag_to_events: dict[str, list[str]] = {}
    for event in events:
        name = event.get("name") or "(unnamed)"
        tags = [t.get("url-name") for t in event.get("meta-tags", []) if t.get("url-name")]
        for tag in tags:
            tag_to_events.setdefault(tag, []).append(name)

    # Filter and print
    matches = {
        tag: evs for tag, evs in sorted(tag_to_events.items())
        if keyword is None or keyword in tag.lower() or any(keyword in e.lower() for e in evs)
    }

    if not matches:
        hint = f" matching {args.search!r}" if args.search else ""
        print(f"No meta-tag url-names found{hint}.")
        return

    label = f"matching {args.search!r}" if args.search else "found"
    print(f"{len(matches)} url-name(s) {label}:\n")
    for tag, evs in matches.items():
        print(f"  {tag!r}  ({len(evs)} event(s))")
        for ev in evs[:5]:
            print(f"    - {ev}")
        if len(evs) > 5:
            print(f"    ... and {len(evs) - 5} more")
        print()


if __name__ == "__main__":
    main()
