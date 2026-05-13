"""
matchbook_bet.py
----------------
Place a back or lay bet on Matchbook given a Matchbook event ID.

The script fetches the event, lists its markets and runners with current
prices, then places a limit offer. If --market-id / --runner-id are omitted
it prints what's available and exits so you can pick the right IDs.

Requires:
    MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD set in .env

Usage:
    # Show markets and runners for an event
    py matchbook_bet.py --event-id 123456789

    # Dry run — show what would be placed without submitting
    py matchbook_bet.py --event-id 123456789 --market-id 111 --runner-id 222 --stake 2.0 --dry-run

    # Place a back bet at best available odds
    py matchbook_bet.py --event-id 123456789 --market-id 111 --runner-id 222 --stake 2.0

    # Place a back bet at specific odds
    py matchbook_bet.py --event-id 123456789 --market-id 111 --runner-id 222 --stake 2.0 --odds 1.95

    # Lay bet
    py matchbook_bet.py --event-id 123456789 --market-id 111 --runner-id 222 --stake 2.0 --side lay

    # List your open offers
    py matchbook_bet.py --offers

    # Cancel an offer
    py matchbook_bet.py --cancel-offer 987654321
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient

_MIN_STAKE = 0.10   # Matchbook minimum stake in account currency


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login(http: HttpClient, base_url: str, username: str, password: str) -> str:
    resp = http.post_json(
        f"{base_url}/bpapi/rest/security/session",
        payload={"username": username, "password": password},
        headers={"Accept": "application/json"},
    )
    token = resp.get("session-token")
    if not token:
        raise RuntimeError(f"Login failed: {resp}")
    return str(token)


# ---------------------------------------------------------------------------
# Event / market helpers
# ---------------------------------------------------------------------------

def fetch_event(http: HttpClient, base_url: str, event_id: int, token: str) -> dict:
    return http.get_json(
        f"{base_url}/edge/rest/events/{event_id}",
        headers={"session-token": token, "Accept": "application/json"},
    )


def print_event_summary(event: dict) -> None:
    """Print all open markets and runners with best back/lay prices."""
    print(f"\nEvent : {event.get('name')}  (id={event.get('id')})")
    print(f"Start : {event.get('start')}")
    print(f"Status: {event.get('status')}\n")

    open_markets = [m for m in event.get("markets", []) if m.get("status") == "open"]
    if not open_markets:
        print("  No open markets found.")
        return

    for market in open_markets:
        print(f"  Market: {market.get('name')}  (market-id={market.get('id')})")
        for runner in market.get("runners", []):
            prices  = runner.get("prices", [])
            back    = _best_price(prices, "back")
            lay     = _best_price(prices, "lay")
            back_str = f"{back:.4f}" if back else "—"
            lay_str  = f"{lay:.4f}"  if lay  else "—"
            print(
                f"    Runner: {runner.get('name'):<30} "
                f"(runner-id={runner.get('id')})  "
                f"back={back_str}  lay={lay_str}"
            )
        print()


def _best_price(prices: list[dict], side: str) -> float | None:
    """Return the best available decimal odds for a side."""
    candidates = [
        float(p.get("decimal-odds") or p.get("odds") or 0)
        for p in prices
        if p.get("side") == side and (p.get("decimal-odds") or p.get("odds"))
    ]
    if not candidates:
        return None
    return max(candidates) if side == "back" else min(candidates)


def resolve_odds(event: dict, market_id: int, runner_id: int, side: str) -> float | None:
    """Find the best available odds for a given market/runner/side."""
    for market in event.get("markets", []):
        if market.get("id") != market_id:
            continue
        for runner in market.get("runners", []):
            if runner.get("id") != runner_id:
                continue
            return _best_price(runner.get("prices", []), side)
    return None


# ---------------------------------------------------------------------------
# Offer management
# ---------------------------------------------------------------------------

def place_offer(
    http: HttpClient,
    base_url: str,
    token: str,
    event_id: int,
    market_id: int,
    runner_id: int,
    side: str,
    stake: float,
    odds: float,
    dry_run: bool = False,
) -> dict:
    payload = {
        "offers": [{
            "event-id":         event_id,
            "market-id":        market_id,
            "runner-id":        runner_id,
            "side":             side,
            "odds":             odds,
            "stake":            stake,
            "odds-type":        "DECIMAL",
            "offer-type":       "LIMIT",
            "remain-unmatched": "KEEP",
        }]
    }

    if dry_run:
        return {"dry_run": True, "payload": payload}

    return http.post_json(
        f"{base_url}/edge/rest/offers",
        payload=payload,
        headers={"session-token": token, "Accept": "application/json"},
    )


def list_offers(http: HttpClient, base_url: str, token: str) -> None:
    resp    = http.get_json(
        f"{base_url}/edge/rest/offers",
        params={"offset": 0, "per-page": 50},
        headers={"session-token": token, "Accept": "application/json"},
    )
    offers = resp.get("offers", [])
    if not offers:
        print("  No open offers.")
        return
    for o in offers:
        print(
            f"  id={o.get('id')}  "
            f"{o.get('runner-name','?')}  "
            f"{o.get('side')}  "
            f"odds={o.get('odds')}  "
            f"stake={o.get('stake')}  "
            f"matched={o.get('matched-stake', 0)}  "
            f"status={o.get('status')}"
        )


def cancel_offer(http: HttpClient, base_url: str, token: str, offer_id: int) -> None:
    resp = http.request_json(
        "DELETE",
        f"{base_url}/edge/rest/offers/{offer_id}",
        headers={"session-token": token, "Accept": "application/json"},
    )
    print(f"  Cancel response: {resp}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Place a bet on Matchbook.")
    parser.add_argument("--event-id",    type=int, default=0,
                        help="Matchbook event ID.")
    parser.add_argument("--market-id",   type=int, default=0,
                        help="Market ID within the event.")
    parser.add_argument("--runner-id",   type=int, default=0,
                        help="Runner (selection) ID within the market.")
    parser.add_argument("--side",        choices=["back", "lay"], default="back",
                        help="Back or lay (default: back).")
    parser.add_argument("--stake",       type=float, default=0,
                        help="Stake in your account currency.")
    parser.add_argument("--odds",        type=float, default=0,
                        help="Decimal odds. If omitted, uses best available.")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Build the offer but do not submit it.")
    parser.add_argument("--offers",      action="store_true",
                        help="List your open offers and exit.")
    parser.add_argument("--cancel-offer", type=int, default=0, metavar="OFFER_ID",
                        help="Cancel an open offer by ID.")
    args = parser.parse_args()

    settings = load_settings(_ROOT)
    mb       = settings.matchbook
    http     = HttpClient()

    if not (mb.username and mb.password):
        sys.exit("ERROR: MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD must be set in .env")

    # ── Login ────────────────────────────────────────────────────────────
    print("Logging in to Matchbook... ", end="", flush=True)
    try:
        token = login(http, mb.base_url, mb.username, mb.password)
    except Exception as exc:
        sys.exit(f"failed\nERROR: {exc}")
    print(f"ok  (token: {token[:20]}…)")

    # ── Offer management shortcuts ────────────────────────────────────────
    if args.offers:
        print("\nOpen offers:")
        list_offers(http, mb.base_url, token)
        return

    if args.cancel_offer:
        print(f"\nCancelling offer {args.cancel_offer}...")
        cancel_offer(http, mb.base_url, token, args.cancel_offer)
        return

    # ── Require event ID for everything else ─────────────────────────────
    if not args.event_id:
        sys.exit("ERROR: --event-id is required.")

    # ── Fetch event ───────────────────────────────────────────────────────
    print(f"\nFetching event {args.event_id}... ", end="", flush=True)
    try:
        event = fetch_event(http, mb.base_url, args.event_id, token)
    except Exception as exc:
        sys.exit(f"failed\nERROR: {exc}")
    print("ok")

    # If no market/runner specified, just show what's available and exit
    if not args.market_id or not args.runner_id:
        print_event_summary(event)
        print("Specify --market-id and --runner-id to place a bet.")
        return

    # ── Resolve odds ──────────────────────────────────────────────────────
    if args.odds:
        odds = args.odds
    else:
        odds = resolve_odds(event, args.market_id, args.runner_id, args.side)
        if odds is None:
            sys.exit(
                f"ERROR: no {args.side} prices found for "
                f"market-id={args.market_id} runner-id={args.runner_id}.\n"
                "Run without --market-id/--runner-id to see available prices."
            )
        print(f"Best available {args.side} odds: {odds}")

    # ── Validate stake ────────────────────────────────────────────────────
    if args.stake <= 0:
        sys.exit("ERROR: --stake must be a positive number.")
    if args.stake < _MIN_STAKE:
        sys.exit(f"ERROR: minimum stake is {_MIN_STAKE}.")

    # ── Place offer ───────────────────────────────────────────────────────
    print(f"\nPlacing offer:")
    print(f"  event-id  : {args.event_id}")
    print(f"  market-id : {args.market_id}")
    print(f"  runner-id : {args.runner_id}")
    print(f"  side      : {args.side}")
    print(f"  odds      : {odds}")
    print(f"  stake     : {args.stake}")
    if args.dry_run:
        print(f"  mode      : DRY RUN — will not be submitted")
    print()

    try:
        resp = place_offer(
            http, mb.base_url, token,
            event_id=args.event_id,
            market_id=args.market_id,
            runner_id=args.runner_id,
            side=args.side,
            stake=args.stake,
            odds=odds,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        sys.exit(f"ERROR: offer failed: {exc}")

    print("Response:")
    print(json.dumps(resp, indent=2, default=str))

    if not args.dry_run:
        offers = resp.get("offers", [])
        if offers:
            o = offers[0]
            print(f"\n  Offer ID : {o.get('id')}")
            print(f"  Status   : {o.get('status')}")
            print(f"  Matched  : {o.get('matched-stake', 0)}")
            offer_id = o.get("id")
            if offer_id:
                print(f"\n  To cancel: py matchbook_bet.py --cancel-offer {offer_id}")


if __name__ == "__main__":
    main()
