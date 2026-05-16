"""
portfolio.py
------------
Portfolio monitor for the matched-betting pipeline.

Shows wallet balances and active bets / open orders across Matchbook,
Polymarket, and SX Bet in a single view.  All three platforms are queried
concurrently so the total wait time equals the slowest platform.

Usage:
    python portfolio.py                  # summary — balances + counts
    python portfolio.py --detail         # summary + every active bet printed
    python portfolio.py --matchbook      # Matchbook only (implies --detail)
    python portfolio.py --polymarket     # Polymarket only (implies --detail)
    python portfolio.py --sx-bet         # SX Bet only (implies --detail)

    python portfolio.py --cancel-mb OFFER_ID
    python portfolio.py --cancel-pm                   # cancel ALL open PM orders
    python portfolio.py --cancel-pm --order-id ID     # cancel one PM order
    python portfolio.py --sell-pm TOKEN_ID --sell-amount SHARES   # close a position
    python portfolio.py --sell-pm TOKEN_ID --sell-amount SHARES --dry-run
    python portfolio.py --cancel-sx ORDER_HASH
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests as _requests

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient

# ── Optional deps ─────────────────────────────────────────────────────────────

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        AssetType,
        BalanceAllowanceParams,
        MarketOrderArgs,
        OpenOrderParams,
        OrderType,
        TradeParams,
    )
    from py_clob_client.order_builder.constants import SELL as _PM_SELL
    _PM_AVAILABLE = True
except ImportError:
    _PM_AVAILABLE = False

try:
    from eth_account import Account as _EthAccount
    _ETH_AVAILABLE = True
except ImportError:
    _ETH_AVAILABLE = False

# ── Constants ─────────────────────────────────────────────────────────────────

_PM_CLOB_HOST = "https://clob.polymarket.com"
_PM_DATA_API  = "https://data-api.polymarket.com"
_PM_CHAIN_ID  = 137
_PM_RPCS      = [
    "https://polygon.drpc.org",
    "https://polygon.meowrpc.com",
    "https://endpoints.omniatech.io/v1/matic/mainnet/public",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
    "https://polygon-rpc.com",
]

# Polymarket V2 collateral: pUSD (ERC-20 backed 1:1 by USDC, 6 decimals)
_PM_PUSD_CONTRACT  = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
# Native USDC on Polygon (Circle issuance — needs wrapping to pUSD via onramp)
_PM_USDC_NATIVE    = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"

# CTF ERC1155 contract — holds Polymarket conditional tokens on Polygon.
# SELL orders require setApprovalForAll(operator, true) from the token holder;
# without it the CTF Exchange cannot transfer tokens and rejects the order.
_PM_CTF_CONTRACT     = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
_PM_CTF_OPERATORS    = [
    "0xE111180000d2663C0091e4f400237545B87B996B",  # CTF Exchange V2
    "0xe2222d279d744050d28e00520010520000310F59",  # Neg Risk CTF Exchange V2
]
_PM_IS_APPROVED_SEL  = bytes.fromhex("e985e9c5")  # isApprovedForAll(address,address)
_PM_SET_APPROVAL_SEL = bytes.fromhex("a22cb465")  # setApprovalForAll(address,bool)
_PM_BALANCE_OF_SEL   = bytes.fromhex("70a08231")  # balanceOf(address)

_SX_ODDS_SCALE    = 10 ** 20
_SX_USDC_DECIMALS = 1_000_000



# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def _term_width() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 80


def _hr(char: str = "━") -> str:
    return char * _term_width()


def _section(title: str) -> None:
    print(f"\n  {title}")
    print(f"  {'─' * (len(title) + 2)}")


def _pm_rpc(method: str, params: list, rpc_list: list[str]) -> Any:
    last: Exception | None = None
    for url in rpc_list:
        try:
            r = _requests.post(
                url,
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                timeout=10,
            )
            r.raise_for_status()
            result = r.json()
            if "error" in result:
                raise RuntimeError(result["error"])
            return result["result"]
        except Exception as exc:
            last = exc
    raise RuntimeError(f"All Polygon RPCs failed. Last: {last}")


def _pm_erc20_balance(token: str, address: str, rpc_list: list[str]) -> float:
    """Return an ERC-20 token balance assuming 6 decimals (USDC / pUSD)."""
    padded = bytes.fromhex("000000000000000000000000" + address.lower().replace("0x", ""))
    calldata = "0x" + (_PM_BALANCE_OF_SEL + padded).hex()
    raw = _pm_rpc("eth_call", [{"to": token, "data": calldata}, "latest"], rpc_list)
    return int(raw, 16) / 1e6


def _pm_ensure_ctf_approval(private_key: str, address: str, rpc_list: list[str]) -> None:
    """Submit setApprovalForAll on the CTF contract for each exchange operator if not already set.

    Required before the first SELL order — BUY orders use a USDC allowance; SELL orders
    need a separate ERC1155 setApprovalForAll so the CTF Exchange can transfer tokens.
    Each approval is a one-time on-chain transaction (~0.001 MATIC gas).
    """
    import time as _time
    from eth_account import Account as _EthAcct

    nonce     = int(_pm_rpc("eth_getTransactionCount", [address, "latest"], rpc_list), 16)
    gas_price = int(int(_pm_rpc("eth_gasPrice", [], rpc_list), 16) * 1.2)
    offset    = 0

    for operator in _PM_CTF_OPERATORS:
        owner_pad    = bytes.fromhex("000000000000000000000000" + address.lower().replace("0x", ""))
        operator_pad = bytes.fromhex("000000000000000000000000" + operator.lower().replace("0x", ""))
        call_data    = "0x" + (_PM_IS_APPROVED_SEL + owner_pad + operator_pad).hex()
        result       = _pm_rpc("eth_call", [{"to": _PM_CTF_CONTRACT, "data": call_data}, "latest"], rpc_list)
        if int(result, 16) != 0:
            continue  # already approved for this operator

        true_pad = (1).to_bytes(32, "big")
        tx_data  = "0x" + (_PM_SET_APPROVAL_SEL + operator_pad + true_pad).hex()
        tx = {
            "nonce":    nonce + offset,
            "gasPrice": gas_price,
            "gas":      100_000,
            "to":       _PM_CTF_CONTRACT,
            "value":    0,
            "data":     tx_data,
            "chainId":  _PM_CHAIN_ID,
        }
        signed  = _EthAcct.sign_transaction(tx, private_key)
        tx_hash = _pm_rpc("eth_sendRawTransaction",
                          ["0x" + signed.raw_transaction.hex()], rpc_list)
        print(f"  Approving CTF Exchange …{operator[-8:]} to transfer tokens")
        print(f"  tx: {tx_hash}")
        print("    waiting", end="", flush=True)
        for _ in range(90):
            receipt = _pm_rpc("eth_getTransactionReceipt", [tx_hash], rpc_list)
            if receipt:
                if int(receipt.get("status", "0x0"), 16) == 1:
                    print(" ✓")
                    break
                raise RuntimeError(f"setApprovalForAll reverted: {tx_hash}")
            _time.sleep(1)
            print(".", end="", flush=True)
        else:
            raise RuntimeError(f"setApprovalForAll not confirmed after 90s: {tx_hash}")
        offset += 1


# ─────────────────────────────────────────────────────────────────────────────
# Matchbook
# ─────────────────────────────────────────────────────────────────────────────

def fetch_matchbook(settings) -> dict:
    result: dict = {"platform": "Matchbook", "ok": False}
    mb = settings.matchbook
    if not (mb.username and mb.password):
        result["error"] = "MATCHBOOK_USERNAME / MATCHBOOK_PASSWORD not set"
        return result
    try:
        http  = HttpClient()
        token = _mb_login(http, mb.base_url, mb.username, mb.password)

        # Account balance, exposure, free funds
        try:
            acc = http.get_json(
                f"{mb.base_url}/edge/rest/account",
                headers={"session-token": token, "Accept": "application/json"},
            )
            result["balance"]    = acc.get("balance")
            result["currency"]   = acc.get("currency", "GBP")
            result["exposure"]   = acc.get("exposure")
            result["free_funds"] = acc.get("free-funds")
        except Exception as exc:
            result["balance_error"] = str(exc)

        # All current offers — group by status client-side
        raw_offers = _mb_fetch_offers(http, mb.base_url, token)
        result["open_offers"]    = [o for o in raw_offers if o["status"] == "open"]
        result["matched_offers"] = [o for o in raw_offers if o["status"] == "matched"]
        result["settled_offers"] = [o for o in raw_offers if o["status"] == "settled"]

        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _mb_login(http: HttpClient, base_url: str, username: str, password: str) -> str:
    resp  = http.post_json(
        f"{base_url}/bpapi/rest/security/session",
        payload={"username": username, "password": password},
        headers={"Accept": "application/json"},
    )
    token = resp.get("session-token")
    if not token:
        raise RuntimeError(f"Login failed: {resp}")
    return str(token)


def _mb_fetch_offers(http: HttpClient, base_url: str, token: str, per_page: int = 50) -> list[dict]:
    resp   = http.get_json(
        f"{base_url}/edge/rest/offers",
        params={"offset": 0, "per-page": per_page},
        headers={"session-token": token, "Accept": "application/json"},
    )
    return [
        {
            "id":        o.get("id"),
            "event":     o.get("event-name", "?"),
            "runner":    o.get("runner-name", "?"),
            "side":      o.get("side"),
            "odds":      o.get("odds"),
            "stake":     o.get("stake"),
            "matched":   o.get("matched-stake", 0),
            "remaining": o.get("remaining-stake", 0),
            "status":    o.get("status"),
            "currency":  o.get("currency", "GBP"),
            "created":   _fmt_ts(o.get("created-at")),
        }
        for o in resp.get("offers", [])
    ]


def cancel_matchbook_offer(settings, offer_id: int) -> None:
    mb    = settings.matchbook
    http  = HttpClient()
    token = _mb_login(http, mb.base_url, mb.username, mb.password)
    resp  = http.request_json(
        "DELETE",
        f"{mb.base_url}/edge/rest/offers/{offer_id}",
        headers={"session-token": token, "Accept": "application/json"},
    )
    print(f"  Cancel response: {resp}")


# ─────────────────────────────────────────────────────────────────────────────
# Polymarket
# ─────────────────────────────────────────────────────────────────────────────

def fetch_polymarket(settings) -> dict:
    result: dict = {"platform": "Polymarket", "ok": False}
    if not _PM_AVAILABLE:
        result["error"] = "py-clob-client not installed  (pip install py-clob-client)"
        return result
    pk = settings.polymarket.private_key
    if not pk:
        result["error"] = "POLYMARKET_PRIVATE_KEY not set"
        return result
    try:
        client  = ClobClient(host=_PM_CLOB_HOST, key=pk, chain_id=_PM_CHAIN_ID)
        client.set_api_creds(client.create_or_derive_api_creds())
        address = client.signer.address()
        result["address"] = address

        # MATIC gas balance
        rpc_list = (
            [settings.polymarket.polygon_rpc_url] + _PM_RPCS
            if settings.polymarket.polygon_rpc_url else _PM_RPCS
        )
        try:
            hex_bal        = _pm_rpc("eth_getBalance", [address, "latest"], rpc_list)
            result["matic"] = round(int(hex_bal, 16) / 1e18, 4)
        except Exception as exc:
            result["matic_error"] = str(exc)

        # pUSD balance — on-chain (primary, Polymarket V2 collateral)
        try:
            result["pusd"] = round(_pm_erc20_balance(_PM_PUSD_CONTRACT, address, rpc_list), 2)
        except Exception as exc:
            result["pusd_error"] = str(exc)

        # Native USDC wallet balance — shown alongside pUSD so user can see
        # unwrapped USDC that still needs wrapping via CollateralOnramp
        try:
            result["usdc_native"] = round(_pm_erc20_balance(_PM_USDC_NATIVE, address, rpc_list), 2)
        except Exception as exc:
            result["usdc_native_error"] = str(exc)

        # CLOB-reported balance (legacy path; may lag or return 0 if CLOB hasn't synced)
        try:
            client.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            bal            = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
            result["usdc"] = round(float(bal.get("balance", 0)) / 1_000_000, 2)
        except Exception as exc:
            result["usdc_error"] = str(exc)

        # Open (unmatched) orders
        try:
            orders = client.get_orders(OpenOrderParams()) or []
            result["open_orders"] = [
                {
                    "id":           o.get("id", "?"),
                    "side":         (o.get("side") or "?").upper(),
                    "token":        o.get("asset_id", "?"),
                    "price":        o.get("price", "?"),
                    "size_total":   float(o.get("original_size", 0)),
                    "size_left":    float(o.get("size_remaining", 0)),
                    "size_matched": float(o.get("size_matched", 0)),
                    "status":       o.get("status", "?"),
                    "created":      (o.get("created_at") or "")[:19].replace("T", " "),
                }
                for o in orders
            ]
        except Exception as exc:
            result["open_orders_error"] = str(exc)
            result["open_orders"]       = []

        # Recent matched trades (last 20).
        # The CLOB API uses "match_time" for the trade timestamp, not "created_at".
        try:
            trades = client.get_trades(TradeParams(maker_address=address)) or []
            result["recent_trades"] = [
                {
                    "side":    (t.get("side") or "?").upper(),
                    "token":   t.get("asset_id", "?"),
                    "price":   t.get("price", "?"),
                    "size":    float(t.get("size", 0)),
                    "status":  t.get("status", "?"),
                    "created": _fmt_ts(t.get("match_time") or t.get("created_at")),
                }
                for t in trades[:20]
            ]
        except Exception as exc:
            result["recent_trades_error"] = str(exc)
            result["recent_trades"]       = []

        # Build token → earliest BUY timestamp from recent trades so positions
        # can show when they were opened (the Data API /positions has no timestamp).
        _buy_time: dict[str, str] = {}
        for t in reversed(result.get("recent_trades", [])):
            if t.get("side", "").upper() == "BUY" and t.get("token") and t.get("created"):
                _buy_time[t["token"]] = t["created"]

        # Positions (Data API — no auth required)
        try:
            resp = _requests.get(
                f"{_PM_DATA_API}/positions",
                params={"user": address, "sizeThreshold": "0.01"},
                timeout=15,
            )
            positions = resp.json() if resp.ok else []
            result["positions"] = [
                {
                    "title":    (p.get("title") or "")[:70],
                    "outcome":  p.get("outcome", "?"),
                    "size":     round(float(p.get("size", 0)), 2),
                    "avg":      round(float(p.get("avgPrice", 0)), 4),
                    "cur":      round(float(p.get("curPrice", 0)), 4),
                    "value":    round(float(p.get("size", 0)) * float(p.get("curPrice", 0)), 2),
                    "pnl":      round(
                        (float(p.get("curPrice", 0)) - float(p.get("avgPrice", 0)))
                        * float(p.get("size", 0)), 2
                    ),
                    "token_id": p.get("asset", ""),
                    "created":  _buy_time.get(p.get("asset", ""), ""),
                }
                for p in positions
            ]
        except Exception as exc:
            result["positions_error"] = str(exc)
            result["positions"]       = []

        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def cancel_polymarket_orders(settings, order_id: str = "") -> None:
    if not _PM_AVAILABLE:
        sys.exit("ERROR: py-clob-client not installed")
    pk = settings.polymarket.private_key
    if not pk:
        sys.exit("ERROR: POLYMARKET_PRIVATE_KEY not set")
    client = ClobClient(host=_PM_CLOB_HOST, key=pk, chain_id=_PM_CHAIN_ID)
    client.set_api_creds(client.create_or_derive_api_creds())
    if order_id:
        resp = client.cancel(order_id)
    else:
        orders = client.get_orders(OpenOrderParams()) or []
        if not orders:
            print("  No open Polymarket orders to cancel.")
            return
        print(f"  Cancelling {len(orders)} open order(s)...")
        resp = client.cancel_all()
    print(json.dumps(resp, indent=2, default=str))


def sell_polymarket_position(settings, token_id: str, amount: float,
                             dry_run: bool = False) -> None:
    """Sell (close) a Polymarket position by submitting a market SELL order.

    token_id — the CLOB token ID shown under 'Active positions' (--polymarket)
    amount   — number of shares to sell (shown as 'shares' in the position list)
    """
    if not _PM_AVAILABLE:
        sys.exit("ERROR: py-clob-client not installed")
    pk = settings.polymarket.private_key
    if not pk:
        sys.exit("ERROR: POLYMARKET_PRIVATE_KEY not set")
    client = ClobClient(host=_PM_CLOB_HOST, key=pk, chain_id=_PM_CHAIN_ID)
    client.set_api_creds(client.create_or_derive_api_creds())
    from eth_account import Account as _EthAcct
    address  = _EthAcct.from_key(pk).address
    rpc_list = (
        [settings.polymarket.polygon_rpc_url] + _PM_RPCS
        if settings.polymarket.polygon_rpc_url else _PM_RPCS
    )
    if dry_run:
        print(f"  [DRY RUN] Would sell {amount} shares of token …{token_id[-20:]}")
        return
    # Ensure the CTF Exchange has setApprovalForAll before posting the SELL order.
    # This is a one-time on-chain step (~0.001 MATIC). Subsequent sells skip it.
    _pm_ensure_ctf_approval(pk, address, rpc_list)
    order = client.create_market_order(
        MarketOrderArgs(token_id=token_id, amount=amount, side=_PM_SELL)
    )
    resp = client.post_order(order, OrderType.FOK)
    print(json.dumps(resp, indent=2, default=str))


# ─────────────────────────────────────────────────────────────────────────────
# SX Bet
# ─────────────────────────────────────────────────────────────────────────────

def fetch_sx_bet(settings) -> dict:
    result: dict = {"platform": "SX Bet", "ok": False}
    pk = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    if not pk:
        result["error"] = "SX_BET_PRIVATE_KEY (or POLYMARKET_PRIVATE_KEY) not set"
        return result
    if not _ETH_AVAILABLE:
        result["error"] = "eth-account not installed  (pip install eth-account)"
        return result
    try:
        wallet   = _EthAccount.from_key(pk).address
        base_url = settings.sx_bet.base_url
        result["address"] = wallet

        # On-chain USDC balance via SX Network block explorer
        try:
            result["usdc"] = round(
                _sx_usdc_balance(wallet, settings.sx_bet.base_token, settings.sx_bet.explorer_url), 2
            )
        except Exception as exc:
            result["usdc_error"] = str(exc)

        # Open maker orders — only ACTIVE (unmatched / partially matched).
        # INACTIVE orders are fully filled and appear in /trades instead.
        raw    = _requests.get(f"{base_url}/orders", params={"maker": wallet}, timeout=15)
        raw.raise_for_status()
        orders = raw.json().get("data", []) or []
        result["open_orders"] = [
            {
                "hash":        o.get("orderHash", "?"),
                "market":      o.get("marketHash", "?"),
                "size":        round(int(o.get("totalBetSize",  0)) / _SX_USDC_DECIMALS, 2),
                "filled":      round(int(o.get("fillAmount",    0)) / _SX_USDC_DECIMALS, 2),
                "outcome_one": _sx_bool(o.get("isMakerBettingOutcomeOne")),
                "odds":        _sx_maker_decimal_odds(o.get("percentageOdds")),
                "status":      o.get("orderStatus", "?"),
                "placed_at":   _fmt_ts(o.get("createdAt")),
            }
            for o in orders
            if o.get("orderStatus") == "ACTIVE"
        ]

        # Filled trades — "bettor" covers both maker and taker fills.
        # Response structure: {"data": {"trades": [...], "nextKey": ..., "count": ...}}
        # Note: this is nested differently from /orders which returns {"data": [...]}.
        try:
            trades_raw = _requests.get(
                f"{base_url}/trades",
                params={"bettor": wallet, "pageSize": 100},
                timeout=15,
            )
            trades_raw.raise_for_status()
            trades_data = trades_raw.json().get("data", {}) or {}
            trades      = trades_data.get("trades", []) or []
            all_fills = []
            for t in trades:
                if t.get("tradeStatus") != "SUCCESS":
                    continue
                maker_field = t.get("maker")
                if isinstance(maker_field, bool):
                    is_maker = maker_field
                else:
                    is_maker = str(maker_field or "").lower() == wallet.lower()
                all_fills.append({
                    "hash":               t.get("fillHash",           "?"),
                    "order":              t.get("orderHash",          "?"),
                    "market":             t.get("marketHash",         "?"),
                    "stake":              round(float(t.get("betTimeValue") or t.get("normalizedStake") or 0), 2),
                    "odds":               _sx_trade_decimal_odds(t.get("odds"), is_maker),
                    "maker":              is_maker,
                    "settled":            t.get("settled",    False),
                    "status":             t.get("tradeStatus",       "?"),
                    "betting_outcome_one": _sx_trade_bet_one(t.get("bettingOutcomeOne"), is_maker),
                    "placed_at":          _fmt_ts(t.get("betTime"), unix=True),
                })
            # Split: in-play = matched but result not yet declared
            result["in_play_trades"] = [t for t in all_fills if not t["settled"]]
            result["settled_trades"] = [t for t in all_fills if     t["settled"]][:10]
        except Exception as exc:
            result["trades_error"]   = str(exc)
            result["in_play_trades"] = []
            result["settled_trades"] = []

        # Enrich orders and trades with human-readable event details.
        all_hashes = list({
            o["market"] for o in result["open_orders"]
        } | {
            t["market"] for t in result["in_play_trades"]
        } | {
            t["market"] for t in result["settled_trades"]
        } - {"?"})
        if all_hashes:
            market_info = _sx_fetch_markets(base_url, all_hashes)
            for o in result["open_orders"]:
                o["event"] = market_info.get(o["market"], {})
            for t in result["in_play_trades"] + result["settled_trades"]:
                t["event"] = market_info.get(t["market"], {})

        result["ok"] = True
    except Exception as exc:
        result["error"] = str(exc)
    return result


def _sx_fetch_markets(base_url: str, hashes: list[str]) -> dict[str, dict]:
    """Batch-lookup market details from GET /markets/find (max 30 per request)."""
    result: dict[str, dict] = {}
    for i in range(0, len(hashes), 30):
        batch = hashes[i : i + 30]
        try:
            r = _requests.get(
                f"{base_url}/markets/find",
                params={"marketHashes": ",".join(batch)},
                timeout=15,
            )
            if not r.ok:
                continue
            for m in r.json().get("data", []) or []:
                h = m.get("marketHash")
                if not h:
                    continue
                game_time = m.get("gameTime")
                try:
                    dt_str = datetime.fromtimestamp(game_time, tz=timezone.utc).strftime(
                        "%d %b %H:%M UTC"
                    )
                except Exception:
                    dt_str = None
                result[h] = {
                    "team1":    m.get("teamOneName")    or "?",
                    "team2":    m.get("teamTwoName")    or "?",
                    "league":   m.get("leagueLabel")    or "?",
                    "sport":    m.get("sportLabel")     or "?",
                    "outcome1": m.get("outcomeOneName") or "outcomeOne",
                    "outcome2": m.get("outcomeTwoName") or "outcomeTwo",
                    "status":   m.get("status")         or "?",
                    "dt":       dt_str,
                }
        except Exception:
            pass
    return result


def _sx_usdc_balance(wallet: str, usdc_contract: str, explorer_url: str) -> float:
    """Query USDC balance via the SX Network block explorer tokenbalance API."""
    r = _requests.get(
        explorer_url,
        params={
            "module":          "account",
            "action":          "tokenbalance",
            "contractaddress": usdc_contract,
            "address":         wallet,
        },
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "1":
        raise RuntimeError(f"Explorer API error: {data.get('message', data)}")
    return int(data["result"]) / _SX_USDC_DECIMALS


def _sx_bool(value: Any) -> bool | None:
    """Coerce an SX Bet boolean field that may arrive as a JSON bool or the string 'true'/'false'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


