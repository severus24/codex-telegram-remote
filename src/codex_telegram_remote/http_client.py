from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from dataclasses import dataclass


RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass(slots=True)
class HttpRequestError(RuntimeError):
    status: int
    body: str

    def __str__(self) -> str:
        if self.status:
            return f"HTTP {self.status}: {self.body[:800]}"
        return f"网络错误: {self.body[:800]}"


def request_body(
    request: urllib.request.Request,
    *,
    timeout: int,
    attempts: int = 3,
) -> str:
    attempts = max(1, attempts)
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            if error.code in RETRYABLE_STATUS and attempt + 1 < attempts:
                time.sleep(_retry_delay(error.headers, attempt))
                continue
            raise HttpRequestError(error.code, body) from error
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
                continue
            break
    raise HttpRequestError(0, str(getattr(last_error, "reason", last_error)))


def _retry_delay(headers, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return min(30.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return float(2**attempt)
