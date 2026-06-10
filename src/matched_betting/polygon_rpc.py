"""
polygon_rpc.py
--------------
Shared Polygon JSON-RPC helpers used by bet.py and portfolio.py.

Centralises the RPC endpoint list, the retry/error-handling wrapper, and
the two common balance helpers so both files stay in sync automatically.
"""
from __future__ import annotations

import requests as _requests

PM_CHAIN_ID = 137

PM_RPCS: list[str] = [
    "https://polygon.drpc.org",
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc.ankr.com/polygon",
    "https://polygon.llamarpc.com",
    "https://1rpc.io/matic",
]

_BALANCE_OF_SEL = bytes.fromhex("70a08231")  # balanceOf(address)


def pm_rpc(method: str, params: list, rpc_list: list[str] | None = None) -> object:
    """Call a Polygon JSON-RPC method, trying each endpoint until one succeeds.

    Application-level errors (insufficient funds, bad nonce, revert) are raised
    immediately without retrying other endpoints — they are deterministic and
    won't be fixed by a different node.
    """
    endpoints  = rpc_list or PM_RPCS
    last_exc: Exception | None = None
    for url in endpoints:
        try:
            r = _requests.post(
                url,
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": 1},
                timeout=15,
            )
            r.raise_for_status()
            result = r.json()
            if "error" in result:
                err = result["error"]
                msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                raise RuntimeError(msg)
            return result["result"]
        except RuntimeError:
            raise
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"All Polygon RPCs failed. Last: {last_exc}")


def pm_matic_balance(address: str, rpc_list: list[str] | None = None) -> float:
    """Return the MATIC (native token) balance of an address in whole MATIC."""
    return int(pm_rpc("eth_getBalance", [address, "latest"], rpc_list), 16) / 1e18


def pm_erc20_balance(token: str, address: str, rpc_list: list[str]) -> float:
    """Return an ERC-20 token balance assuming 6 decimals (USDC / pUSD)."""
    padded   = bytes.fromhex("000000000000000000000000" + address.lower().replace("0x", ""))
    calldata = "0x" + (_BALANCE_OF_SEL + padded).hex()
    raw      = pm_rpc("eth_call", [{"to": token, "data": calldata}, "latest"], rpc_list)
    return int(raw, 16) / 1e6