def _fmt_ts(value: Any, unix: bool = False) -> str:
    """Format a timestamp for display.  unix=True if value is a UNIX epoch integer."""
    if value is None or value == "":
        return ""
    try:
        if unix:
            dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%d %b %H:%M UTC")
    except Exception:
        return ""


def _sx_decimal_odds(pct_odds_raw: Any) -> str:
    """Taker's decimal odds given the maker's percentageOdds: 1 / (1 - pct)."""
    if pct_odds_raw is None:
        return "?"
    try:
        pct = int(pct_odds_raw) / _SX_ODDS_SCALE
        if 0 < pct < 1:
            return f"{1.0 / (1.0 - pct):.4f}"
    except Exception:
        pass
    return "?"


def _sx_maker_decimal_odds(pct_odds_raw: Any) -> str:
    """Maker's own decimal odds given percentageOdds: 1 / pct."""
    if pct_odds_raw is None:
        return "?"
    try:
        pct = int(pct_odds_raw) / _SX_ODDS_SCALE
        if 0 < pct < 1:
            return f"{1.0 / pct:.4f}"
    except Exception:
        pass
    return "?"


def _sx_trade_decimal_odds(pct_odds_raw: Any, is_maker: bool) -> str:
    # The trades API returns odds as the bettor's own win probability (*10^20),
    # so 1/pct is always the correct decimal odds regardless of maker/taker role.
    return _sx_maker_decimal_odds(pct_odds_raw)


