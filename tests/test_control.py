"""The shared Task-control layer behind orch, Telegram and the dashboard."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nightshift import cleanweek, control, daemon, pauses, runner
from nightshift.db import Database, StateChanged
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import now

from test_planning import plan_provider, planner  # noqa: F401  (pytest fixture)


@pytest.fixture
def at(cfg):
    return now(cfg.timezone)


def _running(db, tid):
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    return run


def _other(cfg):
    """Another connection, as the daemon has."""
    return Database(cfg.db_path, tz=cfg.timezone)


def _hands(cfg, db, at):
    week = cleanweek.check(cfg, db, at + timedelta(minutes=5))
    return [(i.command, i.task_id) for i in week.interventions]


# -- refusals --------------------------------------------------------------

def test_every_control_refuses_an_unknown_task(cfg, db, at):
    for res in (
        control.approve(cfg, db, "nope", via="telegram"),
        control.deny(cfg, db, "nope", via="telegram"),
        control.retry(cfg, db, "nope", via="telegram", at=at),
        control.cancel(cfg, db, "nope", via="telegram", at=at),
        control.wait_for_cancel(cfg, db, "nope", timeout=0),
        control.pause(cfg, db, "nope", via="telegram", at=at),
        control.wait_for_pause(cfg, db, "nope", timeout=0),
        control.resume(cfg, db, "nope", via="telegram", at=at),
        control.set_priority(cfg, db, "nope", "urgent", via="telegram", at=at),
        control.plan(cfg, db, "nope"),
    ):
        assert not res.ok and res.refusal == control.UNKNOWN_TASK
        assert res.action == control.REFUSED and res.message == "no Task 'nope'"


def test_wrong_state_refusals_change_nothing(cfg, db, at):
    tid = create_task(cfg, db, "x").spec.id  # queued
    for res in (
        control.approve(cfg, db, tid, via="dashboard"),
        control.deny(cfg, db, tid, via="dashboard"),
        control.retry(cfg, db, tid, via="dashboard", at=at),
        control.resume(cfg, db, tid, via="dashboard", at=at),
        control.plan(cfg, db, tid),
    ):
        assert res.refusal == control.WRONG_STATE, res
        assert res.from_state == TaskState.QUEUED and res.task_id == tid
    assert control.approve(cfg, db, tid, via="x").message == (
        f"Task {tid} is queued, not awaiting approval")
    db.transition(tid, TaskState.CANCELLED, "t")
    for res in (control.cancel(cfg, db, tid, via="telegram", at=at),
                control.pause(cfg, db, tid, via="telegram", at=at),
                control.set_priority(cfg, db, tid, "urgent", via="telegram", at=at)):
        assert res.refusal == control.WRONG_STATE, res
    assert db.task_state(tid) == TaskState.CANCELLED
    assert db.interventions() == []
    bad = control.set_priority(cfg, db, create_task(cfg, db, "y").spec.id, "asap",
                               via="telegram", at=at)
    assert bad.refusal == control.INVALID


def test_a_race_with_the_daemon_is_a_state_changed_refusal(cfg, db, at, monkeypatch):
    # retry: someone else requeued the failed Task between lookup and change.
    tid = create_task(cfg, db, "x").spec.id
    db.transition(tid, TaskState.FAILED, "t")
    real = db.transition

    def racing(task_id, to_state, reason, **kw):
        other = _other(cfg)
        try:
            other.transition(task_id, TaskState.QUEUED, "requeued elsewhere")
        finally:
            other.close()
        return real(task_id, to_state, reason, **kw)

    monkeypatch.setattr(db, "transition", racing)
    res = control.retry(cfg, db, tid, via="telegram", at=at)
    assert res.refusal == control.STATE_CHANGED and res.from_state == TaskState.FAILED

    # pause: the Task keeps moving every time it is looked at.
    q = create_task(cfg, db, "y").spec.id

    def always(*a, **kw):
        raise StateChanged("moved")

    monkeypatch.setattr(db, "transition", always)
    res = control.pause(cfg, db, q, via="telegram", at=at)
    assert res.refusal == control.STATE_CHANGED and "kept changing state" in res.message
    monkeypatch.undo()

    # approve: decided from another surface first.
    a = create_task(cfg, db, "z", needs_approval=True).spec.id
    real_decide = db.decide_approval

    def decided_first(*args, **kw):
        other = _other(cfg)
        try:
            other.decide_approval(a, approved=False, via="dashboard",
                                  to_state=TaskState.CANCELLED, reason="plan denied (dashboard)", plan_sha256=None)
        finally:
            other.close()
        return real_decide(*args, **kw)

    monkeypatch.setattr(db, "decide_approval", decided_first)
    res = control.approve(cfg, db, a, via="telegram")
    assert res.refusal == control.STATE_CHANGED
    assert [r["via"] for r in db.approvals(a)] == ["dashboard"]


# -- the via surface ---------------------------------------------------------

def test_approvals_and_interventions_record_the_surface(cfg, db, at):
    a = create_task(cfg, db, "a", needs_approval=True).spec.id
    d = create_task(cfg, db, "d", needs_approval=True).spec.id
    res = control.approve(cfg, db, a, via="telegram")
    assert res.ok and res.action == control.APPROVED
    assert (res.from_state, res.to_state) == (TaskState.AWAITING_APPROVAL, TaskState.QUEUED)
    assert "daemon isn't running" in res.note  # no daemon in tests
    res = control.deny(cfg, db, d, via="dashboard")
    assert res.action == control.DENIED and res.to_state == TaskState.CANCELLED
    assert db.approvals(a)[-1]["via"] == "telegram"
    assert db.approvals(d)[-1]["via"] == "dashboard"

    p = create_task(cfg, db, "p").spec.id
    res = control.set_priority(cfg, db, p, "urgent", via="telegram", at=at)
    assert res.action == control.PRIORITY_SET and res.message == f"Task {p} priority normal -> urgent"
    assert db.get_task(p)["priority"] == "urgent"
    [i] = db.interventions()
    assert i["command"] == "telegram" and i["detail"] == "priority normal -> urgent"

    res = control.pause(cfg, db, p, via="dashboard", at=at)
    assert res.action == control.PAUSED and res.to_state == TaskState.PAUSED_USAGE
    assert "(dashboard)" in db.transitions(p)[-1]["reason"]
    res = control.resume(cfg, db, p, via="telegram", at=at)
    assert res.action == control.RESUMED and res.to_state == TaskState.QUEUED and res.note
    assert "resumed by hand (telegram)" in db.transitions(p)[-1]["reason"]

    f = create_task(cfg, db, "f").spec.id
    db.transition(f, TaskState.FAILED, "t")
    res = control.retry(cfg, db, f, via="dashboard", at=at)
    assert res.action == control.RETRIED and res.to_state == TaskState.QUEUED
    c = create_task(cfg, db, "c").spec.id
    res = control.cancel(cfg, db, c, via="telegram", at=at)
    assert res.action == control.CANCELLED and res.message == f"Task {c} cancelled (was queued)."

    # Gate 2 sees every one of them, whatever the surface.
    hands = _hands(cfg, db, at)
    for expected in [("telegram", a), ("dashboard", d), ("telegram", p), ("dashboard", p),
                     ("telegram", p), ("dashboard", f), ("telegram", c)]:
        assert expected in hands, (expected, hands)


# -- cancel and pause of a running Task: request, then a bounded wait ----------

def test_cancel_running_is_a_request_plus_a_bounded_wait(cfg, db, at, monkeypatch):
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    tid = create_task(cfg, db, "x").spec.id
    _running(db, tid)
    res = control.cancel(cfg, db, tid, via="telegram", at=at)
    assert res.ok and res.action == control.CANCEL_REQUESTED
    assert db.task_state(tid) == TaskState.RUNNING
    assert db.request_via(tid, "cancel") == "telegram"
    res = control.wait_for_cancel(cfg, db, tid, timeout=0)
    assert res.action == control.CANCEL_REQUESTED and "hasn't stopped it yet" in res.message
    # The daemon died before it could stop the Attempt: settled as cancelled,
    # in the surface's name.
    assert runner.fail_orphaned(cfg, db) == [tid]
    res = control.wait_for_cancel(cfg, db, tid, timeout=5)
    assert res.action == control.CANCELLED and res.to_state == TaskState.CANCELLED
    assert "cancelled by hand (telegram)" in db.transitions(tid)[-1]["reason"]
    assert ("telegram", tid) in _hands(cfg, db, at)


def test_pause_running_is_a_request_plus_a_bounded_wait(cfg, db, at, monkeypatch):
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    tid = create_task(cfg, db, "x").spec.id
    run = _running(db, tid)
    res = control.pause(cfg, db, tid, via="dashboard", at=at)
    assert res.action == control.PAUSE_REQUESTED and res.to_state == TaskState.RUNNING
    assert db.request_via(tid, "pause") == "dashboard"
    assert control.wait_for_pause(cfg, db, tid, timeout=0).action == control.PAUSE_REQUESTED
    # Withdrawn from another surface, then asked again.
    res = control.resume(cfg, db, tid, via="telegram", at=at)
    assert res.action == control.PAUSE_WITHDRAWN and res.note is None
    assert db.get_task(tid)["pause_requested_at"] is None
    control.pause(cfg, db, tid, via="telegram", at=at)
    assert db.request_via(tid, "pause") == "telegram"
    # The daemon pauses it after the Step.
    pauses.pause_after_attempt(cfg, db, tid, kind=pauses.KIND_BY_HAND, limit=None,
                               reason="Attempt 1 paused by hand (telegram)", at=at, run_id=run)
    res = control.wait_for_pause(cfg, db, tid, timeout=5)
    assert res.action == control.PAUSED and res.to_state == TaskState.PAUSED_USAGE

    other = create_task(cfg, db, "y").spec.id
    _running(db, other)
    control.pause(cfg, db, other, via="telegram", at=at)
    db.transition(other, TaskState.DONE, "finished first")
    res = control.wait_for_pause(cfg, db, other, timeout=0)
    assert res.action == control.ENDED and res.to_state == TaskState.DONE


# -- intake, step by step ------------------------------------------------------

def test_intake_step_by_step_without_a_terminal(cfg, db, at, planner):  # noqa: F811
    planner.setenv("FAKE_MINUTES", "45")
    heard = []
    res = control.propose(cfg, "Write a long research report", at=at,
                          adapter_provider=plan_provider, progress=heard.append)
    assert res.ok and res.action == control.PROPOSED and heard
    draft = res.draft
    assert draft.estimated_minutes == 45 and draft.task_type == "general" and draft.needs_plan
    assert "Estimate:  45 minutes" in res.message
    assert db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    new = control.create(cfg, db, draft, via="telegram")
    assert new.ok and new.action == control.CREATED and new.to_state == TaskState.AWAITING_APPROVAL
    tid = new.task_id
    assert db.task_state(tid) == TaskState.AWAITING_APPROVAL
    assert "created by telegram" in db.transitions(tid)[0]["reason"]

    planned = control.plan(cfg, db, tid, adapter_provider=plan_provider)
    assert planned.action == control.PLANNED and planned.plan.startswith("# Plan")
    assert (new.folder / "plan.md").exists()

    res = control.approve(cfg, db, tid, via="telegram")
    assert res.ok and db.task_state(tid) == TaskState.QUEUED
    assert db.approvals(tid)[-1]["via"] == "telegram"
    assert [c["kind"] for c in planner.calls()] == ["estimate", "plan"]


def test_intake_decline_and_short_tasks(cfg, db, at, planner):  # noqa: F811
    draft = control.propose(cfg, "Fix a typo", at=at, task_type="coding",
                            adapter_provider=plan_provider).draft
    assert draft.task_type == "coding" and not draft.needs_plan
    assert control.decline(db, draft).action == control.DECLINED
    assert db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM planning_usage").fetchone()[0] == 1
    new = control.create(cfg, db, draft, via="dashboard")
    assert new.to_state == TaskState.QUEUED and db.task_state(new.task_id) == TaskState.QUEUED
    assert new.note and new.message.startswith(f"Task {new.task_id} queued: ")


def test_intake_refuses_bad_input_before_any_planning_call(cfg, at, planner):  # noqa: F811
    res = control.propose(cfg, "x", at=at, deadline="not a date", adapter_provider=plan_provider)
    assert res.refusal == control.INVALID and res.message.endswith("Nothing was queued.")
    res = control.propose(cfg, "x", at=at, template="nope", adapter_provider=plan_provider)
    assert res.refusal == control.INVALID
    res = control.propose(cfg, "x", at=at, agent="nobody", adapter_provider=plan_provider)
    assert res.refusal == control.INVALID
    assert planner.calls() == []


def test_a_failed_plan_call_is_a_refusal_and_the_task_waits(cfg, db, at, planner):  # noqa: F811
    planner.setenv("FAKE_MINUTES", "60")
    draft = control.propose(cfg, "Big job", at=at, adapter_provider=plan_provider).draft
    tid = control.create(cfg, db, draft, via="telegram").task_id
    planner.setenv("FAKE_PLAN_FAIL", "plan")
    res = control.plan(cfg, db, tid, adapter_provider=plan_provider)
    assert res.refusal == control.NO_PLAN and res.message.startswith("Could not write a plan")
    assert db.task_state(tid) == TaskState.AWAITING_APPROVAL


# -- which controls a state allows ---------------------------------------------

def test_available_controls_follow_the_controls_own_rules(cfg, db, at, monkeypatch):
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    C, P, R, T, PR = (control.CANCEL, control.PAUSE, control.RESUME, control.RETRY,
                      control.PRIORITY)

    def avail(tid):
        return set(control.available(db.get_task(tid)))

    q = create_task(cfg, db, "queued").spec.id
    assert avail(q) == {C, P, PR}
    control.pause(cfg, db, q, via="dashboard", at=at)
    assert avail(q) == {C, R, PR}  # paused by hand: no second pause
    usage = create_task(cfg, db, "usage").spec.id
    db.transition(usage, TaskState.PAUSED_USAGE, "limit", set_fields={"pause_kind": "usage"})
    assert avail(usage) == {C, P, R, PR}
    spend = create_task(cfg, db, "spend").spec.id
    db.transition(spend, TaskState.PAUSED_SPEND, "cap")
    assert avail(spend) == {C, PR}  # resumes on its own once the cap lifts
    approval = create_task(cfg, db, "approval", needs_approval=True).spec.id
    assert avail(approval) == {C, PR}
    run = create_task(cfg, db, "running").spec.id
    _running(db, run)
    assert avail(run) == {C, P, PR}
    control.pause(cfg, db, run, via="dashboard", at=at)
    assert avail(run) == {C, R, PR}  # resume withdraws the pending pause
    control.cancel(cfg, db, run, via="dashboard", at=at)
    assert avail(run) == {R, PR}
    wait = create_task(cfg, db, "retry wait").spec.id
    _running(db, wait)
    db.transition(wait, TaskState.RETRY_WAIT, "crashed")
    assert avail(wait) == {C, P, T, PR}
    for state in (TaskState.FAILED, TaskState.CANCELLED):
        tid = create_task(cfg, db, str(state)).spec.id
        db.transition(tid, state, "t")
        assert avail(tid) == ({T} if state == TaskState.FAILED else set())



# -- which Tasks approve and deny apply to; a stale decision -----------------

def test_decidable_is_a_plan_or_an_agents_request(cfg, db):
    plan = create_task(cfg, db, "plan", needs_approval=True).spec.id
    assert control.decidable(db, db.get_task(plan))
    capped = create_task(cfg, db, "capped").spec.id
    _running(db, capped)
    db.transition(capped, TaskState.NEEDS_YOU, "per-Task spend cap")  # no request to decide
    assert not control.decidable(db, db.get_task(capped))
    assert not control.decidable(db, db.get_task(create_task(cfg, db, "q").spec.id))


def test_a_decision_with_a_stale_request_key_only_reports(cfg, db):
    tid = create_task(cfg, db, "plan", needs_approval=True).spec.id
    seen = control.request_key(db, tid)
    assert control.approve(cfg, db, tid, via="telegram", seen=seen).ok
    assert control.request_key(db, tid) != seen
    res = control.deny(cfg, db, tid, via="dashboard", seen=seen)
    assert res.refusal == control.STATE_CHANGED
    assert res.message.endswith(f"Task {tid} is queued (plan approved via telegram).")
    assert db.task_state(tid) == TaskState.QUEUED and len(db.approvals(tid)) == 1
