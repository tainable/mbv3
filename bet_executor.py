"""
bet_executor.py
---------------
Translates arb dicts (from arb_finder.py / scan.py) into live bet placements.

Called by scan.py when --auto-bet is enabled. Supports back and lay legs on
Polymarket, Matchbook, and SX Bet.

Lay bet mechanics
-----------------
Polymarket (football / soccer):
    Each outcome (home win, draw, away win) is a separate binary Yes/No market
    with two CLOB tokens: a YES token and a NO token. Buying the NO token is
    mathematically equivalent to a traditional exchange lay — it pays out if the
    outcome does NOT happen. The effective "lay odds" = 1 / NO_price.
    This is the correct lay instrument for football where draws are possible.

SX Bet:
    SX Bet has only two outcomes per market (no draw contract). "Laying" an
    outcome is equivalent to backing the opposite outcome. This is a proper
    lay for two-outcome markets (NBA, MLB) but is an APPROXIMATION for
    football — backing "away wins" does not cover a draw.

Matchbook:
    Native lay orders. Works for all market types.

Limitations
-----------
- Azuro and Smarkets are not yet wired for auto-placement.
- If any leg involves an unsupported provider the whole arb is skipped and a
  warning is printed.  This is intentional: placing a partial arb creates
  one-sided risk, not a lock.
"""
from __future__ import annotations

import json
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

import requests as _req

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

from matched_betting.http import HttpClient
from matched_betting import calculator, kelly

_SUPPORTED = {"polymarket", "matchbook", "sx_bet"}

# Legs are placed in this order and aborted on first failure to avoid uncovered positions.
# Matchbook first to confirm GBP liability before USDC is committed on-chain.
_PLATFORM_ORDER = ["matchbook", "sx_bet", "polymarket"]

# Set to True when a leg fails after a prior leg succeeded.
# Persists until process restart — requires human review before resuming.
_HALT: bool = False

_BET_LOG = _ROOT / "outputs" / "bet_log.jsonl"


def _platform_rank(label: str) -> int:
    for i, p in enumerate(_PLATFORM_ORDER):
        if p in label:
            return i
    return len(_PLATFORM_ORDER)

_MB_MONEYLINE = {
    "match odds", "match winner", "money line", "moneyline",
    "winner (incl. overtime)", "winner (including overtime)",
}


# ---------------------------------------------------------------------------
# Name matching (same logic as arb_finder._names_match)
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode("ascii").lower().replace("-", " ").strip()


