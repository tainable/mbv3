"""
bet.py
------
Unified betting CLI for Polymarket, Matchbook, and SX Bet.

Usage:
  # Check balances and active bets across all platforms
  py bet.py --status

  # Polymarket back (YES token)
  py bet.py --pm-token-id <yes_token_id> --pm-amount 5.0
  py bet.py --pm-token-id <yes_token_id> --pm-amount 5.0 --pm-side SELL

  # Polymarket lay (football) — buying the NO token for a binary outcome market.
  # --pm-side LAY resolves the NO token from the YES token automatically.
  # Covers ALL non-event scenarios (draw + away win for a home-win lay).
  py bet.py --pm-token-id <yes_token_id> --pm-amount 5.0 --pm-side LAY

  py bet.py --pm-approve
  py bet.py --pm-cancel [--pm-order-id <id>]

  # Matchbook  (omit --mb-market-id/--mb-runner-id to browse an event)
  py bet.py --mb-event-id <id>
  py bet.py --mb-event-id <id> --mb-market-id <id> --mb-runner-id <id> --mb-stake 5.0
  py bet.py --mb-event-id <id> --mb-market-id <id> --mb-runner-id <id> --mb-stake 5.0 --mb-side lay
  py bet.py --mb-cancel-offer <offer-id>

  # SX Bet
  py bet.py --sx-market-hash <hash> --sx-amount 5.0
  py bet.py --sx-market-hash <hash> --sx-amount 5.0 --sx-take --sx-outcome two
  py bet.py --sx-cancel <order-hash>
  py bet.py --sx-orders

  # Simultaneous bets across platforms (placed concurrently)
  py bet.py --pm-token-id <id> --pm-amount 5.0 \
            --mb-event-id <id> --mb-market-id <id> --mb-runner-id <id> --mb-stake 5.0

  # Dry run any of the above
  py bet.py --pm-token-id <id> --pm-amount 5.0 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests as _requests

_ROOT = Path(__file__).resolve().parent
_BET_LOG = _ROOT / "outputs" / "bet_log.jsonl"


def set_log_path(path: Path) -> None:
    """Redirect per-leg bet logging to a different file (e.g. stream_bet_log.jsonl)."""
    global _BET_LOG
    _BET_LOG = path


def _log_bet(entry: dict) -> None:
    """Append a JSON line to bet_log for every live bet placed."""
    import datetime
    entry = {"timestamp": datetime.datetime.utcnow().isoformat() + "Z", **entry}
    _BET_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _BET_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
sys.path.insert(0, str(_ROOT / "src"))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient
from matched_betting.polygon_rpc import PM_CHAIN_ID, PM_RPCS, pm_rpc, pm_matic_balance
from matched_betting.mb_auth import mb_login, mb_best_price

# ============================================================================
# Polymarket
# ============================================================================

try:
    from py_clob_client_v2.client import ClobClient
    from py_clob_client_v2.clob_types import (
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgsV2 as MarketOrderArgs,
        OpenOrderParams,
        OrderPayload,
        OrderType,
        TradeParams,
    )
    from py_clob_client_v2.order_builder.constants import BUY, SELL
    _PM_AVAILABLE = True
except ImportError:
    _PM_AVAILABLE = False

_PM_CLOB_HOST   = "https://clob.polymarket.com"
_PM_DATA_API    = "https://data-api.polymarket.com"
_PM_USDC        ="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
_PM_SPENDERS    = [
    "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E",
    "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
]
_PM_MAX_UINT256   = 2 ** 256 - 1
_PM_APPROVE_SEL   = bytes.fromhex("095ea7b3")
_PM_MATIC_MIN_GAS = 0.01



def _pm_resolve_no_token(settings, yes_token_id: str) -> str:
    """
    Given a YES CLOB token ID, return the NO CLOB token ID for the same binary market.

    Queries the Polymarket Gamma API with the YES token ID to find the market,
    then returns the other token (the NO contract).
    """
    r = _requests.get(
        f"{settings.polymarket.gamma_base_url}/markets",
        params={"clobTokenIds": yes_token_id},
        timeout=15,
    )
    r.raise_for_status()
    data    = r.json()
    markets = data if isinstance(data, list) else [data]
    if not markets or not markets[0]:
        raise RuntimeError(f"No market found on Gamma for token {yes_token_id}")

    market      = markets[0]
    clob_raw    = market.get("clobTokenIds") or []
    import json as _json
    clob_ids: list[str] = _json.loads(clob_raw) if isinstance(clob_raw, str) else list(clob_raw)

    if len(clob_ids) < 2:
        raise RuntimeError(
            f"Market has <2 clobTokenIds — cannot resolve NO token for {yes_token_id}"
        )

    no_token = next((t for t in clob_ids if t != yes_token_id), None)
    if not no_token:
        raise RuntimeError(f"YES and NO token IDs are identical for {yes_token_id}")
    return no_token


def _pm_build_client(private_key: str) -> "ClobClient":
    client = ClobClient(host=_PM_CLOB_HOST, chain_id=PM_CHAIN_ID, key=private_key)
    # derive_api_key is silent; create_or_derive_api_key attempts create first,
    # which always 400s for existing accounts and logs a noisy error.
    client.set_api_creds(client.derive_api_key())
    return client


def pm_status(settings) -> dict:
    """Return Polymarket balance and bet info as a dict."""
    result: dict = {"platform": "Polymarket", "ok": False}
    if not _PM_AVAILABLE:
        result["error"] = "py-clob-client not installed"
        return result
    pk = settings.polymarket.private_key
    if not pk:
        result["error"] = "POLYMARKET_PRIVATE_KEY not set"
        return result
    try:
        client  = _pm_build_client(pk)
        address = client.signer.address()
        result["address"] = address

        rpc_list = (
            [settings.polymarket.polygon_rpc_url] + PM_RPCS
            if settings.polymarket.polygon_rpc_url else PM_RPCS
        )
        try:
            result["matic"] = round(pm_matic_balance(address, rpc_list), 4)
        except Exception as e:
            result["matic_error"] = str(e)

        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        bal  = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        result["usdc"]       = round(float(bal.get("balance", 0)) / 1_000_000, 2)
        result["allowances"] = {
            k[:10]: round(float(v) / 1_000_000, 2)
            for k, v in bal.get("allowances", {}).items()
        }

        orders    = client.get_open_orders()
        result["open_orders"] = len(orders)

        resp = _requests.get(
            f"{_PM_DATA_API}/positions",
            params={"user": address, "sizeThreshold": "0.01"},
            timeout=15,
        )
        positions = resp.json() if resp.ok else []
        result["positions"] = [
            {
                "outcome": p.get("outcome"),
                "size":    round(float(p.get("size", 0)), 2),
                "avg":     round(float(p.get("avgPrice", 0)), 3),
                "cur":     round(float(p.get("curPrice", 0)), 3),
                "pnl":     round((float(p.get("curPrice", 0)) - float(p.get("avgPrice", 0)))
                                 * float(p.get("size", 0)), 2),
                "title":   (p.get("title") or "")[:60],
            }
            for p in positions
        ]
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def pm_get_balance(settings) -> float | None:
    """Return Polymarket USDC balance, or None on error."""
    if not _PM_AVAILABLE:
        return None
    pk = settings.polymarket.private_key
    if not pk:
        return None
    try:
        client = _pm_build_client(pk)
        client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return round(float(bal.get("balance", 0)) / 1_000_000, 2)
    except Exception:
        return None


def _pm_confirmed_odds(client, token_id: str, timeout: float = 2.0) -> float | None:
    """Query CLOB trades after a FOK fill to get the actual execution price."""
    import time as _time
    deadline = _time.monotonic() + timeout
    address = client.signer.address()
    while _time.monotonic() < deadline:
        try:
            trades = client.get_trades(TradeParams(maker_address=address)) or []
            for t in trades:
                if t.get("asset_id") == token_id:
                    price = float(t.get("price", 0))
                    return round(1.0 / price, 4) if price > 0 else None
        except Exception:
            pass
        _time.sleep(0.5)
    return None


def _pm_best_decimal_odds(token_id: str, side: str) -> float | None:
    """Fetch the best available price for a token and return decimal odds (1/price)."""
    try:
        r = _requests.get(f"{_PM_CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
        if not r.ok:
            return None
        book = r.json()
        entries = book.get("asks" if side == "BUY" else "bids", [])
        if not entries:
            return None
        best_price = float(entries[-1]["price"])
        return round(1.0 / best_price, 4) if best_price > 0 else None
    except Exception:
        return None


def _pm_best_back_and_lay_odds(token_id: str) -> tuple[float | None, float | None]:
    """Fetch a token's order book once and return (back_odds, lay_odds).

    back = 1/best_ask (decimal price to BUY this token — what pm_place_bet
    fills against for a back leg).
    lay  = 1/best_bid (the WS-cache convention in ws_polymarket.py for the
    equivalent lay price of this outcome — actual lay execution buys the
    NO token, but the arb calculator quotes lay edges off this YES-book value).
    """
    try:
        r = _requests.get(f"{_PM_CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
        if not r.ok:
            return None, None
        book = r.json()
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        back = lay = None
        if asks:
            best_ask = float(asks[-1]["price"])
            if best_ask > 0:
                back = round(1.0 / best_ask, 4)
        if bids:
            best_bid = float(bids[-1]["price"])
            if best_bid > 0:
                lay = round(1.0 / best_bid, 4)
        return back, lay
    except Exception:
        return None, None


def pm_check_liquidity(token_id: str, amount_usdc: float, side: str = "BUY") -> tuple[bool, float]:
    """Return (ok, available_usdc) for this token's order book.

    ok is True when resting order depth is sufficient to fill a market order
    of amount_usdc.  Sums price * size across all ask (BUY) or bid (SELL)
    entries to get the total USDC available in the book.

    Returns (True, inf) on any API failure so a transient error does not
    block an otherwise valid arb — the FOK will catch genuine shortfalls.
    """
    try:
        r = _requests.get(f"{_PM_CLOB_HOST}/book", params={"token_id": token_id}, timeout=10)
        if not r.ok:
            return True, float("inf")
        book = r.json()
        entries = book.get("asks" if side == "BUY" else "bids", [])
        available = sum(float(e["price"]) * float(e["size"]) for e in entries)
        return available >= amount_usdc, round(available, 2)
    except Exception:
        return True, float("inf")


def pm_place_bet(settings, token_id: str, amount: float, side: str = "BUY",
                 dry_run: bool = False) -> dict:
    if dry_run:
        decimal_odds = _pm_best_decimal_odds(token_id, side)
        return {"platform": "Polymarket", "ok": True, "dry_run": True,
                "token_id": token_id, "amount": amount, "side": side,
                "decimal_odds": decimal_odds}
    if not _PM_AVAILABLE:
        return {"platform": "Polymarket", "ok": False, "error": "py-clob-client not installed"}
    pk = settings.polymarket.private_key
    if not pk:
        return {"platform": "Polymarket", "ok": False, "error": "POLYMARKET_PRIVATE_KEY not set"}
    for attempt in range(3):
        try:
            client = _pm_build_client(pk)
            decimal_odds = _pm_best_decimal_odds(token_id, side)
            client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            order  = client.create_market_order(
                MarketOrderArgs(token_id=token_id, amount=amount, side=side)
            )
            resp = client.post_order(order, OrderType.FOK)
            execution_odds = _pm_confirmed_odds(client, token_id)
            _log_bet({"platform": "Polymarket", "token_id": token_id,
                      "amount": amount, "side": side, "decimal_odds": decimal_odds,
                      "execution_odds": execution_odds, "response": resp})
            return {"platform": "Polymarket", "ok": True, "response": resp,
                    "amount": amount, "side": side, "decimal_odds": decimal_odds,
                    "execution_odds": execution_odds}
        except Exception as e:
            err_str = str(e)
            if "post_only_mode" in err_str and attempt < 2:
                m = re.search(r"retry_after_seconds['\"]?\s*:\s*(\d+)", err_str)
                wait = int(m.group(1)) + 2 if m else 120
                print(
                    f"  Polymarket post-only mode — waiting {wait}s then retrying"
                    f" (attempt {attempt + 1}/2) ...",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            return {"platform": "Polymarket", "ok": False, "error": err_str}


def pm_approve(settings) -> None:
    from eth_account import Account
    pk       = settings.polymarket.private_key
    rpc_list = (
        [settings.polymarket.polygon_rpc_url] + PM_RPCS
        if settings.polymarket.polygon_rpc_url else PM_RPCS
    )
    account  = Account.from_key(pk)
    address  = account.address
    matic    = pm_matic_balance(address, rpc_list)
    print(f"  MATIC: {matic:.4f}")
    if matic < _PM_MATIC_MIN_GAS:
        raise RuntimeError(f"Insufficient MATIC ({matic:.4f}). Need {_PM_MATIC_MIN_GAS}+.")
    nonce     = int(pm_rpc("eth_getTransactionCount", [address, "latest"], rpc_list), 16)
    gas_price = int(int(pm_rpc("eth_gasPrice", [], rpc_list), 16) * 1.2)
    for i, spender in enumerate(_PM_SPENDERS):
        spender_pad = bytes.fromhex("000000000000000000000000" + spender.lower().replace("0x", ""))
        data        = "0x" + (_PM_APPROVE_SEL + spender_pad + _PM_MAX_UINT256.to_bytes(32, "big")).hex()
        tx = {"nonce": nonce + i, "gasPrice": gas_price, "gas": 100_000,
              "to": _PM_USDC, "value": 0, "data": data, "chainId": PM_CHAIN_ID}
        signed   = Account.sign_transaction(tx, pk)
        tx_hash  = pm_rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()], rpc_list)
        print(f"  tx: {tx_hash}")
        print("    waiting", end="", flush=True)
        for _ in range(90):
            receipt = pm_rpc("eth_getTransactionReceipt", [tx_hash], rpc_list)
            if receipt:
                if int(receipt.get("status", "0x0"), 16) == 1:
                    print(" ✓")
                    break
                raise RuntimeError(f"Transaction {tx_hash} reverted")
            time.sleep(1)
            print(".", end="", flush=True)
    print("  All approvals confirmed.")


def pm_cancel(settings, order_id: str = "") -> None:
    client = _pm_build_client(settings.polymarket.private_key)
    if order_id:
        resp = client.cancel_order(OrderPayload(orderID=order_id))
    else:
        orders = client.get_open_orders()
        if not orders:
            print("  No open Polymarket orders.")
            return
        resp = client.cancel_all()
    print(json.dumps(resp, indent=2, default=str))


# ============================================================================
# Matchbook
# ============================================================================

_MB_MIN_STAKE = 0.10



def mb_status(settings) -> dict:
    result: dict = {"platform": "Matchbook", "ok": False}
    mb = settings.matchbook
    if not (mb.username and mb.password):
        result["error"] = "MATCHBOOK_USERNAME/PASSWORD not set"
        return result
    try:
        http  = HttpClient()
        token = mb_login(http, mb.base_url, mb.username, mb.password)
        result["logged_in"] = True

        try:
            acc   = http.get_json(f"{mb.base_url}/edge/rest/account",
                                  headers={"session-token": token, "Accept": "application/json"})
            result["balance"]  = acc.get("balance")
            result["currency"] = acc.get("currency", "GBP")
            result["exposure"] = acc.get("exposure")
        except Exception:
            result["balance"] = "(unavailable)"

        resp   = http.get_json(
            f"{mb.base_url}/edge/rest/offers",
            params={"offset": 0, "per-page": 50},
            headers={"session-token": token, "Accept": "application/json"},
        )
        offers = resp.get("offers", [])
        result["open_offers"] = len(offers)
        result["offers"] = [
            {
                "id":      o.get("id"),
                "runner":  o.get("runner-name", "?"),
                "side":    o.get("side"),
                "odds":    o.get("odds"),
                "stake":   o.get("stake"),
                "matched": o.get("matched-stake", 0),
                "status":  o.get("status"),
            }
            for o in offers
        ]
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def mb_get_balance(settings) -> float | None:
    """Return Matchbook GBP balance, or None on error."""
    mb = settings.matchbook
    if not (mb.username and mb.password):
        return None
    try:
        http  = HttpClient()
        token = mb_login(http, mb.base_url, mb.username, mb.password)
        acc   = http.get_json(
            f"{mb.base_url}/edge/rest/account",
            headers={"session-token": token, "Accept": "application/json"},
        )
        return float(acc.get("free-funds") or acc.get("balance") or 0)
    except Exception:
        return None


def mb_place_bet(settings, event_id: int, market_id: int, runner_id: int,
                 stake: float, side: str = "back", odds: float = 0.0,
                 dry_run: bool = False) -> dict:
    mb = settings.matchbook
    if not (mb.username and mb.password):
        return {"platform": "Matchbook", "ok": False, "error": "credentials not set"}
    try:
        http  = HttpClient()
        token = mb_login(http, mb.base_url, mb.username, mb.password)

        if not odds:
            event = http.get_json(
                f"{mb.base_url}/edge/rest/events/{event_id}",
                headers={"session-token": token, "Accept": "application/json"},
            )
            for market in event.get("markets", []):
                if market.get("id") != market_id:
                    continue
                for runner in market.get("runners", []):
                    if runner.get("id") != runner_id:
                        continue
                    odds = mb_best_price(runner.get("prices", []), side) or 0.0
            if not odds:
                return {"platform": "Matchbook", "ok": False,
                        "error": f"No {side} prices for runner {runner_id}"}

        payload = {"offers": [{
            "event-id":         event_id,
            "market-id":        market_id,
            "runner-id":        runner_id,
            "side":             side,
            "odds":             odds,
            "stake":            stake,
            "odds-type":        "DECIMAL",
            "offer-type":       "LIMIT",
            "remain-unmatched": "KEEP",
        }]}

        if dry_run:
            return {"platform": "Matchbook", "ok": True, "dry_run": True, "payload": payload,
                    "amount": stake, "decimal_odds": odds}

        resp   = http.post_json(
            f"{mb.base_url}/edge/rest/offers",
            payload=payload,
            headers={"session-token": token, "Accept": "application/json"},
        )
        offers = resp.get("offers", [])
        o      = offers[0] if offers else {}
        _log_bet({"platform": "Matchbook", "event_id": event_id,
                  "market_id": market_id, "runner_id": runner_id,
                  "amount": stake, "side": side, "decimal_odds": odds,
                  "offer_id": o.get("id"), "status": o.get("status")})
        return {"platform": "Matchbook", "ok": True,
                "offer_id": o.get("id"), "status": o.get("status"),
                "matched": o.get("matched-stake", 0), "amount": stake,
                "decimal_odds": odds}
    except Exception as e:
        return {"platform": "Matchbook", "ok": False, "error": str(e)}


def mb_show_event(settings, event_id: int) -> None:
    mb    = settings.matchbook
    http  = HttpClient()
    token = mb_login(http, mb.base_url, mb.username, mb.password)
    event = http.get_json(
        f"{mb.base_url}/edge/rest/events/{event_id}",
        headers={"session-token": token, "Accept": "application/json"},
    )
    print(f"\n  Event : {event.get('name')}  (id={event.get('id')})")
    print(f"  Start : {event.get('start')}  Status: {event.get('status')}\n")
    for market in event.get("markets", []):
        if market.get("status") != "open":
            continue
        print(f"  Market: {market.get('name')}  (market-id={market.get('id')})")
        for runner in market.get("runners", []):
            prices    = runner.get("prices", [])
            back_odds = mb_best_price(prices, "back")
            lay_odds  = mb_best_price(prices, "lay")
            print(
                f"    {runner.get('name'):<30} (runner-id={runner.get('id')})"
                f"  back={back_odds or '—'}  lay={lay_odds or '—'}"
            )
        print()


def mb_cancel_offer(settings, offer_id: int) -> None:
    mb    = settings.matchbook
    http  = HttpClient()
    token = mb_login(http, mb.base_url, mb.username, mb.password)
    resp  = http.request_json(
        "DELETE",
        f"{mb.base_url}/edge/rest/offers/{offer_id}",
        headers={"session-token": token, "Accept": "application/json"},
    )
    print(f"  Cancel response: {resp}")


def mb_get_positions(settings, event_id: int | None = None) -> list[dict]:
    """Return per-runner net positions from Matchbook (GET /edge/rest/positions).

    Each item has runner-level exposure (potential profit/loss).
    Optionally filter to a single event via event_id.
    """
    mb = settings.matchbook
    if not (mb.username and mb.password):
        return []
    try:
        http   = HttpClient()
        token  = mb_login(http, mb.base_url, mb.username, mb.password)
        params: dict = {}
        if event_id:
            params["event-ids"] = event_id
        resp = http.get_json(
            f"{mb.base_url}/edge/rest/positions",
            params=params,
            headers={"session-token": token, "Accept": "application/json"},
        )
        return resp.get("positions", [])
    except Exception:
        return []


def mb_get_runner_prices(
    settings,
    event_id:  int,
    market_id: int,
    runner_id: int,
) -> dict[str, float | None]:
    """Return current best back/lay odds for a specific runner.

    Used when evaluating close prices for an open position.
    Returns {"back_odds": float|None, "lay_odds": float|None}.
    """
    mb = settings.matchbook
    if not (mb.username and mb.password):
        return {"back_odds": None, "lay_odds": None}
    try:
        http   = HttpClient()
        token  = mb_login(http, mb.base_url, mb.username, mb.password)
        event  = http.get_json(
            f"{mb.base_url}/edge/rest/events/{event_id}",
            headers={"session-token": token, "Accept": "application/json"},
        )
        for market in event.get("markets", []):
            if market.get("id") != market_id:
                continue
            for runner in market.get("runners", []):
                if runner.get("id") != runner_id:
                    continue
                prices = runner.get("prices", [])
                return {
                    "back_odds": mb_best_price(prices, "back"),
                    "lay_odds":  mb_best_price(prices, "lay"),
                }
    except Exception:
        pass
    return {"back_odds": None, "lay_odds": None}


# ============================================================================
# SX Bet
# ============================================================================

_SX_CHAIN_ID       = 4162
_SX_ODDS_SCALE     = 10 ** 20
_SX_USDC_DECIMALS  = 1_000_000
_SX_ON_CHAIN_EXPIRY = 2209006800
_SX_API_EXPIRY_SECS = 24 * 60 * 60
_SX_MONEYLINE_TYPE  = 226
_SX_ORDER_TYPES = {
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

# EIP-712 types for the fill (taker) endpoint — POST /orders/fill/v2.
# Domain version "6.0", verifyingContract = EIP712FillHasher (from /metadata).
_SX_FILL_TYPES = {
    "Details": [
        {"name": "action",         "type": "string"},
        {"name": "market",         "type": "string"},
        {"name": "betting",        "type": "string"},
        {"name": "stake",          "type": "string"},
        {"name": "worstOdds",      "type": "string"},
        {"name": "worstReturning", "type": "string"},
        {"name": "fills",          "type": "FillObject"},
    ],
    "FillObject": [
        {"name": "stakeWei",                 "type": "string"},
        {"name": "marketHash",               "type": "string"},
        {"name": "baseToken",                "type": "string"},
        {"name": "desiredOdds",              "type": "string"},
        {"name": "oddsSlippage",             "type": "uint256"},
        {"name": "isTakerBettingOutcomeOne", "type": "bool"},
        {"name": "fillSalt",                 "type": "uint256"},
        {"name": "beneficiary",              "type": "address"},
        {"name": "beneficiaryType",          "type": "uint8"},
        {"name": "cashOutTarget",            "type": "bytes32"},
    ],
}


def _sx_proxies(settings) -> dict | None:
    """Return explicit proxies dict for SX Bet HTTP calls, or None if not configured."""
    proxy_url = getattr(settings, "vpn_proxy_url", None)
    if proxy_url:
        return {"https": proxy_url, "http": proxy_url}
    return None


def _sx_get(url: str, params: dict | None = None, proxies: dict | None = None) -> dict:
    r = _requests.get(url, params=params, timeout=15, proxies=proxies)
    r.raise_for_status()
    return r.json()


def _sx_post(url: str, payload: dict, proxies: dict | None = None) -> dict:
    r = _requests.post(url, json=payload, timeout=15, proxies=proxies)
    if not r.ok:
        try:
            body = r.json()
        except Exception:
            body = r.text
        raise RuntimeError(f"{r.status_code} {r.reason}: {json.dumps(body)}")
    return r.json()


_SX_DEFAULT_EXECUTOR    = "0x3E91041b9e60C7275f8296b8B0dAed9e5902202C"
_SX_DEFAULT_FILL_HASHER = "0x6e0936a1f8b2ff07dCCA13C3d72edC3023eFcAbc"
_SX_RPC                 = "https://rpc-rollup.sx.technology"
_SX_MAX_UINT256         = 2 ** 256 - 1


def _sx_rpc(method: str, params: list, proxies: dict | None = None) -> object:
    r = _requests.post(
        _SX_RPC,
        json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
        timeout=15,
        proxies=proxies,
    )
    r.raise_for_status()
    result = r.json()
    if "error" in result:
        raise RuntimeError(result["error"])
    return result["result"]


def _sx_metadata(base_url: str, proxies: dict | None = None) -> tuple[str, str, int, str]:
    """Returns (executor_address, fill_hasher_address, chain_id, domain_version)."""
    try:
        raw  = _sx_get(f"{base_url}/metadata", proxies=proxies)
        data = raw.get("data", {})
        executor       = data.get("executorAddress") or data.get("executor")
        fill_hasher    = data.get("EIP712FillHasher") or data.get("fillHasher")
        chain_id       = int(data.get("chainId") or _SX_CHAIN_ID)
        domain_version = str(data.get("domainVersion") or "6.0")
        if executor:
            return executor, fill_hasher or _SX_DEFAULT_FILL_HASHER, chain_id, domain_version
    except Exception:
        pass
    return _SX_DEFAULT_EXECUTOR, _SX_DEFAULT_FILL_HASHER, _SX_CHAIN_ID, "6.0"


def _sx_derive_ladder(base_url: str, market_hash: str, base_token: str,
                      proxies: dict | None = None) -> list[int]:
    try:
        raw    = _sx_get(f"{base_url}/orders",
                         params={"marketHashes": market_hash, "baseToken": base_token},
                         proxies=proxies)
        orders = raw.get("data", []) or []
        return sorted({int(o["percentageOdds"]) for o in orders if o.get("percentageOdds")})
    except Exception:
        return []


def _sx_best_taker_odds(best_odds: dict) -> dict:
    result = {}
    for key, maker_key, is_one in (
        ("outcome_one", "outcomeTwo", True),
        ("outcome_two", "outcomeOne", False),
    ):
        raw = best_odds.get(maker_key, {}).get("percentageOdds")
        if raw is None:
            continue
        maker_p = int(raw) / _SX_ODDS_SCALE
        taker_p = 1.0 - maker_p
        if not (0.0 < taker_p < 1.0):
            continue
        result[key] = {
            "decimal":  round(1.0 / taker_p, 4),
            "pct_odds": int(raw),
            "is_one":   is_one,
        }
    return result


def _sx_sign_order(private_key: str, market_hash: str, base_token: str,
                   amount: float, pct_odds: int, is_one: bool,
                   executor: str, chain_id: int) -> dict:
    from eth_account import Account
    account        = Account.from_key(private_key)
    total_bet_size = int(amount * _SX_USDC_DECIMALS)
    salt           = random.randint(1, 2 ** 256 - 1)
    api_expiry     = int(time.time()) + _SX_API_EXPIRY_SECS
    expiry         = _SX_ON_CHAIN_EXPIRY
    mh_bytes       = bytes.fromhex(market_hash.replace("0x", ""))

    signed = Account.sign_typed_data(
        private_key=private_key,
        domain_data={"name": "SX Bet", "version": "1.0",
                     "chainId": chain_id, "verifyingContract": executor},
        message_types=_SX_ORDER_TYPES,
        message_data={"marketHash": mh_bytes, "baseToken": base_token,
                      "totalBetSize": total_bet_size, "percentageOdds": pct_odds,
                      "expiry": expiry, "salt": salt,
                      "maker": account.address, "executor": executor,
                      "isMakerBettingOutcomeOne": is_one},
    )
    raw_sig   = signed.signature.hex() if not isinstance(signed.signature, str) else signed.signature
    signature = raw_sig if raw_sig.startswith("0x") else f"0x{raw_sig}"

    return {
        "marketHash":               market_hash,
        "baseToken":                base_token,
        "totalBetSize":             str(total_bet_size),
        "percentageOdds":           str(pct_odds),
        "expiry":                   expiry,
        "apiExpiry":                api_expiry,
        "salt":                     str(salt),
        "maker":                    account.address,
        "executor":                 executor,
        "isMakerBettingOutcomeOne": is_one,
        "signature":                signature,
    }


def _sx_sign_fill(private_key: str, market_hash: str, base_token: str,
                  amount: float, desired_odds: int, is_one: bool,
                  fill_hasher: str, chain_id: int,
                  domain_version: str = "6.0") -> tuple[str, str]:
    """Sign a taker fill for POST /orders/fill/v2.

    Uses the full_message form of sign_typed_data so primaryType="Details" is
    explicit — avoids eth_account guessing the wrong root struct when two types
    are present.

    Returns (signature_hex, fill_salt_hex).
    """
    import os as _os
    from eth_account import Account
    account       = Account.from_key(private_key)
    stake_wei     = int(amount * _SX_USDC_DECIMALS)
    salt_bytes    = _os.urandom(32)
    fill_salt_int = int.from_bytes(salt_bytes, "big")
    fill_salt_hex = "0x" + salt_bytes.hex()


    full_message = {
        "types": {
            "EIP712Domain": [
                {"name": "name",              "type": "string"},
                {"name": "version",           "type": "string"},
                {"name": "chainId",           "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Details": [
                {"name": "action",         "type": "string"},
                {"name": "market",         "type": "string"},
                {"name": "betting",        "type": "string"},
                {"name": "stake",          "type": "string"},
                {"name": "worstOdds",      "type": "string"},
                {"name": "worstReturning", "type": "string"},
                {"name": "fills",          "type": "FillObject"},
            ],
            "FillObject": [
                {"name": "stakeWei",                 "type": "string"},
                {"name": "marketHash",               "type": "string"},
                {"name": "baseToken",                "type": "string"},
                {"name": "desiredOdds",              "type": "string"},
                {"name": "oddsSlippage",             "type": "uint256"},
                {"name": "isTakerBettingOutcomeOne", "type": "bool"},
                {"name": "fillSalt",                 "type": "uint256"},
                {"name": "beneficiary",              "type": "address"},
                {"name": "beneficiaryType",          "type": "uint8"},
                {"name": "cashOutTarget",            "type": "bytes32"},
            ],
        },
        "domain": {
            "name":              "SX Bet",
            "version":           domain_version,
            "chainId":           chain_id,
            "verifyingContract": fill_hasher,
        },
        "primaryType": "Details",
        "message": {
            "action":         "N/A",
            "market":         market_hash,
            "betting":        "N/A",
            "stake":          "N/A",
            "worstOdds":      "N/A",
            "worstReturning": "N/A",
            "fills": {
                "stakeWei":                 str(stake_wei),
                "marketHash":               market_hash,
                "baseToken":                base_token,
                "desiredOdds":              str(desired_odds),
                "oddsSlippage":             2,
                "isTakerBettingOutcomeOne": is_one,
                "fillSalt":                 fill_salt_int,
                "beneficiary":              "0x0000000000000000000000000000000000000000",
                "beneficiaryType":          0,
                "cashOutTarget":            "0x" + "00" * 32,
            },
        },
    }
    signed = Account.sign_typed_data(private_key=private_key, full_message=full_message)
    raw_sig = signed.signature.hex() if not isinstance(signed.signature, str) else signed.signature
    sig = raw_sig if raw_sig.startswith("0x") else f"0x{raw_sig}"
    return sig, fill_salt_hex


def sx_status(settings) -> dict:
    result: dict = {"platform": "SX Bet", "ok": False}
    pk = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    if not pk:
        result["error"] = "SX_BET_PRIVATE_KEY not set"
        return result
    try:
        from eth_account import Account
        wallet        = Account.from_key(pk).address
        result["address"] = wallet
        base_url      = settings.sx_bet.base_url
        base_token    = settings.sx_bet.base_token
        _prx          = _sx_proxies(settings)

        raw    = _sx_get(f"{base_url}/orders", params={"maker": wallet}, proxies=_prx)
        orders = raw.get("data", []) or []
        result["pending_orders"] = len(orders)
        result["orders"] = [
            {
                "hash":    o.get("orderHash", "?")[:20] + "…",
                "size":    round(int(o.get("totalBetSize", 0)) / _SX_USDC_DECIMALS, 2),
                "filled":  round(int(o.get("fillAmount", 0)) / _SX_USDC_DECIMALS, 2),
                "odds":    round(1 / (1 - int(o.get("percentageOdds", 0)) / _SX_ODDS_SCALE), 4)
                           if 0 < int(o.get("percentageOdds", 0)) < _SX_ODDS_SCALE else "?",
                "outcome_one": o.get("isMakerBettingOutcomeOne"),
            }
            for o in orders
        ]
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)
    return result


def sx_get_balance(settings) -> float | None:
    """Return SX Bet USDC balance (on-chain, SX Network) or None on error."""
    pk = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    if not pk:
        return None
    try:
        from eth_account import Account
        wallet     = Account.from_key(pk).address
        base_token = settings.sx_bet.base_token
        _prx       = _sx_proxies(settings)
        # ERC-20 balanceOf(address) — selector 0x70a08231
        data = "0x70a08231" + "000000000000000000000000" + wallet.lower().replace("0x", "")
        raw  = _sx_rpc("eth_call", [{"to": base_token, "data": data}, "latest"], proxies=_prx)
        return round(int(raw, 16) / _SX_USDC_DECIMALS, 2)
    except Exception:
        return None


def _sx_confirmed_odds(base_url: str, wallet: str, market_hash: str,
                       timeout: float = 2.0,
                       proxies: dict | None = None) -> float | None:
    """Query /trades after a taker fill to get the actual execution odds."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            r = _requests.get(
                f"{base_url}/trades",
                params={"bettor": wallet, "pageSize": 10},
                timeout=5,
                proxies=proxies,
            )
            if r.ok:
                trades_data = r.json().get("data", {}) or {}
                for t in trades_data.get("trades", []) or []:
                    if (t.get("marketHash") == market_hash
                            and t.get("tradeStatus") == "SUCCESS"):
                        pct_raw = t.get("odds")
                        if pct_raw:
                            pct = int(pct_raw) / _SX_ODDS_SCALE
                            return round(1.0 / pct, 4) if 0 < pct < 1 else None
        except Exception:
            pass
        _time.sleep(0.5)
    return None


