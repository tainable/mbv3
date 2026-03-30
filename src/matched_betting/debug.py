from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import sys


DebugLogger = Callable[[str], None]


def noop_debug(_: str) -> None:
    return


def stderr_debug(message: str) -> None:
    timestamp = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    print(f"[debug {timestamp}] {message}", file=sys.stderr, flush=True)