def _sx_trade_bet_one(raw_value: Any, is_maker: bool) -> bool | None:
    # The trades API returns bettingOutcomeOne from the queried bettor's perspective,
    # so no flip is needed for takers.
    return _sx_bool(raw_value)


def cancel_sx_order(settings, order_hash: str) -> None:
    pk = os.getenv("SX_BET_PRIVATE_KEY") or settings.polymarket.private_key
    if not pk or not _ETH_AVAILABLE:
        sys.exit("ERROR: eth-account not installed or private key not set")
    maker = _EthAccount.from_key(pk).address
    resp  = _requests.post(
        f"{settings.sx_bet.base_url}/orders/cancel/v2",
        json={"orderHashes": [order_hash], "maker": maker},
        timeout=15,
    )
    print(f"  {resp.json()}")


# ─────────────────────────────────────────────────────────────────────────────
# Display
# ─────────────────────────────────────────────────────────────────────────────

def _print_matchbook(data: dict, detail: bool) -> None:
    print(_hr())
    print("  Matchbook")
    print(_hr("─"))

    if not data.get("ok"):
        print(f"  ERROR: {data.get('error', 'unknown')}")
        return

    cur = data.get("currency", "GBP")
    print(f"  Balance    : {cur} {data.get('balance', '?')}")
    if data.get("exposure") is not None:
        print(f"  Exposure   : {cur} {data['exposure']}")
    if data.get("free_funds") is not None:
        print(f"  Free funds : {cur} {data['free_funds']}")
    if "balance_error" in data:
        print(f"  Balance    : (error: {data['balance_error']})")

    open_offers    = data.get("open_offers",    [])
    matched_offers = data.get("matched_offers", [])
    settled_offers = data.get("settled_offers", [])
    print(f"  Open (unmatched/partial) : {len(open_offers)}")
    print(f"  Matched (awaiting result): {len(matched_offers)}")
    print(f"  Settled (recent)         : {len(settled_offers)}")

    if not detail:
        return

    if open_offers:
        _section("Open offers  (unmatched / partially matched)")
        for o in open_offers:
            fill_pct = ""
            try:
                if float(o["stake"]) > 0:
                    fill_pct = f"  ({100 * float(o['matched']) / float(o['stake']):.0f}% matched)"
            except (TypeError, ValueError):
                pass
            created_str = f"  placed {o['created']}" if o.get("created") else ""
            print(
                f"    id={o['id']}  {o['runner']}"
                f"  {o['side']}  odds={o['odds']}"
                f"  stake={cur} {o['stake']}"
                f"  matched={cur} {o['matched']}"
                f"{fill_pct}{created_str}"
            )
            if o.get("event") and o["event"] != "?":
                print(f"      event: {o['event']}")

    if matched_offers:
        _section("Matched bets  (fully matched — awaiting settlement)")
        for o in matched_offers:
            created_str = f"  placed {o['created']}" if o.get("created") else ""
            print(
                f"    id={o['id']}  {o['runner']}"
                f"  {o['side']}  odds={o['odds']}"
                f"  stake={cur} {o['stake']}"
                f"  matched={cur} {o['matched']}"
                f"{created_str}"
            )
            if o.get("event") and o["event"] != "?":
                print(f"      event: {o['event']}")

    if settled_offers:
        _section("Recently settled bets")
        for o in settled_offers:
            created_str = f"  placed {o['created']}" if o.get("created") else ""
            print(
                f"    id={o['id']}  {o['runner']}"
                f"  {o['side']}  odds={o['odds']}"
                f"  stake={cur} {o['stake']}"
                f"  matched={cur} {o['matched']}"
                f"{created_str}"
            )


