"""Structured data behind `orch list|show|usage` (`orch schedule list`: test_schedules.py)."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from nightshift import queries, usage
from nightshift.adapters.base import RateLimit
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import iso

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 21, 22, 0, tzinfo=LA)


# --------------------------------------------------------------------------
# Task list
# --------------------------------------------------------------------------


def test_task_list_gives_effective_priority_for_open_tasks_only(cfg, db):
    soon = iso(NOW + timedelta(hours=3))
    due = create_task(cfg, db, "due soon", priority="background", deadline=soon).spec.id
    gone = create_task(cfg, db, "cancelled", priority="background", deadline=soon).spec.id
    db.transition(gone, TaskState.CANCELLED, "test")

    tasks = {t.id: t for t in queries.task_list(cfg, db, NOW)}

    assert tasks[due].priority == "background" and tasks[due].effective_priority == "urgent"
    assert tasks[due].state == TaskState.QUEUED and tasks[due].title == "due soon"
    assert tasks[gone].effective_priority is None  # finished: not evaluated


def test_task_list_filters_by_state(cfg, db):
    a = create_task(cfg, db, "alpha").spec.id
    b = create_task(cfg, db, "beta").spec.id
    db.transition(b, TaskState.CANCELLED, "test")
    assert [t.id for t in queries.task_list(cfg, db, NOW, [TaskState.QUEUED])] == [a]
    assert queries.task_list(cfg, db, NOW, [TaskState.RUNNING]) == []


# --------------------------------------------------------------------------
# Task detail
# --------------------------------------------------------------------------


def test_task_detail_has_settings_attempts_failovers_and_history(cfg, db):
    tid = create_task(cfg, db, "Write a haiku", agent="claude").spec.id
    db.transition(tid, TaskState.RUNNING, "picked")
    first = db.start_attempt(tid, "claude", ["claude"], "logs/a1.jsonl")
    db.finish_attempt(first, exit_code=1, outcome="failed", end_reason="crashed",
                      session_id=None)
    db.record_failover(tid, run_id=first, at=iso(NOW), from_agent="claude", to_agent="codex",
                       trigger="usage_limit", deadline_driven=True, reason="out of usage")
    db.start_attempt(tid, "codex", ["codex"], "logs/a2.jsonl")

    d = queries.task_detail(cfg, db, tid, NOW)

    assert d.id == tid and d.state == TaskState.RUNNING
    assert d.task["prompt"] == "Write a haiku" and d.task["agent"] == "claude"
    assert d.effective_priority == "normal" and d.blocked is None
    assert d.max_retries == cfg.max_retries
    assert [(a.attempt_no, a.agent, a.exit_code, a.outcome, a.end_reason, a.running)
            for a in d.attempts] == [(1, "claude", 1, "failed", "crashed", False),
                                     (2, "codex", None, None, None, True)]
    [f] = d.failovers
    assert (f.from_agent, f.to_agent, f.trigger, f.deadline_driven) == (
        "claude", "codex", "usage_limit", True)
    assert [(t.from_state, t.to_state, t.reason) for t in d.transitions][-1] == (
        "queued", "running", "picked")
    assert d.transitions[0].from_state is None  # created
    assert d.approvals == []


def test_task_detail_says_why_a_queued_task_waits(cfg, db):
    later = iso(NOW + timedelta(hours=2))
    tid = create_task(cfg, db, "later", run_at=later).spec.id
    d = queries.task_detail(cfg, db, tid, NOW)
    assert d.blocked == f"waiting until {later} (--at)"


def test_task_detail_of_a_finished_task_is_not_evaluated(cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    db.transition(tid, TaskState.CANCELLED, "test")
    d = queries.task_detail(cfg, db, tid, NOW)
    assert d.effective_priority is None and d.blocked is None


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------


def test_usage_report_per_agent_and_window(cfg, db):
    db.record_planning_usage(None, "plan", "claude", [{"cost_usd": 0.5, "input_tokens": 100}],
                             at=iso(NOW - timedelta(hours=1)))
    reset = NOW + timedelta(hours=2)
    usage.record_limit_hit(cfg, db, "claude", RateLimit("five_hour", iso(reset)),
                           at=NOW - timedelta(minutes=30), source="attempt")
    db.record_probe("claude", at=iso(NOW), outcome="limited", resets_at=None, detail="still out")
    tid = create_task(cfg, db, "paused one", agent="claude").spec.id
    db.transition(tid, TaskState.RUNNING, "picked")
    db.transition(tid, TaskState.PAUSED_USAGE, "out", set_fields={
        "pause_kind": "usage_limit", "pause_reset_at": iso(reset), "resume_at": iso(reset)})

    report = {a.agent: a for a in queries.usage_report(cfg, db, NOW)}

    assert list(report) == ["claude", "codex", "gemini"]  # configured agents, in config order
    claude = report["claude"]
    five, week = claude.windows
    assert (five.window, week.window) == ("five_hour", "seven_day")
    assert five.resets_at == reset and five.start == reset - timedelta(hours=5)
    assert five.hours == 5
    assert five.used.cost_usd == 0.5 and five.used.input_tokens == 100
    assert five.out_of_usage and five.observation_count == 1
    assert five.baseline == usage.Baseline(cost_usd=0.5, observations=1)
    assert week.resets_at is None and week.start == NOW - timedelta(hours=168)
    assert not week.out_of_usage and week.baseline is None and week.observation_count == 0
    [obs] = claude.recent_observations
    assert (obs.window, obs.cost_usd, obs.source, obs.resets_at) == (
        "five_hour", 0.5, "attempt", iso(reset))
    assert (claude.last_probe.outcome, claude.last_probe.detail) == ("limited", "still out")
    [paused] = claude.paused
    assert (paused.task_id, paused.resume_at, paused.reset_known) == (tid, iso(reset), True)
    assert report["codex"].last_probe is None and report["codex"].paused == []


def test_usage_report_lists_an_unconfigured_agent_with_usage(cfg, db):
    db.record_planning_usage(None, "plan", "gemini", [{"cost_usd": 0.1}],
                             at=iso(NOW - timedelta(minutes=5)))
    assert [a.agent for a in queries.usage_report(cfg, db, NOW)] == ["claude", "codex", "gemini"]


def test_usage_report_keeps_only_the_last_five_limit_observations(cfg, db):
    for days in range(7, 0, -1):
        usage.record_limit_hit(cfg, db, "claude", RateLimit("five_hour", None),
                               at=NOW - timedelta(days=days), source="attempt")
    [claude] = [a for a in queries.usage_report(cfg, db, NOW) if a.agent == "claude"]
    assert [o.hit_at for o in claude.recent_observations] == [
        iso(NOW - timedelta(days=d)) for d in range(5, 0, -1)]
    assert claude.windows[0].observation_count == 7


def test_task_detail_blocked_is_only_for_queued_tasks(cfg, db):
    tid = create_task(cfg, db, "later", run_at=iso(NOW + timedelta(hours=2))).spec.id
    db.transition(tid, TaskState.RUNNING, "started by hand")
    assert queries.task_detail(cfg, db, tid, NOW).blocked is None
