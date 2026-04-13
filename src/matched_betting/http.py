from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class HttpClient:
    timeout_seconds: int = 30
    user_agent: str = "matched-betting/0.1.0"
    max_retries: int = 3

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
        request_url = url
        if params:
            query = urlencode(
                [(key, item) for key, value in params.items() for item in _to_items(value)],
                doseq=True,
            )
            request_url = f"{url}?{query}"

        request_headers = {"User-Agent": self.user_agent}
        if headers:
            request_headers.update(headers)

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = Request(request_url, headers=request_headers, method=method, data=data)
        for attempt in range(self.max_retries + 1):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code == 429 and attempt < self.max_retries:
                    time.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s
                    continue
                try:
                    body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    body = "<unreadable>"
                raise RuntimeError(
                    f"HTTP {exc.code} {request_url}: {body}"
                ) from exc


def _to_items(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]