def _names_match(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    wa, wb = set(a.split()), set(b.split())
    shorter, longer = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return bool(shorter) and shorter.issubset(longer)


# ---------------------------------------------------------------------------
# Platform-specific ID lookups
# ---------------------------------------------------------------------------

def _pm_token_for(game: dict, slot: str) -> str | None:
    """Return the Polymarket YES CLOB token ID for slot ('team1', 'team2', 'draw')."""
    return game.get(f"polymarket_{slot}_clob_token_id")


def _pm_no_token_for(game: dict, slot: str, settings) -> str | None:
    """
    Return the Polymarket NO CLOB token ID for a binary Yes/No outcome slot.

    Soccer outcomes on Polymarket are each a separate binary market with two
    tokens: YES (outcome happens) and NO (outcome doesn't happen).  Buying the
    NO token is equivalent to laying the outcome on a traditional exchange.

    Prefers the stored market_id (game context) for the lookup.  Falls back to
    searching Gamma API by the YES token ID.
    """
    yes_token = game.get(f"polymarket_{slot}_clob_token_id")
    market_id = game.get(f"polymarket_{slot}_market_id")

    market_data: dict | None = None

    if market_id:
        try:
            http = HttpClient()
            raw  = http.get_json(
                f"{settings.polymarket.gamma_base_url}/markets",
                params={"id": market_id},
            )
            market_data = raw[0] if isinstance(raw, list) else raw
        except Exception:
            pass

    if market_data is None and yes_token:
        # Reverse-lookup by the YES token ID
        try:
            r = _req.get(
                f"{settings.polymarket.gamma_base_url}/markets",
                params={"clobTokenIds": yes_token},
                timeout=15,
            )
            if r.ok:
                raw = r.json()
                markets = raw if isinstance(raw, list) else [raw]
                market_data = markets[0] if markets else None
        except Exception:
            pass

    if not market_data:
        return None

    clob_ids_raw = market_data.get("clobTokenIds") or []
    clob_ids: list[str] = (
        json.loads(clob_ids_raw) if isinstance(clob_ids_raw, str) else list(clob_ids_raw)
    )

    if len(clob_ids) < 2:
        return None

    # If we know the YES token, return the other one
    if yes_token and yes_token in clob_ids:
        return next((t for t in clob_ids if t != yes_token), None)

    # Otherwise match the "No" outcome string
    outcomes_raw = market_data.get("outcomes") or []
    outcomes: list[str] = (
        json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else list(outcomes_raw)
    )
    for outcome, token in zip(outcomes, clob_ids):
        if str(outcome).lower() == "no":
            return token

    # Last resort: assume index 1 is the NO token
    return clob_ids[1]


def _sx_outcome_for(game: dict, outcome_name: str) -> str:
    """Return 'one' (team1) or 'two' (team2) for an SX Bet order."""
    # For totals/spread markets the scanner stores which outcome is outcomeOne explicitly.
    stored_one = game.get("sx_bet_outcome_one_team")
    if stored_one:
        return "one" if _names_match(stored_one, outcome_name) else "two"
    if _names_match(game.get("team1", ""), outcome_name):
        return "one"
    return "two"


def _resolve_mb_runner(
    game: dict, outcome_name: str, settings
) -> tuple[int, int, float | None]:
    """
    Login to Matchbook, fetch the event, return (market_id, runner_id, best_back_odds).

    Raises RuntimeError on login failure or if the event cannot be fetched.
    Returns (0, 0, None) if the runner is not found in the moneyline market.
    """
    event_id = game.get("matchbook_event_id")
    if not event_id:
        return 0, 0, None

    http = HttpClient()
    mb   = settings.matchbook

    token_resp = http.post_json(
        f"{mb.base_url}/bpapi/rest/security/session",
        payload={"username": mb.username, "password": mb.password},
        headers={"Accept": "application/json"},
    )
    token = token_resp.get("session-token")
    if not token:
        raise RuntimeError(f"Matchbook login failed: {token_resp}")

    event = http.get_json(
        f"{mb.base_url}/edge/rest/events/{event_id}",
        headers={"session-token": token, "Accept": "application/json"},
    )

    for market in event.get("markets", []):
        if market.get("status") != "open":
            continue
        if str(market.get("name") or "").lower().strip() not in _MB_MONEYLINE:
            continue
        for runner in market.get("runners", []):
            if not _names_match(str(runner.get("name") or ""), outcome_name):
                continue
            prices = runner.get("prices", [])
            best_back = None
            for p in prices:
                if str(p.get("side") or "").lower() == "back":
                    odds = float(p.get("decimal-odds") or p.get("odds") or 0)
                    if odds and (best_back is None or odds > best_back):
                        best_back = odds
            return int(market["id"]), int(runner["id"]), best_back

    return 0, 0, None


# ---------------------------------------------------------------------------
# Placed-game tracking — prevent duplicate bets on the same match
# ---------------------------------------------------------------------------

def log_arb_success(
    arb_type: str,
    arb: dict,
    game: dict,
    results: list[dict] | None = None,
    dry_run: bool = False,
) -> None:
    """Append a PLACED/DRY_RUN entry to bet_log.jsonl."""
    from datetime import datetime, timezone

    if arb_type == "sure_bet":
        scanned_odds = {
            "team1": arb.get("team1_back_odds"),
            "team2": arb.get("team2_back_odds"),
            "draw":  arb.get("draw_back_odds"),
        }
    else:
        scanned_odds = {
            "back": arb.get("back_odds"),
            "lay":  arb.get("lay_odds"),
        }

    execution_odds = {}
    if results:
        for r in results:
            if r.get("ok") and "_timing_s" not in r and r.get("execution_odds") is not None:
                execution_odds[r.get("_leg", r.get("platform", "?"))] = r["execution_odds"]

    entry = {
        "timestamp":      datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status":         "DRY_RUN" if dry_run else "PLACED",
        "arb_type":       arb_type,
        "team1":          game.get("team1"),
        "team2":          game.get("team2"),
        "league":         game.get("league"),
        "date_time":      game.get("date_time"),
        "profit_pct":     arb.get("profit_pct"),
        "scanned_odds":   scanned_odds,
        "execution_odds": execution_odds or None,
    }
    _BET_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _BET_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def game_already_bet(game: dict, window_hours: float = 36.0) -> bool:
    """
    Return True if bet_log.jsonl contains a PLACED entry for this game within
    window_hours.  Matches on normalised team names so minor spelling differences
    across providers don't create false negatives.
    """
    if not _BET_LOG.exists():
        return False
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    t1 = _norm(game.get("team1") or "")
    t2 = _norm(game.get("team2") or "")
    if not t1 or not t2:
        return False
    try:
        with _BET_LOG.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("status") != "PLACED":
                    continue
                try:
                    ts = datetime.fromisoformat(
                        entry.get("timestamp", "").replace("Z", "+00:00")
                    )
                    if ts < cutoff:
                        continue
                except ValueError:
                    continue
                if (
                    _norm(entry.get("team1") or "") == t1
                    and _norm(entry.get("team2") or "") == t2
                ):
                    return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Failed-leg handling: log, cancel, alert, halt
# ---------------------------------------------------------------------------

def _log_arb_failure(
    arb_type: str,
    arb: dict,
    game: dict,
    placed: list[dict],
    failed: dict,
) -> None:
    from datetime import datetime, timezone
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "FAILED_LEG",
        "arb_type": arb_type,
        "game": f"{game.get('team1')} vs {game.get('team2')}",
        "league": game.get("league"),
        "profit_pct": arb.get("profit_pct"),
        "placed_legs": [r.get("_leg") for r in placed if r.get("ok")],
        "failed_leg": failed.get("_leg", failed.get("platform", "unknown")),
        "failed_error": failed.get("error", "unknown"),
    }
    _BET_LOG.parent.mkdir(parents=True, exist_ok=True)
    with _BET_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _cancel_placed_legs(placed: list[dict], settings, dry_run: bool) -> list[str]:
    """
    Attempt to cancel every successfully placed leg where possible.
    Matchbook LIMIT offers can be cancelled by offer_id.
    SX Bet taker orders and Polymarket FOK orders execute immediately — nothing to cancel.
    """
    from bet import mb_cancel_offer
    outcomes: list[str] = []
    for r in placed:
        if not r.get("ok"):
            continue
        leg = r.get("_leg", r.get("platform", ""))

        if "matchbook" in leg.lower():
            offer_id = r.get("offer_id")
            if not offer_id:
                outcomes.append("Matchbook — no offer_id in result, cannot cancel")
            elif dry_run:
                outcomes.append(f"Matchbook offer {offer_id} — dry run, not cancelled")
            else:
                try:
                    mb_cancel_offer(settings, int(offer_id))
                    outcomes.append(f"Matchbook offer {offer_id} — cancel requested")
                except Exception as exc:
                    outcomes.append(f"Matchbook offer {offer_id} — cancel FAILED: {exc}")

        elif "sx_bet" in leg.lower():
            outcomes.append("SX Bet taker order — already executed, cannot cancel")

        elif "polymarket" in leg.lower():
            outcomes.append("Polymarket FOK order — already executed, cannot cancel")

    return outcomes


def _on_leg_failure(
    arb_type: str,
    arb: dict,
    game: dict,
    placed: list[dict],
    failed: dict,
    settings,
    dry_run: bool,
) -> None:
    """Log, attempt cancellation of placed legs, send alert, and set the halt flag.

    Called for every leg failure, including the first leg. When prior legs
    succeeded (uncovered position) the process halts and cancellation is
    attempted. When the first leg fails there is no open position so the
    process continues but still fires a notification.
    """
    global _HALT

    _log_arb_failure(arb_type, arb, game, placed, failed)

    uncovered = bool(placed)  # prior legs placed → real risk of uncovered position
    if uncovered:
        cancel_results = _cancel_placed_legs(placed, settings, dry_run)
        cancel_str = "\n".join(f"  {c}" for c in cancel_results) if cancel_results else "  Nothing to cancel"
        _HALT = True
    else:
        cancel_str = "  Nothing to cancel (first leg — no position opened)"

    from matched_betting import notifier
    placed_labels = [r.get("_leg", "?") for r in placed if r.get("ok")]
    profit = arb.get("profit_pct")
    profit_str = f"{profit:.2f}%" if profit is not None else "?"
    body = (
        f"Game:       {game.get('team1')} vs {game.get('team2')} ({game.get('league', '?')})\n"
        f"Arb type:   {arb_type}  ({profit_str} net)\n"
        f"Failed leg: {failed.get('_leg', failed.get('platform', '?'))}\n"
        f"Error:      {failed.get('error', '?')}\n"
        f"Placed ({len(placed_labels)}): {', '.join(placed_labels) or 'none'}\n"
        f"Cancels:\n{cancel_str}"
    )
    if uncovered:
        notifier.send_alert("FAILED LEG — AUTO-BET HALTED", body, settings)
        print(
            "\n  FAILED LEG — auto-bet HALTED. Restart the process after reviewing bet_log.jsonl.",
            file=sys.stderr,
        )
    else:
        notifier.send_alert("AUTO-BET LEG FAILED (no position opened)", body, settings)
        print(
            f"\n  First leg failed ({failed.get('_leg', '?')}) — arb skipped, continuing.",
            file=sys.stderr,
        )