def refresh_sx_odds_http(game: dict, settings) -> dict:
    """Return a copy of *game* with SX Bet odds refreshed via HTTP GET.

    Calls /orders/odds/best for every SX market hash present in the game dict
    and overwrites the sx_bet_*_back_odds fields with the current API values.
    For soccer per-outcome binary markets (sx_bet_{slot}_market_hash) it also
    refreshes sx_bet_{slot}_lay_odds from the "No" side's taker price.

    This is used by the autobet re-validation step so the arb calculator sees
    the same price source that sx_place_bet() will use, eliminating the gap
    between the WS-cache snapshot and the actual fill price.

    On HTTP/network error the original game is returned unchanged (fail-open).
    On "no live orders" the relevant fields are set to None so the arb
    calculator correctly treats SX as unavailable.
    """
    from matched_betting.normalization import normalize_team_name

    base_url   = settings.sx_bet.base_url
    base_token = settings.sx_bet.base_token
    proxies    = _sx_proxies(settings)
    league     = game.get("league", "")
    updated    = dict(game)

    # ── Two-way market (NBA, MLB, WNBA, KBO, NHL, mlb_totals, mls_totals) ─
    mh = game.get("sx_bet_market_hash")
    if mh:
        try:
            raw       = _sx_get(f"{base_url}/orders/odds/best",
                                params={"marketHashes": mh, "baseToken": base_token},
                                proxies=proxies)
            best_list = raw.get("data", {}).get("bestOdds", []) or []
            o1_norm   = (game.get("sx_bet_outcome_one_team") or "").lower()
            if league in ("mlb_totals", "mls_totals"):
                o1_slot, o2_slot = ("over", "under") if o1_norm == "over" else ("under", "over")
            else:
                t1_norm = normalize_team_name(game.get("team1") or "", league)
                if o1_norm and o1_norm == t1_norm:
                    o1_slot, o2_slot = "team1", "team2"
                else:
                    o1_slot, o2_slot = "team2", "team1"
            if best_list:
                taker = _sx_best_taker_odds(best_list[0])
                updated[f"sx_bet_{o1_slot}_back_odds"] = (
                    taker["outcome_one"]["decimal"] if "outcome_one" in taker else None
                )
                updated[f"sx_bet_{o2_slot}_back_odds"] = (
                    taker["outcome_two"]["decimal"] if "outcome_two" in taker else None
                )
            else:
                updated[f"sx_bet_{o1_slot}_back_odds"] = None
                updated[f"sx_bet_{o2_slot}_back_odds"] = None
        except Exception:
            pass  # network/parse error → keep WS-cache odds

    # ── Soccer per-outcome binary markets ─────────────────────────────────
    for slot, key in (
        ("team1", "sx_bet_team1_market_hash"),
        ("draw",  "sx_bet_draw_market_hash"),
        ("team2", "sx_bet_team2_market_hash"),
    ):
        h = game.get(key)
        if not h:
            continue
        try:
            raw       = _sx_get(f"{base_url}/orders/odds/best",
                                params={"marketHashes": h, "baseToken": base_token},
                                proxies=proxies)
            best_list = raw.get("data", {}).get("bestOdds", []) or []
            if best_list:
                taker = _sx_best_taker_odds(best_list[0])
                updated[f"sx_bet_{slot}_back_odds"] = (
                    taker["outcome_one"]["decimal"] if "outcome_one" in taker else None
                )
                # outcomeTwo = "No" (slot does not happen) — backing it pays out
                # identically to laying the named outcome, so feed it in as the
                # lay price (mirrors ws_sx_bet.py's update_lay_odds wiring).
                updated[f"sx_bet_{slot}_lay_odds"] = (
                    taker["outcome_two"]["decimal"] if "outcome_two" in taker else None
                )
            else:
                updated[f"sx_bet_{slot}_back_odds"] = None
                updated[f"sx_bet_{slot}_lay_odds"] = None
        except Exception:
            pass  # network/parse error → keep WS-cache odds

    return updated


