"""
specials_scan.py
----------------
Read-only scanner. Fetches live odds for every registered specials event,
evaluates all strategies, writes snapshots + audit logs, and prints a
human-readable summary.

    python specials_scan.py                   # all events
    python specials_scan.py --event makerfield_by_election_2026
    python specials_scan.py --list
    python specials_scan.py --debug
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_SRC  = _ROOT / "src"
for _p in (_SRC, _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient
from matched_betting import calculator

from specials import process_event, _utc_iso_now
from specials_registry import list_events, get_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only evaluator for specials (one-off prediction markets)."
    )
    parser.add_argument("--event", action="append",
                        help="Process only the named event(s). Repeat for multiple. Default: all.")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--list", action="store_true", help="List registered events and exit.")
    args = parser.parse_args(argv)

    if args.list:
        for k in list_events():
            ev = get_event(k)
            print(f"  {k}  -- {ev.get('title')}  ({len(ev.get('strategies', []))} strategies)")
        return 0

    settings = load_settings(_ROOT)
    calculator.configure(settings.commission)

    direct_http = HttpClient()
    vpn_http = HttpClient(proxy_url=settings.vpn_proxy_url) if settings.vpn_proxy_url else direct_http

    targets = args.event or list_events()
    if not targets:
        print("No events registered. Add an entry to specials_registry.SPECIALS_EVENTS.", file=sys.stderr)
        return 1

    print(f"Evaluating {len(targets)} event(s) at {_utc_iso_now()}")
    for key in targets:
        try:
            process_event(key, settings, direct_http, vpn_http, args.debug)
        except Exception as exc:
            print(f"  ERROR processing {key}: {exc}", file=sys.stderr)
            if args.debug:
                import traceback
                traceback.print_exc()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
