"""
specials_close.py
-----------------
Close open specials positions by unwinding both legs at current market prices.

Reads outputs/positions.jsonl (written by specials_place.py on successful
placement), fetches live prices, shows estimated close P&L, and prompts before
placing the closing trades.

Closing mechanics
-----------------
  Matchbook back  →  lay same outcome (equalized stake) at current lay odds
  Matchbook lay   →  back same outcome (equalized stake) at current back odds
  Polymarket YES/NO  →  market SELL order via CLOB (fill-or-kill)

The equalized-stake approach guarantees the same P&L regardless of event
outcome, locking in any edge the market has already given back.

CLI examples
------------
    python specials_close.py             # review all open positions, prompt each
    python specials_close.py --dry-run   # show close P&L without placing
    python specials_close.py --debug     # verbose price fetching
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent
_SRC  = _ROOT / "src"
for _p in (_SRC, _ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from matched_betting.config import load_settings
from matched_betting.http import HttpClient

from bet import mb_get_runner_prices, mb_place_bet
from polymarket_bet import _build_client, place_market_order
from portfolio import _pm_ensure_ctf_approval
from matched_betting.polygon_rpc import PM_RPCS as _PM_RPCS

try:
    from py_clob_client.order_builder.constants import SELL as _PM_SELL
except ImportError:
    _PM_SELL = "SELL"

_POSITIONS         = _ROOT / "outputs" / "positions.jsonl"
_GBP_RATE_FALLBACK = 0.79


# ---------------------------------------------------------------------------
# Ledger helpers
# ---------------------------------------------------------------------------

def _load_open_positions(path: Path = _POSITIONS) -> list[dict]:
    """Return all positions with status='open', keyed by position_id (last write wins)."""
    if not path.exists():
        return []
    seen: dict[str, dict] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                seen[rec["position_id"]] = rec
            except (json.JSONDecodeError, KeyError):
                continue
    return [r for r in seen.values() if r.get("status") == "open"]


def _mark_closed(
    path:          Path,
    position_id:   str,
    closed_at:     str,
    close_pnl_usd: float | None,
) -> None:
    """Rewrite the ledger with the matching position marked closed."""
    if not path.exists():
        return
    records: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for r in records:
        if r.get("position_id") == position_id:
            r["status"]    = "closed"
            r["closed_at"] = closed_at
            if close_pnl_usd is not None:
                r["close_pnl_usd"] = round(close_pnl_usd, 4)
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

def _fetch_mb_prices(leg: dict, settings) -> dict[str, float | None]:
    """Fetch current best back/lay odds for the MB runner stored in the leg."""
    return mb_get_runner_prices(
        settings,
        event_id=leg["event_id"],
        market_id=leg["market_id"],
        runner_id=leg["runner_id"],
    )


def _fetch_pm_bid(settings, vpn_http: HttpClient, token_id: str) -> float | None:
    """Return current best bid price for a PM token (estimated sell proceeds per share).

    Uses [-1] on the bids list, consistent with the convention in specials.py.
    This is an estimate — the actual FOK market sell fills at live bid depth.
    """
    base = settings.polymarket.clob_base_url
    try:
        book = vpn_http.get_json(f"{base}/book", params={"token_id": token_id})
        bids = book.get("bids", [])
        if not bids:
            return None
        return float(bids[-1]["price"])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Close P&L estimation
# ---------------------------------------------------------------------------

def _estimate_close_pnl(
    position:  dict,
    mb_prices: dict[str, float | None],
    pm_bid:    float | None,
    gbp_rate:  float,
) -> dict[str, Any]:
    """
    Estimate the P&L from closing both legs now.

    MB close uses the equalized-stake approach:
        close_stake = entry_stake * (entry_odds - 1) / (current_close_odds - 1)
    This guarantees the same net profit regardless of event outcome.

    Returns a dict with ok/reason, per-leg figures, and total_pnl_usd.
    """
    back_leg = position["back"]
    lay_leg  = position["lay"]

    # Identify which leg is on which platform
    mb_leg   = back_leg if back_leg["platform"] == "matchbook" else lay_leg
    pm_leg   = back_leg if back_leg["platform"] == "polymarket" else lay_leg
    mb_role  = "back"   if back_leg["platform"] == "matchbook" else "lay"

    mb_stake = float(mb_leg.get("stake_gbp") or 0.0)
    mb_entry = float(mb_leg.get("entry_odds") or 1.0)

    # Determine which MB price we close at
    mb_close_side = "lay"  if mb_role == "back" else "back"
    mb_close_odds = mb_prices.get("lay_odds" if mb_role == "back" else "back_odds")

    if not mb_close_odds or mb_close_odds <= 1.0:
        return {"ok": False, "reason": f"no MB {mb_close_side} price available"}

    # Equalized-stake close: close_stake = entry_stake × entry_odds / close_odds
    # Derivation: equate the win/lose P&L scenarios and solve for the close stake.
    # Back close: pnl = stake × (B_entry − L_close) / L_close  [negative if lay > back]
    # Lay  close: pnl = stake × (B_close − L_entry) / B_close  [negative if back < lay]
    mb_close_stake = mb_stake * mb_entry / mb_close_odds
    if mb_role == "back":
        mb_pnl_gbp = mb_stake * (mb_entry - mb_close_odds) / mb_close_odds
    else:
        mb_pnl_gbp = mb_stake * (mb_close_odds - mb_entry) / mb_close_odds

    # PM leg
    pm_shares = float(pm_leg.get("shares") or 0.0)
    pm_spend  = float(pm_leg.get("spend_usd") or 0.0)

    pm_proceeds:  float | None = None
    pm_pnl_usd:   float | None = None
    if pm_bid is not None and pm_shares > 0:
        pm_proceeds = pm_shares * pm_bid
        pm_pnl_usd  = pm_proceeds - pm_spend

    # Combined (convert MB GBP → USD)
    total_pnl: float | None = None
    if pm_pnl_usd is not None:
        total_pnl = mb_pnl_gbp / gbp_rate + pm_pnl_usd

    return {
        "ok":                True,
        "reason":            "",
        "mb_pnl_gbp":        round(mb_pnl_gbp, 4),
        "mb_close_stake":    round(mb_close_stake, 2),
        "mb_close_side":     mb_close_side,
        "mb_close_odds":     mb_close_odds,
        "pm_pnl_usd":        round(pm_pnl_usd, 4)    if pm_pnl_usd   is not None else None,
        "pm_close_proceeds": round(pm_proceeds, 2)   if pm_proceeds   is not None else None,
        "pm_shares":         pm_shares,
        "pm_spend":          pm_spend,
        "total_pnl_usd":     round(total_pnl, 4)     if total_pnl    is not None else None,
    }


# ---------------------------------------------------------------------------
# Close execution
# ---------------------------------------------------------------------------

def _execute_mb_close(
    leg:         dict,
    close_stake: float,
    close_side:  str,
    close_odds:  float,
    settings,
    dry_run:     bool,
) -> dict:
    """Place the MB opposing bet to close the position (lay closes a back, back closes a lay)."""
    result = mb_place_bet(
        settings,
        event_id=leg["event_id"],
        market_id=leg["market_id"],
        runner_id=leg["runner_id"],
        stake=close_stake,
        side=close_side,
        odds=close_odds,
        dry_run=dry_run,
    )
    result["leg"] = "close_mb"
    return result


def _execute_pm_close(leg: dict, settings, vpn_http: HttpClient, dry_run: bool) -> dict:
    """Submit a market SELL order on PM for the full share position."""
    token_id = leg["token_id"]
    shares   = leg["shares"]

    if dry_run:
        return {
            "ok": True, "dry_run": True, "leg": "close_pm",
            "token_id": token_id, "shares": shares,
        }

    try:
        pk = settings.polymarket.private_key
        if not pk:
            return {"ok": False, "leg": "close_pm", "error": "POLYMARKET_PRIVATE_KEY not set"}

        # One-time on-chain CTF approval — skipped if already approved.
        from eth_account import Account as _EthAcct
        address  = _EthAcct.from_key(pk).address
        rpc_list = (
            [settings.polymarket.polygon_rpc_url] + _PM_RPCS
            if settings.polymarket.polygon_rpc_url else _PM_RPCS
        )
        _pm_ensure_ctf_approval(pk, address, rpc_list)

        # Route sell through VPN proxy if configured (PM geo-restriction).
        _proxy = settings.vpn_proxy_url
        if _proxy:
            import httpx
            import py_clob_client.http_helpers.helpers as _pm_helpers
            _hx_proxy = _proxy.replace("socks5h://", "socks5://")
            _pm_helpers._http_client = httpx.Client(http2=True, proxy=_hx_proxy)

        client = _build_client(pk, settings)
        # amount = shares for SELL orders (vs USDC for BUY orders)
        resp = place_market_order(client, token_id, shares, dry_run=False, side=_PM_SELL)
        ok   = resp.get("status") == "matched" or bool(resp.get("orderID"))
        return {
            "ok": ok, "leg": "close_pm", "platform": "Polymarket",
            "shares": shares, "response": resp,
        }
    except Exception as exc:
        return {"ok": False, "leg": "close_pm", "error": str(exc)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_usd_gbp_rate() -> float | None:
    try:
        import requests as _req
        s = _req.Session()
        s.trust_env = False
        r = s.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        return float(r.json()["rates"]["GBP"])
    except Exception:
        return None


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Review and close open specials positions."
    )
    parser.add_argument("--dry-run", action="store_true", dest="dry_run",
                        help="Fetch prices and show P&L without placing close trades.")
    parser.add_argument("--debug", action="store_true",
                        help="Print raw price fetches.")
    args = parser.parse_args(argv)

    positions = _load_open_positions()
    if not positions:
        print(f"No open positions found in {_POSITIONS}")
        return 0

    settings    = load_settings(_ROOT)
    direct_http = HttpClient()
    vpn_http    = HttpClient(proxy_url=settings.vpn_proxy_url) if settings.vpn_proxy_url else direct_http

    gbp_rate = _fetch_usd_gbp_rate() or _GBP_RATE_FALLBACK
    print(f"  FX: 1 USD = {gbp_rate:.4f} GBP")
    print(f"  {len(positions)} open position(s)\n")

    for pos in positions:
        pid      = pos["position_id"]
        strat    = pos.get("strategy", "?")
        event_k  = pos.get("event_key", "?")
        placed   = pos.get("placed_at", "?")
        back_leg = pos["back"]
        lay_leg  = pos["lay"]

        mb_leg = back_leg if back_leg["platform"] == "matchbook" else lay_leg
        pm_leg = back_leg if back_leg["platform"] == "polymarket" else lay_leg

        print(f"  {'─'*60}")
        print(f"  {strat}  ({event_k})")
        print(f"    placed:    {placed}")
        print(f"    MB {mb_leg.get('outcome')} entry {mb_leg.get('entry_odds')}"
              f"  stake £{mb_leg.get('stake_gbp', 0):.2f}")
        print(f"    PM {pm_leg.get('outcome')} {pm_leg.get('token_side')}"
              f"  {pm_leg.get('shares', 0):.2f} shares"
              f"  (entry ${pm_leg.get('spend_usd', 0):.2f})")

        # ── Fetch current prices ─────────────────────────────────────────
        mb_prices = _fetch_mb_prices(mb_leg, settings)
        pm_bid    = _fetch_pm_bid(settings, vpn_http, pm_leg["token_id"])

        if args.debug:
            print(f"    MB current prices: {mb_prices}")
            print(f"    PM bid ({pm_leg.get('token_side')}): {pm_bid}")

        # ── Estimate close P&L ───────────────────────────────────────────
        est = _estimate_close_pnl(pos, mb_prices, pm_bid, gbp_rate)
        if not est["ok"]:
            print(f"    Cannot estimate close: {est['reason']}")
            continue

        pnl_sign = "+" if (est["mb_pnl_gbp"] or 0) >= 0 else ""
        print(f"    MB close: {est['mb_close_side']} £{est['mb_close_stake']:.2f}"
              f" @ {est['mb_close_odds']}"
              f"  → £{est['mb_pnl_gbp']:+.2f} guaranteed")

        if pm_bid is not None:
            print(f"    PM close: sell {est['pm_shares']:.2f} shares @ {pm_bid:.5f}"
                  f"  → ${est['pm_close_proceeds']:.2f}"
                  f"  (P&L ${est['pm_pnl_usd']:+.2f})")

        total = est.get("total_pnl_usd")
        if total is not None:
            print(f"    Total estimated close P&L: ${total:+.4f}")
        else:
            print(f"    Total estimated close P&L: unavailable (PM bid missing)")

        if args.dry_run:
            print("    [DRY RUN — skipping placement prompt]")
            continue

        try:
            raw = input("    Close? [y / n]: ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n  Aborted.")
            return 0

        if raw != "y":
            print("    Skipped.")
            continue

        # ── Execute close — MB first, then PM ────────────────────────────
        mb_result = _execute_mb_close(
            mb_leg,
            close_stake=est["mb_close_stake"],
            close_side=est["mb_close_side"],
            close_odds=est["mb_close_odds"],
            settings=settings,
            dry_run=args.dry_run,
        )

        if not mb_result.get("ok"):
            print(f"    MB close FAILED: {mb_result.get('error')} — aborting PM close",
                  file=sys.stderr)
            continue

        pm_result = _execute_pm_close(pm_leg, settings, vpn_http, dry_run=args.dry_run)

        # ── Report outcome ────────────────────────────────────────────────
        mb_ok = mb_result.get("ok")
        pm_ok = pm_result.get("ok")

        if mb_ok and pm_ok:
            total_str = f"${total:+.2f}" if total is not None else "unknown"
            print(f"    Both legs closed.  Estimated P&L: {total_str}")
            _mark_closed(_POSITIONS, pid, _utc_iso_now(), total)
        else:
            if not mb_ok:
                print(f"    MB close failed: {mb_result.get('error')}", file=sys.stderr)
            if not pm_ok:
                print(f"    PM close failed: {pm_result.get('error')}", file=sys.stderr)
            if mb_ok and not pm_ok:
                print("    *** MB closed but PM FAILED — check PM position manually ***",
                      file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