def refresh_pm_odds_http(game: dict, settings) -> dict:
    """Return a copy of *game* with Polymarket back/lay odds refreshed via HTTP GET.

    Calls the CLOB /book endpoint once per polymarket_*_clob_token_id present
    in the game dict and overwrites the matching polymarket_*_back_odds and
    polymarket_*_lay_odds fields with current best-ask / best-bid prices —
    the same (1/best_ask, 1/best_bid) convention ws_polymarket.py uses to
    populate the WS cache.

    This mirrors refresh_sx_odds_http() — used by the autobet re-validation
    step so the arb calculator quotes the same price source pm_place_bet()
    will fill against, instead of the (potentially stale) WS-cache snapshot.

    On HTTP/network error the original odds for that slot are kept unchanged
    (fail-open) so a transient API hiccup doesn't block an otherwise-valid arb.
    """
    updated = dict(game)
    for slot in ("team1", "team2", "draw", "over", "under"):
        token_id = game.get(f"polymarket_{slot}_clob_token_id")
        if not token_id:
            continue
        back_odds, lay_odds = _pm_best_back_and_lay_odds(str(token_id))
        if back_odds is not None:
            updated[f"polymarket_{slot}_back_odds"] = back_odds
        if lay_odds is not None:
            updated[f"polymarket_{slot}_lay_odds"] = lay_odds
    return updated


