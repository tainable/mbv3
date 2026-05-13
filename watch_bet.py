"""
watch_bet.py
------------
Runs the scan every INTERVAL seconds until all legs of an arb are placed live,
then exits automatically.

Usage:
    py watch_bet.py                          # interactive, every 5 minutes
    py watch_bet.py --interval 120           # every 2 minutes
    pythonw watch_bet.py --log out.log       # fully headless — no window, output to file
    Get-Content out.log -Wait -Tail 20       # follow the log from another terminal
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime

_SCAN_BASE = [
    sys.executable, "scan.py",
    "--provider", "sx_bet", "polymarket",
    "--auto-bet",
    "--budget", "5",
]

_SEP = "─" * 60


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _run_once(min_profit: float) -> bool:
    """
    Run one scan pass.  Returns True when scan.py confirms every leg of an arb
    was placed live by printing the sentinel "[WATCH] ALL LEGS PLACED".
    That line is only emitted after all_legs_placed() passes — i.e. every
    executable leg returned ok=True with no dry_run flag.
    """
    placed = False
    cmd = _SCAN_BASE + ["--min-profit", str(min_profit)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        print(line, end="", flush=True)
        if "[WATCH] ALL LEGS PLACED" in line:
            placed = True
    proc.wait()
    return placed


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll scan.py until a bet is placed.")
    parser.add_argument(
        "--interval",
        type=int,
        default=300,
        metavar="SECONDS",
        help="Seconds between scans (default: 300).",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=1.0,
        metavar="PCT",
        help="Minimum net profit %% to act on (default: 1.0).",
    )
    parser.add_argument(
        "--log",
        metavar="FILE",
        default=None,
        help=(
            "Write all output to FILE instead of the console. "
            "Combine with `pythonw watch_bet.py --log out.log` for fully headless operation "
            "(no window). Monitor with: Get-Content out.log -Wait -Tail 20"
        ),
    )
    args = parser.parse_args()

    if args.log:
        # Headless mode: redirect stdout to the log file.
        # line-buffered (buffering=1) so each print flushes immediately.
        sys.stdout = open(args.log, "w", encoding="utf-8", buffering=1)
    elif hasattr(sys.stdout, "reconfigure"):
        # Interactive mode: force UTF-8 so box-drawing chars render correctly.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    attempt = 0
    while True:
        attempt += 1
        print(f"\n{_SEP}")
        print(f"  [watch_bet] Attempt #{attempt}  —  {_now()}")
        print(_SEP)

        placed = _run_once(args.min_profit)

        if placed:
            print(f"\n{_SEP}")
            print(f"  [watch_bet] Bet placed — stopping.  {_now()}")
            print(_SEP)
            break

        print(f"\n  [watch_bet] No bet placed. Next scan in {args.interval}s  "
              f"(~{_now()} + {args.interval // 60}m)  —  Ctrl-C to stop.")
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n  [watch_bet] Interrupted.")
            break


if __name__ == "__main__":
    main()
