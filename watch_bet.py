"""
watch_bet.py
------------
Autonomous 24/7 daemon: runs ids.py then scan.py in a loop forever.

Features
--------
- Periodic ids.py refresh to keep market IDs current
- Per-process timeout: kills a hung scan.py or ids.py
- Exponential backoff after crashes: 30s → 60s → 120s → 300s (capped)
- Circuit breaker: alert + extended pause after N consecutive crashes
- Graceful halt on FAILED_LEG: scan.py exits 2 when bet_executor._HALT is set;
  the daemon stops and alerts rather than restarting into a broken state
- Heartbeat file (outputs/heartbeat.txt) updated after every scan cycle

Exit codes from scan.py that the daemon treats specially:
  0 → success (keep running)
  1 → crash (backoff + retry)
  2 → HALT (failed-leg incident — stop and alert, require human restart)
  3 → BANKROLL (balance below minimum — 24h cooldown, retry up to 3× total, then stop)

Usage
-----
    py watch_bet.py --budget 50 --providers polymarket sx_bet --min-profit 0.5
    py watch_bet.py --budget 50 --log daemon.log --interval 1200
    Get-Content daemon.log -Wait -Tail 30    # follow log on Windows
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT / "src"))

_HEARTBEAT = _ROOT / "outputs" / "heartbeat.txt"
_BET_LOG   = _ROOT / "outputs" / "bet_log.jsonl"
_SEP = "─" * 60

# Backoff schedule (seconds) indexed by consecutive crash count (capped at last value)
_BACKOFF = [30, 60, 120, 300]

# scan.py exit code that signals a halted state requiring human review
_EXIT_HALT = 2
# scan.py exit code that signals bankroll is below the minimum threshold
_EXIT_BANKROLL = 3


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _run_subprocess(
    cmd: list[str],
    timeout_secs: int,
    label: str,
) -> tuple[int, bool]:
    """
    Run a subprocess with real-time stdout streaming and a hard timeout.
    Returns (exit_code, timed_out).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    assert proc.stdout is not None

    def _stream() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            print(line, end="", flush=True)

    reader = threading.Thread(target=_stream, daemon=True)
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_secs)
    except subprocess.TimeoutExpired:
        _log(f"  ⚠  {label} timed out after {timeout_secs}s — killing process")
        proc.kill()
        timed_out = True

    reader.join(timeout=5)
    return proc.returncode or 0, timed_out


def _write_heartbeat(state: dict) -> None:
    _HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    _HEARTBEAT.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _session_summary(started_at: str, bet_log: Path) -> None:
    """Print a short summary of arbs placed during this daemon session."""
    _log(_SEP)
    _log("  SESSION SUMMARY")
    _log(_SEP)
    if not bet_log.exists():
        _log("  No arbs placed this session.")
        return
    session_start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    placed: list[dict] = []
    with bet_log.open(encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if entry.get("status") not in ("PLACED", "DRY_RUN"):
                continue
            try:
                ts = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if ts >= session_start:
                placed.append(entry)
    if not placed:
        _log("  No arbs placed this session.")
        return
    _log(f"  {len(placed)} arb(s) placed:")
    for e in placed:
        team1    = e.get("team1") or "?"
        team2    = e.get("team2") or "?"
        league   = (e.get("league") or "?").upper()
        arb_type = e.get("arb_type") or "?"
        profit   = e.get("profit_pct")
        profit_s = f"+{profit:.2f}%" if profit is not None else "?"
        ts_s     = (e.get("timestamp") or "")[:16].replace("T", " ")
        dry_tag  = "  (dry)" if e.get("status") == "DRY_RUN" else ""
        _log(f"    {ts_s}  {team1} vs {team2}  [{league}]  {arb_type}  {profit_s}{dry_tag}")


def _send_alert(subject: str, body: str, settings) -> None:
    try:
        from matched_betting import notifier
        notifier.send_alert(subject, body, settings)
    except Exception as exc:
        _log(f"  (alert delivery failed: {exc})")
        _log(f"  ALERT: {subject} — {body}")


def _build_ids_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, "ids.py"]
    if args.providers:
        cmd += ["--providers"] + args.providers
    if args.leagues:
        cmd += ["--leagues"] + args.leagues
    return cmd