def sx_place_bet(settings, market_hash: str, amount: float,
                 outcome: str = "one", take: bool = False,
                 dry_run: bool = False) -> dict:
    pk = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    if not pk:
        return {"platform": "SX Bet", "ok": False, "error": "SX_BET_PRIVATE_KEY not set"}
    try:
        from eth_account import Account
        base_url        = settings.sx_bet.base_url
        base_token      = settings.sx_bet.base_token
        _prx            = _sx_proxies(settings)
        executor, fill_hasher, chain_id, domain_version = _sx_metadata(base_url, proxies=_prx)

        odds_raw  = _sx_get(f"{base_url}/orders/odds/best",
                            params={"marketHashes": market_hash, "baseToken": base_token},
                            proxies=_prx)
        best_list = odds_raw.get("data", {}).get("bestOdds", []) or []
        if not best_list:
            return {"platform": "SX Bet", "ok": False, "error": "No live orders for this market"}

        taker = _sx_best_taker_odds(best_list[0])
        key   = f"outcome_{outcome}"
        if key not in taker:
            return {"platform": "SX Bet", "ok": False,
                    "error": f"No odds available for outcome_{outcome}"}

        is_one  = (outcome == "one")
        decimal = taker[key]["decimal"]

        if take:
            # Immediately fill existing maker orders via POST /orders/fill/v2.
            # desiredOdds = taker's implied probability × 10²⁰ = 10²⁰ − maker_pct_odds.
            maker_pct_odds = taker[key]["pct_odds"]
            desired_odds   = _SX_ODDS_SCALE - maker_pct_odds

            if dry_run:
                return {"platform": "SX Bet", "ok": True, "dry_run": True,
                        "market_hash": market_hash, "amount": amount,
                        "decimal_odds": decimal, "take": True}

            wallet = Account.from_key(pk).address
            sig, fill_salt_hex = _sx_sign_fill(
                pk, market_hash, base_token, amount,
                desired_odds, is_one, fill_hasher, chain_id, domain_version,
            )
            payload = {
                "market":                   market_hash,
                "baseToken":                base_token,
                "isTakerBettingOutcomeOne": is_one,
                "stakeWei":                 str(int(amount * _SX_USDC_DECIMALS)),
                "desiredOdds":              str(desired_odds),
                "oddsSlippage":             2,
                "fillSalt":                 fill_salt_hex,
                "taker":                    wallet,
                "takerSig":                 sig,
                "message":                  "N/A",
            }
            resp = _sx_post(f"{base_url}/orders/fill/v2", payload, proxies=_prx)
            execution_odds = None
            try:
                avg_raw = (resp.get("data") or {}).get("averageOdds")
                if avg_raw:
                    pct = int(avg_raw) / _SX_ODDS_SCALE
                    execution_odds = round(1.0 / pct, 4) if 0 < pct < 1 else None
            except Exception:
                pass
            if execution_odds is None:
                execution_odds = _sx_confirmed_odds(
                    base_url, wallet, market_hash, proxies=_prx
                )
            _log_bet({"platform": "SX Bet", "market_hash": market_hash,
                      "amount": amount, "outcome": outcome, "decimal_odds": decimal,
                      "execution_odds": execution_odds, "take": True, "response": resp})
            return {"platform": "SX Bet", "ok": True, "response": resp,
                    "amount": amount, "decimal_odds": decimal,
                    "execution_odds": execution_odds}

        else:
            # Post a new maker order slightly inside the current best price.
            ladder   = _sx_derive_ladder(base_url, market_hash, base_token, proxies=_prx)
            raw      = int((1 - 1.0 / (decimal * 0.99)) * _SX_ODDS_SCALE)
            pct_odds = min(ladder, key=lambda v: abs(v - raw)) if ladder else raw
            mp       = pct_odds / _SX_ODDS_SCALE
            display  = round(1.0 / (1.0 - mp), 4) if mp < 1 else decimal

            order = _sx_sign_order(pk, market_hash, base_token, amount,
                                   pct_odds, is_one, executor, chain_id)

            if dry_run:
                return {"platform": "SX Bet", "ok": True, "dry_run": True,
                        "market_hash": market_hash, "amount": amount,
                        "decimal_odds": display, "take": False}

            resp       = _sx_post(f"{base_url}/orders/new", {"orders": [order]}, proxies=_prx)
            data       = resp.get("data") or []
            order_hash = data[0].get("orderHash") if isinstance(data, list) and data else None
            _log_bet({"platform": "SX Bet", "market_hash": market_hash,
                      "amount": amount, "outcome": outcome, "decimal_odds": display,
                      "take": False, "order_hash": order_hash})
            return {"platform": "SX Bet", "ok": True,
                    "order_hash": order_hash, "response": resp,
                    "amount": amount, "decimal_odds": display}

    except Exception as e:
        return {"platform": "SX Bet", "ok": False, "error": str(e)}