def _on_arb_success(
    arb_type: str,
    arb: dict,
    game: dict,
    results: list[dict],
    settings,
) -> None:
    """Send a success notification after all legs are placed live."""
    from matched_betting import notifier
    profit = arb.get("profit_pct")
    profit_str = f"{profit:.2f}%" if profit is not None else "?"
    legs = [r for r in results if r.get("ok") and "_timing_s" not in r]
    leg_lines = []
    slippage_parts = []
    for r in legs:
        label  = r.get("_leg", r.get("platform", "?"))
        amount = r.get("amount")
        odds   = r.get("decimal_odds")
        exec_o = r.get("execution_odds")
        parts  = [label]
        if amount is not None:
            parts.append(f"${amount:.2f}")
        if odds is not None:
            parts.append(f"@ {odds:.3f}")
        if exec_o is not None:
            parts.append(f"(exec {exec_o:.3f})")
        leg_lines.append("  " + "  ".join(parts))
        if odds is not None and exec_o is not None:
            diff = exec_o - odds
            if abs(diff) >= 0.005:
                sign = "+" if diff >= 0 else ""
                slippage_parts.append(f"{label}: {sign}{diff:.3f}")
    body = (
        f"Game:  {game.get('team1')} vs {game.get('team2')} ({game.get('league', '?')})\n"
        f"Type:  {arb_type}  ({profit_str} net)\n"
        f"Legs:\n" + "\n".join(leg_lines)
    )
    if slippage_parts:
        body += "\nSlippage: " + "  ".join(slippage_parts)
    notifier.send_alert(
        f"BET PLACED +{profit_str} — {game.get('team1')} vs {game.get('team2')}",
        body,
        settings,
    )


# ---------------------------------------------------------------------------
# Bankroll floor check
# ---------------------------------------------------------------------------

def check_bankroll_halt(settings, gbp_rate: float | None) -> bool:
    """
    Return True (and send an alert) if the bankroll has dropped below
    settings.kelly.min_bankroll_usdc.

    Bankroll = min(pm_usd, sx_usd, mb_usd) × 3, matching the Kelly formula.
    Returns False if balances cannot be fetched (fail-open — don't stop on errors).
    """
    threshold = getattr(getattr(settings, "kelly", None), "min_bankroll_usdc", 20.0)
    balances  = _fetch_all_balances(settings)
    bankroll  = kelly.compute_bankroll(
        balances.get("polymarket"),
        balances.get("sx_bet"),
        balances.get("matchbook"),
        gbp_rate,
    )
    if bankroll is None:
        return False
    if bankroll >= threshold:
        return False

    from matched_betting import notifier
    bal_str = (
        f"  Polymarket: ${balances.get('polymarket') or 0:.2f}\n"
        f"  SX Bet:     ${balances.get('sx_bet') or 0:.2f}\n"
        f"  Matchbook:  GBP {balances.get('matchbook') or 0:.2f}"
    )
    notifier.send_alert(
        f"DAEMON STOPPED — bankroll ${bankroll:.2f} below ${threshold:.2f}",
        f"Bankroll (min×3): ${bankroll:.2f}  threshold: ${threshold:.2f}\n{bal_str}",
        settings,
    )
    print(
        f"\n  BANKROLL ${bankroll:.2f} below minimum ${threshold:.2f} — stopping daemon.",
        file=sys.stderr,
    )
    return True


# ---------------------------------------------------------------------------
# Kelly bet sizing
# ---------------------------------------------------------------------------

def _arb_providers(arb: dict, arb_type: str) -> set[str]:
    """Return the set of provider names actually involved in this arb."""
    if arb_type == "back_lay":
        return {arb["back_provider"], arb["lay_provider"]}
    provs = {arb["team1_back_provider"], arb["team2_back_provider"]}
    if arb.get("draw_back_provider"):
        provs.add(arb["draw_back_provider"])
    return provs


def get_platform_balances_usd(
    settings,
    gbp_rate: float | None = None,
    providers: list[str] | None = None,
) -> dict[str, float | None]:
    """Fetch free-fund balances for the given platforms (all three if None), returned in USD.

    Matchbook is fetched in GBP and converted. Returns None for any platform
    whose balance could not be fetched or was not requested.
    """
    balances = _fetch_all_balances(settings, providers=providers)
    rate = gbp_rate or 0.79
    mb_gbp = balances.get("matchbook")
    return {
        "polymarket": balances.get("polymarket"),
        "sx_bet":     balances.get("sx_bet"),
        "matchbook":  mb_gbp / rate if mb_gbp is not None else None,
    }


def _fetch_all_balances(settings, providers: list[str] | None = None) -> dict[str, float | None]:
    """Fetch platform balances concurrently. Pass providers to limit which are fetched."""
    from bet import pm_get_balance, mb_get_balance, sx_get_balance
    all_getters = {
        "polymarket": pm_get_balance,
        "sx_bet":     sx_get_balance,
        "matchbook":  mb_get_balance,
    }
    _getters = {k: v for k, v in all_getters.items() if providers is None or k in providers}
    result: dict[str, float | None] = {k: None for k in all_getters}
    with ThreadPoolExecutor(max_workers=max(1, len(_getters))) as ex:
        futs = {ex.submit(fn, settings): name for name, fn in _getters.items()}
        for f in as_completed(futs):
            name = futs[f]
            try:
                result[name] = f.result()
            except Exception:
                result[name] = None
    return result


