"""The paid-credit rule (§1.5), Phase 2: subscription first.

A Task whose own subscription (Gemini) is out of usage moves to the paid
path (``gemini-api``) only when it is urgent or the subscription's next
reset falls after the end of its Catch-up window; else it waits. A Task
that failed over to Gemini never does (§7, resolved). Run through the
queue on a fake clock with the fake `gemini` CLI.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

from nightshift import paid, schedules, spend, usage
from nightshift.adapters.base import RateLimit
from nightshift.daemon import QueueRunner
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import iso
from nightshift.workspace import read_task_yaml

from test_codex import Clock
from test_schedules import Clock as FixedClock
from test_gemini import _gemini_runs, genv  # noqa: F401  (fixture)
from test_gemini_api import cfg, config_dir, db, provider  # noqa: F401  (fixtures)


def _queue(cfg, db, clock):
    return QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)


def _gemini_out(cfg, db, clock, hours=5):
    """Gemini's daily Window is out until ``hours`` from now."""
    reset = iso(clock() + timedelta(hours=hours))
    usage.record_limit_hit(cfg, db, "gemini", RateLimit("daily", reset), at=clock(),
                           source="probe")


def _new(cfg, db, **kw):
    """A Task on Gemini as its own agent (as a template puts it), not --agent."""
    kw.setdefault("long", False)
    kw.setdefault("task_type", "general")  # no failover to another subscription
    return create_task(cfg, db, "Summarize lecture 3", agent="gemini", agent_override=False,
                       **kw)


def test_urgent_task_runs_on_paid_credits_while_its_subscription_is_out(cfg, db, genv):
    clock = Clock()
    _gemini_out(cfg, db, clock)
    new = _new(cfg, db, priority="urgent")
    tid = new.spec.id

    assert _queue(cfg, db, clock).run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == TaskState.DONE and row["agent"] == "gemini-api"
    assert read_task_yaml(new.folder).agent == "gemini-api"
    assert [a["agent"] for a in db.attempts(tid)] == ["gemini-api"]
    assert db.task_spend(tid) > 0
    [d] = db.paid_credit_decisions(tid)
    assert d["use_paid"] == 1 and d["agent"] == "gemini" and "urgent" in d["reason"]


def test_task_that_can_wait_waits_for_the_reset_and_the_decision_is_logged_once(cfg, db, genv):
    clock = Clock()
    _gemini_out(cfg, db, clock)
    new = _new(cfg, db)
    tid = new.spec.id
    qr = _queue(cfg, db, clock)

    assert qr.run_next() is None and qr.run_next() is None
    row = db.get_task(tid)
    assert row["state"] == TaskState.QUEUED and row["agent"] == "gemini"
    assert not db.spend_events() and not db.failovers(tid)
    [d] = db.paid_credit_decisions(tid)  # once, not once per pick
    assert d["use_paid"] == 0 and d["rule"] == "no_window"
    assert "paid-credit rule" in (new.folder / "logs" / "nightshift.log").read_text()

    clock.advance(hours=5, minutes=1)  # Gemini has reset: the subscription runs it
    assert qr.run_next() == tid
    assert [a["agent"] for a in db.attempts(tid)] == ["gemini"] and not db.spend_events()


TEMPLATE = {
    "description": "daily scan", "task_type": "general", "agent": "gemini",
    "estimated_minutes": 20, "max_runtime_minutes": 30, "long": False, "internet": "on",
    "prompt": "Scan the postings. Request: {{request}}",
}


def _occurrence(cfg, db, tmp_path, fire_at, catch_up="within 12h"):
    """An Occurrence of a Schedule on Gemini, fired at ``fire_at``."""
    tdir = tmp_path / "templates"
    tdir.mkdir(exist_ok=True)
    (tdir / "scan.yaml").write_text(yaml.safe_dump(TEMPLATE))
    cfg.data.setdefault("templates", {})["dir"] = str(tdir)
    schedules.add(cfg, db, "scan", template="scan", cron_rule="30 6 * * *", catch_up=catch_up,
                  at=fire_at - timedelta(days=1))
    d = schedules.fire(cfg, db, schedules.get(db, "scan"), fire_at, now=fire_at,
                       trigger=schedules.TRIGGER_POLL)
    return d.task_id


