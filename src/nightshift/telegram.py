"""Telegram Bot API client (§2.12), outbound plus long polling.

Nightshift only ever calls the Bot API (``sendMessage``, ``getUpdates``
with a long-poll timeout); it never sets a webhook, so no port is opened
to the internet. The bot token is part of every request URL, so errors are
built here from the method name and status only, never from the URL.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol


class TelegramError(Exception):
    """The Bot API could not be reached or refused a call. Never contains the token."""


class BotAPI(Protocol):
    def send_message(self, chat_id: int, text: str) -> None: ...

    def get_updates(self, offset: int | None, timeout: float) -> list[dict]: ...


API_BASE = "https://api.telegram.org"


class HttpBotAPI:
    """The real Bot API over HTTPS (stdlib ``urllib``, no extra dependency)."""

    def __init__(self, token: str, *, base_url: str = API_BASE, http_timeout: float = 15):
        self._token = token
        self._base = base_url.rstrip("/")
        self.http_timeout = http_timeout

    def __repr__(self) -> str:
        return f"HttpBotAPI(base_url={self._base!r})"

    def _call(self, method: str, params: dict[str, Any], *, timeout: float) -> Any:
        req = urllib.request.Request(
            f"{self._base}/bot{self._token}/{method}",
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            # `from None`: the urllib error holds the URL, and so the token.
            raise TelegramError(f"{method}: HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            raise TelegramError(f"{method}: {reason}".replace(self._token, "[REDACTED]")) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else None
            raise TelegramError(f"{method}: refused ({desc or 'no description'})")
        return data.get("result")

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text,
                                   "disable_web_page_preview": True},
                   timeout=self.http_timeout)

    def get_updates(self, offset: int | None, timeout: float) -> list[dict]:
        # allowed_updates [] = Telegram's default kinds, whatever an earlier
        # client of this bot narrowed it to.
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": []}
        if offset is not None:
            params = {"offset": offset, **params}
        result = self._call("getUpdates", params, timeout=timeout + self.http_timeout)
        return result if isinstance(result, list) else []
