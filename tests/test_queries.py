"""Structured data behind `orch list|show|usage` (`orch schedule list`: test_schedules.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nightshift import queries, spend, usage
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


def _task_in(cfg, db, title, *path):
    tid = create_task(cfg, db, title).spec.id
    for state in path:
        db.transition(tid, state, "test")
    return tid


def test_task_groups_put_every_state_in_one_of_six_groups(cfg, db):
    S = TaskState
    ids = {
        "running": _task_in(cfg, db, "running", S.RUNNING),
        "queued": _task_in(cfg, db, "queued"),
        "retry": _task_in(cfg, db, "retry", S.RUNNING, S.RETRY_WAIT),
        "scheduled": _task_in(cfg, db, "scheduled", S.SCHEDULED),
        "usage": _task_in(cfg, db, "usage", S.RUNNING, S.PAUSED_USAGE),
        "spend": _task_in(cfg, db, "spend", S.RUNNING, S.PAUSED_SPEND),
        "needs": _task_in(cfg, db, "needs", S.RUNNING, S.NEEDS_YOU),
        "approval": _task_in(cfg, db, "approval", S.AWAITING_APPROVAL),
        "done": _task_in(cfg, db, "done", S.RUNNING, S.DONE),
        "partial": _task_in(cfg, db, "partial", S.RUNNING, S.PARTIAL),
        "cancelled": _task_in(cfg, db, "cancelled", S.CANCELLED),
        "failed": _task_in(cfg, db, "failed", S.FAILED),
    }

    groups = queries.task_groups(cfg, db, NOW)

    assert [g.name for g in groups] == [
        "running", "queued", "paused", "needs you", "done", "failed"]
    titles = {g.name: sorted(t.title for t in g.tasks) for g in groups}
    assert titles == {
        "running": ["running"],
        "queued": ["queued", "retry", "scheduled"],
        "paused": ["spend", "usage"],
        "needs you": ["approval", "needs"],
        "done": ["cancelled", "done", "partial"],
        "failed": ["failed"],
    }
    assert all(g.total == len(g.tasks) for g in groups)
    assert {t.id for g in groups for t in g.tasks} == set(ids.values())


def test_task_groups_show_only_the_newest_finished_tasks(cfg, db):
    for n in range(4):
        _task_in(cfg, db, f"done {n}", TaskState.RUNNING, TaskState.DONE)
    _task_in(cfg, db, "still queued")
    for n in range(3):
        _task_in(cfg, db, f"open {n}")

    groups = {g.name: g for g in queries.task_groups(cfg, db, NOW, finished_limit=2)}

    assert [t.title for t in groups["done"].tasks] == ["done 3", "done 2"]
    assert groups["done"].total == 4
    assert len(groups["queued"].tasks) == groups["queued"].total == 4  # open groups: all


# --------------------------------------------------------------------------
# "Needs you" inbox
# --------------------------------------------------------------------------


def _folder(db, tid):
    return Path(db.get_task(tid)["folder"])


def test_inbox_shows_the_agents_request_from_needs_you_json(cfg, db):
    tid = _task_in(cfg, db, "Tailor resume", TaskState.RUNNING)
    (_folder(db, tid) / "needs_you.json").write_text(json.dumps({
        "kind": "approval", "summary": "Push to GitHub?", "details": "Repo acme/site",
        "proposed_action": "push the branch"}))
    db.transition(tid, TaskState.NEEDS_YOU, "agent wrote needs_you.json and stopped")

    [r] = queries.needs_you_inbox(cfg, db)

    assert (r.task_id, r.title, r.state) == (tid, "Tailor resume", TaskState.NEEDS_YOU)
    assert (r.kind, r.summary, r.details, r.proposed_action) == (
        "approval", "Push to GitHub?", "Repo acme/site", "push the branch")
    assert r.since == db.get_task(tid)["state_changed_at"]


def test_inbox_survives_a_broken_needs_you_json(cfg, db):
    tid = _task_in(cfg, db, "broken", TaskState.RUNNING)
    (_folder(db, tid) / "needs_you.json").write_text("{not json")
    db.transition(tid, TaskState.NEEDS_YOU, "agent wrote needs_you.json and stopped")
    [r] = queries.needs_you_inbox(cfg, db)
    assert r.kind == "request" and "not a JSON object" in r.summary


def test_inbox_explains_a_per_task_spend_cap_stop(cfg, db):
    tid = _task_in(cfg, db, "paid", TaskState.RUNNING)
    run = db.start_attempt(tid, "gemini-api", ["x"], "log")
    db.finish_attempt(run, exit_code=0, outcome=spend.OUTCOME_CAP, end_reason="cap",
                      session_id=None)
    db.transition(tid, TaskState.NEEDS_YOU, "spend cap")
    [r] = queries.needs_you_inbox(cfg, db)
    assert r.kind == "spend cap" and "per-Task spend cap" in r.summary


def test_inbox_falls_back_to_the_transition_reason(cfg, db):
    tid = _task_in(cfg, db, "odd", TaskState.RUNNING)
    db.transition(tid, TaskState.NEEDS_YOU, "stuck twice")
    [r] = queries.needs_you_inbox(cfg, db)
    assert (r.kind, r.summary) == ("needs you", "stuck twice")


def test_inbox_includes_plan_approvals_and_re_approvals(cfg, db):
    plan = create_task(cfg, db, "Big plan", needs_approval=True, estimated_minutes=90).spec.id
    again = _task_in(cfg, db, "Paused long", TaskState.PAUSED_USAGE)
    db.transition(again, TaskState.AWAITING_APPROVAL,
                  f"paused for more than 72 hours; needs re-approval (`orch approve {again}`)")

    reqs = {r.task_id: r for r in queries.needs_you_inbox(cfg, db)}

    assert reqs[plan].kind == "plan approval" and "90 min" in reqs[plan].summary
    assert reqs[plan].proposed_action == f"orch approve {plan} (or orch deny {plan})"
    assert reqs[again].kind == "re-approval" and "re-approval" in reqs[again].summary


def test_inbox_includes_the_monthly_spend_cap_but_not_the_daily_one(cfg, db):
    month = _task_in(cfg, db, "month", TaskState.RUNNING)
    db.transition(month, TaskState.PAUSED_SPEND, f"paid calls reached the $50.00 "
                  f"{spend.MONTHLY_CAP}", set_fields={"pause_kind": spend.KIND_MONTH})
    day = _task_in(cfg, db, "day", TaskState.RUNNING)
    db.transition(day, TaskState.PAUSED_SPEND, "daily cap",
                  set_fields={"pause_kind": spend.KIND_DAY})

    [r] = queries.needs_you_inbox(cfg, db)

    assert r.task_id == month and r.kind == "spend cap" and spend.MONTHLY_CAP in r.summary


def test_inbox_is_empty_when_nothing_waits_on_you(cfg, db):
    _task_in(cfg, db, "queued")
    _task_in(cfg, db, "running", TaskState.RUNNING)
    _task_in(cfg, db, "done", TaskState.RUNNING, TaskState.DONE)
    assert queries.needs_you_inbox(cfg, db) == []


def test_inbox_lists_the_longest_waiting_first(cfg, db):
    first = _task_in(cfg, db, "first", TaskState.RUNNING, TaskState.NEEDS_YOU)
    second = create_task(cfg, db, "second", needs_approval=True).spec.id
    db.conn.execute("UPDATE tasks SET state_changed_at = ? WHERE id = ?",
                    (iso(NOW - timedelta(hours=5)), second))
    db.conn.commit()
    assert [r.task_id for r in queries.needs_you_inbox(cfg, db)] == [second, first]


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


# --------------------------------------------------------------------------
# Spend tracker
# --------------------------------------------------------------------------


def _paid_spend(db, task_id, usd, at):
    db.record_spend(task_id, run_id=None, agent="gemini-api", model="m", at=at,
                    input_tokens=0, cached_tokens=0, output_tokens=0, thoughts_tokens=0,
                    cost_usd=usd, raw=None)


def test_spend_tracker_lists_tasks_that_spent_this_month_or_are_still_open(cfg, db):
    open_old = create_task(cfg, db, "paused since September", agent="gemini-api").spec.id
    _paid_spend(db, open_old, 0.5, "2026-09-29T10:00:00-07:00")
    done_old = create_task(cfg, db, "done in September", agent="gemini-api").spec.id
    _paid_spend(db, done_old, 0.7, "2026-09-29T11:00:00-07:00")
    db.transition(done_old, TaskState.CANCELLED, "test")
    recent = create_task(cfg, db, "this month", agent="gemini-api").spec.id
    _paid_spend(db, recent, 0.25, "2026-10-21T20:00:00-07:00")
    # Written with another offset: still the latest spend (21:30 local).
    _paid_spend(db, recent, 0.75, "2026-10-22T04:30:00+00:00")

    t = queries.spend_tracker(cfg, db, NOW)

    assert [x.task_id for x in t.tasks] == [recent, open_old]
    assert t.tasks[0].spent == 1.0 and t.tasks[0].percent == 50.0 and not t.tasks[0].at_cap
    assert t.per_task_cap == cfg.per_task_spend_cap
    assert t.credits.spent_today == 1.0 and t.credits.spent_this_month == 1.0
