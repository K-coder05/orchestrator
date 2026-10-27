"""The Bot API client against a fake Bot API server on localhost."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from nightshift.telegram import HttpBotAPI, TelegramError

TOKEN = "123456:secret-bot-token-abcdef"


class FakeBotServer:
    """Answers ``/bot<token>/<method>`` like the Bot API; records the calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.status = 200
        self.updates: list[dict] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                prefix = f"/bot{TOKEN}/"
                if not self.path.startswith(prefix):
                    return self._reply(404, {"ok": False, "description": "Not Found"})
                method = self.path[len(prefix):]
                server.calls.append((method, body))
                if server.status != 200:
                    return self._reply(server.status, {"ok": False, "error_code": server.status,
                                                       "description": "Bad Gateway"})
                result = server.updates if method == "getUpdates" else {"message_id": 1}
                self._reply(200, {"ok": True, "result": result})

            def _reply(self, code, data):
                raw = json.dumps(data).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = FakeBotServer()
    yield s
    s.close()


def test_send_message_posts_to_the_user(server):
    HttpBotAPI(TOKEN, base_url=server.url).send_message(4242, "Task k3f9 done")
    assert server.calls == [("sendMessage", {"chat_id": 4242, "text": "Task k3f9 done",
                                             "disable_web_page_preview": True})]


def test_inline_buttons_edits_and_callback_answers(server):
    api = HttpBotAPI(TOKEN, base_url=server.url)
    api.send_message(4242, "Approve?", buttons=[[("Approve", "approve:k3f9"),
                                                 ("Deny", "deny:k3f9")]])
    api.edit_message_text(4242, 17, "Approve?\n\nApproved.")
    api.answer_callback_query("q1", "Approved")
    api.answer_callback_query("q2")
    assert server.calls == [
        ("sendMessage", {"chat_id": 4242, "text": "Approve?", "disable_web_page_preview": True,
                         "reply_markup": {"inline_keyboard": [[
                             {"text": "Approve", "callback_data": "approve:k3f9"},
                             {"text": "Deny", "callback_data": "deny:k3f9"}]]}}),
        # No reply_markup: the buttons go with the edit.
        ("editMessageText", {"chat_id": 4242, "message_id": 17, "text": "Approve?\n\nApproved.",
                             "disable_web_page_preview": True}),
        ("answerCallbackQuery", {"callback_query_id": "q1", "text": "Approved"}),
        ("answerCallbackQuery", {"callback_query_id": "q2"}),
    ]


def test_get_updates_long_polls_with_offset(server):
    server.updates = [{"update_id": 5, "message": {"from": {"id": 1}}}]
    got = HttpBotAPI(TOKEN, base_url=server.url).get_updates(5, 1)
    assert got == server.updates
    assert server.calls == [("getUpdates", {"offset": 5, "timeout": 1,
                                            "allowed_updates": []})]


def test_errors_never_carry_the_token(server):
    server.status = 502
    with pytest.raises(TelegramError) as err:
        HttpBotAPI(TOKEN, base_url=server.url).send_message(1, "x")
    assert "sendMessage" in str(err.value) and "502" in str(err.value)
    assert TOKEN not in str(err.value)
    server.close()
    with pytest.raises(TelegramError) as err:
        HttpBotAPI(TOKEN, base_url=server.url, http_timeout=2).send_message(1, "x")
    assert TOKEN not in str(err.value) and TOKEN not in repr(err.value)
    # The urllib error (whose URL holds the token) is not chained into tracebacks.
    assert err.value.__cause__ is None and err.value.__suppress_context__
