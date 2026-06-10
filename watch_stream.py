"""
watch_stream.py
---------------
Autonomous 24/7 stream daemon: runs stream.py in a monitored loop forever.

Unlike watch_bet.py (which runs short-lived scan.py subprocesses), stream.py is a
long-running WebSocket process.  The daemon polls it at regular intervals to:
  - check it is still alive
  - check per-platform balances
  - send heartbeat pings
  - refresh market IDs (by killing + restarting stream.py cleanly)
  - check Polymarket status page

Exit codes from stream.py that the daemon treats specially:
  0 → clean exit / KeyboardInterrupt (keep running)
  1 → crash (backoff + retry)
  2 → HALT (failed-leg — stop and alert, require human restart)

Usage
-----
    py watch_stream.py --budget 50 --min-profit 0.5
    py watch_stream.py --budget 50 --log stream_daemon.log
    Get-Content stream_daemon.log -Wait -Tail 30    # follow log on Windows
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

_HEARTBEAT  = _ROOT / "outputs" / "stream_heartbeat.txt"
_BET_LOG    = _ROOT / "outputs" / "stream_bet_log.jsonl"
_SEP        = "-" * 60

_BACKOFF = [30, 60, 120, 300]

_EXIT_HALT = 2

_HEARTBEAT_NTFY_INTERVAL = 3600   # ntfy alive ping every hour
_POLL_INTERVAL           = 30     # seconds between alive-checks while stream.py is running

_PM_STATUS_URL       = "https://status.polymarket.com/api/v2/components.json"
_PM_DEGRADED_STATUSES = {"degraded_performance", "partial_outage", "major_outage", "under_maintenance"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_polymarket_status() -> tuple[bool, str]:
    import urllib.request
    try:
        req = urllib.request.Request(_PM_STATUS_URL, headers={"User-Agent": "mbv3-watchstream/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        degraded = [
            f"{c.get('name', 'unknown')}: {c.get('status')}"
            for c in data.get("components", [])
            if c.get("status") in _PM_DEGRADED_STATUSES
        ]
        if degraded:
            return False, "; ".join(degraded)
        return True, ""
    except Exception:
        return True, ""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _log(msg: str) -> None:
    print(f"[{_now()}] {msg}", flush=True)


def _write_heartbeat(state: dict) -> None:
    _HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    _HEARTBEAT.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _send_alert(subject: str, body: str, settings) -> None:
    try:
        from matched_betting import notifier
        notifier.send_alert(subject, body, settings)
    except Exception as exc:
        _log(f"  (alert delivery failed: {exc})")
        _log(f"  ALERT: {subject} -- {body}")


def _send_heartbeat(subject: str, body: str, settings) -> None:
    try:
        from matched_betting import notifier
        notifier.send_heartbeat(subject, body, settings)
    except Exception as exc:
        _log(f"  (heartbeat delivery failed: {exc})")


def _backoff_secs(crash_count: int) -> int:
    idx = min(crash_count - 1, len(_BACKOFF) - 1)
    return _BACKOFF[idx]


def _session_summary(started_at: str, bet_log: Path) -> None:
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


# ---------------------------------------------------------------------------
# subprocess management
# ---------------------------------------------------------------------------

def _start_stream(cmd: list[str]) -> subprocess.Popen:
    """Launch stream.py and return the Popen handle.  stdout+stderr are merged and streamed."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env={**os.environ, "PYTHONUTF8": "1"},
    )

    def _stream_output() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            print(line, end="", flush=True)

    t = threading.Thread(target=_stream_output, daemon=True)
    t.start()
    return proc


def _kill(proc: subprocess.Popen, label: str = "stream.py") -> None:
    try:
        proc.kill()
        proc.wait(timeout=10)
    except Exception:
        pass
    _log(f"  [{label}] process killed")


# ---------------------------------------------------------------------------
# command builders
# ---------------------------------------------------------------------------