def sx_cancel(settings, order_hash: str) -> None:
    pk = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    from eth_account import Account
    maker = Account.from_key(pk).address
    _prx  = _sx_proxies(settings)
    resp  = _sx_post(
        f"{settings.sx_bet.base_url}/orders/cancel/v2",
        {"orderHashes": [order_hash], "maker": maker},
        proxies=_prx,
    )
    print(f"  {resp}")


def sx_approve(settings) -> None:
    """
    Approve the SX Bet TokenTransferProxy to spend USDC.

    Tries EIP-2612 Permit first (gasless, uses DOMAIN_SEPARATOR from the token
    contract to avoid guessing domain parameters).  Falls back to a regular
    on-chain approve() transaction if the token does not support Permit.
    """
    from eth_account import Account

    pk = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    if not pk:
        raise RuntimeError("SX_BET_PRIVATE_KEY not set")

    account    = Account.from_key(pk)
    base_url   = settings.sx_bet.base_url
    base_token = settings.sx_bet.base_token   # USDC on SX Network
    _prx       = _sx_proxies(settings)

    meta  = _sx_get(f"{base_url}/metadata", proxies=_prx).get("data", {})
    proxy = meta.get("TokenTransferProxy")
    if not proxy:
        raise RuntimeError("TokenTransferProxy not in /metadata response")

    print(f"  Wallet             : {account.address}")
    print(f"  USDC               : {base_token}")
    print(f"  TokenTransferProxy : {proxy}")

    # Check if the token supports EIP-2612 by calling DOMAIN_SEPARATOR()
    domain_sep: bytes | None = None
    try:
        raw_ds = _sx_rpc("eth_call", [{"to": base_token, "data": "0x3644e515"}, "latest"],
                         proxies=_prx)
        if len(raw_ds) >= 66:   # "0x" + 64 hex chars = 32 bytes
            domain_sep = bytes.fromhex(raw_ds.replace("0x", ""))
            if int.from_bytes(domain_sep, "big") == 0:
                domain_sep = None
    except Exception:
        pass

    if domain_sep:
        print(f"  EIP-2612 Permit supported  (DOMAIN_SEPARATOR present)")
        _sx_permit_approve(pk, account, base_url, base_token, proxy, domain_sep, proxies=_prx)
    else:
        print("  EIP-2612 Permit not available — using on-chain approve()")
        _sx_onchain_approve(pk, account, base_token, proxy, proxies=_prx)


