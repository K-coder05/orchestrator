"""The 8am Telegram digest (§2.12), on a fake clock against a fake Bot API."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

from nightshift import digest
from nightshift.alerts import AlertService, Notifier
from nightshift.config import ConfigError, load_config
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState

from test_alerts import TOKEN, USER_ID, FakeBotAPI, _wait_for, api, finish, tg_cfg, tg_db  # noqa: F401

LA = ZoneInfo("America/Los_Angeles")


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=LA)


class Clock:
    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock():
    return Clock(at(3, 22))  # the evening before


@pytest.fixture
def notifier(tg_cfg, tg_db, api, clock):
    logs: list[str] = []
    n = Notifier(tg_cfg, tg_db, api, log=logs.append, clock=clock)
    n.logs = logs
    return n


def digests(api) -> list[str]:
    return [text for _, text in api.sent if text.startswith("Nightshift digest")]


def overnight(cfg, db):
    """A night's worth of outcomes; returns the Task ids by what happened."""
    done = finish(cfg, db, "Summarize the Q3 notes")
    failed = finish(cfg, db, "Fix the flaky test", TaskState.FAILED,
                    reason="Attempt 3 crashed: agent exited with code 1; no retries left")
    paused = create_task(cfg, db, "Long research on caching").spec.id
    db.transition(paused, TaskState.RUNNING, "Attempt 1 started")
    db.transition(paused, TaskState.PAUSED_USAGE,
                  "claude hit its five_hour limit; resets at 2026-11-04T03:00:00-08:00")
    return {"done": done, "failed": failed, "paused": paused}


def test_digest_is_sent_once_at_8am_with_every_overnight_outcome(
        tg_cfg, tg_db, api, notifier, clock):
    ids = overnight(tg_cfg, tg_db)
    clock.t = at(4, 7, 59)
    notifier.tick()
    assert digests(api) == []
    clock.t = at(4, 8, 0)
    notifier.tick()
    [text] = digests(api)
    assert text.startswith("Nightshift digest, Wed Nov 4\nSince Tue 10:00pm")
    assert "Finished (1)" in text and ids["done"] in text
    assert "Wrote the summary to output/summary.md." in text
    assert "Failed (1)" in text and "Attempt 3 crashed" in text
    # The usage pause never alerted, but the digest shows it.
    alerts = [t for _, t in api.sent if t not in digests(api)]
    assert not any(ids["paused"] in t for t in alerts)
    assert "Paused for usage (1)" in text and ids["paused"] in text
    assert "claude hit its five_hour limit" in text
    assert "Now: 1 paused for usage" in text
    # Never again that day, nor before the next 8am.
    for t in (at(4, 8, 1), at(4, 15), at(5, 7, 59)):
        clock.t = t
        notifier.tick()
    assert len(digests(api)) == 1
    assert len(tg_db.digests()) == 1


def test_next_digest_covers_only_what_happened_since(tg_cfg, tg_db, api, notifier, clock):
    first = finish(tg_cfg, tg_db, "First night's task")
    clock.t = at(4, 8)
    notifier.tick()
    second = finish(tg_cfg, tg_db, "Second night's task")
    clock.t = at(5, 8)
    notifier.tick()
    one, two = digests(api)
    assert first in one and second not in one
    assert second in two and first not in two
    assert "Since Wed 8:00am" in two


def test_laptop_asleep_at_8am_sends_the_digest_once_on_wake(tg_cfg, tg_db, api, notifier, clock):
    tid = finish(tg_cfg, tg_db, "Overnight scrape")
    notifier.tick()  # 10pm: nothing due
    clock.t = at(4, 10, 37)  # woke at 10:37am
    notifier.tick()
    notifier.tick()
    # A daemon restart (launchd) doesn't make it again.
    Notifier(tg_cfg, tg_db, api, clock=clock).tick()
    [text] = digests(api)
    assert tid in text
    assert [d["digest_date"] for d in tg_db.digests()] == ["2026-11-04"]


def test_asleep_through_several_8ams_makes_one_digest(tg_cfg, tg_db, api, notifier, clock):
    tid = finish(tg_cfg, tg_db, "Before the long sleep")
    clock.t = at(7, 2)  # three 8ams missed; it's 2am
    notifier.tick()
    [text] = digests(api)
    assert tid in text
    assert [d["digest_date"] for d in tg_db.digests()] == ["2026-11-06"]
    clock.t = at(7, 8)
    notifier.tick()
    assert len(digests(api)) == 2
    assert "Nothing happened overnight." in digests(api)[1]


