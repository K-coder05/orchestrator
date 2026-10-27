"""Telegram alerts for Task outcomes (§2.12).

Alerts are made from the ``transitions`` table, not from the code paths
that move Tasks, so every way a Task can finish, fail or need you alerts
the same way. Each alert is written to the ``alerts`` outbox first and sent
afterwards: a Telegram outage only leaves alerts pending (retried until
``telegram.alert_max_age_hours``), it never fails or holds up a Task.

| Transition to                          | Alert                         |
|----------------------------------------|-------------------------------|
| done, partial                          | finished, one-line Result     |
| failed                                 | failed, with the reason       |
| needs_you                              | needs you, with the request   |
| awaiting_approval from paused_usage    | needs re-approval             |
| paused_spend on the monthly cap        | needs you: raise the cap      |
| anything else (incl. paused_usage and  | nothing                       |
| paused_spend on the daily cap)         |                               |

Comedy Tasks get status lines only: never the title, the Result, the
reason or the request, which may carry script or idea content (§1.6).

An alert for a Task awaiting approval (re-approval) carries Approve / Deny
inline buttons; their presses come back through the long poll.

The same loop makes and sends the 8am digest (``nightshift.digest``); the
long poll answers your commands (``nightshift.remote``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import digest, report, spend
from .config import Config
from .db import Database
from .states import TaskState
from .telegram import BotAPI, Buttons, HttpBotAPI, TelegramError
from .timeutil import iso, now, parse_iso

#: Task types whose alerts are status lines only (unreleased material).
STATUS_ONLY_TYPES = frozenset({"comedy"})

CURSOR_KEY = "alerts.last_transition_id"
OFFSET_KEY = "telegram.update_offset"

#: Longest detail line an alert carries.
MAX_LINE = 300

S = TaskState


def alert_kind(from_state: str | None, to_state: str, reason: str = "") -> str | None:
    if to_state in (S.DONE, S.PARTIAL):
        return "finished"
    if to_state == S.FAILED:
        return "failed"
    if to_state == S.NEEDS_YOU:
        return "needs_you"
    if to_state == S.AWAITING_APPROVAL and from_state == S.PAUSED_USAGE:
        return "reapproval"
    if to_state == S.PAUSED_SPEND and spend.MONTHLY_CAP in reason:
        return "spend_cap"
    return None


def one_line(text: str) -> str:
    line = " ".join(text.split())
    return line if len(line) <= MAX_LINE else line[: MAX_LINE - 1].rstrip() + "…"


def result_line(folder: Path) -> str | None:
    """The one-line Result of a finished Task's ``report.md``: the agent's
    own line if it wrote one, else Nightshift's verdict."""
    path = folder / "report.md"
    if not path.exists():
        return None
    _, sections = report.parse_sections(path.read_text())
    body = sections.get(1, "")
    paras = [p.strip() for p in body.split("\n\n") if p.strip()]
    agent = next((p[len("Agent:"):] for p in paras if p.startswith("Agent:")), None)
    line = agent if agent and agent.strip() else (paras[0] if paras else "")
    line = line.strip().splitlines()[0] if line.strip() else ""
    return one_line(line.replace("**", "")) or None


def needs_you_line(folder: Path) -> str | None:
    """The first entry of the report's Needs you section."""
    path = folder / "report.md"
    if not path.exists():
        return None
    _, sections = report.parse_sections(path.read_text())
    lines = [ln.strip().lstrip("- ").replace("**", "") for ln in sections.get(2, "").splitlines()]
    first = next((ln for ln in lines if ln and ln != "Nothing."), None)
    return one_line(first) if first else None


def alert_text(task, kind: str, reason: str) -> str:
    """The message for one alert. ``task`` is the Task's DB row."""
    tid, status_only = task["id"], task["task_type"] in STATUS_ONLY_TYPES
    state = {"finished": task["state"] if task["state"] in (S.DONE, S.PARTIAL) else "done",
             "failed": "failed", "needs_you": "needs you",
             "reapproval": "needs re-approval", "spend_cap": "needs you"}[kind]
    if status_only:
        head = f"Nightshift: {task['task_type']} Task {tid} {state}."
        return f"{head}\nDetails: orch report {tid}"
    head = f"Nightshift: Task {tid} {state}: {one_line(task['title'])}"
    folder = Path(task["folder"])
    if kind == "finished":
        detail = result_line(folder)
    elif kind == "failed":
        detail = one_line(reason)
    elif kind == "needs_you":
        detail = needs_you_line(folder) or one_line(reason)
    elif kind == "spend_cap":
        detail = one_line(reason)
    else:
        detail = f"Paused too long; approve it again with orch approve {tid}"
    return "\n".join(filter(None, [head, detail, f"Details: orch report {tid}"]))


