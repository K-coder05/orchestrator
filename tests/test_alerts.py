"""Telegram alerts (§2.12), against a fake Bot API."""

from __future__ import annotations

import threading
import time

import pytest
import yaml

from nightshift import report
from nightshift.alerts import AlertService, Notifier
from nightshift.config import load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.telegram import TelegramError

from test_daemon import FakeAttempts

USER_ID = 424242424
TOKEN = "123456:secret-bot-token-abcdef"


class FakeBotAPI:
    """Stands in for the Telegram Bot API: records sends, serves updates."""

    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.updates: list[dict] = []
        self.down = False
        self.offsets: list[int | None] = []

    def send_message(self, chat_id: int, text: str) -> None:
        if self.down:
            raise TelegramError("sendMessage: connection refused")
        self.sent.append((chat_id, text))

    def get_updates(self, offset: int | None, timeout: float) -> list[dict]:
        if self.down:
            raise TelegramError("getUpdates: connection refused")
        self.offsets.append(offset)
        out = [u for u in self.updates if offset is None or u["update_id"] >= offset]
        return out


@pytest.fixture
def tg_cfg(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "telegram": {"user_id": USER_ID},
    }))
    env = d / ".env"
    env.write_text(f"TELEGRAM_BOT_TOKEN={TOKEN}\n")
    env.chmod(0o600)
    return load_config(d, host=FakeHost())


@pytest.fixture
def tg_db(tg_cfg):
    d = Database(tg_cfg.db_path, tz=tg_cfg.timezone)
    yield d
    d.close()


@pytest.fixture
def api():
    return FakeBotAPI()


@pytest.fixture
def notifier(tg_cfg, tg_db, api):
    logs: list[str] = []
    n = Notifier(tg_cfg, tg_db, api, log=logs.append)
    n.logs = logs
    return n


def finish(cfg, db, prompt, state=TaskState.DONE, *, agent_line="Wrote the summary to output/summary.md.",
           task_type=None, reason="agent finished"):
    """A Task that ran one Attempt and ended in ``state``, report finalized."""
    new = create_task(cfg, db, prompt, task_type=task_type)
    tid = new.spec.id
    folder = new.folder
    (folder / "report.md").write_text(
        report.skeleton(prompt).replace("_(not written yet)_", agent_line, 1))
    report.finalize(folder, title=prompt, result=str(state), result_detail=None,
                    attempts=[], usage_rows=[])
    db.transition(tid, TaskState.RUNNING, "Attempt 1 started")
    db.transition(tid, state, reason)
    return tid


def test_finished_task_sends_its_one_line_result(tg_cfg, tg_db, api, notifier):
    tid = finish(tg_cfg, tg_db, "Summarize the Q3 notes")
    notifier.tick()
    assert len(api.sent) == 1
    chat, text = api.sent[0]
    assert chat == USER_ID
    assert tid in text and "done" in text and "Summarize the Q3 notes" in text
    assert "Wrote the summary to output/summary.md." in text
    # Sent once only.
    notifier.tick()
    assert len(api.sent) == 1


def test_failed_task_sends_the_reason_immediately(tg_cfg, tg_db, api, notifier):
    tid = finish(tg_cfg, tg_db, "Fix the flaky test", TaskState.FAILED,
                 reason="Attempt 3 crashed: agent exited with code 1; no retries left (2 of 2 used)")
    notifier.tick()
    [(_, text)] = api.sent
    assert f"Task {tid} failed: Fix the flaky test" in text
    assert "Attempt 3 crashed: agent exited with code 1; no retries left" in text


def test_task_that_needs_you_sends_the_request(tg_cfg, tg_db, api, notifier):
    new = create_task(tg_cfg, tg_db, "Book the venue")
    (new.folder / "needs_you.json").write_text(
        '{"kind": "decision", "summary": "Two venues fit; pick one."}')
    report.finalize(new.folder, title="Book the venue", result="needs_you", result_detail=None,
                    attempts=[], usage_rows=[])
    tg_db.transition(new.spec.id, TaskState.RUNNING, "Attempt 1 started")
    tg_db.transition(new.spec.id, TaskState.NEEDS_YOU, "agent wrote needs_you.json and stopped")
    notifier.tick()
    [(_, text)] = api.sent
    assert f"Task {new.spec.id} needs you: Book the venue" in text
    assert "Two venues fit; pick one." in text


def test_usage_pause_and_other_moves_send_nothing(tg_cfg, tg_db, api, notifier):
    tid = create_task(tg_cfg, tg_db, "Long research").spec.id
    tg_db.transition(tid, TaskState.RUNNING, "Attempt 1 started")
    tg_db.transition(tid, TaskState.PAUSED_USAGE, "claude hit its five_hour limit")
    tg_db.transition(tid, TaskState.QUEUED, "usage pause over")
    tg_db.transition(tid, TaskState.RUNNING, "Attempt 2 started")
    tg_db.transition(tid, TaskState.RETRY_WAIT, "Attempt 2 crashed; retry 1 of 2")
    tg_db.transition(tid, TaskState.CANCELLED, "cancelled by hand")
    notifier.tick()
    assert api.sent == []


