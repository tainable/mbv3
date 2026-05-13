"""
notifier.py
-----------
Sends alert messages to a configured webhook URL.

Supported webhook types (auto-detected by URL pattern):
  - ntfy.sh:   https://ntfy.sh/your-topic
  - Telegram:  https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>
  - Discord:   https://discord.com/api/webhooks/...
  - Slack:     https://hooks.slack.com/...
  - Generic:   any URL — posts {"content": ..., "text": ...}

Falls back to stderr when no webhook is configured or delivery fails.
"""
from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from matched_betting.config import Settings


def send_alert(subject: str, body: str, settings: "Settings") -> None:
    """Send an alert via the configured webhook, or print to stderr."""
    if not getattr(settings, "alert_enabled", True):
        return

    url = getattr(settings, "alert_webhook_url", None)
    if not url:
        _to_stderr(subject, body)
        return

    try:
        import requests
        full = f"{subject}\n\n{body}" if body else subject
        if "ntfy.sh" in url:
            requests.post(
                url,
                data=body.encode("utf-8"),
                headers={"Title": subject, "Priority": "high"},
                timeout=10,
            )
        elif "telegram" in url or "t.me" in url:
            requests.post(url, json={"text": full}, timeout=10)
        else:
            # Discord / Slack / generic JSON webhook
            requests.post(url, json={"content": full, "text": full}, timeout=10)
    except Exception as exc:
        _to_stderr(subject, body)
        print(f"  (alert webhook failed: {exc})", file=sys.stderr)


def _to_stderr(subject: str, body: str) -> None:
    print(f"\nALERT: {subject}", file=sys.stderr)
    if body:
        print(body, file=sys.stderr)