def _alert_buttons(alert) -> Buttons | None:
    """Approve / Deny on an alert for a Task awaiting approval."""
    if alert["kind"] != "reapproval":
        return None
    from . import remote  # control -> daemon -> alerts: imported on use
    return remote.approval_buttons(alert["task_id"])


class Notifier:
    """Makes alerts from new transitions and sends them to the configured user.

    ``api`` is the Bot API (tests pass a fake). Nothing here raises on a
    Telegram problem: it is logged and retried later.
    """

    def __init__(
        self,
        cfg: Config,
        db: Database,
        api: BotAPI,
        *,
        log: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        uid = cfg.telegram_user_id
        if uid is None:
            raise ValueError("telegram.user_id is not set")
        self.cfg = cfg
        self.db = db
        self.api = api
        self.user_id = uid
        self._log = log or (lambda msg: None)
        self.clock = clock or (lambda: now(cfg.timezone))
        self.max_age = timedelta(hours=cfg.telegram_setting("alert_max_age_hours"))
        if db.get_meta(CURSOR_KEY) is None:
            # First start: alert on what happens from now on, not on history.
            db.set_meta(CURSOR_KEY, str(db.last_transition_id()))
        digest.start(db, self.clock(), cfg.telegram_digest_time)

    def log(self, msg: str) -> None:
        self._log(self.cfg.secrets.redact(msg))

    def collect(self) -> int:
        """Write an alert for every new transition that calls for one."""
        made = 0
        last = int(self.db.get_meta(CURSOR_KEY) or 0)
        oldest = self.clock() - self.max_age
        for t in self.db.transitions_after(last):
            last = t["id"]
            kind = alert_kind(t["from_state"], t["to_state"], t["reason"])
            if kind is None or parse_iso(t["at"]) < oldest:
                continue
            task = self.db.get_task(t["task_id"])
            text = self.cfg.secrets.redact(alert_text(task, kind, t["reason"]))
            self.db.insert_alert(transition_id=t["id"], task_id=t["task_id"], kind=kind,
                                 text=text, at=iso(self.clock()))
            made += 1
        self.db.set_meta(CURSOR_KEY, str(last))
        return made

    def collect_digest(self) -> bool:
        """Make the morning digest if it is due (8am, or the first tick after
        it); True if one was made."""
        return digest.make_due(self.cfg, self.db, self.clock())

    def deliver(self) -> bool:
        """Send pending alerts, oldest first, then a pending digest. False if
        Telegram failed (the rest wait for the next try)."""
        return self._deliver_alerts() and self._deliver_digests()

    def _deliver_digests(self) -> bool:
        oldest = self.clock() - self.max_age
        for d in self.db.pending_digests():
            if parse_iso(d["created_at"]) < oldest:
                self.db.mark_digest(d["id"], dropped_at=iso(self.clock()))
                self.log(f"Telegram digest for {d['digest_date']} dropped: never delivered "
                         f"({d['tries']} tries, last error: {d['last_error']})")
                continue
            try:
                self.api.send_message(self.user_id, d["text"])
            except TelegramError as exc:
                err = self.cfg.secrets.redact(str(exc))
                self.db.mark_digest(d["id"], error=err)
                self.log(f"Telegram digest for {d['digest_date']} not sent yet: {err}")
                return False
            self.db.mark_digest(d["id"], sent_at=iso(self.clock()))
        return True

    def _deliver_alerts(self) -> bool:
        oldest = self.clock() - self.max_age
        for a in self.db.pending_alerts():
            if parse_iso(a["created_at"]) < oldest:
                self.db.mark_alert(a["id"], dropped_at=iso(self.clock()))
                self.log(f"Telegram alert for Task {a['task_id']} dropped: never delivered "
                         f"({a['tries']} tries, last error: {a['last_error']})")
                continue
            try:
                self.api.send_message(self.user_id, a["text"], buttons=_alert_buttons(a))
            except TelegramError as exc:
                err = self.cfg.secrets.redact(str(exc))
                self.db.mark_alert(a["id"], error=err)
                self.log(f"Telegram alert for Task {a['task_id']} not sent yet: {err}")
                return False
            self.db.mark_alert(a["id"], sent_at=iso(self.clock()))
        return True

    def poll_updates(self) -> bool:
        """One long poll for incoming updates. A text message from
        ``telegram.user_id`` is a command (``nightshift.remote``), answered
        to that user, and a ``callback_query`` from them is an inline button
        press; any other update from them is ignored. One from another user
        is logged with its sender ID (never its content) and never answered.
        False if Telegram failed."""
        offset = self.db.get_meta(OFFSET_KEY)
        timeout = self.cfg.telegram_setting("poll_timeout_seconds")
        try:
            updates = self.api.get_updates(int(offset) if offset else None, timeout)
        except TelegramError as exc:
            self.log(f"Telegram getUpdates failed: {exc}")
            return False
        for u in updates:
            # Confirmed before it is acted on: a crash or restart mid-command
            # never runs it twice.
            self.db.set_meta(OFFSET_KEY, str(int(u["update_id"]) + 1))
            body: dict[str, Any] = next(
                (u[k] for k in u if k != "update_id" and isinstance(u[k], dict)), {})
            sender = (body.get("from") or {}).get("id")
            if sender != self.user_id:
                self.log(f"Telegram: ignored an update from user {sender}, who is not "
                         "telegram.user_id")
            elif isinstance(u.get("callback_query"), dict):
                self.button(u["callback_query"])
            elif not isinstance(u.get("message"), dict):
                self.log("Telegram: ignored an update from you that is not a new message")
            else:
                self.command(u["message"].get("text") or "")
        return True

    #: Bounds of the wait for the daemon after a cancel or pause (None:
    #: ``control``'s own); tests shorten them.
    cancel_wait: float | None = None
    pause_wait: float | None = None
    #: The planning calls of ``add`` (None: the configured agent); tests fake it.
    adapter_provider: Any = None

    def _reply(self, msg: str, buttons: Buttons | None = None) -> None:
        try:
            self.api.send_message(self.user_id, self.cfg.secrets.redact(msg), buttons=buttons)
        except TelegramError as exc:
            self.log(f"Telegram reply not sent: {exc}")

    def command(self, text: str) -> None:
        """Run one command from you and answer it. Its text is never logged."""
        from . import remote  # control -> daemon -> alerts: imported on use

        try:
            verb = remote.run(self.cfg, self.db, text, at=self.clock(), reply=self._reply,
                              cancel_wait=self.cancel_wait, pause_wait=self.pause_wait,
                              adapter_provider=self.adapter_provider)
        except Exception as exc:  # a bug in one command must not end the poll
            self.log(f"Telegram command error: {type(exc).__name__}: {exc}")
            self._reply(f"Nightshift hit an error running that command "
                        f"({type(exc).__name__}); see the daemon log.")
            return
        self.log(f"Telegram: {verb} command from you")

    def button(self, query: dict[str, Any]) -> None:
        """Act on one inline button press from you: answer the press (its
        spinner stops, with a short toast) and edit the pressed message to
        show the decision, which also takes its buttons away."""
        from . import remote

        message = query.get("message") if isinstance(query.get("message"), dict) else {}
        chat_id = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        original = message.get("text") or ""
        answered = False

        def answer(text: str | None = None) -> None:
            nonlocal answered
            if answered:
                return
            answered = True
            try:
                self.api.answer_callback_query(str(query.get("id")),
                                               self.cfg.secrets.redact(text) if text else None)
            except TelegramError as exc:
                self.log(f"Telegram button answer not sent: {exc}")

        def edit(text: str) -> None:
            text = self.cfg.secrets.redact(text)
            if chat_id is None or message_id is None or not original:
                self._reply(text)  # too old to edit: say it in a new message
                return
            try:
                self.api.edit_message_text(chat_id, message_id, text)
            except TelegramError as exc:
                self.log(f"Telegram message edit not sent: {exc}")

        try:
            verb = remote.press(self.cfg, self.db, str(query.get("data") or ""),
                                original=original, at=self.clock(), answer=answer, edit=edit,
                                reply=self._reply, adapter_provider=self.adapter_provider)
        except Exception as exc:  # a bug in one button must not end the poll
            self.log(f"Telegram button error: {type(exc).__name__}: {exc}")
            answer(f"Nightshift hit an error ({type(exc).__name__}); see the daemon log.")
            return
        answer()
        self.log(f"Telegram: {verb} button from you")

    def tick(self) -> bool:
        """Collect alerts and a due digest, then deliver; False if Telegram failed."""
        self.collect()
        self.collect_digest()
        return self.deliver()


#: Longest wait between tries while Telegram is failing.
MAX_BACKOFF_SECONDS = 300.0


class AlertService:
    """Runs a Notifier in the daemon on two background threads: one sends
    alerts (every ``interval`` seconds, or at once after :meth:`kick`), one
    long-polls for incoming updates and answers commands. Each has its own
    DB connection. A Telegram outage or hang only stalls these threads,
    never the queue.
    """

    def __init__(self, cfg: Config, api: BotAPI, *, log: Callable[[str], None] | None = None,
                 interval: float = 5.0, clock: Callable[[], datetime] | None = None):
        self.cfg = cfg
        self.api = api
        self.clock = clock
        self._log = log or (lambda msg: None)
        self.interval = interval
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._threads: list[threading.Thread] = []

    def kick(self) -> None:
        """Send new alerts now (the queue just finished an Attempt)."""
        self._kick.set()

    def start(self) -> None:
        for name, loop in (("telegram-alerts", self._send_loop),
                           ("telegram-updates", self._poll_loop)):
            t = threading.Thread(target=self._run, args=(name, loop), name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the threads; the sender first sends what is pending. One
        stuck in a Bot API call is left to die with the process (they are
        daemon threads)."""
        self._stop.set()
        self._kick.set()
        for t in self._threads:
            t.join(timeout)

    def _run(self, name: str, loop: Callable[[Notifier], None]) -> None:
        try:
            db = Database(self.cfg.db_path, tz=self.cfg.timezone)
        except Exception as exc:
            self._log(self.cfg.secrets.redact(
                f"Telegram {name} could not start: {type(exc).__name__}: {exc}"))
            return
        try:
            loop(Notifier(self.cfg, db, self.api, log=self._log, clock=self.clock))
        except Exception as exc:  # never let alerts take the daemon down
            self._log(self.cfg.secrets.redact(
                f"Telegram {name} could not start: {type(exc).__name__}: {exc}"))
        finally:
            db.close()

    @staticmethod
    def _step(notifier: Notifier, step: Callable[[], bool]) -> bool:
        try:
            return step()
        except Exception as exc:  # a bug here must not end alerts for the whole run
            notifier.log(f"Telegram alerts error: {type(exc).__name__}: {exc}")
            return False

    def _send_loop(self, notifier: Notifier) -> None:
        failures = 0
        while not self._stop.is_set():
            ok = self._step(notifier, notifier.tick)
            failures = 0 if ok else failures + 1
            self._kick.wait(self.interval if ok else
                            min(MAX_BACKOFF_SECONDS, self.interval * 2 ** failures))
            self._kick.clear()
        # The daemon is stopping: the last Attempt's alert still goes out.
        self._step(notifier, notifier.tick)

    def _poll_loop(self, notifier: Notifier) -> None:
        failures = 0
        while not self._stop.is_set():
            ok = self._step(notifier, notifier.poll_updates)
            failures = 0 if ok else failures + 1
            if not ok:
                self._stop.wait(min(MAX_BACKOFF_SECONDS, 2.0 ** failures))


def service_from_config(cfg: Config, *, log: Callable[[str], None],
                        interval: float = 5.0) -> AlertService | None:
    """The daemon's AlertService, or None (logged) when Telegram isn't set up."""
    if cfg.telegram_user_id is None:
        log("Telegram alerts off (telegram.user_id is not set)")
        return None
    token = cfg.secrets.get(Config.TELEGRAM_TOKEN_SECRET)
    if not token:
        log(f"Telegram alerts off: secret {Config.TELEGRAM_TOKEN_SECRET} not found in .env "
            "or the Keychain")
        return None
    log("Telegram alerts and commands on (long polling; no port opened)")
    return AlertService(cfg, HttpBotAPI(token), log=log, interval=interval)