@pytest.mark.parametrize("state, reason", [
    (TaskState.DONE, "agent finished"),
    (TaskState.FAILED, "Attempt 1 crashed: the dentist bit premise was unclear"),
])
def test_comedy_tasks_send_status_lines_only(tg_cfg, tg_db, api, notifier, state, reason):
    tid = finish(tg_cfg, tg_db, "Tighten the dentist bit", state, task_type="comedy",
                 agent_line="New tag: the drill sounds like my landlord.", reason=reason)
    notifier.tick()
    [(_, text)] = api.sent
    assert text == f"Nightshift: comedy Task {tid} {state}.\nDetails: orch report {tid}"
    for leaked in ("dentist", "drill", "landlord", "premise"):
        assert leaked not in text


def test_token_and_user_id_never_reach_messages_or_logs(tg_cfg, tg_db, api, notifier):
    tg_cfg.secrets.require("TELEGRAM_BOT_TOKEN")  # loaded, as the daemon does
    finish(tg_cfg, tg_db, "Echo config", TaskState.FAILED,
           reason=f"agent printed {TOKEN} and user {USER_ID}")
    api.down = True
    notifier.tick()
    api.down = False
    notifier.tick()
    [(_, text)] = api.sent
    assert TOKEN not in text and str(USER_ID) not in text
    assert "[REDACTED]" in text
    stored = tg_db.alerts()[0]
    assert TOKEN not in stored["text"] and str(USER_ID) not in stored["text"]
    assert notifier.logs and not any(TOKEN in m or str(USER_ID) in m for m in notifier.logs)


def test_outage_keeps_alerts_pending_then_sends_them_in_order(tg_cfg, tg_db, api, notifier):
    first = finish(tg_cfg, tg_db, "First")
    second = finish(tg_cfg, tg_db, "Second", TaskState.FAILED, reason="boom")
    api.down = True
    assert notifier.tick() is False
    assert notifier.tick() is False
    assert api.sent == []
    assert [a["tries"] for a in tg_db.alerts()] == [2, 0]
    api.down = False
    assert notifier.tick() is True
    assert [f"Task {first} " in t for _, t in api.sent] == [True, False]
    assert f"Task {second} failed" in api.sent[1][1]


def test_alert_never_delivered_is_dropped_when_too_old(tg_cfg, tg_db, api):
    from datetime import timedelta

    from nightshift.timeutil import now

    # 10am to 11pm today: no 8am digest in between.
    clock = [now(tg_cfg.timezone).replace(hour=10, minute=0)]
    logs: list[str] = []
    n = Notifier(tg_cfg, tg_db, api, log=logs.append, clock=lambda: clock[0])
    tid = finish(tg_cfg, tg_db, "Stale")
    api.down = True
    n.tick()
    clock[0] += timedelta(hours=13)  # telegram.alert_max_age_hours is 12
    api.down = False
    n.tick()
    assert api.sent == []
    assert tg_db.alerts(tid)[0]["dropped_at"] is not None
    assert any(f"Task {tid} dropped" in m for m in logs)


def _msg(update_id, from_id, text):
    return {"update_id": update_id,
            "message": {"message_id": update_id, "from": {"id": from_id, "is_bot": False},
                        "chat": {"id": from_id, "type": "private"}, "text": text}}


def test_incoming_messages_from_other_users_are_ignored_and_logged(tg_cfg, tg_db, api, notifier):
    api.updates = [_msg(7, 999001, "/approve all"), _msg(8, USER_ID, "status?")]
    assert notifier.poll_updates() is True
    # Nothing is answered or acted on; only the configured user is ever written to.
    assert api.sent == []
    assert any("ignored" in m and "999001" in m for m in notifier.logs)
    assert not any("/approve all" in m for m in notifier.logs)
    assert not any(str(USER_ID) in m for m in notifier.logs)
    # The next poll confirms both, also after a daemon restart.
    notifier.poll_updates()
    assert api.offsets[-1] == 9
    Notifier(tg_cfg, tg_db, api).poll_updates()
    assert api.offsets[-1] == 9


def test_polling_during_an_outage_does_not_raise(tg_cfg, tg_db, api, notifier):
    api.down = True
    assert notifier.poll_updates() is False
    assert any("getUpdates" in m for m in notifier.logs)


# -- In the daemon -------------------------------------------------------------