def _print_polymarket(data: dict, detail: bool) -> None:
    print(_hr())
    print("  Polymarket")
    print(_hr("─"))

    if not data.get("ok"):
        print(f"  ERROR: {data.get('error', 'unknown')}")
        return

    print(f"  Wallet     : {data.get('address', '?')}")

    if "matic" in data:
        matic_status = "OK" if data["matic"] >= 0.01 else "LOW — top up for gas"
        print(f"  MATIC      : {data['matic']}  ({matic_status})")
    elif "matic_error" in data:
        print(f"  MATIC      : (error fetching: {data['matic_error']})")

    if "pusd" in data:
        print(f"  pUSD       : ${data['pusd']:.2f}  (trading balance, on-chain)")
    elif "pusd_error" in data:
        print(f"  pUSD       : (error: {data['pusd_error']})")

    if "usdc_native" in data and data["usdc_native"] > 0:
        print(f"  USDC (native): ${data['usdc_native']:.2f}  (wallet — needs wrapping to pUSD to trade)")
    elif "usdc_native_error" in data:
        print(f"  USDC (native): (error: {data['usdc_native_error']})")

    if "usdc" in data:
        print(f"  pUSD (CLOB): ${data['usdc']:.2f}  (CLOB-reported)")
    elif "usdc_error" in data:
        print(f"  pUSD (CLOB): (error: {data['usdc_error']})")

    open_orders = data.get("open_orders", [])
    positions   = data.get("positions", [])
    trades      = data.get("recent_trades", [])
    total_pnl   = sum(p["pnl"]   for p in positions)
    total_value = sum(p["value"] for p in positions)
    pnl_str = f"+${total_pnl:.2f}" if total_pnl >= 0 else f"-${abs(total_pnl):.2f}"

    print(f"  Open orders : {len(open_orders)}")
    print(f"  Positions   : {len(positions)}  (value ${total_value:.2f}  PnL {pnl_str})")
    print(f"  Recent trades: {len(trades)}")

    if "open_orders_error" in data:
        print(f"  (open orders error: {data['open_orders_error']})")
    if "positions_error" in data:
        print(f"  (positions error: {data['positions_error']})")

    if not detail:
        return

    if open_orders:
        _section("Open orders  (unmatched)")
        for o in open_orders:
            fill_pct = ""
            if o["size_total"] > 0:
                fill_pct = f"  ({100 * o['size_matched'] / o['size_total']:.0f}% matched)"
            print(
                f"    [{o['status']}] {o['side']}"
                f"  ${o['size_left']:.2f}/${o['size_total']:.2f} USDC"
                f"  @ {o['price']}"
                f"  token=…{str(o['token'])[-12:]}"
                f"  {o['created']}"
                f"{fill_pct}"
            )
            print(f"      id: {o['id']}")

    if positions:
        _section("In-play positions  (matched — awaiting result  |  --sell-pm TOKEN to close)")
        for p in positions:
            pnl_str     = f"+${p['pnl']:.2f}" if p["pnl"] >= 0 else f"-${abs(p['pnl']):.2f}"
            created_str = f"  placed {p['created']}" if p.get("created") else ""
            print(
                f"    {p['outcome']:<8}  {p['size']:.2f} shares"
                f"  avg={p['avg']:.4f}  cur={p['cur']:.4f}"
                f"  value=${p['value']:.2f}  PnL {pnl_str}{created_str}"
            )
            print(f"      {p['title']}")
            if p.get("token_id"):
                print(f"      token: {p['token_id']}")

    if trades:
        _section("Recent trades  (last 20 matched)")
        for t in trades:
            print(
                f"    [{t['status']}] {t['side']}"
                f"  ${t['size']:.2f} USDC"
                f"  @ {t['price']}"
                f"  token=…{str(t['token'])[-12:]}"
                f"  {t['created']}"
            )


