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

Usage
-----
    py watch_bet.py --budget 50 --providers polymarket sx_bet --min-profit 0.5
    py watch_bet.py --budget 50 --log daemon.log --interval 180
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
_SEP = "─" * 60

# Backoff schedule (seconds) indexed by consecutive crash count (capped at last value)
_BACKOFF = [30, 60, 120, 300]

# scan.py exit code that signals a halted state requiring human review
_EXIT_HALT = 2


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


def _build_scan_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [sys.executable, "scan.py", "--auto-bet", "--budget", str(args.budget)]
    if args.providers:
        cmd += ["--providers"] + args.providers
    if args.leagues:
        cmd += ["--leagues"] + args.leagues
    cmd += ["--min-profit", str(args.min_profit)]
    return cmd


def _backoff_secs(crash_count: int) -> int:
    idx = min(crash_count - 1, len(_BACKOFF) - 1)
    return _BACKOFF[idx]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="24/7 matched-betting daemon: runs ids.py + scan.py in a loop."
    )
    parser.add_argument("--interval", type=int, default=300, metavar="SECS",
                        help="Seconds between scan.py runs (default: 300).")
    parser.add_argument("--ids-refresh-interval", type=int, default=30, metavar="MINS",
                        help="Minutes between ids.py re-runs (default: 30).")
    parser.add_argument("--scan-timeout", type=int, default=600, metavar="SECS",
                        help="Kill scan.py after this many seconds (default: 600).")
    parser.add_argument("--ids-timeout", type=int, default=300, metavar="SECS",
                        help="Kill ids.py after this many seconds (default: 300).")
    parser.add_argument("--max-restarts", type=int, default=10, metavar="N",
                        help="Consecutive crash limit before circuit-breaker pause (default: 10).")
    parser.add_argument("--pause-on-max-restarts", type=int, default=3600, metavar="SECS",
                        help="How long to pause after hitting the crash limit (default: 3600).")
    parser.add_argument("--budget", type=float, default=50.0, metavar="USDC",
                        help="Max stake budget passed to scan.py --budget (default: 50).")
    parser.add_argument("--min-profit", type=float, default=0.5, metavar="PCT",
                        help="Min net profit %% passed to scan.py (default: 0.5).")
    parser.add_argument("--providers", nargs="+", metavar="PROVIDER",
                        help="Providers passed to ids.py and scan.py.")
    parser.add_argument("--leagues", nargs="+", metavar="LEAGUE",
                        help="Leagues passed to ids.py and scan.py.")
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
    last_ids_refresh: float | None = None

    _log(_SEP)
    _log(f"  watch_bet daemon starting")
    _log(f"  budget={args.budget} USDC  min-profit={args.min_profit}%  "
         f"interval={args.interval}s  ids-refresh={args.ids_refresh_interval}m")
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
            ids_rc, ids_timeout = _run_subprocess(ids_cmd, args.ids_timeout, "ids.py")
            if ids_timeout or ids_rc != 0:
                _log(f"  ⚠  ids.py finished with issues (exit={ids_rc}, timeout={ids_timeout}) — "
                     f"continuing with existing IDs")
            else:
                _log(f"  ✓  IDs refreshed")
            last_ids_refresh = time.monotonic()

        # ── 2. Run scan ────────────────────────────────────────────────────
        _log(_SEP)
        _log(f"  SCAN #{scan_count + 1}")
        _log(_SEP)
        scan_cmd = _build_scan_cmd(args)
        scan_rc, scan_timeout = _run_subprocess(scan_cmd, args.scan_timeout, "scan.py")
        scan_count += 1

        # ── 3. Update heartbeat ────────────────────────────────────────────
        _write_heartbeat({
            "pid":                  os.getpid(),
            "started_at":           started_at,
            "last_scan_at":         _now(),
            "last_ids_refresh_at":  datetime.fromtimestamp(
                                        last_ids_refresh, tz=timezone.utc
                                    ).isoformat().replace("+00:00", "Z"),
            "scan_count":           scan_count,
            "consecutive_crashes":  crash_count,
        })

        # ── 4. Handle halt (failed-leg incident) ──────────────────────────
        if scan_rc == _EXIT_HALT:
            _log("  ⛔  scan.py exited with HALT code — a leg placement failed.")
            _log("  ⛔  Daemon stopping. Review outputs/bet_log.jsonl, then restart manually.")
            if settings:
                _send_alert(
                    "⛔ watch_bet DAEMON STOPPED — FAILED LEG",
                    "scan.py exited with halt code 2. "
                    "Check outputs/bet_log.jsonl for the unhedged position.",
                    settings,
                )
            break

        # ── 5. Handle crash / timeout ─────────────────────────────────────
        if scan_timeout or scan_rc != 0:
            crash_count += 1
            backoff = _backoff_secs(crash_count)
            reason = f"timeout after {args.scan_timeout}s" if scan_timeout else f"exit code {scan_rc}"
            _log(f"  ⚠  scan.py failed ({reason}) — crash #{crash_count}, backing off {backoff}s")

            if crash_count > args.max_restarts:
                pause = args.pause_on_max_restarts
                _log(f"  ⛔  Circuit breaker: {crash_count} consecutive crashes — pausing {pause}s")
                if settings:
                    _send_alert(
                        f"⛔ watch_bet circuit breaker: {crash_count} crashes",
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
        _log(f"  ✓  Scan #{scan_count} complete. Next scan in {args.interval}s.")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            _log("  Interrupted.")
            break

    _log(_SEP)
    _log("  watch_bet daemon stopped.")
    _log(_SEP)


if __name__ == "__main__":
    main()