class HangingBotAPI(FakeBotAPI):
    """A Bot API that never answers until released."""

    def __init__(self):
        super().__init__()
        self.release = threading.Event()

    def send_message(self, chat_id, text):
        self.release.wait()
        raise TelegramError("sendMessage: timed out")

    def get_updates(self, offset, timeout):
        self.release.wait()
        raise TelegramError("getUpdates: timed out")


def _wait_for(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def test_daemon_sends_alerts_after_each_task(tg_cfg, tg_db, api):
    host = FakeHost()
    tid = create_task(tg_cfg, tg_db, "Quick one").spec.id
    service = AlertService(tg_cfg, api, interval=60)
    runner = QueueRunner(tg_cfg, tg_db, host, run_attempt_fn=FakeAttempts(host), alerts=service)
    service.start()
    try:
        # The runner kicks the sender after each Attempt: no 60s interval wait.
        assert runner.drain() == [tid]
        assert _wait_for(lambda: len(api.sent) == 1)
    finally:
        service.stop()
    assert f"Task {tid} done" in api.sent[0][1]


def test_telegram_outage_never_blocks_or_fails_a_task(tg_cfg, tg_db):
    host = FakeHost()
    api = HangingBotAPI()
    ids = [create_task(tg_cfg, tg_db, f"Task {i}").spec.id for i in range(2)]
    service = AlertService(tg_cfg, api, interval=0.01)
    runner = QueueRunner(tg_cfg, tg_db, host, run_attempt_fn=FakeAttempts(host), alerts=service)
    service.start()
    started = time.monotonic()
    try:
        assert runner.drain() == ids
        assert time.monotonic() - started < 5
        assert all(tg_db.task_state(t) == TaskState.DONE for t in ids)
    finally:
        api.release.set()
        service.stop()
    # Nothing is lost: once Telegram is back (here, the next daemon start),
    # both alerts go out.
    back = FakeBotAPI()
    Notifier(tg_cfg, tg_db, back).tick()
    assert [f"Task {t} done" in text for t, (_, text) in zip(ids, back.sent)] == [True, True]


def test_stopping_the_daemon_still_sends_the_last_alert(tg_cfg, tg_db, api):
    from nightshift.alerts import CURSOR_KEY

    service = AlertService(tg_cfg, api, interval=60)
    service.start()
    assert _wait_for(lambda: tg_db.get_meta(CURSOR_KEY) is not None)
    tid = finish(tg_cfg, tg_db, "Last before shutdown")
    service.stop()
    assert [f"Task {tid} done" in text for _, text in api.sent] == [True]


def test_alert_thread_that_cannot_start_says_so(tg_cfg, api, monkeypatch):
    from nightshift import alerts

    def broken(*a, **kw):
        raise OSError("database is locked")

    monkeypatch.setattr(alerts, "Database", broken)
    logs: list[str] = []
    service = AlertService(tg_cfg, api, log=logs.append)
    service.start()
    service.stop()
    assert any("could not start" in m and "database is locked" in m for m in logs)


def test_monthly_spend_cap_raises_a_needs_you_alert_and_the_daily_one_is_silent(
        tg_cfg, tg_db, api, notifier):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from nightshift import spend

    la = ZoneInfo("America/Los_Angeles")
    day_task = create_task(tg_cfg, tg_db, "Summarize lecture 3", agent="gemini-api",
                           task_type="summary").spec.id
    month_task = create_task(tg_cfg, tg_db, "Summarize lecture 4", agent="gemini-api",
                             task_type="summary").spec.id
    tg_db.record_spend(day_task, run_id=None, agent="gemini-api", model=None,
                       at="2026-10-22T09:00:00-07:00", input_tokens=0, cached_tokens=0,
                       output_tokens=0, thoughts_tokens=0, cost_usd=5.0, raw=None)
    spend.pause_paid_queued(tg_cfg, tg_db, datetime(2026, 10, 22, 10, tzinfo=la))
    notifier.tick()
    assert api.sent == []  # like a usage pause: it resumes on its own

    spend.promote_paused(tg_cfg, tg_db, datetime(2026, 10, 23, 0, tzinfo=la))
    tg_db.record_spend(day_task, run_id=None, agent="gemini-api", model=None,
                       at="2026-10-23T09:00:00-07:00", input_tokens=0, cached_tokens=0,
                       output_tokens=0, thoughts_tokens=0, cost_usd=10.0, raw=None)
    spend.pause_paid_queued(tg_cfg, tg_db, datetime(2026, 10, 23, 10, tzinfo=la))
    notifier.tick()
    assert len(api.sent) == 2  # both queued paid Tasks
    [text] = [t for _, t in api.sent if f"Task {month_task} " in t]
    assert f"Task {month_task} needs you" in text
    assert "$15.00 monthly spend cap" in text and "orch raise-cap" in text