def _provider_max_budgets(
    arb: dict,
    arb_type: str,
    balances: dict[str, float | None],
    gbp_rate: float | None,
) -> list[float]:
    """
    For each provider leg, compute the maximum total USDC budget that provider
    can support given its current balance.

    Matchbook balance (GBP) is converted to USD via gbp_rate.
    Polymarket lay outlay accounts for the NO-token cost formula.
    """
    rate = gbp_rate or 0.79
    mb_gbp = balances.get("matchbook")
    balances_usd: dict[str, float | None] = {
        "polymarket": balances.get("polymarket"),
        "sx_bet":     balances.get("sx_bet"),
        "matchbook":  mb_gbp / rate if mb_gbp is not None else None,
    }

    # Compute each provider's USDC fraction of a unit budget
    if arb_type == "sure_bet":
        usdc_fractions: dict[str, float] = {}
        for _slot, provider, _name, _odds, fraction in _sure_bet_stakes(arb, 1.0):
            usdc_fractions[provider] = usdc_fractions.get(provider, 0.0) + fraction
    else:
        back_frac, lay_frac = _back_lay_stakes(arb, 1.0)
        back_prov = arb["back_provider"]
        lay_prov  = arb["lay_provider"]
        usdc_fractions = {back_prov: back_frac}
        if lay_prov == "polymarket":
            _pm_f = calculator._polymarket_fee_rate(arb["lay_odds"])
            pm_frac = lay_frac * (arb["lay_odds"] - 1) * (1 + _pm_f)
            usdc_fractions[lay_prov] = usdc_fractions.get(lay_prov, 0.0) + pm_frac
        elif lay_prov == "matchbook":
            # Matchbook requires the full liability (backer_stake × (odds − 1)) in free funds.
            usdc_fractions[lay_prov] = usdc_fractions.get(lay_prov, 0.0) + lay_frac * (arb["lay_odds"] - 1)
        elif lay_prov == "sx_bet":
            # SX Bet binary No-token: actual USDC = lay_frac × (lay_odds − 1).
            usdc_fractions[lay_prov] = usdc_fractions.get(lay_prov, 0.0) + lay_frac * (arb["lay_odds"] - 1)
        else:
            usdc_fractions[lay_prov] = usdc_fractions.get(lay_prov, 0.0) + lay_frac

    max_budgets: list[float] = []
    for provider, frac in usdc_fractions.items():
        if frac <= 0:
            continue
        bal = balances_usd.get(provider)
        if bal is None or bal <= 0:
            continue
        max_budgets.append(bal / frac)
    return max_budgets


def _compute_kelly_budget(
    arb: dict,
    arb_type: str,
    settings,
    gbp_rate: float | None,
    max_budget_usdc: float,
) -> float:
    """
    Compute the Kelly-sized stake for this arb.

    Steps:
      1. Fetch all platform balances and convert to USD
      2. Compute bankroll = min(arb platforms) × n_arb_platforms
      3. Apply profit-scaled fraction → kelly_stake
      4. Constrain by per-provider balance limits
      5. Enforce max_stake_usdc and min_stake_usdc caps
      6. Return 0.0 if stake falls below minimum (caller should skip the arb)

    Returns max_budget_usdc unchanged when Kelly is disabled or balances unavailable.
    """
    ks = getattr(settings, "kelly", None)
    if ks is None or not ks.enabled:
        return max_budget_usdc

    arb_provs = _arb_providers(arb, arb_type)
    balances = _fetch_all_balances(settings, providers=list(arb_provs))

    # Convert arb platform balances to USD.
    rate = gbp_rate or 0.79
    mb_gbp = balances.get("matchbook")
    all_usd: dict[str, float | None] = {
        "polymarket": balances.get("polymarket"),
        "sx_bet":     balances.get("sx_bet"),
        "matchbook":  mb_gbp / rate if mb_gbp is not None else None,
    }
    arb_usd = {p: all_usd.get(p) for p in arb_provs}

    bankroll = kelly.compute_bankroll_for_arb(arb_usd)

    if bankroll is None:
        print(
            "  Kelly: could not fetch balances for arb platforms "
            f"({', '.join(sorted(arb_provs))}) -- using --budget as stake.",
            file=sys.stderr,
        )
        return max_budget_usdc

    profit_pct = arb.get("profit_pct", 0.0)
    profit_24h_pct = arb.get("profit_24h_pct")
    kelly_edge = min(profit_pct, profit_24h_pct) if profit_24h_pct is not None else profit_pct

    frac = kelly.kelly_fraction(
        kelly_edge,
        ks.low_profit, ks.high_profit, ks.low_fraction, ks.high_fraction,
    )
    stake = frac * bankroll

    # Constrain by what each provider can actually cover
    provider_maxes = _provider_max_budgets(arb, arb_type, balances, gbp_rate)
    if provider_maxes:
        stake = min(stake, min(provider_maxes))

    final = min(stake, ks.max_stake_usdc, max_budget_usdc)

    if final < ks.min_stake_usdc:
        print(
            f"  Kelly stake ${final:.2f} below minimum ${ks.min_stake_usdc:.2f} -- skipping arb.",
            file=sys.stderr,
        )
        return 0.0

    edge_str = f"{kelly_edge:.2f}%"
    if profit_24h_pct is not None and profit_24h_pct < profit_pct:
        edge_str += f" (24h-adj from {profit_pct:.2f}%)"
    platforms_str = "+".join(sorted(arb_provs))
    print(
        f"  Kelly: bankroll=${bankroll:.0f} ({platforms_str})  edge={edge_str}  "
        f"fraction={frac:.1%}  stake=${final:.2f}"
    )
    return final


# ---------------------------------------------------------------------------
# Stake calculators
# ---------------------------------------------------------------------------

def _sure_bet_stakes(arb: dict, budget_usdc: float) -> list[tuple[str, str, str, float, float]]:
    """
    Calculate USDC stakes for each leg of a sure bet so that every outcome
    returns the same guaranteed profit (after commissions).

    Returns a list of (slot, provider, outcome_name, raw_odds, stake_usdc).
    """
    is_totals = arb.get("league") in ("mlb_totals", "mls_totals")
    slot1 = "over" if is_totals else "team1"
    slot2 = "under" if is_totals else "team2"
    legs = [(slot1, arb["team1_back_provider"], arb["team1"], arb["team1_back_odds"])]
    if arb.get("market_type") == "three_way" and arb.get("draw_back_odds"):
        legs.append(("draw", arb["draw_back_provider"], "Draw", arb["draw_back_odds"]))
    legs.append((slot2, arb["team2_back_provider"], arb["team2"], arb["team2_back_odds"]))

    # Use effective (post-commission) odds so stakes give equal net profit
    eff = [(slot, prov, name, odds, calculator._eff_back_odds(odds, prov))
           for slot, prov, name, odds in legs]
    margin = sum(1.0 / e for *_, e in eff)

    stakes = [
        (slot, prov, name, odds, round(budget_usdc / (e * margin), 2))
        for slot, prov, name, odds, e in eff
    ]

    guaranteed_returns = [s * e for (_, _, _, _, e), (_, _, _, _, s) in zip(eff, stakes)]
    total_staked = sum(s for *_, s in stakes)
    min_return = min(guaranteed_returns)
    spread = max(guaranteed_returns) - min_return

    if min_return < total_staked:
        print(
            f"  ABORT: rounding error turns worst outcome into a loss "
            f"(return {min_return:.3f} < staked {total_staked:.3f}) — skipping arb.",
            file=sys.stderr,
        )
        return []

    if spread > 0.01:
        print(
            f"  WARNING: sure-bet legs imbalanced by {spread:.3f} USDC after rounding "
            f"({', '.join(f'{r:.3f}' for r in guaranteed_returns)})",
            file=sys.stderr,
        )

    return stakes


