"""
event_log.py
------------
Append-only file logging for arb detection, provider warnings, and crashes.

Three files written to outputs/:
  arb_log.jsonl   — every profitable arb detected (profit_pct > 0), one JSON per line
  warnings.log    — provider warnings and soft fetch failures
  crashes.log     — unhandled exceptions with full traceback
"""
from __future__ import annotations

import json
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _outputs_dir(project_root: Path) -> Path:
    d = project_root / "outputs"
    d.mkdir(exist_ok=True)
    return d


def log_arb(arb_type: str, arb: dict, project_root: Path) -> None:
    """Append one profitable arb to outputs/arb_log.jsonl."""
    if arb_type == "sure_bet":
        providers = sorted({
            p for p in (
                arb.get("team1_back_provider"),
                arb.get("team2_back_provider"),
                arb.get("draw_back_provider"),
            ) if p
        })
        entry: dict = {
            "timestamp": _now_iso(),
            "arb_type": "sure_bet",
            "league": arb.get("league"),
            "game_team1": arb.get("game_team1"),
            "game_team2": arb.get("game_team2"),
            "team1": arb.get("team1"),
            "team2": arb.get("team2"),
            "date_time": arb.get("date_time"),
            "total_line": arb.get("total_line"),
            "spread": arb.get("spread"),
            "team1_back_odds": arb.get("team1_back_odds"),
            "team1_back_provider": arb.get("team1_back_provider"),
            "team2_back_odds": arb.get("team2_back_odds"),
            "team2_back_provider": arb.get("team2_back_provider"),
            "providers": providers,
            "profit_pct": arb.get("profit_pct"),
            "profit_24h_pct": arb.get("profit_24h_pct"),
        }
    elif arb_type == "back_lay":
        entry = {
            "timestamp": _now_iso(),
            "arb_type": "back_lay",
            "league": arb.get("league"),
            "game_team1": arb.get("team1"),
            "game_team2": arb.get("team2"),
            "team1": arb.get("team1"),
            "team2": arb.get("team2"),
            "date_time": arb.get("date_time"),
            "outcome": arb.get("arb_outcome"),
            "back_odds": arb.get("back_odds"),
            "back_provider": arb.get("back_provider"),
            "lay_odds": arb.get("lay_odds"),
            "lay_provider": arb.get("lay_provider"),
            "providers": sorted({arb.get("back_provider"), arb.get("lay_provider")} - {None}),
            "profit_pct": arb.get("profit_pct"),
            "profit_24h_pct": arb.get("profit_24h_pct"),
        }
    elif arb_type == "kbo":
        entry = {
            "timestamp": _now_iso(),
            "arb_type": "kbo",
            "league": "kbo",
            "team1": arb.get("team1"),
            "team2": arb.get("team2"),
            "providers": ["polymarket", "sx_bet"],
            "profit_pct": arb.get("profit_pct"),
            "profit_24h_pct": arb.get("profit_24h_pct"),
        }
    else:
        return

    path = _outputs_dir(project_root) / "arb_log.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_warning(source: str, message: str, project_root: Path) -> None:
    """Append one warning line to outputs/warnings.log."""
    path = _outputs_dir(project_root) / "warnings.log"
    with path.open("a", encoding="utf-8") as f:
        f.write(f"{_now_iso()}  [{source}]  {message}\n")


def log_crash(source: str, exc: BaseException, project_root: Path, context: str = "") -> None:
    """Append one crash block to outputs/crashes.log."""
    path = _outputs_dir(project_root) / "crashes.log"
    tb = traceback.format_exc()
    ctx = f"  {context}" if context else ""
    with path.open("a", encoding="utf-8") as f:
        f.write("=" * 80 + "\n")
        f.write(f"{_now_iso()}  {source}{ctx}\n")
        f.write(f"{type(exc).__name__}: {exc}\n")
        f.write(tb + "\n")
