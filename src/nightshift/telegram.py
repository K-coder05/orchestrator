"""Telegram Bot API client (§2.12), outbound plus long polling.

Nightshift only ever calls the Bot API (``sendMessage``, ``editMessageText``,
``answerCallbackQuery``, ``getUpdates`` with a long-poll timeout); it never
sets a webhook, so no port is opened to the internet. Inline buttons are
rows of ``(label, callback data)``; a press comes back as a
``callback_query`` update. The bot token is part of every request URL, so errors are
built here from the method name and status only, never from the URL.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol


class TelegramError(Exception):
    """The Bot API could not be reached or refused a call. Never contains the token."""


#: Inline keyboard: rows of (label, callback data); data is at most 64 bytes.
Buttons = list[list[tuple[str, str]]]


class BotAPI(Protocol):
    def send_message(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None: ...

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None: ...

    def answer_callback_query(self, query_id: str, text: str | None = None) -> None: ...

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

    def send_message(self, chat_id: int, text: str, buttons: Buttons | None = None) -> None:
        params: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                  "disable_web_page_preview": True}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": [
                [{"text": label, "callback_data": data} for label, data in row]
                for row in buttons]}
        self._call("sendMessage", params, timeout=self.http_timeout)

    def edit_message_text(self, chat_id: int, message_id: int, text: str) -> None:
        """Replace a message's text; its inline buttons go (no reply_markup)."""
        self._call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                       "text": text, "disable_web_page_preview": True},
                   timeout=self.http_timeout)

    def answer_callback_query(self, query_id: str, text: str | None = None) -> None:
        """Stop the pressed button's spinner, with a short toast."""
        params: dict[str, Any] = {"callback_query_id": query_id}
        if text:
            params["text"] = text[:200]
        self._call("answerCallbackQuery", params, timeout=self.http_timeout)

    def get_updates(self, offset: int | None, timeout: float) -> list[dict]:
        # allowed_updates [] = Telegram's default kinds, whatever an earlier
        # client of this bot narrowed it to.
        params: dict[str, Any] = {"timeout": timeout, "allowed_updates": []}
        if offset is not None:
            params = {"offset": offset, **params}
        result = self._call("getUpdates", params, timeout=timeout + self.http_timeout)
        return result if isinstance(result, list) else []