def _back_lay_stakes(arb: dict, budget_usdc: float) -> tuple[float, float]:
    """
    Return (back_stake_usdc, lay_stake_usdc) sized for equal net profit in both outcomes.

    Stakes are derived from the commission-adjusted equal-profit condition:
      lay_stake = back_stake × eff_back / lay_odds

    Budget allocation:
      Polymarket lay:  back_stake + lay_stake × (lay_odds − 1) × (1 + fee) = budget
      SX Bet binary:   back_stake + lay_stake × (lay_odds − 1)             = budget
      Other lay:       back_stake + lay_stake                               = budget

    SX Bet soccer uses per-slot Yes/No binary markets identical in structure to
    Polymarket.  The stored lay_odds is the traditional exchange equivalent
    (1 / maker_prob).  Buying the No token costs lay_stake × (lay_odds − 1) USDC,
    exactly like Polymarket — just without the fee factor.
    """
    back_odds  = arb["back_odds"]
    lay_odds   = arb["lay_odds"]
    back_prov  = arb.get("back_provider", "")
    lay_prov   = arb.get("lay_provider", "")

    eff_back = calculator._eff_back_odds(back_odds, back_prov)

    if lay_prov == "polymarket":
        # Equal-profit condition: lay_stake = back_stake * eff_back / lay_odds
        # Budget covers back_stake + NO-token cost including Polymarket fee on order size.
        f = calculator._polymarket_fee_rate(lay_odds)
        back_stake = budget_usdc * lay_odds / (lay_odds + eff_back * (lay_odds - 1) * (1 + f))
        lay_stake  = back_stake * eff_back / lay_odds
    elif lay_prov == "sx_bet":
        # SX Bet per-slot binary No-token: same mechanics as Polymarket, no fee.
        # Actual USDC spent on No tokens = lay_stake × (lay_odds − 1).
        # Budget = back_stake + lay_stake × (lay_odds − 1).
        back_stake = budget_usdc * lay_odds / (lay_odds + eff_back * (lay_odds - 1))
        lay_stake  = back_stake * eff_back / lay_odds
    else:
        # Equal-profit condition: lay_stake = back_stake * eff_back / (lay_odds - c_lay)
        # Budget covers back_stake + lay_stake (backer's stake committed by layer).
        c_lay = calculator.PROVIDER_COMMISSION.get(lay_prov, 0.0)
        back_stake = budget_usdc * (lay_odds - c_lay) / (lay_odds - c_lay + eff_back)
        lay_stake  = back_stake * eff_back / (lay_odds - c_lay)

    return round(back_stake, 2), round(lay_stake, 2)


# ---------------------------------------------------------------------------
# Unsupported-provider check
# ---------------------------------------------------------------------------

def _unsupported_providers(providers: list[str]) -> set[str]:
    return {p for p in providers if p not in _SUPPORTED}


# ---------------------------------------------------------------------------
# Pre-placement balance checks
# ---------------------------------------------------------------------------

def check_balances_for_arb(
    arb_type:    str,
    arb:         dict,
    budget_usdc: float,
    settings,
    gbp_rate:    float | None,
) -> tuple[list[str], list[str]]:
    """Check that every leg of an arb has sufficient funds before placement.

    Returns (blocking_issues, warnings).
    - blocking_issues: confirmed shortfalls — each entry is a human-readable reason
      the arb MUST be skipped.
    - warnings: balance fetch failures where we could not verify coverage.

    Matchbook stakes are compared in GBP; Polymarket and SX Bet in USDC.
    For the Polymarket lay leg the effective spend is lay_stake × (lay_odds − 1).
    """
    from bet import pm_get_balance, mb_get_balance, sx_get_balance  # lazy import

    # --- Required amount per provider in that provider's native currency ---
    required: dict[str, float] = {}

    if arb_type == "sure_bet":
        for _slot, provider, _name, _odds, stake_usdc in _sure_bet_stakes(arb, budget_usdc):
            if provider == "matchbook":
                native = round(stake_usdc * gbp_rate, 2) if gbp_rate else stake_usdc
            else:
                native = stake_usdc
            required[provider] = required.get(provider, 0.0) + native

    else:  # back_lay
        back_stake, lay_stake = _back_lay_stakes(arb, budget_usdc)
        back_prov = arb["back_provider"]
        lay_prov  = arb["lay_provider"]

        # Back leg
        if back_prov == "matchbook":
            back_native = round(back_stake * gbp_rate, 2) if gbp_rate else back_stake
        else:
            back_native = back_stake
        required[back_prov] = required.get(back_prov, 0.0) + back_native

        # Lay leg — effective USDC/GBP spend differs by provider
        if lay_prov == "polymarket":
            _pm_f = calculator._polymarket_fee_rate(arb["lay_odds"])
            lay_native = round(lay_stake * (arb["lay_odds"] - 1) * (1 + _pm_f), 2)
        elif lay_prov == "matchbook":
            # Matchbook requires the full liability (backer_stake × (odds − 1)) in free funds.
            lay_liability = lay_stake * (arb["lay_odds"] - 1)
            lay_native = round(lay_liability * gbp_rate, 2) if gbp_rate else lay_liability
        elif lay_prov == "sx_bet":
            # SX Bet binary No-token: actual spend = lay_stake × (lay_odds − 1).
            lay_native = round(lay_stake * (arb["lay_odds"] - 1), 2)
        else:
            lay_native = lay_stake
        required[lay_prov] = required.get(lay_prov, 0.0) + lay_native

    if not required:
        return [], []

    # --- Fetch balances concurrently ---
    _getters = {
        "polymarket": pm_get_balance,
        "matchbook":  mb_get_balance,
        "sx_bet":     sx_get_balance,
    }
    balances: dict[str, float | None] = {}
    with ThreadPoolExecutor(max_workers=len(required)) as ex:
        futs = {ex.submit(_getters[p], settings): p for p in required if p in _getters}
        for f in as_completed(futs):
            p = futs[f]
            try:
                balances[p] = f.result()
            except Exception:
                balances[p] = None

    # --- Classify results ---
    blocking: list[str] = []
    warnings: list[str] = []
    for provider, needed in required.items():
        bal = balances.get(provider)
        cur = "GBP" if provider == "matchbook" else "USDC"
        if bal is None:
            warnings.append(
                f"{provider}: balance fetch failed — {cur} {needed:.2f} needed but unverified"
            )
        elif bal < needed:
            blocking.append(
                f"{provider}: need {cur} {needed:.2f}, have {cur} {bal:.2f}"
            )

    return blocking, warnings


