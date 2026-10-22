"""Daily and monthly spend caps (§1.5): paid calls pause (``paused_spend``)
once a day's spend reaches ``spend.per_day_usd`` (until midnight LA time) or
a month's reaches ``spend.per_month_usd`` (until you raise the cap). All on
a fake clock."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nightshift import spend
from nightshift.daemon import QueueRunner
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState

from test_daemon import FakeAttempts

LA = ZoneInfo("America/Los_Angeles")


def at(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


def _spent(db, task_id, usd, when: datetime) -> None:
    db.record_spend(task_id, run_id=None, agent="gemini-api", model="gemini-3.1-pro",
                    at=when.isoformat(timespec="seconds"), input_tokens=0, cached_tokens=0,
                    output_tokens=0, thoughts_tokens=0, cost_usd=usd, raw=None)


def _paid(cfg, db, prompt="Summarize lecture 3"):
    return create_task(cfg, db, prompt, agent="gemini-api", task_type="summary").spec.id


def _spent_by_another_task(cfg, db, usd, when: datetime) -> None:
    """``usd`` spent at ``when`` by a paid Task that is no longer in the queue."""
    earlier = _paid(cfg, db, "earlier paid Task")
    _spent(db, earlier, usd, when)
    db.transition(earlier, TaskState.CANCELLED, "test: out of the way")


def test_daily_cap_pauses_until_midnight_la_time(cfg, db):
    tid = _paid(cfg, db)
    _spent(db, tid, 3.0, at(2026, 10, 22, 9, 0))
    assert spend.spend_pause(cfg, db, at(2026, 10, 22, 10, 0)) is None

    _spent(db, tid, 2.0, at(2026, 10, 22, 21, 30))
    found = spend.spend_pause(cfg, db, at(2026, 10, 22, 22, 0))
    assert found is not None and found.kind == spend.KIND_DAY
    assert found.until == at(2026, 10, 23, 0, 0)
    assert "$5.00 daily spend cap" in found.reason and "$5.00 spent" in found.reason

    # The next day (LA time) starts from zero.
    assert spend.spend_pause(cfg, db, at(2026, 10, 23, 0, 0)) is None


def test_spend_is_dated_in_la_time_whatever_offset_it_was_written_with(cfg, db):
    tid = _paid(cfg, db)
    # 06:30 UTC on the 23rd is 23:30 on the 22nd in Los Angeles.
    _spent(db, tid, 5.0, datetime(2026, 10, 23, 6, 30, tzinfo=ZoneInfo("UTC")))
    assert spend.spend_pause(cfg, db, at(2026, 10, 22, 23, 45)).kind == spend.KIND_DAY
    assert spend.spend_pause(cfg, db, at(2026, 10, 23, 0, 15)) is None


def test_day_and_month_ends_are_local_midnight_across_daylight_saving(cfg, db):
    tid = _paid(cfg, db)
    # 2026-11-01 is 25 hours long in Los Angeles (clocks fall back at 2am).
    _spent(db, tid, 5.0, at(2026, 11, 1, 23, 0))
    assert spend.spend_pause(cfg, db, at(2026, 11, 1, 23, 30)).until == at(2026, 11, 2, 0, 0)
    # 2027-03-14 is 23 hours long (clocks spring forward at 2am).
    _spent(db, tid, 5.0, at(2027, 3, 14, 1, 0))
    assert spend.spend_pause(cfg, db, at(2027, 3, 14, 1, 30)).until == at(2027, 3, 15, 0, 0)
    # A month at its cap that ends in a different UTC offset than it began.
    _spent(db, tid, 6.0, at(2026, 11, 20, 12, 0))
    _spent(db, tid, 4.0, at(2026, 11, 21, 12, 0))
    assert spend.spend_pause(cfg, db, at(2026, 11, 21, 13, 0)).until == at(2026, 12, 1, 0, 0)


def test_monthly_cap_pauses_until_you_raise_it(cfg, db):
    tid = _paid(cfg, db)
    for day in range(1, 5):  # $4 a day: under the daily cap, $16 by the 4th
        _spent(db, tid, 4.0, at(2026, 10, day, 12, 0))
    found = spend.spend_pause(cfg, db, at(2026, 10, 22, 9, 0))
    assert found is not None and found.kind == spend.KIND_MONTH
    assert found.until == at(2026, 11, 1, 0, 0)
    assert "$15.00 monthly spend cap" in found.reason and "$16.00 spent" in found.reason
    assert "orch raise-cap" in found.reason

    spend.raise_month_cap(cfg, db, at(2026, 10, 22, 9, 5), 20)
    assert spend.month_cap(cfg, db, at(2026, 10, 22, 9, 5)) == 20
    assert spend.spend_pause(cfg, db, at(2026, 10, 22, 9, 5)) is None

    # The raise is for this month only; a new month starts from zero anyway.
    assert spend.month_cap(cfg, db, at(2026, 11, 1, 0, 0)) == 15
    assert spend.spend_pause(cfg, db, at(2026, 11, 1, 0, 0)) is None


def test_a_raise_must_be_above_the_current_cap(cfg, db):
    with pytest.raises(ValueError, match=r"above this month's cap of \$15.00"):
        spend.raise_month_cap(cfg, db, at(2026, 10, 22, 9, 0), 15)


def test_the_monthly_cap_wins_over_the_daily_one(cfg, db):
    # A day at its cap in a month at its cap: midnight alone won't resume it.
    tid = _paid(cfg, db)
    _spent(db, tid, 10.0, at(2026, 10, 2, 12, 0))
    _spent(db, tid, 5.0, at(2026, 10, 22, 12, 0))
    assert spend.spend_pause(cfg, db, at(2026, 10, 22, 13, 0)).kind == spend.KIND_MONTH


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def test_daily_cap_pauses_paid_tasks_until_midnight_and_leaves_subscription_work(cfg, db):
    host = FakeHost()
    clock = Clock(at(2026, 10, 22, 22, 0))
    earlier = _paid(cfg, db, "earlier paid Task")
    _spent(db, earlier, 5.0, at(2026, 10, 22, 21, 0))
    paid = _paid(cfg, db)
    sub = create_task(cfg, db, "Summarize lecture 4", agent="gemini", task_type="summary").spec.id
    db.transition(earlier, TaskState.CANCELLED, "test: out of the way")
    fake = FakeAttempts(host)
    runner = QueueRunner(cfg, db, host, run_attempt_fn=fake, clock=clock)

    assert runner.drain() == [sub]
    assert db.task_state(sub) == TaskState.DONE
    assert db.task_state(paid) == TaskState.PAUSED_SPEND
    why = db.transitions(paid)[-1]["reason"]
    assert "$5.00 daily spend cap" in why and "midnight" in why
    assert db.get_task(paid)["resume_at"] == "2026-10-23T00:00:00-07:00"

    clock.t = at(2026, 10, 22, 23, 59)
    assert runner.drain() == []  # still the same day

    clock.t = at(2026, 10, 23, 0, 0)
    assert runner.drain() == [paid]
    assert [t["to_state"] for t in db.transitions(paid)] == [
        "queued", "paused_spend", "queued", "running", "done"]
    assert "daily spend cap" in db.transitions(paid)[2]["reason"]


def test_monthly_cap_holds_paid_tasks_past_midnight_until_the_cap_is_raised(cfg, db):
    host = FakeHost()
    clock = Clock(at(2026, 10, 22, 10, 0))
    _spent_by_another_task(cfg, db, 15.0, at(2026, 10, 3, 12, 0))
    paid = _paid(cfg, db)
    runner = QueueRunner(cfg, db, host, run_attempt_fn=FakeAttempts(host), clock=clock)

    assert runner.drain() == []
    assert db.get_task(paid)["pause_kind"] == spend.KIND_MONTH
    clock.t = at(2026, 10, 23, 0, 0)
    assert runner.drain() == []  # midnight is not enough

    spend.raise_month_cap(cfg, db, clock.t, 25)
    assert runner.drain() == [paid]
    assert "monthly spend cap pause over" in db.transitions(paid)[2]["reason"]


def test_a_new_month_lifts_the_monthly_cap(cfg, db):
    host = FakeHost()
    clock = Clock(at(2026, 10, 31, 23, 0))
    _spent_by_another_task(cfg, db, 15.0, at(2026, 10, 3, 12, 0))
    paid = _paid(cfg, db)
    runner = QueueRunner(cfg, db, host, run_attempt_fn=FakeAttempts(host), clock=clock)
    assert runner.drain() == []
    clock.t = at(2026, 11, 1, 0, 0)
    assert runner.drain() == [paid]


def test_a_paid_task_whose_deadline_passes_while_paused_is_cancelled(cfg, db):
    host = FakeHost()
    clock = Clock(at(2026, 10, 22, 10, 0))
    _spent_by_another_task(cfg, db, 15.0, at(2026, 10, 3, 12, 0))
    paid = create_task(cfg, db, "Tailor for Acme", agent="gemini-api", task_type="summary",
                       deadline="2026-10-24T09:00:00-07:00").spec.id
    runner = QueueRunner(cfg, db, host, run_attempt_fn=FakeAttempts(host), clock=clock)
    runner.drain()
    clock.t = at(2026, 10, 24, 9, 30)
    runner.drain()
    assert db.task_state(paid) == TaskState.CANCELLED
    assert "deadline 2026-10-24T09:00:00-07:00 passed while paused on spend" in (
        db.transitions(paid)[-1]["reason"])


def test_orch_show_and_resume_explain_a_spend_pause(cfg, db):
    from nightshift import pauses, queries, views

    _spent_by_another_task(cfg, db, 5.0, at(2026, 10, 22, 9, 0))
    paid = _paid(cfg, db)
    spend.pause_paid_queued(cfg, db, at(2026, 10, 22, 10, 0))
    shown = views.format_show(queries.task_detail(cfg, db, paid, at(2026, 10, 22, 10, 0)))
    assert "paused on the daily spend cap until 2026-10-23T00:00:00-07:00" in shown
    with pytest.raises(pauses.NotAllowed, match="resumes on its own once the cap lifts"):
        pauses.resume_by_hand(cfg, db, paid, at=at(2026, 10, 22, 10, 5))
