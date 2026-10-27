from __future__ import annotations

import http.client
import io
import unittest
import urllib.error
import urllib.request
from email.message import Message
from unittest.mock import patch

from codex_telegram_remote.http_client import HttpRequestError, request_body


class HttpClientTests(unittest.TestCase):
    def test_retry_after_is_used_for_rate_limit(self) -> None:
        headers = Message()
        headers["Retry-After"] = "0"
        error = urllib.error.HTTPError(
            "https://example.test",
            429,
            "rate limited",
            headers,
            io.BytesIO(b'{"error":"rate limited"}'),
        )
        response = io.BytesIO(b'{"ok":true}')
        with (
            patch("codex_telegram_remote.http_client.urllib.request.urlopen", side_effect=[error, response]),
            patch("codex_telegram_remote.http_client.time.sleep") as sleep,
        ):
            body = request_body(
                urllib.request.Request("https://example.test"),
                timeout=1,
                attempts=2,
            )

        self.assertEqual('{"ok":true}', body)
        sleep.assert_called_once_with(0.0)

    def test_non_retryable_status_is_returned_with_body(self) -> None:
        headers = Message()
        error = urllib.error.HTTPError(
            "https://example.test",
            401,
            "unauthorized",
            headers,
            io.BytesIO(b'{"error":"bad token"}'),
        )
        with patch("codex_telegram_remote.http_client.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(HttpRequestError) as raised:
                request_body(
                    urllib.request.Request("https://example.test"),
                    timeout=1,
                )
        self.assertEqual(401, raised.exception.status)
        self.assertIn("bad token", str(raised.exception))

    def test_remote_disconnect_is_retried(self) -> None:
        response = io.BytesIO(b'{"ok":true}')
        with (
            patch(
                "codex_telegram_remote.http_client.urllib.request.urlopen",
                side_effect=[
                    http.client.RemoteDisconnected("connection closed"),
                    response,
                ],
            ),
            patch("codex_telegram_remote.http_client.time.sleep") as sleep,
        ):
            body = request_body(
                urllib.request.Request("https://example.test"),
                timeout=1,
                attempts=2,
            )

        self.assertEqual('{"ok":true}', body)
        sleep.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