def test_comedy_tasks_appear_as_status_lines_only(tg_cfg, tg_db, api, notifier, clock):
    done = finish(tg_cfg, tg_db, "Tighten the dentist bit", task_type="comedy",
                  agent_line="New tag: the drill sounds like my landlord.")
    paused = create_task(tg_cfg, tg_db, "Punch up the landlord premise",
                         task_type="comedy").spec.id
    tg_db.transition(paused, TaskState.RUNNING, "Attempt 1 started")
    tg_db.transition(paused, TaskState.PAUSED_USAGE, "claude hit its limit on the drill premise")
    clock.t = at(4, 8)
    notifier.tick()
    [text] = digests(api)
    assert f"comedy Task {done} done" in text
    assert f"comedy Task {paused} paused usage" in text
    for leaked in ("dentist", "drill", "landlord", "premise", "Tighten", "Punch"):
        assert leaked not in text


def test_first_start_does_not_send_history(tg_cfg, tg_db, api, clock):
    old = finish(tg_cfg, tg_db, "Long before Telegram")
    clock.t = at(4, 15)  # enabled at 3pm, after today's 8am
    n = Notifier(tg_cfg, tg_db, api, clock=clock)
    n.tick()
    assert digests(api) == []
    new = finish(tg_cfg, tg_db, "After Telegram")
    clock.t = at(5, 8)
    n.tick()
    [text] = digests(api)
    assert new in text and old not in text


def test_outage_at_8am_retries_and_sends_once(tg_cfg, tg_db, api, notifier, clock):
    finish(tg_cfg, tg_db, "Something")
    clock.t = at(4, 8)
    api.down = True
    assert notifier.tick() is False
    clock.t = at(4, 8, 5)
    assert notifier.tick() is False
    api.down = False
    clock.t = at(4, 8, 10)
    assert notifier.tick() is True
    notifier.tick()
    assert len(digests(api)) == 1
    [row] = tg_db.digests()
    assert row["tries"] == 3 and row["sent_at"] is not None


def test_digest_never_delivered_is_dropped_when_too_old(tg_cfg, tg_db, api, notifier, clock):
    clock.t = at(4, 8)
    api.down = True
    notifier.tick()
    api.down = False
    clock.t = at(4, 21)  # telegram.alert_max_age_hours is 12
    notifier.tick()
    assert digests(api) == []
    assert tg_db.digests()[0]["dropped_at"] is not None
    assert any("digest for 2026-11-04 dropped" in m for m in notifier.logs)


def test_secrets_are_redacted(tg_cfg, tg_db, api, notifier, clock):
    tg_cfg.secrets.require("TELEGRAM_BOT_TOKEN")
    finish(tg_cfg, tg_db, "Echo config", TaskState.FAILED, reason=f"agent printed {TOKEN}")
    clock.t = at(4, 8)
    notifier.tick()
    [text] = digests(api)
    assert TOKEN not in text and "[REDACTED]" in text


def test_long_night_fits_one_telegram_message(tg_cfg, tg_db, api, notifier, clock):
    for i in range(60):
        finish(tg_cfg, tg_db, f"Task number {i} " + "x" * 80)
    clock.t = at(4, 8)
    notifier.tick()
    [text] = digests(api)
    assert len(text) <= 4096
    assert "more lines: orch list" in text


def test_due_date_is_the_latest_digest_time():
    from datetime import date, time

    eight = time(8, 0)
    assert digest.due_date(at(4, 8), eight) == date(2026, 11, 4)
    assert digest.due_date(at(4, 7, 59), eight) == date(2026, 11, 3)
    # Across the DST change (Nov 1, 2026): 8am exists every day.
    assert digest.due_date(datetime(2026, 11, 1, 8, tzinfo=LA), eight) == date(2026, 11, 1)


def test_digest_time_is_config(tmp_path):
    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({"telegram": {"digest_time": "07:30"}}))
    from datetime import time

    assert load_config(d, host=FakeHost()).telegram_digest_time == time(7, 30)
    (d / "config.yaml").write_text(yaml.safe_dump({"telegram": {"digest_time": "8am"}}))
    with pytest.raises(ConfigError, match="digest_time"):
        load_config(d, host=FakeHost())


def test_daemon_alert_loop_sends_the_digest(tg_cfg, tg_db, api):
    clock = Clock(at(4, 7, 59))
    service = AlertService(tg_cfg, api, interval=60, clock=clock)
    service.start()
    try:
        assert _wait_for(lambda: tg_db.get_meta(digest.SINCE_KEY) is not None)
        tid = finish(tg_cfg, tg_db, "Overnight work")
        clock.t = at(4, 8)  # 8am, or the machine just woke
        service.kick()
        assert _wait_for(lambda: len(digests(api)) == 1)
    finally:
        service.stop()
    assert tid in digests(api)[0]