def _build_ids_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, "ids.py"]
    if args.providers:
        cmd += ["--providers"] + args.providers
    if args.leagues:
        cmd += ["--leagues"] + args.leagues
    return cmd


def _build_stream_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable, "stream.py",
        "--stream-leagues",
    ] + args.leagues + [
        "--min-profit", str(args.min_profit),
        "--budget",     str(args.budget),
        "--autobet",
        "--autobet-age",   str(args.autobet_age),
        "--autobet-delay", str(args.autobet_delay),
    ]
    if args.autobet_min_profit is not None:
        cmd += ["--autobet-min-profit", str(args.autobet_min_profit)]
    if args.no_matchbook:
        cmd.append("--no-matchbook")
    elif args.poll_only_mb:
        cmd.append("--no-mb-ondemand")
    if args.min_start > 0:
        cmd += ["--min-start", str(args.min_start)]
    if args.bet_dry_run:
        cmd.append("--bet-dry-run")
    if args.autobet_test:
        cmd.append("--autobet-test")
    if args.debug:
        cmd.append("--debug")
    return cmd


# ---------------------------------------------------------------------------
# main daemon loop
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="24/7 stream daemon: keeps stream.py running with IDs refresh + crash backoff."
    )
    parser.add_argument("--ids-refresh-interval", type=int, default=360, metavar="MINS",
                        help="Minutes between ids.py re-runs (IDs refreshed via kill+restart, default: 360).")
    parser.add_argument("--ids-timeout", type=int, default=600, metavar="SECS",
                        help="Kill ids.py after this many seconds (default: 600).")
    parser.add_argument("--max-restarts", type=int, default=10, metavar="N",
                        help="Consecutive crash limit before circuit-breaker pause (default: 10).")
    parser.add_argument("--pause-on-max-restarts", type=int, default=3600, metavar="SECS",
                        help="How long to pause after hitting the crash limit (default: 3600).")
    parser.add_argument("--budget", type=float, default=50.0, metavar="USDC",
                        help="Max stake budget passed to stream.py (default: 50).")
    parser.add_argument("--min-profit", type=float, default=0.0, metavar="PCT",
                        help="Min net profit %% passed to stream.py (default: 0.0).")
    parser.add_argument("--autobet-min-profit", type=float, default=None, metavar="PCT",
                        help="Autobet-specific profit threshold (default: same as --min-profit).")
    parser.add_argument("--autobet-age", type=float, default=30.0, metavar="SECS",
                        help="Seconds an arb must be continuously live before placing (default: 30).")
    parser.add_argument("--autobet-delay", type=float, default=0.0, metavar="SECS",
                        help="Extra wait after the age gate before placing (default: 0).")
    parser.add_argument("--providers", nargs="+", default=["polymarket", "sx_bet"],
                        metavar="PROVIDER", help="Providers for ids.py.")
    parser.add_argument("--leagues", nargs="+", default=["NBA", "NHL", "MLB"],
                        metavar="LEAGUE", help="Leagues for stream.py and ids.py.")
    parser.add_argument("--no-matchbook", action="store_true",
                        help="Disable Matchbook completely.")
    parser.add_argument("--poll-only-mb", action="store_true",
                        help="Matchbook poll only, no on-demand fetches.")
    parser.add_argument("--min-start", type=float, default=10.0, metavar="MINS",
                        help="Exclude games starting within N minutes (default: 10).")
    parser.add_argument("--bet-dry-run", action="store_true",
                        help="Pass --bet-dry-run to stream.py (simulate bets only).")
    parser.add_argument("--autobet-test", action="store_true",
                        help="Pass --autobet-test to stream.py (flat $5 test mode).")
    parser.add_argument("--skip-initial-ids", action="store_true",
                        help="Skip ids.py at startup and use the existing IDs file.")
    parser.add_argument("--platform-balance-threshold", type=float, default=12.0, metavar="USD",
                        help="USD free-funds floor; logs a warning when a platform drops below this (default: 12.0).")
    parser.add_argument("--debug", action="store_true", help="Pass --debug to stream.py.")
    parser.add_argument("--log", metavar="FILE", default=None,
                        help="Write all daemon output to FILE (headless mode).")
    args = parser.parse_args()

    if args.log:
        sys.stdout = open(args.log, "w", encoding="utf-8", buffering=1)
    elif hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        from matched_betting.config import load_settings
        settings = load_settings(_ROOT)
    except Exception as exc:
        _log(f"  WARNING: Could not load settings for notifications: {exc}")
        settings = None

    started_at   = _now()
    daemon_start = time.monotonic()
    run_count    = 0
    crash_count  = 0

    last_ids_refresh: float | None = time.monotonic() if args.skip_initial_ids else None
    last_heartbeat_ntfy: float | None = None

    mode = "DRY RUN" if args.bet_dry_run else "LIVE"
    _log(_SEP)
    _log(f"  watch_stream daemon starting  [{mode}]")
    _log(f"  budget={args.budget} USDC  min-profit={args.min_profit}%  "
         f"autobet-age={args.autobet_age:.0f}s  "
         f"ids-refresh={args.ids_refresh_interval}m  max-restarts={args.max_restarts}")
    if args.skip_initial_ids:
        _log(f"  --skip-initial-ids: using existing IDs file, next refresh in {args.ids_refresh_interval}m")
    _log(_SEP)

    while True:
        now = time.monotonic()

        # ── 1. Run ids.py if stale ──────────────────────────────────────────
        ids_age_mins = (now - last_ids_refresh) / 60 if last_ids_refresh is not None else None
        ids_stale    = last_ids_refresh is None or ids_age_mins >= args.ids_refresh_interval
        if ids_stale:
            _log(_SEP)
            label = f"  IDS REFRESH (age={ids_age_mins:.0f}m)" if ids_age_mins else "  IDS REFRESH (startup)"
            _log(label)
            _log(_SEP)
            ids_cmd = _build_ids_cmd(args)
            try:
                ids_proc = subprocess.run(
                    ids_cmd,
                    timeout=args.ids_timeout,
                    cwd=str(_ROOT),
                    env={**os.environ, "PYTHONUTF8": "1"},
                )
                if ids_proc.returncode != 0:
                    _log(f"  WARNING: ids.py exited {ids_proc.returncode} -- continuing with existing IDs")
                else:
                    _log("  IDs refreshed OK")
            except subprocess.TimeoutExpired:
                _log(f"  WARNING: ids.py timed out after {args.ids_timeout}s -- continuing with existing IDs")
            except KeyboardInterrupt:
                _log("  Interrupted.")
                break
            last_ids_refresh = time.monotonic()

        # ── 2. Launch stream.py ─────────────────────────────────────────────
        stream_cmd = _build_stream_cmd(args)
        run_count += 1
        _log(_SEP)
        _log(f"  STREAM RUN #{run_count}  (crash streak: {crash_count})")
        _log(f"  $ {' '.join(stream_cmd)}")
        _log(_SEP)

        try:
            proc = _start_stream(stream_cmd)
        except KeyboardInterrupt:
            _log("  Interrupted before stream started.")
            break

        # ── 3. Poll loop while stream.py is alive ───────────────────────────
        stream_rc: int | None = None
        next_ids_kill: float | None = None
        if last_ids_refresh is not None:
            next_ids_kill = last_ids_refresh + args.ids_refresh_interval * 60

        try:
            while True:
                # Check if process has exited
                rc = proc.poll()
                if rc is not None:
                    stream_rc = rc
                    break

                time.sleep(_POLL_INTERVAL)
                now = time.monotonic()

                # ── 3a. IDs refresh: kill + restart ───────────────────────
                if next_ids_kill is not None and now >= next_ids_kill:
                    _log("  IDs refresh due -- killing stream.py for clean restart")
                    _kill(proc, "stream.py")
                    stream_rc = None  # signal: not a crash, restart intentionally
                    last_ids_refresh = None  # will trigger ids.py run at top of outer loop
                    break

                # ── 3b. Heartbeat ─────────────────────────────────────────
                heartbeat_age = (now - last_heartbeat_ntfy) if last_heartbeat_ntfy is not None else None
                if last_heartbeat_ntfy is None or heartbeat_age >= _HEARTBEAT_NTFY_INTERVAL:
                    uptime_mins = int((now - daemon_start) / 60)
                    _send_heartbeat(
                        "watch_stream alive",
                        f"Stream run #{run_count}. Uptime: {uptime_mins}m.",
                        settings,
                    )
                    last_heartbeat_ntfy = time.monotonic()

                # ── 3c. Per-platform balance warning ─────────────────────
                if settings:
                    try:
                        from bet_executor import get_platform_balances_usd
                        balances_usd = get_platform_balances_usd(settings, providers=list(args.providers or []))
                        for prov, bal in balances_usd.items():
                            if bal is not None and bal < args.platform_balance_threshold:
                                _log(f"  WARNING: {prov} free funds ${bal:.2f} below "
                                     f"${args.platform_balance_threshold:.2f}")
                    except Exception:
                        pass

                # ── 3d. Polymarket status ─────────────────────────────────
                if "polymarket" in (args.providers or []):
                    pm_ok, pm_reason = _check_polymarket_status()
                    if not pm_ok:
                        _log(f"  WARNING: Polymarket status degraded ({pm_reason}) -- stream continues")
                        if settings:
                            _send_alert(
                                "Polymarket status degraded",
                                pm_reason,
                                settings,
                            )

                # ── 3e. Write heartbeat file ──────────────────────────────
                _write_heartbeat({
                    "pid":                 os.getpid(),
                    "stream_pid":          proc.pid,
                    "started_at":          started_at,
                    "last_checked_at":     _now(),
                    "run_count":           run_count,
                    "consecutive_crashes": crash_count,
                })

        except KeyboardInterrupt:
            _log("  Interrupted -- killing stream.py")
            _kill(proc)
            break

        # ── 4. Handle exit code ─────────────────────────────────────────────

        # IDs-refresh restart (stream_rc is None after intentional kill)
        if stream_rc is None:
            crash_count = 0
            _log("  Stream stopped for IDs refresh -- restarting")
            continue

        _log(f"  stream.py exited with code {stream_rc}")

        if stream_rc == _EXIT_HALT:
            _log("  DAEMON STOPPED -- HALT code 2 (failed leg placement).")
            _log("  Review outputs/stream_bet_log.jsonl for the unhedged position, then restart manually.")
            if settings:
                _send_alert(
                    "watch_stream DAEMON STOPPED - FAILED LEG",
                    "stream.py exited with halt code 2. "
                    "Check outputs/stream_bet_log.jsonl for the unhedged position.",
                    settings,
                )
            break

        if stream_rc == 0:
            # Clean exit (e.g. Ctrl+C forwarded to child).  Restart immediately.
            crash_count = 0
            _log("  Stream exited cleanly -- restarting")
            continue

        # Crash (exit code 1 or any other non-zero, non-halt code)
        crash_count += 1
        backoff = _backoff_secs(crash_count)
        _log(f"  WARNING: stream.py crashed (exit {stream_rc}) -- "
             f"crash #{crash_count}, backing off {backoff}s")

        if crash_count > args.max_restarts:
            pause = args.pause_on_max_restarts
            _log(f"  CIRCUIT BREAKER: {crash_count} consecutive crashes -- pausing {pause}s")
            if settings:
                _send_alert(
                    f"watch_stream circuit breaker: {crash_count} crashes",
                    f"stream.py has crashed {crash_count} times in a row (exit {stream_rc}). "
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

    _session_summary(started_at, _BET_LOG)
    _log(_SEP)
    _log("  watch_stream daemon stopped.")
    _log(_SEP)


if __name__ == "__main__":
    main()
