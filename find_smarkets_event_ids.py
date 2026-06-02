"""
find_smarkets_event_ids.py
--------------------------
Several strategies for finding Smarkets root competition IDs.

Strategies
----------
1. Name search (fastest — hits the API search param directly):
       python find_smarkets_event_ids.py --name "premier"

2. Ancestors — walk UP from a known match event ID to find its root:
       python find_smarkets_event_ids.py --ancestors 12345678
   Get a match event ID by browsing the Smarkets website and copying the
   event ID from the URL, e.g. https://smarkets.com/event/12345678/...

3. Browse children of a parent event:
       python find_smarkets_event_ids.py --parent 1234567

4. Recursive search downward from a parent:
       python find_smarkets_event_ids.py --parent 1234567 --search "premier" --depth 3

5. No args — list all top-level sports:
       python find_smarkets_event_ids.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent / "src"))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient


def fetch_children(base_url: str, http: HttpClient, parent_id: int | None, limit: int = 200) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    if parent_id is not None:
        params["parent_id"] = parent_id
    payload = http.get_json(
        f"{base_url}/v3/events/",
        params=params,
        headers={"Accept": "application/json"},
    )
    return list(payload.get("events", []))


def fetch_event(base_url: str, http: HttpClient, event_id: int) -> dict[str, Any] | None:
    try:
        payload = http.get_json(
            f"{base_url}/v3/events/{event_id}/",
            headers={"Accept": "application/json"},
        )
        events = payload.get("events", [])
        return events[0] if events else None
    except Exception:
        return None


def name_search(base_url: str, http: HttpClient, query: str, limit: int = 200) -> list[dict[str, Any]]:
    """Try the Smarkets name/search query param — returns matching events at any level."""
    results: list[dict[str, Any]] = []
    # Smarkets supports both `name` and `q` as search params; try both
    for param_key in ("name", "q"):
        try:
            payload = http.get_json(
                f"{base_url}/v3/events/",
                params={param_key: query, "limit": limit},
                headers={"Accept": "application/json"},
            )
            batch = payload.get("events", [])
            if batch:
                print(f"  (found {len(batch)} results using param '{param_key}')")
                results.extend(batch)
                break
        except Exception as exc:
            print(f"  (param '{param_key}' failed: {exc})")
    return results


def walk_ancestors(base_url: str, http: HttpClient, event_id: int) -> None:
    """Walk from event_id up to the root, printing the full ancestry chain."""
    chain: list[dict[str, Any]] = []
    current_id: int | None = event_id

    print(f"Walking ancestry from event {event_id}...\n")
    seen: set[int] = set()
    while current_id is not None:
        if current_id in seen:
            print("  [cycle detected, stopping]")
            break
        seen.add(current_id)

        event = fetch_event(base_url, http, current_id)
        if event is None:
            print(f"  [could not fetch event {current_id}]")
            break
        chain.append(event)
        current_id = event.get("parent_id")

    chain.reverse()
    print("Root → Leaf ancestry chain:\n")
    for depth, e in enumerate(chain):
        pad = "  " * depth
        marker = "  <-- ROOT" if depth == 0 else ("  <-- THIS" if e["id"] == event_id else "")
        print(f"{pad}id={e['id']}  name={e.get('name')!r}  type={e.get('type')!r}  parent={e.get('parent_id')}{marker}")


def search_recursive(
    base_url: str,
    http: HttpClient,
    parent_id: int,
    keyword: str,
    depth: int,
    current_depth: int = 0,
) -> None:
    if current_depth > depth:
        return
    try:
        children = fetch_children(base_url, http, parent_id)
    except Exception as exc:
        print(f"{'  ' * current_depth}[error fetching children of {parent_id}: {exc}]")
        return

    for e in children:
        name = str(e.get("name") or "")
        if keyword.lower() in name.lower():
            pad = "  " * current_depth
            print(f"{pad}MATCH  id={e['id']}  name={name!r}  type={e.get('type')!r}  bettable={e.get('bettable')}")
        search_recursive(base_url, http, e["id"], keyword, depth, current_depth + 1)


def print_events(events: list[dict[str, Any]], indent: int = 0) -> None:
    pad = "  " * indent
    for e in events:
        print(f"{pad}id={e['id']}  name={e.get('name')!r}  type={e.get('type')!r}  bettable={e.get('bettable')}  parent={e.get('parent_id')}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Find Smarkets root competition event IDs.")
    parser.add_argument("--name", type=str, default=None, help="Search for events by name via API query param (e.g. 'premier').")
    parser.add_argument("--ancestors", type=int, default=None, help="Walk up from a known match event ID to find its root competition ID.")
    parser.add_argument("--parent", type=int, default=None, help="List children of a specific event ID.")
    parser.add_argument("--search", type=str, default=None, help="With --parent: recursively search children for a keyword.")
    parser.add_argument("--depth", type=int, default=2, help="Recursion depth for --search (default: 2).")
    parser.add_argument("--limit", type=int, default=200, help="Max events per page (default: 200).")
    args = parser.parse_args()

    project_root = Path(__file__).parent
    settings = load_settings(project_root)
    base_url = settings.smarkets.base_url
    http = HttpClient()

    print(f"Smarkets base URL: {base_url}\n")

    if args.name:
        print(f"Searching API for events matching {args.name!r}...\n")
        results = name_search(base_url, http, args.name, limit=args.limit)
        if results:
            print(f"\nResults ({len(results)} events):\n")
            print_events(results)
        else:
            print("No results found.")

    elif args.ancestors:
        walk_ancestors(base_url, http, args.ancestors)

    elif args.parent and args.search:
        print(f"Recursively searching children of {args.parent} for {args.search!r} (depth={args.depth}):\n")
        search_recursive(base_url, http, args.parent, args.search, args.depth)

    elif args.parent:
        events = fetch_children(base_url, http, args.parent, limit=args.limit)
        print(f"Children of event {args.parent} ({len(events)} found):\n")
        print_events(events)

    else:
        events = fetch_children(base_url, http, None, limit=args.limit)
        print(f"Top-level events ({len(events)} found):\n")
        print_events(events)


if __name__ == "__main__":
    main()
