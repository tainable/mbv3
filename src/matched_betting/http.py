from __future__ import annotations

from dataclasses import dataclass
import random
import sys
import time
from typing import Any

import requests
from requests.exceptions import ConnectionError as _ConnError
from requests.exceptions import HTTPError, Timeout


@dataclass(frozen=True)
class HttpClient:
    timeout_seconds: int = 30
    user_agent: str = "matched-betting/0.1.0"
    max_retries: int = 3
    proxy_url: str | None = None  # e.g. "socks5h://user:pass@host:1080"

    def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self.request_json("GET", url, params=params, headers=headers)

    def post_json(
        self,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        return self.request_json("POST", url, headers=request_headers, payload=payload)

    def request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)

        proxies = {"https": self.proxy_url, "http": self.proxy_url}

        for attempt in range(self.max_retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    params=params,
                    headers=request_headers,
                    json=payload,
                    timeout=self.timeout_seconds,
                    proxies=proxies,
                )
                response.raise_for_status()
                return response.json()
            except (Timeout, _ConnError) as exc:
                if attempt < self.max_retries:
                    delay = 2 ** (attempt + 1) + random.uniform(0, 0.5)
                    kind = "timeout" if isinstance(exc, Timeout) else "connection error"
                    print(
                        f"  [http] {kind} on {url} — "
                        f"retrying in {delay:.1f}s (attempt {attempt + 1}/{self.max_retries})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                raise
            except HTTPError as exc:
                status = exc.response.status_code if exc.response is not None else None
                if status == 429 and attempt < self.max_retries:
                    delay = 2 ** (attempt + 1) + random.uniform(0, 0.5)
                    print(
                        f"  [http] 429 rate-limited by {url} — "
                        f"sleeping {delay:.1f}s (attempt {attempt + 1}/{self.max_retries})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                if status is not None and 500 <= status < 600 and attempt < self.max_retries:
                    delay = 2 ** (attempt + 1) + random.uniform(0, 0.5)
                    print(
                        f"  [http] {status} server error from {url} — "
                        f"retrying in {delay:.1f}s (attempt {attempt + 1}/{self.max_retries})",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                body = "<unreadable>"
                if exc.response is not None:
                    try:
                        body = exc.response.text
                    except Exception:
                        pass
                raise RuntimeError(
                    f"HTTP {status if status is not None else '?'} {url}: {body}"
                ) from exc
