"""
menu.py
-------
Interactive menu for the matched-betting pipeline.

Usage:
    python menu.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_PY   = sys.executable

# ─── Terminal helpers ─────────────────────────────────────────────────────────

def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _tw() -> int:
    try:
        return os.get_terminal_size().columns
    except OSError:
        return 72


def _hr(char: str = "━") -> str:
    return char * _tw()


def _header(subtitle: str = "") -> None:
    _clear()
    print(_hr())
    title = "  Matched Betting"
    if subtitle:
        title += f"  ›  {subtitle}"
    print(title)
    print(_hr("─"))
    print()


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{suffix}: ").strip()
        return val if val else default
    except (KeyboardInterrupt, EOFError):
        return default


def _ask_int(prompt: str, default: int) -> int:
    raw = _ask(prompt, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _ask_float(prompt: str, default: float) -> float:
    raw = _ask(prompt, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


def _ask_yn(prompt: str, default: bool = True) -> bool:
    marker = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} [{marker}]: ").strip().lower()
    except (KeyboardInterrupt, EOFError):
        return default
    return default if not val else val.startswith("y")


def _pause() -> None:
    try:
        input("\n  Press Enter to return to menu...")
    except (KeyboardInterrupt, EOFError):
        pass


def _run(cmd: list[str]) -> None:
    """Print the command then stream its output."""
    print()
    print(_hr("─"))
    print(f"  $ {' '.join(cmd)}")
    print(_hr("─"))
    print()
    try:
        subprocess.run(cmd, cwd=_ROOT)
    except KeyboardInterrupt:
        print("\n  (interrupted)")


# ─── Persistent config ────────────────────────────────────────────────────────

_ALL_LEAGUES   = ["nba", "mlb", "mlb_spread", "ucl", "epl", "uel", "nhl", "ipl", "seria", "laliga"]
_ALL_PROVIDERS = ["matchbook", "polymarket", "sx_bet", "azuro", "smarkets"]

_cfg: dict = {
    "leagues":          list(_ALL_LEAGUES),
    "providers":        ["matchbook", "polymarket", "sx_bet"],
    "min_profit":       0.0,
    "budget":           10.0,
    "debug":            False,
    "show_odds":        False,
    "auto_bet":         False,
    "dry_run":          False,
    "scan_interval":    300,
    "max_runtime":      0,   # 0 = unlimited
    "skip_imminent":    True,
    "imminent_minutes": 10,
    # Daemon (watch_bet.py) settings
    "daemon_interval":      300,   # seconds between scan.py runs
    "daemon_ids_refresh":   360,   # minutes between ids.py re-runs
    "daemon_scan_timeout":  600,   # kill scan.py after N seconds
    "daemon_ids_timeout":   600,   # kill ids.py after N seconds
    "daemon_max_restarts":  10,    # consecutive crash limit before circuit-breaker pause
    "daemon_log":           "",    # path to log file; empty = stream to stdout
    "daemon_skip_initial_ids": False,  # skip ids.py at startup and use existing file
}


def _ls() -> str:
    return "  ".join(_cfg["leagues"]) or "(none)"


def _ps() -> str:
    return "  ".join(_cfg["providers"]) or "(none)"


# ─── Multi-select toggle ──────────────────────────────────────────────────────

def _toggle_list(key: str, choices: list[str], title: str) -> None:
    while True:
        _header(title)
        current: list[str] = _cfg[key]
        for i, item in enumerate(choices, 1):
            mark = "✓" if item in current else " "
            print(f"  [{i}] [{mark}] {item}")
        print()
        print("  [a] All    [n] None    [0] Done")
        print()
        choice = _ask("Toggle").lower()
        if choice == "0":
            break
        elif choice == "a":
            _cfg[key] = list(choices)
        elif choice == "n":
            _cfg[key] = []
        else:
            try:
                idx = int(choice) - 1
                item = choices[idx]
                if item in current:
                    current.remove(item)
                else:
                    current.append(item)
            except (ValueError, IndexError):
                pass


# ─── Settings menu ────────────────────────────────────────────────────────────

def settings_menu() -> None:
    while True:
        _header("Settings")
        auto_s = ("on [DRY RUN]" if _cfg["dry_run"] else "on [LIVE]") if _cfg["auto_bet"] else "off"
        print(f"  [1]  Leagues       :  {_ls()}")
        print(f"  [2]  Providers     :  {_ps()}")
        print(f"  [3]  Min profit    :  {_cfg['min_profit']:.1f}%")
        print(f"  [4]  Budget        :  ${_cfg['budget']:.2f} USDC per arb")
        max_s    = f"{_cfg['max_runtime']}s" if _cfg["max_runtime"] else "unlimited"
        imminent = f"skip <{_cfg['imminent_minutes']}m" if _cfg["skip_imminent"] else "include all"
        print(f"  [5]  Scan interval    :  {_cfg['scan_interval']}s  (loop mode)")
        print(f"  [5b] Max runtime      :  {max_s}  (loop mode)")
        print(f"  [5c] Imminent games   :  {imminent}")
        print(f"  [5d] Imminent cutoff  :  {_cfg['imminent_minutes']} minutes")
        print(f"  [6]  Debug            :  {'on' if _cfg['debug'] else 'off'}")
        print(f"  [7]  Show odds     :  {'on' if _cfg['show_odds'] else 'off'}")
        print(f"  [8]  Auto-bet      :  {auto_s}")
        print(f"  [9]  Dry run       :  {'on' if _cfg['dry_run'] else 'off'}")
        print()
        print("  [0]  Back")
        print()
        c = _ask("Choice")
        if   c == "0": break
        elif c == "1": _toggle_list("leagues",   _ALL_LEAGUES,   "Leagues")
        elif c == "2": _toggle_list("providers", _ALL_PROVIDERS, "Providers")
        elif c == "3": _cfg["min_profit"]    = _ask_float("Min profit %", _cfg["min_profit"])
        elif c == "4": _cfg["budget"]        = _ask_float("Budget (USDC)", _cfg["budget"])
        elif c == "5":  _cfg["scan_interval"] = max(10, _ask_int("Interval (seconds)", _cfg["scan_interval"]))
        elif c == "5b":
            raw = _ask_int("Max runtime (seconds, 0 = unlimited)", _cfg["max_runtime"])
            _cfg["max_runtime"] = max(0, raw)
        elif c == "5c":
            _cfg["skip_imminent"] = not _cfg["skip_imminent"]
        elif c == "5d":
            _cfg["imminent_minutes"] = max(1, _ask_int("Imminent cutoff (minutes)", _cfg["imminent_minutes"]))
        elif c == "6": _cfg["debug"]     = not _cfg["debug"]
        elif c == "7": _cfg["show_odds"] = not _cfg["show_odds"]
        elif c == "8": _cfg["auto_bet"]  = not _cfg["auto_bet"]
        elif c == "9": _cfg["dry_run"]   = not _cfg["dry_run"]


# ─── IDs ──────────────────────────────────────────────────────────────────────

def _ids_age() -> str:
    p = _ROOT / "outputs" / "active_game_ids.json"
    if not p.exists():
        return "not found"
    age = datetime.now(timezone.utc).timestamp() - p.stat().st_mtime
    if age < 3600:
        return f"{int(age // 60)}m old"
    return f"{age / 3600:.1f}h old"


def run_ids() -> None:
    while True:
        _header("Refresh IDs  (Stage 1)")
        print(f"  Current IDs file : {_ids_age()}")
        print()
        print(f"  [1]  Run  (leagues: {_ls()})")
        print(f"  [2]  Edit leagues")
        print(f"  [3]  Edit providers  ({_ps()})")
        print(f"  [4]  Toggle debug    ({'on' if _cfg['debug'] else 'off'})")
        print()
        print("  [0]  Back")
        print()
        c = _ask("Choice")
        if   c == "0": break
        elif c == "2": _toggle_list("leagues",   _ALL_LEAGUES,   "Leagues — IDs")
        elif c == "3": _toggle_list("providers", _ALL_PROVIDERS, "Providers — IDs")
        elif c == "4": _cfg["debug"] = not _cfg["debug"]
        elif c == "1":
            cmd = ([_PY, "ids.py",
                    "--leagues"]   + _cfg["leagues"] +
                   ["--providers"] + _cfg["providers"])
            if _cfg["debug"]:
                cmd.append("--debug")
            _run(cmd)
            _pause()
            break


# ─── Scan helpers ─────────────────────────────────────────────────────────────

def _build_scan_cmd() -> list[str]:
    cmd = ([_PY, "scan.py",
            "--leagues"]   + _cfg["leagues"]   +
           ["--providers"] + _cfg["providers"] +
           ["--min-profit", str(_cfg["min_profit"]),
            "--budget",     str(_cfg["budget"])])
    if not _cfg["skip_imminent"]:
        cmd.append("--no-skip-imminent")
    elif _cfg["imminent_minutes"] != 10:
        cmd += ["--imminent-minutes", str(_cfg["imminent_minutes"])]
    if _cfg["debug"]:             cmd.append("--debug")
    if _cfg["show_odds"]:         cmd.append("--show-odds")
    if _cfg["auto_bet"]:
        cmd.append("--auto-bet")
        if _cfg["dry_run"]:
            cmd.append("--bet-dry-run")
    return cmd


def _scan_summary() -> None:
    auto_s = ("on [DRY RUN]" if _cfg["dry_run"] else "on [LIVE]") if _cfg["auto_bet"] else "off"
    print(f"  Leagues    : {_ls()}")
    print(f"  Providers  : {_ps()}")
    print(f"  Min profit : {_cfg['min_profit']:.1f}%")
    print(f"  Show odds  : {'on' if _cfg['show_odds'] else 'off'}")
    print(f"  Auto-bet   : {auto_s}   budget=${_cfg['budget']:.2f}")
    imminent = "skip" if _cfg["skip_imminent"] else "include"
    print(f"  Imminent   : {imminent}")
    print(f"  Debug      : {'on' if _cfg['debug'] else 'off'}")


# ─── Single scan ──────────────────────────────────────────────────────────────

def run_scan() -> None:
    while True:
        _header("Scan  (single pass)")
        _scan_summary()
        print()
        print("  [1]  Run scan")
        print("  [2]  Settings")
        print()
        print("  [0]  Back")
        print()
        c = _ask("Choice")
        if   c == "0": break
        elif c == "2": settings_menu()
        elif c == "1":
            _run(_build_scan_cmd())
            _pause()
            break


# ─── Scan loop ────────────────────────────────────────────────────────────────

def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h, rem = divmod(seconds, 3600)
    return f"{h}h {rem // 60}m"


def run_loop_scan() -> None:
    while True:
        _header("Scan loop  (Ctrl+C to stop)")
        _scan_summary()
        max_s = _fmt_duration(_cfg["max_runtime"]) if _cfg["max_runtime"] else "unlimited"
        print(f"  Interval   : {_fmt_duration(_cfg['scan_interval'])}")
        print(f"  Max runtime: {max_s}")
        print()
        print("  [1]  Start loop")
        print("  [2]  Change interval")
        print("  [3]  Change max runtime")
        print("  [4]  Settings")
        print()
        print("  [0]  Back")
        print()
        c = _ask("Choice")
        if   c == "0": break
        elif c == "4": settings_menu()
        elif c == "2":
            _cfg["scan_interval"] = max(10, _ask_int("Interval (seconds)", _cfg["scan_interval"]))
        elif c == "3":
            raw = _ask_int("Max runtime (seconds, 0 = unlimited)", _cfg["max_runtime"])
            _cfg["max_runtime"] = max(0, raw)
        elif c == "1":
            cmd        = _build_scan_cmd()
            interval   = _cfg["scan_interval"]
            max_rt     = _cfg["max_runtime"]
            deadline   = time.monotonic() + max_rt if max_rt else None
            i          = 0
            stop_reason = "Ctrl+C"
            print()
            print(_hr("─"))
            limit_note = f"  max {_fmt_duration(max_rt)}" if deadline else ""
            print(f"  Scan loop — every {_fmt_duration(interval)}{limit_note} — Ctrl+C to stop")
            print(_hr("─"))
            try:
                while True:
                    if deadline and time.monotonic() >= deadline:
                        stop_reason = f"max runtime ({_fmt_duration(max_rt)}) reached"
                        break
                    i += 1
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    elapsed_note = ""
                    if deadline:
                        remaining = max(0, int(deadline - time.monotonic()))
                        elapsed_note = f"  ({_fmt_duration(remaining)} remaining)"
                    print(f"\n  [{ts}]  Pass #{i}{elapsed_note}")
                    print(_hr("─"))
                    subprocess.run(cmd, cwd=_ROOT)
                    print()
                    print(_hr("─"))
                    if deadline:
                        remaining = max(0, int(deadline - time.monotonic()))
                        if remaining == 0:
                            stop_reason = f"max runtime ({_fmt_duration(max_rt)}) reached"
                            break
                        wait = min(interval, remaining)
                        print(f"  Next scan in {_fmt_duration(wait)}  —  {_fmt_duration(remaining)} left  —  Ctrl+C to stop")
                    else:
                        wait = interval
                        print(f"  Next scan in {_fmt_duration(wait)}  —  Ctrl+C to stop")
                    print(_hr("─"))
                    time.sleep(wait)
            except KeyboardInterrupt:
                pass
            total = int(time.monotonic() - (deadline - max_rt if deadline else time.monotonic()))
            print(f"\n  Stopped: {stop_reason}  ({i} pass(es) completed)")
            _pause()
            break


# ─── Portfolio ────────────────────────────────────────────────────────────────

def run_portfolio() -> None:
    while True:
        _header("Portfolio")
        print("  View")
        print("  " + "─" * 40)
        print("  [1]  All platforms  (summary)")
        print("  [2]  All platforms  (detail)")
        print("  [3]  Matchbook only")
        print("  [4]  Polymarket only")
        print("  [5]  SX Bet only")
        print()
        print("  Actions")
        print("  " + "─" * 40)
        print("  [6]  Cancel Matchbook offer")
        print("  [7]  Cancel Polymarket order(s)")
        print("  [8]  Sell Polymarket position")
        print("  [9]  Cancel SX Bet order")
        print()
        print("  [0]  Back")
        print()
        c = _ask("Choice")

        if c == "0":
            break
        elif c == "1":
            _run([_PY, "portfolio.py"])
            _pause()
        elif c == "2":
            _run([_PY, "portfolio.py", "--detail"])
            _pause()
        elif c == "3":
            _run([_PY, "portfolio.py", "--matchbook"])
            _pause()
        elif c == "4":
            _run([_PY, "portfolio.py", "--polymarket"])
            _pause()
        elif c == "5":
            _run([_PY, "portfolio.py", "--sx-bet"])
            _pause()
        elif c == "6":
            offer_id = _ask("Matchbook offer ID")
            if offer_id:
                _run([_PY, "portfolio.py", "--cancel-mb", offer_id])
                _pause()
        elif c == "7":
            _header("Cancel Polymarket orders")
            print("  [1]  Cancel ALL open orders")
            print("  [2]  Cancel one specific order")
            print()
            print("  [0]  Back")
            print()
            sub = _ask("Choice")
            if sub == "1":
                _run([_PY, "portfolio.py", "--cancel-pm"])
                _pause()
            elif sub == "2":
                oid = _ask("Order ID")
                if oid:
                    _run([_PY, "portfolio.py", "--cancel-pm", "--order-id", oid])
                    _pause()
        elif c == "8":
            _header("Sell Polymarket position")
            print("  Run  python portfolio.py --polymarket  to see token IDs.")
            print()
            token = _ask("Token ID")
            if token:
                amount = _ask("Shares to sell")
                if amount:
                    dry = _ask_yn("Dry run first?", default=True)
                    cmd = [_PY, "portfolio.py",
                           "--sell-pm", token,
                           "--sell-amount", amount]
                    if dry:
                        cmd.append("--dry-run")
                    _run(cmd)
                    _pause()
        elif c == "9":
            _header("Cancel SX Bet order")
            print("  Run  python portfolio.py --sx-bet  to see order hashes.")
            print()
            h = _ask("Order hash")
            if h:
                _run([_PY, "portfolio.py", "--cancel-sx", h])
                _pause()


# ─── Polymarket setup ────────────────────────────────────────────────────────

def run_polymarket_setup() -> None:
    while True:
        _header("Polymarket Setup")
        print("  [1]  Approve USDC       — one-time on-chain approval (3 contracts)")
        print("  [2]  Check wallet       — view balance & allowance status")
        print()
        print("  [0]  Back")
        print()
        c = _ask("Choice")
        if c == "0":
            break
        elif c == "1":
            _header("Approve USDC")
            print("  This sends 3 on-chain transactions on Polygon.")
            print("  You need a small amount of MATIC for gas (~0.01 MATIC).")
            print("  Only required once per wallet.")
            print()
            if _ask_yn("Proceed with approval?", default=False):
                _run([_PY, "polymarket_bet.py", "--approve"])
                _pause()
        elif c == "2":
            _run([_PY, "polymarket_bet.py", "--check"])
            _pause()


# ─── Daemon (watch_bet.py) ────────────────────────────────────────────────────

_HEARTBEAT = _ROOT / "outputs" / "heartbeat.txt"


def _heartbeat_summary() -> str:
    """One-line daemon status from the heartbeat file, or a short 'not running' note."""
    if not _HEARTBEAT.exists():
        return "not running"
    try:
        hb = json.loads(_HEARTBEAT.read_text(encoding="utf-8"))
        last = hb.get("last_scan_at", "?")
        scans = hb.get("scan_count", "?")
        crashes = hb.get("consecutive_crashes", 0)
        crash_s = f"  crashes={crashes}" if crashes else ""
        # Age of the heartbeat file
        age_s = int(time.time() - _HEARTBEAT.stat().st_mtime)
        if age_s < 60:
            age_label = f"{age_s}s ago"
        elif age_s < 3600:
            age_label = f"{age_s // 60}m ago"
        else:
            age_label = f"{age_s / 3600:.1f}h ago"
        return f"last scan {last}  ({age_label})  scans={scans}{crash_s}"
    except Exception:
        return "heartbeat unreadable"


def _build_daemon_cmd() -> list[str]:
    cmd = [
        _PY, "watch_bet.py",
        "--budget",               str(_cfg["budget"]),
        "--min-profit",           str(_cfg["min_profit"]),
        "--interval",             str(_cfg["daemon_interval"]),
        "--ids-refresh-interval", str(_cfg["daemon_ids_refresh"]),
        "--scan-timeout",         str(_cfg["daemon_scan_timeout"]),
        "--ids-timeout",          str(_cfg["daemon_ids_timeout"]),
        "--max-restarts",         str(_cfg["daemon_max_restarts"]),
    ]
    if _cfg["providers"]:
        cmd += ["--providers"] + _cfg["providers"]
    if _cfg["leagues"]:
        cmd += ["--leagues"] + _cfg["leagues"]
    if _cfg["dry_run"]:
        cmd.append("--bet-dry-run")
    if _cfg["daemon_skip_initial_ids"]:
        cmd.append("--skip-initial-ids")
    if _cfg["daemon_log"]:
        cmd += ["--log", _cfg["daemon_log"]]
    return cmd


def _daemon_summary() -> None:
    print(f"  Leagues      : {_ls()}")
    print(f"  Providers    : {_ps()}")
    print(f"  Budget       : ${_cfg['budget']:.2f} USDC  |  Min profit: {_cfg['min_profit']:.1f}%")
    print(f"  Interval     : {_fmt_duration(_cfg['daemon_interval'])} between scans")
    skip_s = "yes (use existing file)" if _cfg["daemon_skip_initial_ids"] else "no (run ids.py first)"
    print(f"  IDs refresh  : every {_cfg['daemon_ids_refresh']}m  |  skip initial: {skip_s}")
    print(f"  Scan timeout : {_cfg['daemon_scan_timeout']}s")
    print(f"  Max restarts : {_cfg['daemon_max_restarts']} before circuit-breaker pause")
    log_s = _cfg["daemon_log"] or "(stdout)"
    print(f"  Log file     : {log_s}")
    print(f"  Heartbeat    : {_heartbeat_summary()}")


def run_daemon() -> None:
    while True:
        _header("Daemon  (24/7 autonomous)")
        _daemon_summary()
        print()
        mode_s = "DRY RUN" if _cfg["dry_run"] else "LIVE"
        print(f"  [1]  Start daemon          — launches watch_bet.py [{mode_s}]  (Ctrl+C to stop)")
        print(f"  [d]  Toggle dry run        — currently: {mode_s}")
        print("  [2]  Providers")
        print("  [3]  Leagues")
        print("  [4]  Budget / min-profit")
        print("  [5]  Scan interval")
        print("  [6]  IDs refresh interval  (minutes)")
        print("  [7]  Scan timeout          (seconds before killing a hung scan.py)")
        print("  [8]  Max restarts          (circuit-breaker threshold)")
        skip_ids_s = "yes" if _cfg["daemon_skip_initial_ids"] else "no"
        print(f"  [s]  Skip initial IDs scan — currently: {skip_ids_s}")
        print("  [9]  Log file              (empty = stream to terminal)")
        print()
        print("  [0]  Back")
        print()
        c = _ask("Choice")
        if c == "0":
            break
        elif c == "d":
            _cfg["dry_run"] = not _cfg["dry_run"]
        elif c == "s":
            _cfg["daemon_skip_initial_ids"] = not _cfg["daemon_skip_initial_ids"]
        elif c == "2":
            _toggle_list("providers", _ALL_PROVIDERS, "Providers — Daemon")
        elif c == "3":
            _toggle_list("leagues", _ALL_LEAGUES, "Leagues — Daemon")
        elif c == "4":
            _cfg["budget"]     = _ask_float("Budget per arb (USDC)", _cfg["budget"])
            _cfg["min_profit"] = _ask_float("Min profit %", _cfg["min_profit"])
        elif c == "5":
            _cfg["daemon_interval"] = max(30, _ask_int("Seconds between scans", _cfg["daemon_interval"]))
        elif c == "6":
            _cfg["daemon_ids_refresh"] = max(1, _ask_int("Minutes between IDs refresh", _cfg["daemon_ids_refresh"]))
        elif c == "7":
            _cfg["daemon_scan_timeout"] = max(60, _ask_int("Scan timeout (seconds)", _cfg["daemon_scan_timeout"]))
        elif c == "8":
            _cfg["daemon_max_restarts"] = max(1, _ask_int("Max consecutive crashes", _cfg["daemon_max_restarts"]))
        elif c == "9":
            raw = _ask("Log file path (leave blank for stdout)", _cfg["daemon_log"])
            _cfg["daemon_log"] = raw.strip()
        elif c == "1":
            cmd = _build_daemon_cmd()
            print()
            print(_hr("─"))
            print(f"  $ {' '.join(cmd)}")
            if _cfg["daemon_log"]:
                print(f"  Output → {_cfg['daemon_log']}")
                print(f"  Follow with:  Get-Content \"{_cfg['daemon_log']}\" -Wait -Tail 30")
            print(_hr("─"))
            print()
            print("  Daemon running — press Ctrl+C to stop.")
            print()
            try:
                subprocess.run(cmd, cwd=_ROOT)
            except KeyboardInterrupt:
                print("\n  Daemon stopped.")
            _pause()
            break


# ─── Main menu ────────────────────────────────────────────────────────────────

def main() -> None:
    while True:
        _header()
        now = datetime.now().strftime("%d %b %Y  %H:%M")
        ids_s = _ids_age()
        print(f"  {now}   |   IDs: {ids_s}")
        print()
        print("  Pipeline")
        print("  " + "─" * 48)
        print("  [1]  Refresh IDs          — discover markets  (Stage 1)")
        print("  [2]  Scan  (single pass)  — check for arbs   (Stage 2)")
        print("  [3]  Scan  (loop)         — repeat every N seconds")
        print()
        print("  Autonomous")
        print("  " + "─" * 48)
        print("  [7]  Daemon (24/7)        — watch_bet.py  (IDs refresh + backoff + alerts)")
        print(f"       {_heartbeat_summary()}")
        print()
        print("  Portfolio")
        print("  " + "─" * 48)
        print("  [4]  Portfolio            — balances + active bets")
        print()
        print("  Polymarket")
        print("  " + "─" * 48)
        print("  [6]  Polymarket setup     — wallet approvals & status check")
        print()
        print("  Config")
        print("  " + "─" * 48)
        print(f"  [5]  Settings")
        print(f"       Leagues  : {_ls()}")
        print(f"       Providers: {_ps()}")
        auto_s = ("on [DRY RUN]" if _cfg["dry_run"] else "on [LIVE]") if _cfg["auto_bet"] else "off"
        print(f"       Profit≥{_cfg['min_profit']:.1f}%  Budget=${_cfg['budget']:.2f}  "
              f"Auto-bet:{auto_s}  Debug:{'on' if _cfg['debug'] else 'off'}")
        print()
        print("  [0]  Exit")
        print()
        c = _ask("Choice")
        if   c == "0": print(); break
        elif c == "1": run_ids()
        elif c == "2": run_scan()
        elif c == "3": run_loop_scan()
        elif c == "4": run_portfolio()
        elif c == "5": settings_menu()
        elif c == "6": run_polymarket_setup()
        elif c == "7": run_daemon()


if __name__ == "__main__":
    main()
