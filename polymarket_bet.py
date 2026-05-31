"""
polymarket_bet.py
-----------------
Place a Fill-or-Kill market order on Polymarket via the CLOB API.

Requires:
    POLYMARKET_PRIVATE_KEY set in .env  (your Polygon wallet private key, 0x...)
    pip install py-clob-client

Usage:
    python polymarket_bet.py --token-id <clob_token_id> --amount <usdc>
    python polymarket_bet.py --token-id abc123 --amount 25.0
    python polymarket_bet.py --token-id abc123 --amount 25.0 --dry-run

The CLOB token ID for each outcome is stored in active_game_ids.json under:
    polymarket_team1_clob_token_id
    polymarket_draw_clob_token_id
    polymarket_team2_clob_token_id
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests as _requests

# Session for Polygon RPC calls — bypasses HTTPS_PROXY so on-chain calls go direct.
# Polygon RPC endpoints are public blockchain infrastructure with no geo-restrictions.
_rpc_session = _requests.Session()
_rpc_session.trust_env = False

_PROJECT_ROOT = Path(__file__).resolve().parent
_SRC_DIR = _PROJECT_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from matched_betting.config import load_settings

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
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:
    print(
        "ERROR: py-clob-client is not installed.\n"
        "Run:  pip install py-clob-client",
        file=sys.stderr,
    )
    sys.exit(1)

_CLOB_HOST = "https://clob.polymarket.com"
_DATA_API = "https://data-api.polymarket.com"
_POLYGON_CHAIN_ID = 137
_POLYGON_RPCS_FALLBACK = [
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
_PUSD_CONTRACT  = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
# Native USDC on Polygon (Circle's issuance — must be wrapped to pUSD via onramp)
_USDC_NATIVE    = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
# USDC.e (legacy bridged USDC — wrap to pUSD via CollateralOnramp.wrap())
_USDC_LEGACY    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Kept as alias so approve_usdc still refers to the right collateral (pUSD)
_USDC_CONTRACT  = _PUSD_CONTRACT
# CollateralOnramp: call wrap(amount) to convert USDC.e → pUSD
_ONRAMP_CONTRACT = "0x93070a847efEf7F70739046A929D47a521F5B8ee"

# V2 exchange contracts that must be approved to spend pUSD
_SPENDER_CONTRACTS = [
    "0xE111180000d2663C0091e4f400237545B87B996B",  # CTF Exchange V2
    "0xe2222d279d744050d28e00520010520000310F59",  # Neg Risk CTF Exchange V2
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",  # Neg Risk Adapter (unchanged)
]

_MAX_UINT256 = 2 ** 256 - 1
_APPROVE_SELECTOR   = bytes.fromhex("095ea7b3")  # keccak256("approve(address,uint256)")[:4]
_BALANCE_OF_SELECTOR = bytes.fromhex("70a08231")  # keccak256("balanceOf(address)")[:4]


# ---------------------------------------------------------------------------
# Client setup
# ---------------------------------------------------------------------------

def _build_client(private_key: str, settings=None) -> ClobClient:
    """Initialise a ClobClient and attach L2 API credentials derived from the private key."""
    client = ClobClient(
        host=_CLOB_HOST,
        key=private_key,
        chain_id=_POLYGON_CHAIN_ID,
    )
    # Derives L2 API keys from the L1 private key (idempotent — safe on every run).
    api_creds = client.create_or_derive_api_creds()
    client.set_api_creds(api_creds)
    return client


# ---------------------------------------------------------------------------
# On-chain approval helpers
# ---------------------------------------------------------------------------

def _rpc(method: str, params: list, rpc_list: list[str] | None = None) -> object:
    endpoints = rpc_list if rpc_list is not None else _POLYGON_RPCS_FALLBACK
    last_exc: Exception | None = None
    for rpc_url in endpoints:
        try:
            resp = _rpc_session.post(
                rpc_url,
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                timeout=15,
            )
            resp.raise_for_status()
            result = resp.json()
            if "error" in result:
                raise RuntimeError(result["error"])
            return result["result"]
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(
        f"All Polygon RPC endpoints failed. Last error: {last_exc}\n"
        "  Tip: set POLYGON_RPC_URL=<your Alchemy/QuickNode URL> in .env for a reliable private endpoint."
    )


_MATIC_MIN_FOR_GAS = 0.01  # ~3 approve txns at typical Polygon gas prices


def _get_matic_balance(address: str, rpc_list: list[str] | None = None) -> float:
    """Return MATIC balance in human-readable units (not wei)."""
    hex_balance = _rpc("eth_getBalance", [address, "latest"], rpc_list)
    return int(hex_balance, 16) / 1e18


def _get_erc20_balance(token: str, address: str, rpc_list: list[str] | None = None) -> float:
    """Return an ERC-20 token balance assuming 6 decimals (USDC / pUSD)."""
    padded = bytes.fromhex("000000000000000000000000" + address.lower().replace("0x", ""))
    calldata = "0x" + (_BALANCE_OF_SELECTOR + padded).hex()
    raw = _rpc("eth_call", [{"to": token, "data": calldata}, "latest"], rpc_list)
    return int(raw, 16) / 1e6


def _build_approve_calldata(spender: str) -> str:
    spender_padded = bytes.fromhex("000000000000000000000000" + spender.lower().replace("0x", ""))
    amount_padded = _MAX_UINT256.to_bytes(32, "big")
    return "0x" + (_APPROVE_SELECTOR + spender_padded + amount_padded).hex()


def _wait_for_receipt(tx_hash: str, timeout: int = 90, rpc_list: list[str] | None = None) -> None:
    print("    waiting", end="", flush=True)
    for _ in range(timeout):
        receipt = _rpc("eth_getTransactionReceipt", [tx_hash], rpc_list)
        if receipt:
            status = int(receipt.get("status", "0x0"), 16)
            if status == 1:
                print(" OK")
                return
            raise RuntimeError(f"Transaction {tx_hash} reverted on-chain")
        time.sleep(1)
        print(".", end="", flush=True)
    raise RuntimeError(f"Transaction {tx_hash} not confirmed after {timeout}s")


def approve_usdc(private_key: str, polygon_rpc_url: str | None = None) -> None:
    """Send approve(spender, maxUint256) on USDC for each Polymarket exchange contract."""
    from eth_account import Account

    rpc_list = ([polygon_rpc_url] + _POLYGON_RPCS_FALLBACK) if polygon_rpc_url else _POLYGON_RPCS_FALLBACK

    account = Account.from_key(private_key)
    address = account.address

    matic = _get_matic_balance(address, rpc_list)
    print(f"  MATIC balance  : {matic:.4f}")
    if matic < _MATIC_MIN_FOR_GAS:
        raise RuntimeError(
            f"Insufficient MATIC for gas: {matic:.4f} MATIC in wallet.\n"
            f"  You need at least {_MATIC_MIN_FOR_GAS} MATIC to cover 3 approval transactions.\n"
            f"  Buy MATIC on an exchange and send it to: {address}"
        )

    nonce = int(_rpc("eth_getTransactionCount", [address, "latest"], rpc_list), 16)
    gas_price = int(int(_rpc("eth_gasPrice", [], rpc_list), 16) * 1.2)  # +20% buffer

    for i, spender in enumerate(_SPENDER_CONTRACTS):
        calldata = _build_approve_calldata(spender)
        tx = {
            "nonce": nonce + i,
            "gasPrice": gas_price,
            "gas": 100_000,
            "to": _USDC_CONTRACT,
            "value": 0,
            "data": calldata,
            "chainId": _POLYGON_CHAIN_ID,
        }
        signed = Account.sign_transaction(tx, private_key)
        tx_hash = _rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()], rpc_list)
        print(f"  Approving {spender[:10]}…  tx: {tx_hash}")
        _wait_for_receipt(tx_hash, rpc_list=rpc_list)

    print("\n  All pUSD approvals confirmed. Run --check to verify, then place your order.")


# ---------------------------------------------------------------------------
# pUSD wrapping via CollateralOnramp  (USDC → pUSD)
# ---------------------------------------------------------------------------

_WRAP_SELECTOR = bytes.fromhex("62355638")  # wrap(address,address,uint256)


def wrap_usdc_to_pusd(
    private_key: str,
    amount_usdc: float,
    polygon_rpc_url: str | None = None,
    native: bool = False,
) -> None:
    """Wrap USDC (native or USDC.e) into pUSD via Polymarket's CollateralOnramp.

    Steps:
      1. Approve CollateralOnramp to spend the source USDC token
      2. Call CollateralOnramp.wrap(asset, to, amount) → receive equivalent pUSD

    Args:
        native: if True, wrap native USDC (0x3c49…); otherwise wrap USDC.e (0x2791…).
    """
    from eth_account import Account

    rpc_list   = ([polygon_rpc_url] + _POLYGON_RPCS_FALLBACK) if polygon_rpc_url else _POLYGON_RPCS_FALLBACK
    account    = Account.from_key(private_key)
    address    = account.address
    usdc_token = _USDC_NATIVE if native else _USDC_LEGACY
    label      = "USDC (native)" if native else "USDC.e"

    amount_raw = int(amount_usdc * 1_000_000)  # both tokens have 6 decimals

    usdc_bal = _get_erc20_balance(usdc_token, address, rpc_list)
    print(f"  {label} balance: {usdc_bal:.6f}")
    if usdc_bal < amount_usdc - 1e-6:
        raise RuntimeError(
            f"Insufficient {label}: have {usdc_bal:.6f}, need {amount_usdc:.6f}"
        )

    matic = _get_matic_balance(address, rpc_list)
    print(f"  MATIC balance  : {matic:.4f}")
    if matic < _MATIC_MIN_FOR_GAS:
        raise RuntimeError(
            f"Insufficient MATIC for gas: {matic:.4f}  (need >= {_MATIC_MIN_FOR_GAS})"
        )

    nonce     = int(_rpc("eth_getTransactionCount", [address, "latest"], rpc_list), 16)
    gas_price = int(int(_rpc("eth_gasPrice", [], rpc_list), 16) * 1.2)

    # Step 1 — approve CollateralOnramp to spend the source USDC token
    approve_calldata = _build_approve_calldata(_ONRAMP_CONTRACT)
    tx_approve = {
        "nonce": nonce, "gasPrice": gas_price, "gas": 100_000,
        "to": usdc_token, "value": 0, "data": approve_calldata,
        "chainId": _POLYGON_CHAIN_ID,
    }
    signed  = Account.sign_transaction(tx_approve, private_key)
    tx_hash = _rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()], rpc_list)
    print(f"  Approving CollateralOnramp to spend {label}...  tx: {tx_hash}")
    _wait_for_receipt(tx_hash, rpc_list=rpc_list)

    # Step 2 — call wrap(asset, to, amount)
    asset_padded  = bytes.fromhex("000000000000000000000000" + usdc_token.lower().replace("0x", ""))
    to_padded     = bytes.fromhex("000000000000000000000000" + address.lower().replace("0x", ""))
    amount_padded = amount_raw.to_bytes(32, "big")
    wrap_calldata = "0x" + (_WRAP_SELECTOR + asset_padded + to_padded + amount_padded).hex()
    tx_wrap = {
        "nonce": nonce + 1, "gasPrice": gas_price, "gas": 220_000,
        "to": _ONRAMP_CONTRACT, "value": 0, "data": wrap_calldata,
        "chainId": _POLYGON_CHAIN_ID,
    }
    signed  = Account.sign_transaction(tx_wrap, private_key)
    tx_hash = _rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()], rpc_list)
    print(f"  Wrapping {amount_usdc:.6f} {label} -> pUSD...  tx: {tx_hash}")
    _wait_for_receipt(tx_hash, rpc_list=rpc_list)

    pusd_bal = _get_erc20_balance(_PUSD_CONTRACT, address, rpc_list)
    print(f"\n  Done. pUSD balance: {pusd_bal:.6f}")
    print("  Run --approve to approve V2 exchange contracts to spend pUSD, then place orders.")


def withdraw_via_bridge(
    private_key: str,
    amount_pusd: float,
    polygon_rpc_url: str | None = None,
    native: bool = False,
) -> None:
    """Withdraw pUSD via Polymarket's bridge service, receiving USDC on Polygon.

    The CollateralOnramp contract does not expose a user-callable unwrap function.
    Withdrawals go through Polymarket's bridge API instead:

      1. POST https://bridge.polymarket.com/withdraw → get a one-time EVM deposit address
      2. Transfer pUSD to that deposit address (standard ERC-20 transfer)
      3. Bridge sends USDC.e (or native USDC) to your wallet on Polygon

    Once you have USDC on Polygon, bridge to SX Network via https://sx.bet/wallet/bridge

    Args:
        native: if True, request native USDC (0x3c49…) back;
                otherwise request USDC.e (0x2791…, legacy bridged — default).
    """
    from eth_account import Account

    rpc_list   = ([polygon_rpc_url] + _POLYGON_RPCS_FALLBACK) if polygon_rpc_url else _POLYGON_RPCS_FALLBACK
    account    = Account.from_key(private_key)
    address    = account.address
    dest_token = _USDC_NATIVE if native else _USDC_LEGACY
    dest_label = "USDC (native)" if native else "USDC.e"

    # Pre-flight checks
    pusd_bal = _get_erc20_balance(_PUSD_CONTRACT, address, rpc_list)
    print(f"  pUSD balance   : {pusd_bal:.6f}")
    if pusd_bal < amount_pusd - 1e-6:
        raise RuntimeError(
            f"Insufficient pUSD: have {pusd_bal:.6f}, need {amount_pusd:.6f}"
        )
    matic = _get_matic_balance(address, rpc_list)
    print(f"  MATIC balance  : {matic:.4f}")
    if matic < _MATIC_MIN_FOR_GAS:
        raise RuntimeError(
            f"Insufficient MATIC for gas: {matic:.4f}  (need >= {_MATIC_MIN_FOR_GAS})"
        )

    # Step 1 — ask the bridge for a deposit address
    bridge_session = _requests.Session()
    bridge_session.trust_env = False  # bypass VPN proxy — bridge API is not geo-blocked
    print(f"  Requesting bridge deposit address...")
    resp = bridge_session.post(
        "https://bridge.polymarket.com/withdraw",
        json={
            "address":        address,
            "toChainId":      "137",       # Polygon — receive USDC on same chain
            "toTokenAddress": dest_token,
            "recipientAddr":  address,     # return funds to the same wallet
        },
        timeout=15,
    )
    resp.raise_for_status()
    bridge_data = resp.json()
    deposit_addr = bridge_data.get("address", {}).get("evm")
    if not deposit_addr:
        raise RuntimeError(
            f"Bridge did not return an EVM deposit address. Response: {bridge_data}"
        )
    print(f"  Deposit address: {deposit_addr}")

    # Step 2 — transfer pUSD to the bridge deposit address
    amount_raw    = int(amount_pusd * 1_000_000)
    transfer_sel  = bytes.fromhex("a9059cbb")  # transfer(address,uint256)
    to_padded     = bytes.fromhex("000000000000000000000000" + deposit_addr.lower().replace("0x", ""))
    amount_padded = amount_raw.to_bytes(32, "big")
    calldata      = "0x" + (transfer_sel + to_padded + amount_padded).hex()

    nonce     = int(_rpc("eth_getTransactionCount", [address, "latest"], rpc_list), 16)
    gas_price = int(int(_rpc("eth_gasPrice", [], rpc_list), 16) * 1.2)
    tx = {
        "nonce": nonce, "gasPrice": gas_price, "gas": 100_000,
        "to": _PUSD_CONTRACT, "value": 0, "data": calldata,
        "chainId": _POLYGON_CHAIN_ID,
    }
    signed  = Account.sign_transaction(tx, private_key)
    tx_hash = _rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()], rpc_list)
    print(f"  Sending {amount_pusd:.6f} pUSD to bridge...  tx: {tx_hash}")
    _wait_for_receipt(tx_hash, rpc_list=rpc_list)

    print(f"\n  Done. pUSD sent -- {dest_label} will arrive at {address} shortly.")
    print(f"  Check with: python polymarket_bet.py --check")
    print(f"  Then bridge to SX Network via: https://sx.bet/wallet/bridge")


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_market_order(
    client: ClobClient,
    token_id: str,
    amount_usdc: float,
    dry_run: bool = False,
    side: str = BUY,
) -> dict:
    """Build and post a FOK market order for `amount_usdc` USDC on `token_id`.

    Returns the raw response dict from the CLOB API, or a dry-run summary.
    """
    # Sync the CLOB server's view of the on-chain USDC balance/allowance.
    # Without this, the server may cache a stale zero balance and reject the order.
    client.update_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    )

    order = client.create_market_order(
        MarketOrderArgs(token_id=token_id, amount=amount_usdc, side=side)
    )

    if dry_run:
        return {
            "dry_run": True,
            "token_id": token_id,
            "amount_usdc": amount_usdc,
            "side": side,
            "order": str(order),
        }

    return client.post_order(order, OrderType.FOK)


# ---------------------------------------------------------------------------
# Bet / position lookup
# ---------------------------------------------------------------------------

def show_bets(client: ClobClient, address: str) -> None:
    """Print open orders, recent trades, and current positions for the wallet."""

    # ── 1. Open orders (placed but not yet matched) ───────────────────────
    print("Open orders (unmatched):")
    try:
        orders = client.get_orders(OpenOrderParams())
        if not orders:
            print("  (none)")
        for o in orders:
            side       = o.get("side", "?").upper()
            token_id   = o.get("asset_id", "?")
            price      = o.get("price", "?")
            size_total = float(o.get("original_size", 0))
            size_match = float(o.get("size_matched", 0))
            size_left  = float(o.get("size_remaining", 0))
            status     = o.get("status", "?")
            created    = o.get("created_at", "")[:19].replace("T", " ")
            print(
                f"  [{status}] {side} {size_left:.2f}/${size_total:.2f} USDC"
                f"  @ {price}  token=…{token_id[-12:]}  {created}"
            )
    except Exception as exc:
        print(f"  (could not fetch orders: {exc})")

    # ── 2. Recent trades (matched bets) ──────────────────────────────────
    print("\nRecent trades (last 20 matched):")
    try:
        trades = client.get_trades(TradeParams(maker_address=address))
        if not trades:
            print("  (none)")
        for t in (trades[:20]):
            side     = t.get("side", "?").upper()
            token_id = t.get("asset_id", "?")
            price    = t.get("price", "?")
            size     = float(t.get("size", 0))
            status   = t.get("status", "?")
            created  = t.get("created_at", "")[:19].replace("T", " ")
            print(
                f"  [{status}] {side} {size:.2f} USDC"
                f"  @ {price}  token=…{token_id[-12:]}  {created}"
            )
    except Exception as exc:
        print(f"  (could not fetch trades: {exc})")

    # ── 3. Current positions (Data API — no auth required) ────────────────
    print("\nCurrent positions:")
    try:
        resp = _requests.get(
            f"{_DATA_API}/positions",
            params={"user": address, "sizeThreshold": "0.01"},
            timeout=15,
        )
        resp.raise_for_status()
        positions = resp.json()
        if not positions:
            print("  (none)")
        for p in positions:
            title   = p.get("title") or p.get("market", "?")
            outcome = p.get("outcome", "?")
            size    = float(p.get("size", 0))
            avg     = float(p.get("avgPrice", 0))
            cur     = float(p.get("curPrice", 0))
            value   = size * cur
            pnl     = (cur - avg) * size
            pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
            print(
                f"  {outcome:<6}  {size:.2f} shares @ avg {avg:.3f}"
                f"  cur {cur:.3f}  value ${value:.2f}  PnL {pnl_str}"
            )
            print(f"    {title[:80]}")
    except Exception as exc:
        print(f"  (could not fetch positions: {exc})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Place a Polymarket CLOB market order (Fill or Kill)."
    )
    parser.add_argument(
        "--token-id",
        required=False,
        default="",
        metavar="CLOB_TOKEN_ID",
        help=(
            "CLOB token ID for the outcome to back. "
            "Find this in active_game_ids.json (polymarket_team1/draw/team2_clob_token_id) "
            "or in scan.py --show-odds output."
        ),
    )
    parser.add_argument(
        "--amount",
        type=float,
        required=False,
        default=0,
        metavar="USDC",
        help="Amount of USDC to stake.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build and sign the order but do not submit it to the CLOB.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Show the wallet address and USDC balance/allowance, then exit.",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "One-time setup: approve the V2 exchange contracts to spend pUSD. "
            "Run after --wrap (or after receiving pUSD), before placing any orders."
        ),
    )
    parser.add_argument(
        "--wrap",
        type=float,
        default=0.0,
        metavar="AMOUNT",
        help=(
            "Wrap USDC into pUSD via Polymarket's CollateralOnramp. "
            "Wraps USDC.e by default; add --wrap-native to wrap native USDC instead. "
            "Example: --wrap 10.0  or  --wrap 10.0 --wrap-native. "
            "Run --approve afterwards to allow the exchange to spend pUSD."
        ),
    )
    parser.add_argument(
        "--wrap-native",
        action="store_true",
        help=(
            "Use with --wrap: wrap native USDC (0x3c49…, Circle's Polygon issuance) "
            "instead of the default USDC.e (0x2791…, legacy bridged)."
        ),
    )
    parser.add_argument(
        "--unwrap",
        type=float,
        default=0.0,
        metavar="AMOUNT",
        help=(
            "Withdraw pUSD back to USDC via Polymarket's bridge service. "
            "Sends pUSD to a one-time bridge deposit address; you receive USDC on Polygon. "
            "Produces USDC.e by default; add --unwrap-native to receive native USDC instead. "
            "Example: --unwrap 50.0  (then bridge to SX Network via https://sx.bet/wallet/bridge)"
        ),
    )
    parser.add_argument(
        "--unwrap-native",
        action="store_true",
        help=(
            "Use with --unwrap: receive native USDC (0x3c49…) "
            "instead of the default USDC.e (0x2791…, legacy bridged)."
        ),
    )
    parser.add_argument(
        "--bets",
        action="store_true",
        help="Show open orders, recent trades, and current positions for your wallet.",
    )
    parser.add_argument(
        "--side",
        choices=["BUY", "SELL"],
        default="BUY",
        help="Order side: BUY (default) to back an outcome, SELL to close a position.",
    )
    parser.add_argument(
        "--cancel",
        action="store_true",
        help="Cancel open orders. Cancels all orders unless --order-id is also provided.",
    )
    parser.add_argument(
        "--order-id",
        default="",
        metavar="ORDER_ID",
        help="Order ID to cancel (use with --cancel). If omitted, all open orders are cancelled.",
    )
    args = parser.parse_args()

    if not args.check and not args.approve and not args.bets and not args.cancel and args.wrap <= 0 and args.unwrap <= 0 and args.amount <= 0:
        print("ERROR: --amount must be a positive number.", file=sys.stderr)
        sys.exit(1)

    settings = load_settings(_PROJECT_ROOT)

    # Route all Polymarket API calls through the VPN proxy when set.
    # py_clob_client uses an httpx.Client singleton created at import time — it
    # is immune to HTTPS_PROXY env vars set afterward.  We must patch it directly.
    # (The env vars still help any requests-based calls in this module.)
    _proxy = settings.vpn_proxy_url
    if _proxy:
        os.environ.setdefault("HTTP_PROXY",  _proxy)
        os.environ.setdefault("HTTPS_PROXY", _proxy)
        # httpx requires socks5:// (not socks5h://); it always resolves hostnames via proxy.
        _hx_proxy = _proxy.replace("socks5h://", "socks5://")
        try:
            import httpx as _httpx
            import py_clob_client.http_helpers.helpers as _pm_v1_helpers
            _pm_v1_helpers._http_client = _httpx.Client(http2=True, proxy=_hx_proxy)
            print(f"[proxy] routing through {_proxy}  (httpx singleton patched)")
        except Exception as _patch_err:
            print(f"[proxy] WARNING: could not patch py_clob_client httpx client: {_patch_err}",
                  file=sys.stderr)

    private_key = settings.polymarket.private_key
    if not private_key:
        print(
            "ERROR: POLYMARKET_PRIVATE_KEY is not set in .env\n"
            "Add your Polygon wallet private key (0x...) to .env and retry.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.wrap > 0:
        src = "USDC (native)" if args.wrap_native else "USDC.e"
        print(f"Wrapping {args.wrap} {src} -> pUSD via Polymarket CollateralOnramp...")
        print("(You need a small amount of MATIC in your wallet for gas)\n")
        rpc_url = settings.polymarket.polygon_rpc_url
        try:
            wrap_usdc_to_pusd(private_key, args.wrap, polygon_rpc_url=rpc_url, native=args.wrap_native)
        except Exception as exc:
            print(f"\nERROR: wrap failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.unwrap > 0:
        dst = "USDC (native)" if args.unwrap_native else "USDC.e"
        print(f"Withdrawing {args.unwrap} pUSD -> {dst} via Polymarket bridge...")
        print("(You need a small amount of MATIC in your wallet for gas)\n")
        rpc_url = settings.polymarket.polygon_rpc_url
        try:
            withdraw_via_bridge(private_key, args.unwrap, polygon_rpc_url=rpc_url, native=args.unwrap_native)
        except Exception as exc:
            print(f"\nERROR: withdraw failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    if args.approve:
        print("Approving V2 exchange contracts to spend pUSD...")
        print("(You need a small amount of MATIC in your wallet for gas)\n")
        rpc_url = settings.polymarket.polygon_rpc_url
        print(f"  RPC URL: {rpc_url or '(none — using public fallbacks)'}")
        try:
            approve_usdc(private_key, polygon_rpc_url=rpc_url)
        except Exception as exc:
            print(f"\nERROR: approval failed: {exc}", file=sys.stderr)
            sys.exit(1)
        return

    print("Connecting to CLOB... ", end="", flush=True)
    try:
        client = _build_client(private_key)
    except Exception as exc:
        print(f"failed\nERROR: could not initialise CLOB client: {exc}", file=sys.stderr)
        sys.exit(1)
    print("ok")

    if args.check:
        address = client.signer.address()
        rpc_list = (
            [settings.polymarket.polygon_rpc_url] + _POLYGON_RPCS_FALLBACK
            if settings.polymarket.polygon_rpc_url
            else _POLYGON_RPCS_FALLBACK
        )
        print(f"\n  Wallet address : {address}")
        print(f"  Polygon scan   : https://polygonscan.com/address/{address}#tokentxns")
        try:
            matic = _get_matic_balance(address, rpc_list)
            status = "OK" if matic >= _MATIC_MIN_FOR_GAS else f"LOW — need {_MATIC_MIN_FOR_GAS} for approvals"
            print(f"  MATIC balance  : {matic:.4f}  ({status})")
        except Exception as exc:
            print(f"  MATIC balance  : (could not fetch: {exc})")
        # On-chain balances (source of truth)
        for label, token in [
            ("pUSD (trading)", _PUSD_CONTRACT),
            ("USDC native    ", _USDC_NATIVE),
            ("USDC.e (legacy)", _USDC_LEGACY),
        ]:
            try:
                bal_onchain = _get_erc20_balance(token, address, rpc_list)
                print(f"  {label} : {bal_onchain:.2f}")
            except Exception as exc:
                print(f"  {label} : (could not fetch: {exc})")
        # CLOB-reported balance (may lag until update_balance_allowance syncs)
        try:
            client.update_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            bal = client.get_balance_allowance(
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            clob_bal = float(bal.get("balance", 0)) / 1_000_000
            print(f"  pUSD (CLOB)    : {clob_bal:.2f}  (as seen by CLOB)")
            for contract, amount in bal.get("allowances", {}).items():
                approved = float(amount) / 1_000_000
                print(f"  Allowance {contract[:10]}…: {approved:.2f}")
        except Exception as exc:
            print(f"  pUSD (CLOB)    : (could not fetch: {exc})")
        print()
        return

    if args.bets:
        address = client.signer.address()
        print(f"Wallet: {address}\n")
        show_bets(client, address)
        return

    if args.cancel:
        if args.order_id:
            print(f"Cancelling order {args.order_id}... ", end="", flush=True)
            try:
                resp = client.cancel(args.order_id)
                print("done")
                print(json.dumps(resp, indent=2, default=str))
            except Exception as exc:
                print(f"failed\nERROR: {exc}", file=sys.stderr)
                sys.exit(1)
        else:
            orders = client.get_orders(OpenOrderParams())
            if not orders:
                print("No open orders to cancel.")
                return
            print(f"Cancelling all {len(orders)} open order(s)... ", end="", flush=True)
            try:
                resp = client.cancel_all()
                print("done")
                print(json.dumps(resp, indent=2, default=str))
            except Exception as exc:
                print(f"failed\nERROR: {exc}", file=sys.stderr)
                sys.exit(1)
        return

    side = args.side  # "BUY" or "SELL"
    print(f"\nPolymarket market order (Fill or Kill)")
    print(f"  token_id : {args.token_id}")
    print(f"  amount   : {args.amount} USDC")
    print(f"  side     : {side}")
    if args.dry_run:
        print(f"  mode     : DRY RUN — order will be built but not submitted")
    print()

    print("Placing order... ", end="", flush=True)
    try:
        resp = place_market_order(client, args.token_id, args.amount, dry_run=args.dry_run, side=side)
    except Exception as exc:
        print(f"failed\nERROR: order placement failed: {exc}", file=sys.stderr)
        sys.exit(1)
    print("done\n")

    print("Response:")
    print(json.dumps(resp, indent=2, default=str))

    # Surface the most important fields if present
    if isinstance(resp, dict) and not resp.get("dry_run"):
        status = resp.get("status") or resp.get("errorMsg") or "(unknown)"
        order_id = resp.get("orderID") or resp.get("orderId") or ""
        print()
        print(f"  Status   : {status}")
        if order_id:
            print(f"  Order ID : {order_id}")


if __name__ == "__main__":
    main()