def _print_sx_bet(data: dict, detail: bool) -> None:
    print(_hr())
    print("  SX Bet")
    print(_hr("─"))

    if not data.get("ok"):
        print(f"  ERROR: {data.get('error', 'unknown')}")
        return

    open_orders    = data.get("open_orders",   [])
    in_play_trades = data.get("in_play_trades", [])
    settled_trades = data.get("settled_trades", [])
    filled_total   = sum(o["filled"]             for o in open_orders)
    unfilled_total = sum(o["size"] - o["filled"] for o in open_orders)
    in_play_stake  = sum(t["stake"]              for t in in_play_trades)

    print(f"  Wallet         : {data.get('address', '?')}")

    if "usdc" in data:
        print(f"  USDC balance   : ${data['usdc']:.2f}  (on-chain, SX Network)")
    elif "usdc_error" in data:
        print(f"  USDC balance   : (error: {data['usdc_error']})")

    print(f"  Open orders    : {len(open_orders)}")
    if open_orders:
        print(f"    Filled        : ${filled_total:.2f} USDC")
        print(f"    Unfilled      : ${unfilled_total:.2f} USDC")
    print(f"  In-play bets   : {len(in_play_trades)}  (matched — awaiting result"
          + (f", ${in_play_stake:.2f} staked)" if in_play_trades else ")"))
    print(f"  Recently settled: {len(settled_trades)}")
    if "trades_error" in data:
        print(f"  (trades fetch error: {data['trades_error']})")

    if not detail:
        return

    if open_orders:
        _section("Open maker orders  (unmatched / partially matched)")
        for o in open_orders:
            ev  = o.get("event", {})
            fill_pct = ""
            if o["size"] > 0:
                fill_pct = f"  ({100 * o['filled'] / o['size']:.0f}% filled)"
            outcome_name = ev.get("outcome1", "outcomeOne") if o["outcome_one"] \
                           else ev.get("outcome2", "outcomeTwo")
            placed_str = f"  placed {o['placed_at']}" if o.get("placed_at") else ""
            print(
                f"    ${o['filled']:.2f}/${o['size']:.2f} USDC"
                f"  odds={o['odds']}"
                f"  {outcome_name}  (maker)"
                f"{fill_pct}{placed_str}"
            )
            if ev.get("team1"):
                time_str = f"  {ev['dt']}" if ev.get("dt") else ""
                print(f"      {ev['team1']} vs {ev['team2']}  [{ev['league']}]{time_str}")
            print(f"      order: …{str(o['hash'])[-24:]}")

    if in_play_trades:
        _section("In-play bets  (matched — awaiting result)")
        for t in in_play_trades:
            ev   = t.get("event", {})
            role = "maker" if t.get("maker") else "taker"
            bet_one = t.get("betting_outcome_one")
            if bet_one is not None:
                outcome_name = ev.get("outcome1", "outcomeOne") if bet_one else ev.get("outcome2", "outcomeTwo")
            else:
                outcome_name = ""
            placed_str = f"  placed {t['placed_at']}" if t.get("placed_at") else ""
            print(
                f"    ${t['stake']:.2f} USDC"
                f"  odds={t['odds']}"
                + (f"  {outcome_name}" if outcome_name else "")
                + f"  ({role}){placed_str}"
            )
            if ev.get("team1"):
                time_str = f"  {ev['dt']}" if ev.get("dt") else ""
                status_tag = f"  [{ev['status']}]" if ev.get("status") else ""
                print(f"      {ev['team1']} vs {ev['team2']}  [{ev['league']}]{time_str}{status_tag}")
            print(f"      fill: …{str(t['hash'])[-24:]}")

    if settled_trades:
        _section("Recently settled bets  (last 10)")
        for t in settled_trades:
            ev   = t.get("event", {})
            role = "maker" if t.get("maker") else "taker"
            bet_one = t.get("betting_outcome_one")
            if bet_one is not None:
                outcome_name = ev.get("outcome1", "outcomeOne") if bet_one else ev.get("outcome2", "outcomeTwo")
            else:
                outcome_name = ""
            placed_str = f"  placed {t['placed_at']}" if t.get("placed_at") else ""
            print(
                f"    ${t['stake']:.2f} USDC"
                f"  odds={t['odds']}"
                + (f"  {outcome_name}" if outcome_name else "")
                + f"  ({role}){placed_str}"
            )
            if ev.get("team1"):
                print(f"      {ev['team1']} vs {ev['team2']}  [{ev['league']}]")
            print(f"      fill: …{str(t['hash'])[-24:]}")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Portfolio monitor — balances and active bets across all platforms.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    parser.add_argument(
        "--detail",
        action="store_true",
        help="Print full bet/order/position lists under the summary.",
    )
    parser.add_argument("--matchbook",  action="store_true", help="Matchbook only (implies --detail).")
    parser.add_argument("--polymarket", action="store_true", help="Polymarket only (implies --detail).")
    parser.add_argument("--sx-bet",     action="store_true", help="SX Bet only (implies --detail).")

    g = parser.add_argument_group("Cancel / sell actions")
    g.add_argument(
        "--cancel-mb",
        type=int,
        default=0,
        metavar="OFFER_ID",
        help="Cancel a Matchbook offer by ID.",
    )
    g.add_argument(
        "--cancel-pm",
        action="store_true",
        help="Cancel Polymarket orders (all open, unless --order-id is given).",
    )
    g.add_argument(
        "--order-id",
        default="",
        metavar="ORDER_ID",
        help="Specific Polymarket order ID to cancel (use with --cancel-pm).",
    )
    g.add_argument(
        "--sell-pm",
        default="",
        metavar="TOKEN_ID",
        help="Sell (close) a Polymarket position — pass the token ID shown under 'Active positions'.",
    )
    g.add_argument(
        "--sell-amount",
        type=float,
        default=0.0,
        metavar="SHARES",
        help="Number of shares to sell (required with --sell-pm).",
    )
    g.add_argument(
        "--dry-run",
        action="store_true",
        help="With --sell-pm: build the order but do not submit it.",
    )
    g.add_argument(
        "--cancel-sx",
        default="",
        metavar="ORDER_HASH",
        help="Cancel an SX Bet maker order by hash.",
    )

    args     = parser.parse_args()
    settings = load_settings(_ROOT)

    # ── Cancel actions (no status fetch needed) ───────────────────────────
    if args.cancel_mb:
        print(f"Cancelling Matchbook offer {args.cancel_mb}...")
        cancel_matchbook_offer(settings, args.cancel_mb)
        return

    if args.cancel_pm:
        label = f"order {args.order_id}" if args.order_id else "all open orders"
        print(f"Cancelling Polymarket {label}...")
        cancel_polymarket_orders(settings, args.order_id)
        return

    if args.sell_pm:
        if args.sell_amount <= 0:
            print("ERROR: --sell-amount SHARES is required with --sell-pm and must be > 0.",
                  file=sys.stderr)
            sys.exit(1)
        mode = "DRY RUN — " if args.dry_run else ""
        print(f"Selling {args.sell_amount} shares of token …{args.sell_pm[-20:]}  [{mode}Polymarket]...")
        sell_polymarket_position(settings, args.sell_pm, args.sell_amount, dry_run=args.dry_run)
        return

    if args.cancel_sx:
        print(f"Cancelling SX Bet order {args.cancel_sx[:20]}...")
        cancel_sx_order(settings, args.cancel_sx)
        return

    # ── Determine which platforms to query ────────────────────────────────
    platform_flags = {
        "matchbook":  args.matchbook,
        "polymarket": args.polymarket,
        "sx_bet":     args.sx_bet,
    }
    show_all = not any(platform_flags.values())
    detail   = args.detail or any(platform_flags.values())

    platforms_to_fetch = (
        list(platform_flags.keys()) if show_all
        else [k for k, v in platform_flags.items() if v]
    )

    fetchers = {
        "matchbook":  fetch_matchbook,
        "polymarket": fetch_polymarket,
        "sx_bet":     fetch_sx_bet,
    }
    printers = {
        "matchbook":  _print_matchbook,
        "polymarket": _print_polymarket,
        "sx_bet":     _print_sx_bet,
    }

    # ── Header ────────────────────────────────────────────────────────────
    print(_hr())
    print(f"  Portfolio  {_utc_now()}")
    print(_hr())

    # ── Parallel fetch ────────────────────────────────────────────────────
    results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(platforms_to_fetch)) as ex:
        futures = {ex.submit(fetchers[p], settings): p for p in platforms_to_fetch}
        for f in as_completed(futures):
            p = futures[f]
            try:
                results[p] = f.result()
            except Exception as exc:
                results[p] = {"platform": p, "ok": False, "error": str(exc)}

    # ── Print in fixed order ──────────────────────────────────────────────
    for p in ["matchbook", "polymarket", "sx_bet"]:
        if p in results:
            printers[p](results[p], detail=detail)

    print(_hr())
    print()


if __name__ == "__main__":
    main()
