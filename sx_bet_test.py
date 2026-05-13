"""
sx_bet_test.py
--------------
Place a minimum-stake maker order on SX Bet via the REST API.

SX Bet is a P2P order book — you post a *maker* limit order at your desired
odds and wait for another user to fill it, unlike Polymarket's FOK market order.

Stages:
  1. Fetch metadata  (chain ID, executor address)
  2. Find an active market with liquidity
  3. Build + sign an EIP-712 maker order
  4. Post the order  (requires --live flag, otherwise dry-run only)
  5. Print order status / how to cancel

Requirements:
  SX_BET_PRIVATE_KEY set in .env   (your wallet private key, 0x...)
  pip install eth-account requests

Usage:
    py sx_bet_test.py                       # dry run — sign but don't submit
    py sx_bet_test.py --live                # submit the order
    py sx_bet_test.py --orders              # list your pending orders
    py sx_bet_test.py --cancel <orderHash>  # cancel a pending order
    py sx_bet_test.py --league nba          # pick a different league (default: nba)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

import requests

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SX Network chain ID (used in EIP-712 domain)
_SX_CHAIN_ID = 4162

# USDC on SX Network — matches SX_BET_BASE_TOKEN in .env
_USDC_SX = "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"

# Minimum order size in USDC (raw units, 6 decimals)
_MIN_BET_USDC = 1.0
_USDC_DECIMALS = 1_000_000

# Probability scale used by SX Bet (percentageOdds = probability * 10^20)
_ODDS_SCALE = 10 ** 20

# Moneyline market type
_MONEYLINE_TYPE = 226

# League → leagueId map (mirrors providers/sx_bet.py)
_LEAGUE_IDS = {
    "nba": 1,
    "mlb": 171,
    "nhl": 3,
    "epl": 29,
    "ucl": 30,
}

# EIP-712 Order type definition
_ORDER_TYPES = {
    "Order": [
        {"name": "marketHash",               "type": "bytes32"},
        {"name": "baseToken",                "type": "address"},
        {"name": "totalBetSize",             "type": "uint256"},
        {"name": "percentageOdds",           "type": "uint256"},
        {"name": "expiry",                   "type": "uint256"},
        {"name": "salt",                     "type": "uint256"},
        {"name": "maker",                    "type": "address"},
        {"name": "executor",                 "type": "address"},
        {"name": "isMakerBettingOutcomeOne", "type": "bool"},
    ]
}

# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _get(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _post(url: str, payload: dict) -> dict:
    resp = requests.post(url, json=payload, timeout=15)
    if not resp.ok:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        raise RuntimeError(f"{resp.status_code} {resp.reason}: {json.dumps(body, indent=2)}")
    return resp.json()


# ---------------------------------------------------------------------------
# Stage 1 — metadata
# ---------------------------------------------------------------------------

def fetch_metadata(base_url: str) -> tuple[str, int]:
    """Return (executor_address, chain_id) from the SX Bet metadata endpoint."""
    try:
        raw = _get(f"{base_url}/metadata")
        data = raw.get("data", {})
        executor = data.get("executorAddress") or data.get("executor")
        chain_id = int(data.get("chainId") or _SX_CHAIN_ID)
        if executor:
            return executor, chain_id
    except Exception as exc:
        print(f"  (metadata endpoint unavailable: {exc} — using hardcoded values)")

    # Hardcoded fallback for SX Network
    return "0x3E91041b9e60C7275f8296b8B0dAed9e5902202C", _SX_CHAIN_ID


# ---------------------------------------------------------------------------
# Stage 2 — market discovery
# ---------------------------------------------------------------------------

def find_market(base_url: str, base_token: str, league: str) -> dict | None:
    """Return the first moneyline market with live orders for the given league.

    The returned dict includes both outcomes' best odds so the caller can choose
    which side to back and whether to make or take.
    """
    league_id = _LEAGUE_IDS.get(league)
    if league_id is None:
        print(f"ERROR: unknown league '{league}'. Choose from: {list(_LEAGUE_IDS)}")
        return None

    print(f"  Fetching active {league.upper()} markets (leagueId={league_id})...")
    raw = _get(f"{base_url}/markets/active", params={"leagueId": str(league_id)})
    markets = raw.get("data", {}).get("markets", []) if isinstance(raw.get("data"), dict) else []
    moneyline = [m for m in markets if m.get("type") == _MONEYLINE_TYPE]
    print(f"  Found {len(moneyline)} moneyline markets")

    if not moneyline:
        return None

    # Pick the first market that has live orders
    hashes = [m["marketHash"] for m in moneyline[:20]]
    odds_raw = _get(
        f"{base_url}/orders/odds/best",
        params={"marketHashes": ",".join(hashes), "baseToken": base_token},
    )
    best_odds = {
        e["marketHash"]: e
        for e in (odds_raw.get("data", {}).get("bestOdds", []) or [])
        if "marketHash" in e
    }

    hash_to_market = {m["marketHash"]: m for m in moneyline}
    for market_hash in hashes:
        if market_hash in best_odds:
            return {
                "market":    hash_to_market[market_hash],
                "best_odds": best_odds[market_hash],
            }

    return None


# ---------------------------------------------------------------------------
# Stage 3 — order building + signing
# ---------------------------------------------------------------------------

def _pct_odds_from_decimal(decimal_odds: float) -> int:
    """Convert decimal odds to SX Bet percentageOdds (maker probability * 10^20)."""
    taker_prob = 1.0 / decimal_odds
    maker_prob = 1.0 - taker_prob
    return int(maker_prob * _ODDS_SCALE)


def fetch_odds_ladder(base_url: str) -> list[int]:
    """Return valid percentageOdds values by trying known API paths.

    Falls back to an empty list — caller should then derive the ladder from
    live market orders instead.
    """
    for path in ("/odds-ladder", "/betting/odds-ladder", "/meta/odds-ladder"):
        try:
            raw  = _get(f"{base_url}{path}")
            data = raw.get("data", {})
            vals = data.get("oddsLadder") or data.get("ladder") or []
            if vals:
                return [int(v) for v in vals]
        except Exception:
            continue
    return []


def derive_ladder_from_orders(base_url: str, market_hash: str, base_token: str) -> list[int]:
    """Extract unique percentageOdds from live orders — guaranteed to be on the ladder."""
    try:
        raw    = _get(f"{base_url}/orders",
                      params={"marketHashes": market_hash, "baseToken": base_token})
        orders = raw.get("data", []) or []
        vals   = {int(o["percentageOdds"]) for o in orders if o.get("percentageOdds")}
        return sorted(vals)
    except Exception:
        return []


def snap_to_ladder(pct_odds: int, ladder: list[int]) -> int:
    """Snap a percentageOdds value to the nearest valid step on the ladder."""
    if not ladder:
        return pct_odds
    return min(ladder, key=lambda v: abs(v - pct_odds))


def best_taker_odds(best_odds: dict) -> dict:
    """Extract the best available taker odds for both outcomes.

    SX Bet stores percentageOdds as the *maker's* probability.
    A taker backing outcomeOne is matched against outcomeTwo makers, so:
      taker_prob(outcomeOne) = 1 - outcomeTwo.percentageOdds / scale

    Returns:
      {
        "outcome_one": {"decimal": float, "pct_odds": int, "is_betting_outcome_one": True},
        "outcome_two": {"decimal": float, "pct_odds": int, "is_betting_outcome_one": False},
      }
    """
    result = {}
    for outcome_key, maker_key, is_one in (
        ("outcome_one", "outcomeTwo", True),
        ("outcome_two", "outcomeOne", False),
    ):
        raw_pct = best_odds.get(maker_key, {}).get("percentageOdds")
        if raw_pct is None:
            continue
        maker_prob = int(raw_pct) / _ODDS_SCALE
        taker_prob = 1.0 - maker_prob
        if not (0.0 < taker_prob < 1.0):
            continue
        decimal = round(1.0 / taker_prob, 4)
        result[outcome_key] = {
            "decimal":                decimal,
            "pct_odds":               int(raw_pct),   # use maker's exact odds to guarantee a fill
            "is_betting_outcome_one": is_one,
        }
    return result


_ON_CHAIN_EXPIRY   = 2209006800          # sentinel meaning "no on-chain expiry" (~year 2040)
_API_EXPIRY_SECONDS = 24 * 60 * 60       # apiExpiry: order cancels from book after this


def build_and_sign_order(
    private_key: str,
    market_hash: str,
    base_token: str,
    amount_usdc: float,
    decimal_odds: float,
    is_betting_outcome_one: bool,
    executor: str,
    chain_id: int,
    pct_odds_override: int | None = None,
) -> dict:
    """Build an EIP-712 signed order and return the dict ready to POST.

    pct_odds_override: pass the maker's exact percentageOdds when taking
    existing orders to guarantee a fill. If None, derived from decimal_odds.
    """
    from eth_account import Account

    account = Account.from_key(private_key)
    maker = account.address

    total_bet_size = int(amount_usdc * _USDC_DECIMALS)
    percentage_odds = pct_odds_override if pct_odds_override is not None else _pct_odds_from_decimal(decimal_odds)
    salt = random.randint(1, 2 ** 256 - 1)
    api_expiry = int(time.time()) + _API_EXPIRY_SECONDS
    expiry     = _ON_CHAIN_EXPIRY   # always use the sentinel for on-chain expiry
    market_hash_bytes = bytes.fromhex(market_hash.replace("0x", ""))

    domain = {
        "name":              "SX Bet",
        "version":           "1.0",
        "chainId":           chain_id,
        "verifyingContract": executor,
    }

    message = {
        "marketHash":               market_hash_bytes,
        "baseToken":                base_token,
        "totalBetSize":             total_bet_size,
        "percentageOdds":           percentage_odds,
        "expiry":                   expiry,
        "salt":                     salt,
        "maker":                    maker,
        "executor":                 executor,
        "isMakerBettingOutcomeOne": is_betting_outcome_one,
    }

    signed = Account.sign_typed_data(
        private_key=private_key,
        domain_data=domain,
        message_types=_ORDER_TYPES,
        message_data=message,
    )

    raw_sig = (
        signed.signature.hex()
        if not isinstance(signed.signature, str)
        else signed.signature
    )
    signature = raw_sig if raw_sig.startswith("0x") else f"0x{raw_sig}"

    return {
        "marketHash":               market_hash,
        "baseToken":                base_token,
        "totalBetSize":             str(total_bet_size),
        "percentageOdds":           str(percentage_odds),
        "expiry":                   expiry,       # on-chain sentinel (integer)
        "apiExpiry":                api_expiry,   # API-level expiry (integer)
        "salt":                     str(salt),
        "maker":                    maker,
        "executor":                 executor,
        "isMakerBettingOutcomeOne": is_betting_outcome_one,
        "signature":                signature,
    }


# ---------------------------------------------------------------------------
# Order management
# ---------------------------------------------------------------------------

def list_orders(base_url: str, maker_address: str) -> None:
    """Print all pending orders for the wallet."""
    raw = _get(f"{base_url}/orders", params={"maker": maker_address})
    orders = raw.get("data", []) or []
    if not orders:
        print("  No pending orders.")
        return
    for o in orders:
        size   = int(o.get("totalBetSize", 0)) / _USDC_DECIMALS
        filled = int(o.get("fillAmount", 0)) / _USDC_DECIMALS
        pct    = int(o.get("percentageOdds", 0)) / _ODDS_SCALE
        odds   = round(1 / (1 - pct), 4) if 0 < pct < 1 else "?"
        print(
            f"  hash={o.get('orderHash','?')[:20]}…"
            f"  size=${size:.2f}  filled=${filled:.2f}"
            f"  odds={odds}  outcome_one={o.get('isMakerBettingOutcomeOne')}"
        )


def cancel_order(base_url: str, private_key: str, order_hash: str) -> None:
    """Cancel a single order by hash (signs the cancellation)."""
    from eth_account import Account

    account = Account.from_key(private_key)
    maker   = account.address

    # SX Bet cancel: sign a message proving ownership, then DELETE
    cancel_payload = {
        "orderHashes": [order_hash],
        "maker":       maker,
    }
    raw = _post(f"{base_url}/orders/cancel/v2", cancel_payload)
    print(f"  Cancel response: {raw}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="SX Bet order test.")
    parser.add_argument("--live",    action="store_true", help="Submit the order (default: dry run).")
    parser.add_argument("--league",  default="nba",       help="League to bet on (default: nba).")
    parser.add_argument("--amount",  type=float, default=_MIN_BET_USDC, help="Stake in USDC (default: 1.0).")
    parser.add_argument("--orders",  action="store_true", help="List your pending orders then exit.")
    parser.add_argument("--cancel",  metavar="ORDER_HASH", help="Cancel a pending order by hash.")
    parser.add_argument("--take",        action="store_true",
                        help="Take existing odds (fill a maker order immediately) instead of posting a new maker order.")
    parser.add_argument("--outcome",     choices=["one", "two"], default="one",
                        help="Which outcome to bet on: 'one' (home/team one) or 'two' (away/team two). Default: one.")
    parser.add_argument("--market-hash", default="",
                        help="Skip market discovery and bet directly on this market hash.")
    args = parser.parse_args()

    settings   = load_settings(_ROOT)
    base_url   = settings.sx_bet.base_url
    base_token = settings.sx_bet.base_token

    private_key = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    if not private_key:
        sys.exit("ERROR: set SX_BET_PRIVATE_KEY (or POLYMARKET_PRIVATE_KEY) in .env")

    from eth_account import Account
    wallet = Account.from_key(private_key).address
    print(f"Wallet : {wallet}")
    print(f"API    : {base_url}\n")

    # ── 1. Metadata ───────────────────────────────────────────────────────
    print("1. Fetching metadata...")
    executor, chain_id = fetch_metadata(base_url)
    print(f"   executor={executor}  chainId={chain_id}")
    ladder = fetch_odds_ladder(base_url)
    if ladder:
        print(f"   odds ladder: {len(ladder)} valid steps (from API)")
    else:
        print(f"   (odds ladder API unavailable — will derive from live orders)")

    # ── Order management shortcuts ────────────────────────────────────────
    if args.orders:
        print("\nPending orders:")
        list_orders(base_url, wallet)
        return

    if args.cancel:
        print(f"\nCancelling {args.cancel}...")
        cancel_order(base_url, private_key, args.cancel)
        return

    # ── 2. Find a market ─────────────────────────────────────────────────
    if args.market_hash:
        print(f"\n2. Using provided market hash: {args.market_hash}")
        market_hash = args.market_hash

        # Fetch market details by hash — filter the result ourselves since
        # /markets/active may not support marketHashes as a query filter.
        market = {"marketHash": market_hash}  # fallback if lookup fails
        try:
            market_raw = _get(f"{base_url}/markets/active",
                              params={"marketHash": market_hash})
            markets = market_raw.get("data", {}).get("markets", []) \
                if isinstance(market_raw.get("data"), dict) else []
            # Filter by the exact hash — don't just take index 0
            matched = [m for m in markets if m.get("marketHash") == market_hash]
            if matched:
                market = matched[0]
            else:
                print("  (market metadata not found for this hash — team names unavailable)")
        except Exception as exc:
            print(f"  (market metadata lookup failed: {exc})")

        odds_raw  = _get(f"{base_url}/orders/odds/best",
                         params={"marketHashes": market_hash, "baseToken": base_token})
        best_list = odds_raw.get("data", {}).get("bestOdds", []) or []
        if not best_list:
            sys.exit(f"No live orders found for market hash {market_hash}.\n"
                     "Check the hash is correct and the market is still active.")
        best_odds = best_list[0]
        team_one  = market.get("teamOneName", "Outcome One")
        team_two  = market.get("teamTwoName", "Outcome Two")
    else:
        print(f"\n2. Finding active {args.league.upper()} market...")
        result = find_market(base_url, base_token, args.league)
        if not result:
            sys.exit(f"No active {args.league.upper()} markets with liquidity found.")

        market      = result["market"]
        best_odds   = result["best_odds"]
        market_hash = market["marketHash"]
        team_one    = market.get("teamOneName", "Team One")
        team_two    = market.get("teamTwoName", "Team Two")

    # Best taker odds for outcomeOne (backing team_one)
    # Taker probability = 1 - maker's percentageOdds / scale
    oc2_pct = best_odds.get("outcomeTwo", {}).get("percentageOdds")
    if oc2_pct:
        maker_prob = int(oc2_pct) / _ODDS_SCALE
        taker_prob = 1.0 - maker_prob
        best_decimal = round(1.0 / taker_prob, 4)
    else:
        best_decimal = 2.0  # fallback: evens

    taker_odds = best_taker_odds(best_odds)

    # Derive ladder from live orders and inspect their structure
    if not ladder:
        try:
            raw_orders = _get(f"{base_url}/orders",
                              params={"marketHashes": market_hash, "baseToken": base_token})
            live_orders = raw_orders.get("data", []) or []
            if live_orders:
                print(f"\n   Sample live order (first result):")
                print(json.dumps(live_orders[0], indent=4))
            ladder = sorted({int(o["percentageOdds"]) for o in live_orders if o.get("percentageOdds")})
        except Exception as exc:
            live_orders = []
            print(f"   (could not fetch live orders: {exc})")
        print(f"   odds ladder: {len(ladder)} valid steps (derived from live orders)")

    print(f"   Market : {team_one} vs {team_two}")
    print(f"   Hash   : {market_hash}")
    if "outcome_one" in taker_odds:
        print(f"   Best odds on {team_one} (outcome one): {taker_odds['outcome_one']['decimal']}")
    if "outcome_two" in taker_odds:
        print(f"   Best odds on {team_two} (outcome two): {taker_odds['outcome_two']['decimal']}")

    # ── 3. Build order ───────────────────────────────────────────────────
    outcome_key = f"outcome_{args.outcome}"    # "outcome_one" or "outcome_two"
    team_name   = team_one if args.outcome == "one" else team_two

    if args.take:
        # Taking: use the maker's exact percentageOdds to guarantee an immediate fill
        if outcome_key not in taker_odds:
            sys.exit(f"No available odds on {team_name} to take right now.")
        odds_info    = taker_odds[outcome_key]
        use_decimal  = odds_info["decimal"]
        pct_override = odds_info["pct_odds"]
        mode_label   = f"TAKE (immediate fill) @ {use_decimal}"
    else:
        # Making: post at 1% worse than best, snapped to nearest ladder step
        if outcome_key in taker_odds:
            best_dec    = taker_odds[outcome_key]["decimal"]
            use_decimal = round(best_dec * 0.99, 4)
        else:
            use_decimal = 2.0  # fallback: evens
        raw_pct      = _pct_odds_from_decimal(use_decimal)
        snapped_pct  = snap_to_ladder(raw_pct, ladder)
        pct_override = snapped_pct
        # Recalculate display decimal from snapped value
        snapped_maker_prob = snapped_pct / _ODDS_SCALE
        snapped_taker_prob = 1.0 - snapped_maker_prob
        use_decimal  = round(1.0 / snapped_taker_prob, 4) if snapped_taker_prob > 0 else use_decimal
        mode_label   = f"MAKE (rests in book) @ {use_decimal}"

    is_one = (args.outcome == "one")
    print(f"\n3. Building ${args.amount} order on '{team_name}' — {mode_label}...")

    order = build_and_sign_order(
        private_key=private_key,
        market_hash=market_hash,
        base_token=base_token,
        amount_usdc=args.amount,
        decimal_odds=use_decimal,
        is_betting_outcome_one=is_one,
        executor=executor,
        chain_id=chain_id,
        pct_odds_override=pct_override,
    )

    print(f"   maker={order['maker']}")
    print(f"   totalBetSize={order['totalBetSize']}  ({args.amount} USDC)")
    print(f"   percentageOdds={order['percentageOdds']}")
    print(f"   sig={order['signature'][:30]}…")

    if not args.live:
        print("\n  DRY RUN — order built and signed but not submitted.")
        print("  Re-run with --live to submit.")
        return

    # ── 4. Post order ────────────────────────────────────────────────────
    print("\n4. Posting order...")
    print("   Payload:")
    print(json.dumps({"orders": [order]}, indent=4))
    try:
        resp = _post(f"{base_url}/orders/new", {"orders": [order]})
        print(f"   Response: {json.dumps(resp, indent=2)}")
        order_hash = (
            (resp.get("data") or [{}])[0].get("orderHash")
            if isinstance(resp.get("data"), list)
            else resp.get("data", {}).get("orderHash")
        )
        if order_hash:
            print(f"\n   Order hash : {order_hash}")
            print(f"   To cancel  : py sx_bet_test.py --cancel {order_hash}")
    except Exception as exc:
        print(f"   ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
