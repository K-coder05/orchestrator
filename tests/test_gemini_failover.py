"""Gemini failover (§2.8) and retry-with-switch to Gemini (§2.7), Phase 2.

The mechanism is #15's (nightshift.failover); this is the config of the Task
types that fail over to Gemini, run through the queue with the fake Claude,
Gemini and Codex CLIs.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from nightshift import usage
from nightshift.daemon import QueueRunner
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import iso
from nightshift.workspace import read_task_yaml

from test_codex import Clock, cenv, fake_codex  # noqa: F401  (fixture)
from test_gemini import _gemini_runs, fake_gemini, genv  # noqa: F401  (fixture)
from test_runner import env, fake_provider as fake_claude_provider  # noqa: F401  (fixture)


def provider(name, settings):
    """Every agent through its real adapter, with fake CLIs."""
    if name == "gemini":
        return fake_gemini(settings)
    if name == "codex":
        return fake_codex(settings)
    return fake_claude_provider(name, settings)


@pytest.fixture
def fake_clis(cenv, genv):  # noqa: F811
    """The env of the fake Claude, Codex and Gemini CLIs."""
    return cenv


def _new(cfg, db, task_type, prompt="Read up on photosynthesis", **kw):
    kw.setdefault("long", False)  # not gated to the overnight window in tests
    return create_task(cfg, db, prompt, task_type=task_type, **kw)


def _queue(cfg, db, clock):
    return QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)


# --------------------------------------------------------------------------
# Out of Claude usage
# --------------------------------------------------------------------------


@pytest.mark.parametrize("task_type", ["research", "scraping", "summary", "study"])
def test_fails_over_from_claude_to_gemini(cfg, db, fake_clis, task_type):
    clock = Clock()
    cfg.data["retries"]["max_retries"] = 0  # a failover must not need retry budget
    new = _new(cfg, db, task_type, simulate_rate_limit_after=2)
    tid = new.spec.id
    (new.folder / "plan.md").write_text("# Plan\n1. read the slides\n")
    fake_clis.setenv("FAKE_PROGRESS", "claude: read slides 1-10")
    qr = _queue(cfg, db, clock)

    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "queued" and row["agent"] == "gemini" and row["retries_used"] == 0
    assert read_task_yaml(new.folder).agent == "gemini"
    [f] = db.failovers(tid)
    assert (f["from_agent"], f["to_agent"], f["deadline_driven"]) == ("claude", "gemini", 0)

    assert qr.run_next() == tid  # no wait for Claude's reset
    assert db.task_state(tid) == TaskState.DONE
    [run] = _gemini_runs(new.folder)
    assert "--resume" not in run["argv"]  # can't resume Claude's session: fresh
    assert "plan.md" in run["stdin"] and "progress.md" in run["stdin"]
    assert "claude" in run["stdin"] and "gemini" in run["stdin"]  # the takeover note
    assert [(a["agent"], a["outcome"]) for a in db.attempts(tid)] == [
        ("claude", "paused"), ("gemini", "completed")]
    text = (new.folder / "report.md").read_text()
    assert "## 2. Needs you\n\nNothing." in text  # not deadline-driven


def _needs_you(folder) -> str:
    return (folder / "report.md").read_text().split("## 2. Needs you")[1].split("## 3.")[0]


def test_resume_near_deadline_fails_over_to_gemini_and_is_flagged(cfg, db, fake_clis):
    clock = Clock()
    new = _new(cfg, db, "resume", "Tailor", simulate_rate_limit_after=2,
               deadline=iso(clock() + timedelta(hours=10)))
    tid = new.spec.id
    qr = _queue(cfg, db, clock)
    assert qr.run_next() == tid and db.get_task(tid)["agent"] == "gemini"
    [f] = db.failovers(tid)
    assert f["deadline_driven"] == 1
    assert qr.run_next() == tid and db.task_state(tid) == TaskState.DONE
    needs = _needs_you(new.folder)
    assert "Deadline-driven failover" in needs and "claude to gemini" in needs


def test_resume_near_deadline_goes_to_codex_when_gemini_is_out(cfg, db, fake_clis):
    clock = Clock()
    usage.record_limit_hit(cfg, db, "gemini", None, at=clock(), source="probe")
    new = _new(cfg, db, "resume", "Tailor", simulate_rate_limit_after=2,
               deadline=iso(clock() + timedelta(hours=10)))
    tid = new.spec.id
    qr = _queue(cfg, db, clock)
    assert qr.run_next() == tid and db.get_task(tid)["agent"] == "codex"
    assert qr.run_next() == tid and db.task_state(tid) == TaskState.DONE
    assert "claude to codex" in _needs_you(new.folder)


@pytest.mark.parametrize("deadline_h", [None, 5 * 24])
def test_resume_without_a_near_deadline_waits_for_claude(cfg, db, fake_clis, deadline_h):
    clock = Clock()
    deadline = iso(clock() + timedelta(hours=deadline_h)) if deadline_h else None
    new = _new(cfg, db, "resume", "Tailor", simulate_rate_limit_after=2, deadline=deadline)
    tid = new.spec.id
    qr = _queue(cfg, db, clock)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["agent"] == "claude"
    assert not db.failovers(tid)


def test_comedy_always_waits_for_claude(cfg, db, fake_clis):
    clock = Clock()
    fake_clis.setenv("FAKE_LIMIT_AT", "4")
    fake_clis.setenv("FAKE_RESETS_AT", str(int((clock() + timedelta(hours=3)).timestamp())))
    new = _new(cfg, db, "comedy", "Punch up the cold open",
               deadline=iso(clock() + timedelta(hours=2)))
    tid = new.spec.id
    qr = _queue(cfg, db, clock)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["agent"] == "claude"
    assert qr.run_next() is None  # Claude is out; Gemini and Codex are free but not used
    assert not db.failovers(tid) and not _gemini_runs(new.folder)


# --------------------------------------------------------------------------
# Retry with switch (§2.7)
# --------------------------------------------------------------------------


def test_second_retry_switches_to_gemini_first_never(cfg, db, fake_clis):
    clock = Clock()
    fake_clis.setenv("FAKE_EXIT", "3")  # every Claude Attempt fails
    new = _new(cfg, db, "research")
    tid = new.spec.id
    qr = _queue(cfg, db, clock)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "retry_wait" and row["agent"] == "claude"  # retry 1: same agent
    clock.advance(minutes=10)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "retry_wait" and row["agent"] == "gemini"  # retry 2: switched
    [f] = db.failovers(tid)
    assert (f["from_agent"], f["to_agent"], f["trigger"]) == ("claude", "gemini", "retry")
    clock.advance(minutes=30)
    assert qr.run_next() == tid and db.task_state(tid) == TaskState.DONE
    assert [a["agent"] for a in db.attempts(tid)] == ["claude", "claude", "gemini"]
    [run] = _gemini_runs(new.folder)
    assert "--resume" not in run["argv"] and "progress.md" in run["stdin"]


@pytest.mark.parametrize("task_type", ["comedy", "resume"])  # resume: no near deadline
def test_types_that_wait_for_claude_never_switch_on_retry(cfg, db, fake_clis, task_type):
    clock = Clock()
    fake_clis.setenv("FAKE_EXIT", "3")
    new = _new(cfg, db, task_type, "x")
    qr = _queue(cfg, db, clock)
    for wait in (10, 30, 0):
        qr.run_next()
        clock.advance(minutes=wait)
    assert db.task_state(new.spec.id) == TaskState.FAILED
    assert {a["agent"] for a in db.attempts(new.spec.id)} == {"claude"}