# ---------------------------------------------------------------------------
# Pre-flight Polymarket liquidity check
# ---------------------------------------------------------------------------

def _pm_liquidity_issues(tasks: list[tuple[str, Callable, dict]]) -> list[str]:
    """Check order book depth for every Polymarket leg before any leg is placed.

    Returns a list of human-readable issues; empty means all clear.
    Fails open (no issue reported) when the CLOB API is unreachable.
    """
    from bet import pm_check_liquidity
    issues: list[str] = []
    for label, _fn, kwargs in tasks:
        if "polymarket" not in label.lower():
            continue
        token_id = kwargs.get("token_id")
        amount   = kwargs.get("amount")
        side     = kwargs.get("side", "BUY")
        if not token_id or not amount:
            continue
        ok, available = pm_check_liquidity(str(token_id), float(amount), str(side))
        if not ok:
            issues.append(
                f"{label}: need ${float(amount):.2f} but book depth is ~${available:.2f}"
            )
    return issues


# ---------------------------------------------------------------------------
# Main placement functions
# ---------------------------------------------------------------------------

def place_sure_bet(
    arb:         dict,
    game:        dict,
    budget_usdc: float,
    settings,
    gbp_rate:    float | None,
    dry_run:     bool = False,
) -> list[dict]:
    """
    Place all legs of a sure bet in platform order: matchbook → sx_bet → polymarket.

    Aborts after the first failure so a missed leg never leaves an uncovered position.
    Returns a list of result dicts — one per leg (including prep failures).
    Each result has at minimum {"platform": str, "ok": bool}.
    """
    from bet import pm_place_bet, mb_place_bet, sx_place_bet  # lazy import

    budget_usdc = _compute_kelly_budget(arb, "sure_bet", settings, gbp_rate, budget_usdc)
    if budget_usdc == 0.0:
        return [{"ok": False, "skipped": True, "error": "Kelly stake below minimum — arb skipped"}]

    legs = _sure_bet_stakes(arb, budget_usdc)
    if not legs:
        return [{"ok": False, "skipped": True,
                 "error": "Skipped: rounding error would create a loss on at least one outcome."}]

    min_stake = getattr(getattr(settings, "kelly", None), "min_stake_usdc", 1.50)
    too_small = [(prov, name, s) for _, prov, name, _, s in legs if s < min_stake]
    if too_small:
        details = ", ".join(f"{prov} ({name}) ${s:.2f}" for prov, name, s in too_small)
        print(f"  ⚠  Leg stake below minimum ${min_stake:.2f} — skipping arb: {details}", file=sys.stderr)
        return [{"ok": False, "skipped": True,
                 "error": f"Leg stake below minimum ${min_stake:.2f}: {details}"}]

    providers = [prov for _, prov, *_ in legs]
    if len(set(providers)) == 1:
        return [{"ok": False, "skipped": True,
                 "error": f"Auto-bet skipped: all legs are on the same platform ({providers[0]})."}]
    bad = _unsupported_providers(providers)
    if bad:
        return [{"ok": False, "skipped": True,
                 "error": f"Auto-bet skipped: provider(s) {bad} not supported for auto-placement."}]

    tasks:      list[tuple[str, Callable, dict]] = []
    pre_errors: list[dict] = []

    for slot, provider, outcome_name, raw_odds, stake_usdc in legs:
        label = f"{provider} ({outcome_name})"

        if provider == "polymarket":
            token_id = _pm_token_for(game, slot)
            if not token_id:
                pre_errors.append({
                    "platform": "Polymarket", "ok": False,
                    "error": f"No CLOB token ID stored for slot '{slot}'",
                })
                continue
            tasks.append((label, pm_place_bet, {
                "settings": settings,
                "token_id": token_id,
                "amount":   stake_usdc,
                "side":     "BUY",
                "dry_run":  dry_run,
            }))

        elif provider == "matchbook":
            mb_stake = round(stake_usdc * gbp_rate, 2) if gbp_rate else stake_usdc
            try:
                market_id, runner_id, _ = _resolve_mb_runner(game, outcome_name, settings)
            except Exception as exc:
                pre_errors.append({
                    "platform": "Matchbook", "ok": False,
                    "error": f"Runner resolution failed: {exc}",
                })
                continue
            if not market_id:
                pre_errors.append({
                    "platform": "Matchbook", "ok": False,
                    "error": f"No moneyline runner found for '{outcome_name}'",
                })
                continue
            tasks.append((label, mb_place_bet, {
                "settings":  settings,
                "event_id":  int(game["matchbook_event_id"]),
                "market_id": market_id,
                "runner_id": runner_id,
                "stake":     mb_stake,
                "side":      "back",
                "odds":      raw_odds,   # place at the arb odds (KEEP if unmatched)
                "dry_run":   dry_run,
            }))

        elif provider == "sx_bet":
            market_hash = game.get("sx_bet_market_hash")
            if not market_hash:
                pre_errors.append({
                    "platform": "SX Bet", "ok": False,
                    "error": "No sx_bet_market_hash in game context",
                })
                continue
            outcome = _sx_outcome_for(game, outcome_name)
            tasks.append((label, sx_place_bet, {
                "settings":    settings,
                "market_hash": market_hash,
                "amount":      stake_usdc,
                "outcome":     outcome,
                "take":        True,   # sure-bet → take best available price now
                "dry_run":     dry_run,
            }))

    results = list(pre_errors)
    if not tasks:
        return results

    if pre_errors:
        _on_leg_failure("sure_bet", arb, game, [], pre_errors[0], settings, dry_run)
        return results

    pm_issues = _pm_liquidity_issues(tasks)
    if pm_issues:
        for issue in pm_issues:
            print(f"  Liquidity check failed: {issue}", file=sys.stderr)
        issue_str = "; ".join(pm_issues)
        return [{"ok": False, "skipped": True,
                 "error": f"Polymarket liquidity insufficient: {issue_str}"}]

    t0 = time.monotonic()
    for label, fn, kwargs in sorted(tasks, key=lambda t: _platform_rank(t[0])):
        r = fn(**kwargs)
        r["_leg"] = label
        results.append(r)
        if not r.get("ok"):
            placed = [x for x in results if x.get("ok") and "_timing_s" not in x]
            _on_leg_failure("sure_bet", arb, game, placed, r, settings, dry_run)
            break
    results.append({"_timing_s": round(time.monotonic() - t0, 2)})
    if all_legs_ok(results) and not dry_run:
        _on_arb_success("sure_bet", arb, game, results, settings)
    return results