def _sx_permit_approve(pk: str, account, base_url: str, base_token: str,
                       proxy: str, domain_sep: bytes,
                       proxies: dict | None = None) -> None:
    """Sign an EIP-2612 Permit using the token's own DOMAIN_SEPARATOR."""
    from eth_hash.auto import keccak

    nonces_call = "0x7ecebe00" + "000000000000000000000000" + account.address.lower().replace("0x", "")
    raw_nonce   = _sx_rpc("eth_call", [{"to": base_token, "data": nonces_call}, "latest"],
                          proxies=proxies)
    nonce       = int(raw_nonce, 16)
    deadline    = int(time.time()) + 7200

    print(f"  Nonce: {nonce}   Deadline: {deadline}")

    def _addr(a: str) -> bytes:
        return b"\x00" * 12 + bytes.fromhex(a.lower().replace("0x", ""))

    PERMIT_TYPEHASH = keccak(
        b"Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)"
    )
    struct_hash = keccak(
        PERMIT_TYPEHASH
        + _addr(account.address)
        + _addr(proxy)
        + _SX_MAX_UINT256.to_bytes(32, "big")
        + nonce.to_bytes(32, "big")
        + deadline.to_bytes(32, "big")
    )
    digest = keccak(b"\x19\x01" + domain_sep + struct_hash)

    from eth_keys import keys as _eth_keys
    eth_key = _eth_keys.PrivateKey(bytes.fromhex(pk.replace("0x", "")))
    raw_sig = eth_key.sign_msg_hash(digest)
    v       = raw_sig.v + 27
    sig_hex = "0x" + raw_sig.r.to_bytes(32, "big").hex() + raw_sig.s.to_bytes(32, "big").hex() + bytes([v]).hex()

    payload = {
        "owner":        account.address,
        "spender":      proxy,
        "tokenAddress": base_token,
        "value":        str(_SX_MAX_UINT256),
        "deadline":     deadline,
        "signature":    sig_hex,
    }
    resp = _sx_post(f"{base_url}/orders/approve", payload, proxies=proxies)
    print(f"  Permit approved: {resp}")