def _build_scan_cmd(args: argparse.Namespace, disabled_providers: set[str]) -> list[str]:
    cmd = [sys.executable, "scan.py", "--auto-bet", "--budget", str(args.budget)]
    active = [p for p in (args.providers or []) if p not in disabled_providers]
    if active:
        cmd += ["--providers"] + active
    if args.leagues:
        cmd += ["--leagues"] + args.leagues
    cmd += ["--min-profit", str(args.min_profit)]
    if args.bet_dry_run:
        cmd.append("--bet-dry-run")
    if args.allow_topup:
        cmd.append("--allow-topup")
    if args.scan_delay > 0:
        cmd += ["--scan-delay", str(args.scan_delay)]
    return cmd


def _backoff_secs(crash_count: int) -> int:
    idx = min(crash_count - 1, len(_BACKOFF) - 1)
    return _BACKOFF[idx]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="24/7 matched-betting daemon: runs ids.py + scan.py in a loop."
    )
    parser.add_argument("--interval", type=int, default=600, metavar="SECS",
                        help="Seconds between scan.py runs (default: 600).")
    parser.add_argument("--ids-refresh-interval", type=int, default=360, metavar="MINS",
                        help="Minutes between ids.py re-runs (default: 360).")
    parser.add_argument("--scan-timeout", type=int, default=600, metavar="SECS",
                        help="Kill scan.py after this many seconds (default: 600).")
    parser.add_argument("--ids-timeout", type=int, default=600, metavar="SECS",
                        help="Kill ids.py after this many seconds (default: 600).")
    parser.add_argument("--max-restarts", type=int, default=10, metavar="N",
                        help="Consecutive crash limit before circuit-breaker pause (default: 10).")
    parser.add_argument("--pause-on-max-restarts", type=int, default=3600, metavar="SECS",
                        help="How long to pause after hitting the crash limit (default: 3600).")
    parser.add_argument("--budget", type=float, default=50.0, metavar="USDC",
                        help="Max stake budget passed to scan.py --budget (default: 50).")
    parser.add_argument("--min-profit", type=float, default=0.0, metavar="PCT",
                        help="Min net profit %% passed to scan.py (default: 0.0).")
    parser.add_argument("--providers", nargs="+", metavar="PROVIDER",
                        help="Providers passed to ids.py and scan.py.")
    parser.add_argument("--leagues", nargs="+", metavar="LEAGUE",
                        help="Leagues passed to ids.py and scan.py.")
    parser.add_argument("--bet-dry-run", action="store_true",
                        help="Pass --bet-dry-run to every scan.py run (simulate bets, no real placement).")
    parser.add_argument("--allow-topup", action="store_true",
                        help="Pass --allow-topup to every scan.py run (incremental Kelly top-ups on improved arbs).")
    parser.add_argument("--scan-delay", type=float, default=0.0, metavar="SECS",
                        help="Extra pause between games passed to scan.py (default: 0.0). "
                             "Use to reduce Matchbook request rate.")
    parser.add_argument("--skip-initial-ids", action="store_true",
                        help="Skip the ids.py run at startup and use the existing IDs file.")
    parser.add_argument("--redeem-interval", type=int, default=480, metavar="MINS",
                        help="Minutes between Polymarket redemption runs and platform re-enable checks (default: 480 = 8h, 0 = disabled).")
    parser.add_argument("--platform-balance-threshold", type=float, default=12.0, metavar="USD",
                        help="USD free-funds floor; a platform is disabled when its balance drops below this (default: 12.0).")
    parser.add_argument("--redeem-timeout", type=int, default=300, metavar="SECS",
                        help="Kill the redeem run after this many seconds (default: 300).")
    parser.add_argument("--log", metavar="FILE", default=None,
                        help="Write all output to FILE (headless mode). "
                             "Monitor with: Get-Content FILE -Wait -Tail 30")
    args = parser.parse_args()

    # ── Headless mode ─────────────────────────────────────────────────────
    if args.log:
        sys.stdout = open(args.log, "w", encoding="utf-8", buffering=1)
    elif hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # ── Load settings for notifications ───────────────────────────────────
    try:
        from matched_betting.config import load_settings
        settings = load_settings(_ROOT)
    except Exception as exc:
        _log(f"  ⚠  Could not load settings for notifications: {exc}")
        settings = None

    started_at = _now()
    scan_count = 0
    crash_count = 0
    disabled_providers: set[str] = set()
    last_ids_refresh: float | None = time.monotonic() if args.skip_initial_ids else None
    last_redeem:      float | None = None  # None = run on first iteration

    mode = "DRY RUN" if args.bet_dry_run else "LIVE"
    _log(_SEP)
    _log(f"  watch_bet daemon starting  [{mode}]")
    _log(f"  budget={args.budget} USDC  min-profit={args.min_profit}%  "
         f"interval={args.interval}s  ids-refresh={args.ids_refresh_interval}m")
    redeem_note = f"every {args.redeem_interval}m" if args.redeem_interval else "disabled"
    _log(f"  pm-redeem/platform-check={redeem_note}  balance-threshold=${args.platform_balance_threshold:.2f}")
    if args.skip_initial_ids:
        _log(f"  --skip-initial-ids: using existing IDs file, next refresh in {args.ids_refresh_interval}m")
    _log(_SEP)

    while True:
        now = time.monotonic()

        # ── 1. Refresh IDs if stale ────────────────────────────────────────
        ids_age_mins = (now - last_ids_refresh) / 60 if last_ids_refresh is not None else None
        ids_stale = last_ids_refresh is None or ids_age_mins >= args.ids_refresh_interval
        if ids_stale:
            _log(_SEP)
            _log(f"  IDS REFRESH (age={ids_age_mins:.0f}m)" if ids_age_mins else "  IDS REFRESH (startup)")
            _log(_SEP)
            ids_cmd = _build_ids_cmd(args)
            try:
                ids_rc, ids_timeout = _run_subprocess(ids_cmd, args.ids_timeout, "ids.py")
            except KeyboardInterrupt:
                _log("  Interrupted.")
                break
            if ids_timeout or ids_rc != 0:
                _log(f"  ⚠  ids.py finished with issues (exit={ids_rc}, timeout={ids_timeout}) — "
                     f"continuing with existing IDs")
            else:
                _log(f"  ✓  IDs refreshed")
            last_ids_refresh = time.monotonic()

        # ── 1.5. Redeem Polymarket + re-enable platforms if due ──────────────
        if args.redeem_interval > 0:
            redeem_age_mins = (now - last_redeem) / 60 if last_redeem is not None else None
            redeem_due = last_redeem is None or redeem_age_mins >= args.redeem_interval
            if redeem_due:
                _log(_SEP)
                _log(f"  PM REDEEM + PLATFORM CHECK (age={redeem_age_mins:.0f}m)" if redeem_age_mins else "  PM REDEEM + PLATFORM CHECK (startup)")
                _log(_SEP)
                redeem_cmd = [sys.executable, "portfolio.py", "--redeem-pm"]
                try:
                    redeem_rc, redeem_timeout = _run_subprocess(
                        redeem_cmd, args.redeem_timeout, "portfolio.py --redeem-pm"
                    )
                except KeyboardInterrupt:
                    _log("  Interrupted.")
                    break
                if redeem_timeout or redeem_rc != 0:
                    _log(f"  WARNING: Redeem finished with issues (exit={redeem_rc}, timeout={redeem_timeout}) -- continuing")
                else:
                    _log("  Redemption complete")
                # Re-enable any platform whose balance is now above the threshold.
                if disabled_providers and settings:
                    try:
                        sys.path.insert(0, str(_ROOT / "src"))
                        from bet_executor import get_platform_balances_usd
                        recheck = get_platform_balances_usd(settings, providers=list(args.providers or []))
                        newly_enabled = {
                            p for p in disabled_providers
                            if (recheck.get(p) or 0.0) >= args.platform_balance_threshold
                        }
                        if newly_enabled:
                            disabled_providers -= newly_enabled
                            _log(f"  Re-enabled providers: {', '.join(sorted(newly_enabled))}")
                        else:
                            _log(f"  No providers re-enabled (still below ${args.platform_balance_threshold:.2f}): "
                                 f"{', '.join(sorted(disabled_providers))}")
                    except Exception as exc:
                        _log(f"  (platform re-enable check failed: {exc})")
                last_redeem = time.monotonic()

        # ── 2. Run scan ────────────────────────────────────────────────────
        active_providers = [p for p in (args.providers or []) if p not in disabled_providers]
        if not active_providers:
            _log("  All providers disabled (low balance) -- skipping scan until next platform check.")
            try:
                time.sleep(args.interval)
            except KeyboardInterrupt:
                _log("  Interrupted.")
                break
            continue

        _log(_SEP)
        _log(f"  SCAN #{scan_count + 1}" + (f"  (disabled: {', '.join(sorted(disabled_providers))})" if disabled_providers else ""))
        _log(_SEP)
        scan_cmd = _build_scan_cmd(args, disabled_providers)
        try:
            scan_rc, scan_timeout = _run_subprocess(scan_cmd, args.scan_timeout, "scan.py")
            scan_count += 1
        except KeyboardInterrupt:
            _log("  Interrupted.")
            break

        # ── 2.5. Check per-platform balances after scan ───────────────────
        if settings:
            try:
                sys.path.insert(0, str(_ROOT / "src"))
                from bet_executor import get_platform_balances_usd
                balances_usd = get_platform_balances_usd(settings, providers=list(args.providers or []))
                for prov, bal in balances_usd.items():
                    if bal is not None and bal < args.platform_balance_threshold and prov not in disabled_providers:
                        disabled_providers.add(prov)
                        _log(f"  WARNING: {prov} free funds ${bal:.2f} below ${args.platform_balance_threshold:.2f} -- disabled from scan")
                        _send_alert(
                            f"{prov} disabled - low balance",
                            f"Free funds: ${bal:.2f} USD (threshold ${args.platform_balance_threshold:.2f})",
                            settings,
                        )
            except Exception as exc:
                _log(f"  (balance check failed: {exc})")

        # ── 3. Update heartbeat ────────────────────────────────────────────
        _write_heartbeat({
            "pid":                  os.getpid(),
            "started_at":           started_at,
            "last_scan_at":         _now(),
            "last_ids_refresh_at":  datetime.fromtimestamp(
                                        last_ids_refresh, tz=timezone.utc
                                    ).isoformat().replace("+00:00", "Z"),
            "last_redeem_at":       (
                datetime.fromtimestamp(last_redeem, tz=timezone.utc)
                .isoformat().replace("+00:00", "Z")
                if last_redeem is not None else None
            ),
            "scan_count":           scan_count,
            "consecutive_crashes":  crash_count,
            "disabled_providers":   sorted(disabled_providers),
        })

        # ── 4. Handle halt (failed-leg incident) ──────────────────────────
        if scan_rc == _EXIT_HALT:
            _log("  DAEMON STOPPED -- scan.py exited with HALT code (failed leg placement).")
            _log("  Review outputs/bet_log.jsonl for the unhedged position, then restart manually.")
            if settings:
                _send_alert(
                    "watch_bet DAEMON STOPPED - FAILED LEG",
                    "scan.py exited with halt code 2. "
                    "Check outputs/bet_log.jsonl for the unhedged position.",
                    settings,
                )
            break

        # ── 4b. exit code 3 (old bankroll halt) — now handled by per-platform disabling ──
        if scan_rc == _EXIT_BANKROLL:
            _log("  (exit code 3: old bankroll signal -- per-platform balance check will handle low funds)")
            # fall through to normal handling

        # ── 5. Handle crash / timeout ─────────────────────────────────────
        if scan_timeout or (scan_rc != 0 and scan_rc != _EXIT_BANKROLL):
            crash_count += 1
            backoff = _backoff_secs(crash_count)
            reason = f"timeout after {args.scan_timeout}s" if scan_timeout else f"exit code {scan_rc}"
            _log(f"  WARNING: scan.py failed ({reason}) -- crash #{crash_count}, backing off {backoff}s")

            if crash_count > args.max_restarts:
                pause = args.pause_on_max_restarts
                _log(f"  CIRCUIT BREAKER: {crash_count} consecutive crashes -- pausing {pause}s")
                if settings:
                    _send_alert(
                        f"watch_bet circuit breaker: {crash_count} crashes",
                        f"scan.py has crashed {crash_count} times in a row ({reason}). "
                        f"Pausing {pause}s before resuming.",
                        settings,
                    )
                try:
                    time.sleep(pause)
                except KeyboardInterrupt:
                    _log("  Interrupted during pause.")
                    break
                crash_count = 0
                last_ids_refresh = None  # force IDs re-fetch after long pause
            else:
                try:
                    time.sleep(backoff)
                except KeyboardInterrupt:
                    _log("  Interrupted.")
                    break
            continue

        # ── 6. Success — sleep until next scan ────────────────────────────
        crash_count = 0
        _log(f"  Scan #{scan_count} complete. Next scan in {args.interval}s.")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            _log("  Interrupted.")
            break

    _session_summary(started_at, _BET_LOG)
    _log(_SEP)
    _log("  watch_bet daemon stopped.")
    _log(_SEP)


if __name__ == "__main__":
    main()