def place_back_lay_arb(
    arb:         dict,
    game:        dict,
    budget_usdc: float,
    settings,
    gbp_rate:    float | None,
    dry_run:     bool = False,
) -> list[dict]:
    """
    Place both legs of a back-lay arb in platform order: matchbook → sx_bet → polymarket.

    Aborts after the first failure so a missed leg never leaves an uncovered position.

    Lay leg mechanics by provider
    ------------------------------
    matchbook : native lay order — works for all market types.
    polymarket: BUY the NO CLOB token of the binary outcome market.
                Effective lay odds = 1/NO_price = 1/(1−YES_ask_price).
                Correct for football — NO token pays out on draw OR away win.
    sx_bet    : back the OPPOSITE outcome (2-outcome markets only).
                For football this is an approximation — does NOT cover draws.
                Use Polymarket NO token for proper football lay.

    Returns a list of result dicts — one per leg (including prep failures).
    """
    from bet import pm_place_bet, mb_place_bet, sx_place_bet  # lazy import

    back_prov    = arb["back_provider"]
    lay_prov     = arb["lay_provider"]
    outcome_name = arb["arb_outcome"]
    if back_prov == lay_prov:
        return [{"ok": False, "skipped": True,
                 "error": f"Auto-bet skipped: both legs are on the same platform ({back_prov})."}]
    bad = _unsupported_providers([back_prov, lay_prov])
    if bad:
        return [{"ok": False, "skipped": True,
                 "error": f"Auto-bet skipped: provider(s) {bad} not supported."}]

    budget_usdc = _compute_kelly_budget(arb, "back_lay", settings, gbp_rate, budget_usdc)
    if budget_usdc == 0.0:
        return [{"ok": False, "skipped": True, "error": "Kelly stake below minimum — arb skipped"}]

    back_stake, lay_stake = _back_lay_stakes(arb, budget_usdc)

    if lay_prov == "matchbook":
        mb_liability = lay_stake * (arb["lay_odds"] - 1)
        liability_line = f"  Matchbook liability: ~${mb_liability:.2f} USDC"
        if gbp_rate:
            liability_line += f"  (~£{mb_liability * gbp_rate:.2f})"
        print(liability_line)

    min_stake = getattr(getattr(settings, "kelly", None), "min_stake_usdc", 1.50)
    too_small = []
    if back_stake < min_stake:
        too_small.append(f"{back_prov} (back) ${back_stake:.2f}")
    if lay_stake < min_stake:
        too_small.append(f"{lay_prov} (lay) ${lay_stake:.2f}")
    if too_small:
        details = ", ".join(too_small)
        print(f"  ⚠  Leg stake below minimum ${min_stake:.2f} — skipping arb: {details}", file=sys.stderr)
        return [{"ok": False, "skipped": True,
                 "error": f"Leg stake below minimum ${min_stake:.2f}: {details}"}]

    # Determine outcome slot for Polymarket token lookups
    if _names_match(game.get("team1", ""), outcome_name):
        slot = "team1"
    elif outcome_name.lower() in ("draw", "tie"):
        slot = "draw"
    else:
        slot = "team2"

    tasks:      list[tuple[str, Callable, dict]] = []
    pre_errors: list[dict] = []

    # ── Back leg ──────────────────────────────────────────────────────────
    if back_prov == "polymarket":
        token_id = _pm_token_for(game, slot)
        if not token_id:
            pre_errors.append({
                "platform": "Polymarket", "ok": False,
                "error": f"No CLOB token ID for slot '{slot}'",
            })
        else:
            tasks.append((f"polymarket back ({outcome_name})", pm_place_bet, {
                "settings": settings,
                "token_id": token_id,
                "amount":   back_stake,
                "side":     "BUY",
                "dry_run":  dry_run,
            }))

    elif back_prov == "sx_bet":
        market_hash = game.get("sx_bet_market_hash")
        if not market_hash:
            pre_errors.append({
                "platform": "SX Bet", "ok": False,
                "error": "No sx_bet_market_hash in game context",
            })
        else:
            outcome = _sx_outcome_for(game, outcome_name)
            tasks.append((f"sx_bet back ({outcome_name})", sx_place_bet, {
                "settings":    settings,
                "market_hash": market_hash,
                "amount":      back_stake,
                "outcome":     outcome,
                "take":        True,
                "dry_run":     dry_run,
            }))

    elif back_prov == "matchbook":
        mb_stake = round(back_stake * gbp_rate, 2) if gbp_rate else back_stake
        try:
            market_id, runner_id, _ = _resolve_mb_runner(game, outcome_name, settings)
        except Exception as exc:
            pre_errors.append({"platform": "Matchbook back", "ok": False, "error": str(exc)})
            market_id = 0
        if market_id:
            tasks.append((f"matchbook back ({outcome_name})", mb_place_bet, {
                "settings":  settings,
                "event_id":  int(game["matchbook_event_id"]),
                "market_id": market_id,
                "runner_id": runner_id,
                "stake":     mb_stake,
                "side":      "back",
                "odds":      arb["back_odds"],
                "dry_run":   dry_run,
            }))

    # ── Lay leg ───────────────────────────────────────────────────────────
    if lay_prov == "matchbook":
        mb_lay_stake = round(lay_stake * gbp_rate, 2) if gbp_rate else lay_stake
        try:
            market_id, runner_id, _ = _resolve_mb_runner(game, outcome_name, settings)
        except Exception as exc:
            pre_errors.append({"platform": "Matchbook lay", "ok": False, "error": str(exc)})
            market_id = 0
        if market_id:
            tasks.append((f"matchbook lay ({outcome_name})", mb_place_bet, {
                "settings":  settings,
                "event_id":  int(game["matchbook_event_id"]),
                "market_id": market_id,
                "runner_id": runner_id,
                "stake":     mb_lay_stake,
                "side":      "lay",
                "odds":      arb["lay_odds"],
                "dry_run":   dry_run,
            }))

    elif lay_prov == "polymarket":
        # Buy the NO CLOB token — equivalent to a traditional exchange lay.
        # The NO token pays $1 when the outcome does NOT happen, covering all
        # non-event scenarios (including draws in 3-way football markets).
        no_token = _pm_no_token_for(game, slot, settings)
        if not no_token:
            pre_errors.append({
                "platform": "Polymarket lay", "ok": False,
                "error": (
                    f"Cannot resolve NO token for slot '{slot}'. "
                    "Ensure polymarket_{slot}_market_id is populated in the game index."
                ),
            })
        else:
            # NO-token cost = lay_stake × (lay_odds − 1) × (1 + fee_rate).
            # The Polymarket fee is charged on the order size (stake), so the fee
            # is an additional outlay on top of the raw token cost.
            _pm_f = calculator._polymarket_fee_rate(arb["lay_odds"])
            pm_no_usdc = round(lay_stake * (arb["lay_odds"] - 1) * (1 + _pm_f), 2)
            tasks.append((f"polymarket lay / NO-token ({outcome_name})", pm_place_bet, {
                "settings": settings,
                "token_id": no_token,
                "amount":   pm_no_usdc,
                "side":     "BUY",
                "dry_run":  dry_run,
            }))

    elif lay_prov == "sx_bet":
        # SX Bet soccer (UCL/EPL/UEL): each outcome has its own separate binary
        # market (identical structure to Polymarket Yes/No markets).
        #   outcomeOne = the labelled outcome (e.g. "Team A wins" / "Draw")
        #   outcomeTwo = the "No" outcome ("Team A does NOT win")
        # Lay = bet outcome='two' (No) on the slot-specific binary market.
        # This correctly covers ALL non-event scenarios including draws.
        #
        # Non-soccer / fallback: back the opposite outcome on the main market.
        # This is an approximation for 3-way markets (draw not covered).
        slot_hash = game.get(f"sx_bet_{slot}_market_hash")
        main_hash = game.get("sx_bet_market_hash")

        if slot_hash:
            # Soccer: per-slot binary market — buy the No token (outcomeTwo).
            # Cost = lay_stake × (lay_odds − 1), same as Polymarket No-token but no fee.
            sx_no_usdc = round(lay_stake * (arb["lay_odds"] - 1), 2)
            tasks.append((f"sx_bet lay / No ({outcome_name})", sx_place_bet, {
                "settings":    settings,
                "market_hash": slot_hash,
                "amount":      sx_no_usdc,
                "outcome":     "two",   # outcomeTwo = "No" = outcome doesn't happen
                "take":        True,
                "dry_run":     dry_run,
            }))
        elif main_hash:
            # Non-soccer 2-outcome market: back the opposite outcome.
            # For soccer leagues this is a fallback when per-slot hashes are missing —
            # backing the opposite team does NOT cover a draw. Re-run ids.py to fix.
            if game.get("league") in ("ucl", "epl", "uel", "seria", "laliga", "mls"):
                print(
                    f"  WARNING: SX Bet lay for '{outcome_name}' ({game.get('league')}) is using "
                    f"back-opposite fallback — draw outcomes are NOT covered. Re-run ids.py.",
                    file=sys.stderr,
                )
            sx_back_out = _sx_outcome_for(game, outcome_name)
            sx_lay_out  = "two" if sx_back_out == "one" else "one"
            tasks.append((f"sx_bet lay / back-opposite ({outcome_name})", sx_place_bet, {
                "settings":    settings,
                "market_hash": main_hash,
                "amount":      lay_stake,
                "outcome":     sx_lay_out,
                "take":        True,
                "dry_run":     dry_run,
            }))
        else:
            pre_errors.append({
                "platform": "SX Bet lay", "ok": False,
                "error": (
                    f"No sx_bet_{slot}_market_hash or sx_bet_market_hash in game context. "
                    "Re-run ids.py to populate per-slot hashes for soccer games."
                ),
            })

    results = list(pre_errors)
    if not tasks:
        return results if results else [{"ok": False, "error": "No executable legs built"}]

    pm_issues = _pm_liquidity_issues(tasks)
    if pm_issues:
        for issue in pm_issues:
            print(f"  Liquidity check failed: {issue}", file=sys.stderr)
        issue_str = "; ".join(pm_issues)
        return [{"ok": False, "skipped": True,
                 "error": f"Polymarket liquidity insufficient: {issue_str}"}]

    t0 = time.monotonic()
    for label, fn, kwargs in sorted(tasks, key=lambda t: _platform_rank(t[0])):
        r = fn(**kwargs)
        r["_leg"] = label
        results.append(r)
        if not r.get("ok"):
            placed = [x for x in results if x.get("ok") and "_timing_s" not in x]
            _on_leg_failure("back_lay", arb, game, placed, r, settings, dry_run)
            break
    results.append({"_timing_s": round(time.monotonic() - t0, 2)})
    if all_legs_ok(results) and not dry_run:
        _on_arb_success("back_lay", arb, game, results, settings)
    return results