def _sx_onchain_approve(pk: str, account, base_token: str, proxy: str,
                        proxies: dict | None = None) -> None:
    """Submit a regular ERC-20 approve() transaction on SX Network."""
    from eth_account import Account

    native = int(_sx_rpc("eth_getBalance", [account.address, "latest"], proxies=proxies), 16)
    print(f"  Native balance: {native / 1e18:.6f} SX")
    if native == 0:
        raise RuntimeError(
            "No SX gas tokens on SX Network. "
            "Either bridge SX to the network or place one test bet via the sx.bet UI "
            "to trigger approval automatically."
        )

    nonce     = int(_sx_rpc("eth_getTransactionCount", [account.address, "latest"],
                            proxies=proxies), 16)
    gas_price = int(int(_sx_rpc("eth_gasPrice", [], proxies=proxies), 16) * 1.2)
    proxy_pad = b"\x00" * 12 + bytes.fromhex(proxy.lower().replace("0x", ""))
    data      = "0x" + (bytes.fromhex("095ea7b3") + proxy_pad + _SX_MAX_UINT256.to_bytes(32, "big")).hex()

    tx        = {"nonce": nonce, "gasPrice": gas_price, "gas": 80_000,
                 "to": base_token, "value": 0, "data": data, "chainId": _SX_CHAIN_ID}
    signed_tx = Account.sign_transaction(tx, pk)
    tx_hash   = _sx_rpc("eth_sendRawTransaction",
                        ["0x" + signed_tx.raw_transaction.hex()], proxies=proxies)
    print(f"  tx: {tx_hash}  waiting", end="", flush=True)
    for _ in range(90):
        receipt = _sx_rpc("eth_getTransactionReceipt", [tx_hash], proxies=proxies)
        if receipt:
            if int(receipt.get("status", "0x0"), 16) == 1:
                print(" OK")
                return
            raise RuntimeError(f"Transaction {tx_hash} reverted")
        time.sleep(1)
        print(".", end="", flush=True)
    raise RuntimeError("Timed out waiting for on-chain approve transaction")


# ============================================================================
# Status display
# ============================================================================

def _fmt_status(result: dict) -> None:
    platform = result["platform"]
    bar      = "=" * 40
    print(f"\n{bar}")
    print(f"  {platform}")
    print(bar)

    if not result.get("ok"):
        print(f"  ERROR: {result.get('error', 'unknown')}")
        return

    if platform == "Polymarket":
        print(f"  Wallet : {result.get('address')}")
        matic  = result.get("matic")
        status = "OK" if matic and matic >= 0.01 else "LOW"
        print(f"  MATIC  : {matic}  ({status})")
        print(f"  USDC   : ${result.get('usdc', 0):.2f}")
        for k, v in result.get("allowances", {}).items():
            print(f"  Allowance {k}…: ${v:.2f}")
        print(f"  Open orders : {result.get('open_orders', 0)}")
        for p in result.get("positions", []):
            pnl = p["pnl"]
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            print(f"  Position: {p['outcome']}  {p['size']} shares @ {p['avg']} → {p['cur']}  PnL {pnl_str}")
            print(f"    {p['title']}")

    elif platform == "Matchbook":
        bal = result.get("balance")
        cur = result.get("currency", "GBP")
        print(f"  Balance : {cur} {bal}")
        print(f"  Exposure: {result.get('exposure')}")
        print(f"  Open offers: {result.get('open_offers', 0)}")
        for o in result.get("offers", []):
            print(
                f"  [{o['status']}] id={o['id']}  {o['runner']}  "
                f"{o['side']}  odds={o['odds']}  "
                f"stake={o['stake']}  matched={o['matched']}"
            )

    elif platform == "SX Bet":
        print(f"  Wallet  : {result.get('address')}")
        print(f"  Pending orders: {result.get('pending_orders', 0)}")
        for o in result.get("orders", []):
            print(
                f"  {o['hash']}  ${o['size']:.2f}  filled=${o['filled']:.2f}"
                f"  odds={o['odds']}  outcome_one={o['outcome_one']}"
            )


