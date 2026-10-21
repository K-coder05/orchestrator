from __future__ import annotations

import pytest

from nightshift.db import MIGRATIONS, Database
from nightshift.states import ALLOWED, TERMINAL, InvalidTransition, TaskState
from nightshift.timeutil import parse_iso


def _task(db, task_id="ab12"):
    db.insert_task(
        {"id": task_id, "title": "t", "slug": "t", "prompt": "p", "task_type": "general",
         "agent": "claude", "priority": "normal", "internet": "off",
         "created_at": "2026-10-20T00:00:00-07:00", "folder": "/tmp/x"},
        TaskState.QUEUED, "created",
    )
    return task_id


def test_migrations_are_idempotent(tmp_path):
    p = tmp_path / "n.db"
    Database(p).close()
    d = Database(p)
    assert d.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    tables = {r[0] for r in d.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "runs", "transitions", "usage_events"} <= tables


def test_every_transition_recorded_with_timestamp_and_reason(db):
    tid = _task(db)
    run = db.start_attempt(tid, "claude", ["claude", "-p"], "/tmp/log")
    db.transition(tid, TaskState.RUNNING, "Attempt 1 started", run_id=run)
    db.transition(tid, TaskState.DONE, "agent finished", run_id=run)
    rows = db.transitions(tid)
    assert [(r["from_state"], r["to_state"]) for r in rows] == [
        (None, "queued"), ("queued", "running"), ("running", "done")
    ]
    for r in rows:
        assert r["reason"]
        parse_iso(r["at"])  # valid timestamp with offset
    assert rows[2]["run_id"] == run
    assert db.task_state(tid) == TaskState.DONE


def test_invalid_transition_rejected_and_not_recorded(db):
    tid = _task(db)
    with pytest.raises(InvalidTransition):
        db.transition(tid, TaskState.DONE, "skipping running")
    assert len(db.transitions(tid)) == 1
    assert db.task_state(tid) == TaskState.QUEUED


def test_transition_requires_reason(db):
    tid = _task(db)
    with pytest.raises(ValueError):
        db.transition(tid, TaskState.RUNNING, "")


def test_terminal_states():
    assert ALLOWED[TaskState.DONE] == frozenset()
    assert ALLOWED[TaskState.CANCELLED] == frozenset()
    assert TERMINAL == {TaskState.DONE, TaskState.PARTIAL, TaskState.FAILED, TaskState.CANCELLED}
    assert set(ALLOWED) == set(TaskState)


def test_attempts_numbered_per_task(db):
    tid = _task(db)
    r1 = db.start_attempt(tid, "claude", [], "l1")
    db.finish_attempt(r1, exit_code=1, outcome="failed", end_reason="x", session_id="s1")
    r2 = db.start_attempt(tid, "claude", [], "l2")
    rows = db.attempts(tid)
    assert [r["attempt_no"] for r in rows] == [1, 2]
    assert rows[0]["session_id"] == "s1" and rows[1]["id"] == r2