LA = ZoneInfo("America/Los_Angeles")


@pytest.mark.parametrize("reset_hours,paid", [(14, True), (4, False)])
def test_occurrence_uses_paid_credits_only_if_the_reset_is_after_its_catch_up_window(
        cfg, db, genv, tmp_path, reset_hours, paid):
    fire_at = datetime(2026, 10, 22, 6, 30, tzinfo=LA)  # window: until 18:30
    clock = FixedClock(fire_at)
    _gemini_out(cfg, db, clock, hours=reset_hours)  # back at 20:30 / 10:30
    tid = _occurrence(cfg, db, tmp_path, fire_at)

    ran = _queue(cfg, db, clock).run_next()
    [d] = db.paid_credit_decisions(tid)
    if paid:
        assert ran == tid and db.task_state(tid) == TaskState.DONE
        assert [a["agent"] for a in db.attempts(tid)] == ["gemini-api"]
        assert d["rule"] == "after_window" and "2026-10-22T18:30:00-07:00" in d["reason"]
    else:
        assert ran is None and db.get_task(tid)["agent"] == "gemini"
        assert d["rule"] == "before_window" and d["use_paid"] == 0


def test_run_on_next_wake_has_no_catch_up_window_so_it_waits(cfg, db, genv, tmp_path):
    fire_at = datetime(2026, 10, 22, 6, 30, tzinfo=LA)
    clock = FixedClock(fire_at)
    _gemini_out(cfg, db, clock, hours=40)
    tid = _occurrence(cfg, db, tmp_path, fire_at, catch_up="next-wake")
    assert _queue(cfg, db, clock).run_next() is None
    assert [d["rule"] for d in db.paid_credit_decisions(tid)] == ["no_window"]


def test_task_that_failed_over_to_gemini_waits_when_gemini_is_out_too(cfg, db, genv):
    clock = Clock()
    usage.record_limit_hit(cfg, db, "claude", RateLimit("five_hour", iso(clock() + timedelta(hours=3))),
                           at=clock(), source="probe")
    genv.setenv("FAKE_GEMINI_LIMIT", "1")  # Gemini's quota runs out on its first call
    new = create_task(cfg, db, "Summarize lecture 3", task_type="summary", priority="urgent",
                      long=False)
    tid = new.spec.id
    qr = _queue(cfg, db, clock)

    assert qr.run_next() == tid  # failed over from Claude, then hit Gemini's limit
    assert db.get_task(tid)["state"] == TaskState.PAUSED_USAGE
    assert [f["to_agent"] for f in db.failovers(tid)] == ["gemini"]
    assert qr.run_next() is None
    row = db.get_task(tid)
    assert row["state"] == TaskState.PAUSED_USAGE and row["agent"] == "gemini"
    assert not db.spend_events()
    [d] = db.paid_credit_decisions(tid)
    assert d["use_paid"] == 0 and d["rule"] == "failed_over"


def _section8(folder) -> str:
    return (folder / "report.md").read_text().split("## 8.")[1].split("## 9.")[0]


def test_urgent_task_that_hits_the_limit_mid_run_continues_on_paid_credits(cfg, db, genv):
    clock = Clock()
    genv.setenv("FAKE_GEMINI_LIMIT", "1")
    genv.setenv("FAKE_GEMINI_LIMIT_ONCE", "1")
    new = _new(cfg, db, deadline=iso(clock() + timedelta(hours=6)))  # urgent by its deadline
    tid = new.spec.id
    (new.folder / "plan.md").write_text("# Plan\n1. read\n")
    qr = _queue(cfg, db, clock)

    assert qr.run_next() == tid and db.task_state(tid) == TaskState.PAUSED_USAGE
    assert qr.run_next() == tid  # no wait for Gemini's reset
    assert db.task_state(tid) == TaskState.DONE
    assert [a["agent"] for a in db.attempts(tid)] == ["gemini", "gemini-api"]
    last = _gemini_runs(new.folder)[-1]
    assert "--resume" not in last["argv"] and "progress.md" in last["stdin"]
    [f] = db.failovers(tid)
    assert (f["from_agent"], f["to_agent"], f["trigger"]) == ("gemini", "gemini-api", "paid_credit")
    s8 = _section8(new.folder)
    assert "Paid credits" in s8 and "used paid credits" in s8 and "urgent" in s8
    assert "| gemini-api |" in s8 and "$0.0480 (paid credits)" in s8