def run_status(settings) -> None:
    print("Fetching status from all platforms concurrently...")
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {
            ex.submit(pm_status, settings): "Polymarket",
            ex.submit(mb_status, settings): "Matchbook",
            ex.submit(sx_status, settings): "SX Bet",
        }
        results = {}
        for f in as_completed(futures):
            r = f.result()
            results[r["platform"]] = r

    for platform in ("Polymarket", "Matchbook", "SX Bet"):
        if platform in results:
            _fmt_status(results[platform])
    print()


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    p = argparse.ArgumentParser(
        description="Unified betting CLI — Polymarket, Matchbook, SX Bet.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument("--status",   action="store_true",
                   help="Check balances and active bets across all platforms.")
    p.add_argument("--dry-run",  action="store_true",
                   help="Build orders but do not submit them.")

    # Polymarket
    g = p.add_argument_group("Polymarket")
    g.add_argument("--pm-token-id",  default="", metavar="TOKEN_ID")
    g.add_argument("--pm-amount",    type=float, default=0, metavar="USDC")
    g.add_argument("--pm-side",      choices=["BUY", "SELL", "LAY"], default="BUY",
                   help="BUY=back (YES token), SELL=close position, "
                        "LAY=buy NO token (auto-resolved from YES token).")
    g.add_argument("--pm-approve",   action="store_true",
                   help="Approve Polymarket exchange contracts to spend USDC.")
    g.add_argument("--pm-cancel",    action="store_true",
                   help="Cancel Polymarket orders.")
    g.add_argument("--pm-order-id",  default="",
                   help="Specific order ID to cancel (use with --pm-cancel).")

    # Matchbook
    g = p.add_argument_group("Matchbook")
    g.add_argument("--mb-event-id",   type=int, default=0, metavar="EVENT_ID")
    g.add_argument("--mb-market-id",  type=int, default=0, metavar="MARKET_ID")
    g.add_argument("--mb-runner-id",  type=int, default=0, metavar="RUNNER_ID")
    g.add_argument("--mb-stake",      type=float, default=0, metavar="AMOUNT")
    g.add_argument("--mb-odds",       type=float, default=0,
                   help="Decimal odds (uses best available if omitted).")
    g.add_argument("--mb-side",       choices=["back", "lay"], default="back")
    g.add_argument("--mb-cancel-offer", type=int, default=0, metavar="OFFER_ID")

    # SX Bet
    g = p.add_argument_group("SX Bet")
    g.add_argument("--sx-market-hash", default="", metavar="HASH")
    g.add_argument("--sx-amount",      type=float, default=0, metavar="USDC")
    g.add_argument("--sx-outcome",     choices=["one", "two"], default="one")
    g.add_argument("--sx-take",        action="store_true",
                   help="Take existing odds (immediate fill).")
    g.add_argument("--sx-approve",      action="store_true",
                   help="Approve TokenTransferProxy to spend USDC (run once before filling).")
    g.add_argument("--sx-cancel",      default="", metavar="ORDER_HASH")
    g.add_argument("--sx-orders",      action="store_true",
                   help="List pending SX Bet orders.")

    args     = p.parse_args()
    settings = load_settings(_ROOT)

    # ── Proxy setup (standalone use) ─────────────────────────────────────
    # When bet.py is run directly (not imported via scan.py), scan.py's proxy
    # patching has not run yet.  py_clob_client_v2 uses an httpx.Client singleton
    # created at import time — immune to HTTPS_PROXY env vars — so we patch it here.
    _proxy = settings.vpn_proxy_url
    if _proxy:
        os.environ.setdefault("HTTP_PROXY",  _proxy)
        os.environ.setdefault("HTTPS_PROXY", _proxy)
        _hx_proxy = _proxy.replace("socks5h://", "socks5://")
        try:
            import httpx as _httpx
            import py_clob_client_v2.http_helpers.helpers as _pm_helpers
            _pm_helpers._http_client = _httpx.Client(http2=True, proxy=_hx_proxy)
        except Exception:
            pass

    # ── Status ───────────────────────────────────────────────────────────
    if args.status:
        run_status(settings)
        return

    # ── Polymarket one-off commands ───────────────────────────────────────
    if args.pm_approve:
        print("Approving Polymarket contracts...")
        pm_approve(settings)
        return

    if args.pm_cancel:
        print("Cancelling Polymarket orders...")
        pm_cancel(settings, args.pm_order_id)
        return

    # ── Matchbook one-off commands ────────────────────────────────────────
    if args.mb_cancel_offer:
        print(f"Cancelling Matchbook offer {args.mb_cancel_offer}...")
        mb_cancel_offer(settings, args.mb_cancel_offer)
        return

    # ── SX Bet one-off commands ───────────────────────────────────────────
    if args.sx_approve:
        print("Approving SX Bet TokenTransferProxy...")
        sx_approve(settings)
        return

    if args.sx_cancel:
        print(f"Cancelling SX Bet order {args.sx_cancel}...")
        sx_cancel(settings, args.sx_cancel)
        return

    if args.sx_orders:
        r = sx_status(settings)
        _fmt_status(r)
        return

    # ── Matchbook event browser (no market/runner = show event) ──────────
    if args.mb_event_id and not (args.mb_market_id and args.mb_runner_id):
        mb_show_event(settings, args.mb_event_id)
        return

    # ── Build list of bets to place ───────────────────────────────────────
    bets: list[tuple[str, callable, dict]] = []

    if args.pm_token_id and args.pm_amount > 0:
        pm_token_id = args.pm_token_id
        pm_side     = args.pm_side

        if args.pm_side == "LAY":
            # Resolve the NO token from the supplied YES token via Gamma API.
            # Buying the NO token is equivalent to laying the outcome:
            #   NO pays $1 if outcome does NOT happen → covers draw + any other result.
            print(f"  Resolving NO token for YES token {pm_token_id}...")
            try:
                pm_token_id = _pm_resolve_no_token(settings, pm_token_id)
                print(f"  YES token : {args.pm_token_id}")
                print(f"  NO token  : {pm_token_id}  (will BUY this)")
            except Exception as exc:
                sys.exit(f"ERROR: cannot resolve NO token: {exc}")
            pm_side = "BUY"

        bets.append(("Polymarket", pm_place_bet, {
            "settings": settings,
            "token_id": pm_token_id,
            "amount":   args.pm_amount,
            "side":     pm_side,
            "dry_run":  args.dry_run,
        }))

    if args.mb_event_id and args.mb_market_id and args.mb_runner_id and args.mb_stake > 0:
        bets.append(("Matchbook", mb_place_bet, {
            "settings":  settings,
            "event_id":  args.mb_event_id,
            "market_id": args.mb_market_id,
            "runner_id": args.mb_runner_id,
            "stake":     args.mb_stake,
            "side":      args.mb_side,
            "odds":      args.mb_odds,
            "dry_run":   args.dry_run,
        }))

    if args.sx_market_hash and args.sx_amount > 0:
        bets.append(("SX Bet", sx_place_bet, {
            "settings":    settings,
            "market_hash": args.sx_market_hash,
            "amount":      args.sx_amount,
            "outcome":     args.sx_outcome,
            "take":        args.sx_take,
            "dry_run":     args.dry_run,
        }))

    if not bets:
        p.print_help()
        return

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"Placing {len(bets)} bet(s) [{mode}]" +
          (" concurrently..." if len(bets) > 1 else "..."))

    if len(bets) == 1:
        name, fn, kwargs = bets[0]
        result = fn(**kwargs)
        print(f"\n{name}: {'OK' if result.get('ok') else 'FAILED'}")
        print(json.dumps(result, indent=2, default=str))
    else:
        with ThreadPoolExecutor(max_workers=len(bets)) as ex:
            futures = {ex.submit(fn, **kwargs): name for name, fn, kwargs in bets}
            for f in as_completed(futures):
                name   = futures[f]
                result = f.result()
                print(f"\n{name}: {'OK' if result.get('ok') else 'FAILED'}")
                print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