# ---------------------------------------------------------------------------
# Display helper
# ---------------------------------------------------------------------------

def all_legs_placed(results: list[dict]) -> bool:
    """Return True only when every executable leg succeeded live (no errors, no dry runs)."""
    legs = [r for r in results if "_timing_s" not in r and "skipped" not in r]
    return bool(legs) and all(r.get("ok") and not r.get("dry_run") for r in legs)


def all_legs_ok(results: list[dict]) -> bool:
    """Return True when every executable leg has ok=True (includes dry-run legs)."""
    legs = [r for r in results if "_timing_s" not in r and "skipped" not in r]
    return bool(legs) and all(r.get("ok") for r in legs)


def print_bet_results(results: list[dict], indent: str = "    ") -> None:
    """Print placement results in a compact human-readable format."""
    timing = None
    for r in results:
        if "_timing_s" in r:
            timing = r["_timing_s"]
            continue
        if "skipped" in r:
            print(f"{indent}⚠  {r['error']}")
            continue
        leg   = r.get("_leg", r.get("platform", "?"))
        ok    = r.get("ok", False)
        if ok:
            dry    = r.get("dry_run")
            amount = r.get("amount")
            odds   = r.get("decimal_odds")
            stake_str = f"  ${amount:.2f} USDC" if amount is not None else ""
            odds_str  = f"  @ {odds:.3f}" if odds is not None else ""
            if dry:
                print(f"{indent}✓  {leg}{stake_str}{odds_str}  [DRY RUN — not submitted]")
            else:
                offer_id   = r.get("offer_id")
                order_hash = r.get("order_hash")
                resp       = r.get("response", {})
                detail = stake_str + odds_str
                if offer_id:
                    detail += f"  offer_id={offer_id}  status={r.get('status')}  matched={r.get('matched', 0)}"
                elif order_hash:
                    detail += f"  order_hash={str(order_hash)[:20]}…"
                elif resp:
                    detail += f"  {str(resp)[:80]}"
                print(f"{indent}✓  {leg}{detail}")
        else:
            print(f"{indent}✗  {leg}  ERROR: {r.get('error', '?')}")
    if timing is not None:
        print(f"{indent}   placed in {timing:.2f}s")