def _spent_today(cfg, db, clock, usd):
    """Paid calls of another Task that spent ``usd`` today."""
    other = create_task(cfg, db, "earlier paid work", agent="gemini-api", long=False)
    db.record_spend(other.spec.id, run_id=None, agent="gemini-api", model=None, at=iso(clock()),
                    input_tokens=0, cached_tokens=0, output_tokens=0, thoughts_tokens=0,
                    cost_usd=usd, raw=None)
    db.transition(other.spec.id, TaskState.CANCELLED, "test")


def test_spend_caps_still_apply_an_urgent_task_waits_while_the_daily_cap_holds(cfg, db, genv):
    clock = FixedClock(datetime(2026, 10, 22, 9, 0, tzinfo=LA))
    _gemini_out(cfg, db, clock)
    _spent_today(cfg, db, clock, 5.0)
    new = _new(cfg, db, priority="urgent")
    tid = new.spec.id

    assert _queue(cfg, db, clock).run_next() is None
    assert db.get_task(tid)["agent"] == "gemini" and db.task_spend(tid) == 0
    [d] = db.paid_credit_decisions(tid)
    assert d["use_paid"] == 0 and d["rule"] == "spend_cap" and "daily spend cap" in d["reason"]


def test_no_api_key_means_the_task_waits(cfg, db, genv):
    clock = Clock()
    cfg.secrets._env.pop("GEMINI_PAID_API_KEY")
    _gemini_out(cfg, db, clock)
    new = _new(cfg, db, priority="urgent")
    assert _queue(cfg, db, clock).run_next() is None
    assert [d["rule"] for d in db.paid_credit_decisions(new.spec.id)] == ["no_key"]


def test_task_with_agent_override_never_moves_to_paid_credits(cfg, db, genv):
    clock = Clock()
    _gemini_out(cfg, db, clock)
    new = create_task(cfg, db, "x", agent="gemini", priority="urgent", long=False)  # --agent
    assert paid.apply(cfg, db, clock()) == []
    assert db.get_task(new.spec.id)["agent"] == "gemini"


@pytest.mark.parametrize("spend_paused", [False, True])
def test_task_goes_back_to_its_subscription_once_it_resets(cfg, db, genv, spend_paused):
    clock = FixedClock(datetime(2026, 10, 22, 9, 0, tzinfo=LA))
    _gemini_out(cfg, db, clock)
    new = _new(cfg, db, priority="urgent")
    tid = new.spec.id
    [moved] = paid.apply(cfg, db, clock())
    assert moved.to_agent == "gemini-api" and db.get_task(tid)["agent"] == "gemini-api"
    if spend_paused:
        _spent_today(cfg, db, clock, 5.0)
        assert spend.pause_paid_queued(cfg, db, clock()) == [tid]

    clock.advance(minutes=30)  # still out: stays on paid credits
    assert paid.apply(cfg, db, clock()) == []
    clock.advance(hours=5)
    [back] = paid.apply(cfg, db, clock())
    row = db.get_task(tid)
    assert (back.to_agent, row["agent"], row["state"]) == ("gemini", "gemini", TaskState.QUEUED)
    assert read_task_yaml(new.folder).agent == "gemini"
    assert [f["trigger"] for f in db.failovers(tid)] == ["paid_credit", "subscription_reset"]
    assert [d["rule"] for d in db.paid_credit_decisions(tid)] == ["urgent", "subscription_reset"]
    assert "back on the subscription" in "\n".join(paid.report_lines(db, tid))
