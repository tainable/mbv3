from __future__ import annotations

from dataclasses import dataclass
import sys
import time
from typing import Any

import requests
from requests.exceptions import HTTPError


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

        proxies = {"https": self.proxy_url, "http": self.proxy_url} if self.proxy_url else None

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
            except HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 429 and attempt < self.max_retries:
                    delay = 2 ** (attempt + 1)  # 2s, 4s, 8s
                    print(
                        f"  [http] 429 rate-limited by {url} — "
                        f"sleeping {delay}s (attempt {attempt + 1}/{self.max_retries})",
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
                    f"HTTP {exc.response.status_code if exc.response is not None else '?'} {url}: {body}"
                ) from exc
