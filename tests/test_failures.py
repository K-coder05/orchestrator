"""Runtime caps, the retry budget, retries and cancel (§2.7)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, daemon, failures, runner
from nightshift.config import ConfigError, load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.runner import AttemptControl, create_task, run_attempt
from nightshift.states import TaskState

from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")


class Clock:
    """A fake clock the test moves by hand."""

    def __init__(self, start: datetime):
        self.t = start

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _report(new) -> str:
    return (new.folder / "report.md").read_text()


# --------------------------------------------------------------------------
# Runtime caps
# --------------------------------------------------------------------------


def test_runtime_cap_defaults(cfg, db):
    assert create_task(cfg, db, "x").spec.max_runtime_minutes == 120
    coding = create_task(cfg, db, "y", task_type="coding")
    assert coding.spec.max_runtime_minutes == 360  # overnight coding: 6 hours
    tpl = create_task(cfg, db, "z", task_type="coding", max_runtime_minutes=45)
    assert tpl.spec.max_runtime_minutes == 45  # a template's own cap wins
    assert db.get_task(tpl.spec.id)["max_runtime_minutes"] == 45
    assert failures.runtime_cap_seconds(cfg, db.get_task(coding.spec.id)) == 360 * 60
    with pytest.raises(ValueError):
        create_task(cfg, db, "bad", max_runtime_minutes=0)


@pytest.mark.parametrize(
    "override",
    [
        {"defaults": {"max_runtime_minutes": 0}},
        {"task_types": {"coding": {"max_runtime_minutes": "6h"}}},
        {"retries": {"max_retries": -1}},
        {"retries": {"wait_minutes": []}},
        {"retries": {"wait_minutes": ["ten"]}},
    ],
)
def test_bad_failure_config_is_rejected(tmp_path, override):
    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump(override))
    with pytest.raises(ConfigError):
        load_config(d, host=FakeHost())


def test_runtime_cap_ends_attempt_as_failed_attempt(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_SLEEP", "30")
    clock = Clock(datetime(2026, 10, 21, 1, 0, tzinfo=LA))
    new = create_task(cfg, db, "slow")
    start = time.monotonic()
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider, clock=clock,
                      max_runtime_seconds=0.5)
    assert time.monotonic() - start < 10
    assert res.state == TaskState.RETRY_WAIT
    [a] = db.attempts(new.spec.id)
    assert a["outcome"] == "failed" and "runtime cap" in a["end_reason"]
    row = db.get_task(new.spec.id)
    assert row["retries_used"] == 1
    assert row["retry_at"] == "2026-10-21T01:10:00-07:00"  # 10 minutes later
    assert "runtime cap" in db.transitions(new.spec.id)[-1]["reason"]


def test_agent_ignoring_sigterm_is_killed(cfg, db, env, monkeypatch):  # noqa: F811
    env.setenv("FAKE_SLEEP", "60")
    env.setenv("FAKE_IGNORE_TERM", "1")
    monkeypatch.setattr(runner, "KILL_GRACE_SECONDS", 0.5)
    new = create_task(cfg, db, "stubborn")
    start = time.monotonic()
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider,
                      max_runtime_seconds=0.3)
    assert time.monotonic() - start < 15
    assert res.state == TaskState.RETRY_WAIT and res.exit_code == -9


# --------------------------------------------------------------------------
# What counts as a Failed Attempt
# --------------------------------------------------------------------------


def test_failed_post_hook_is_a_failed_attempt(cfg, db, env):  # noqa: F811
    def merge_rows(folder, row):
        raise failures.PostHookFailed("output/proposed_rows.csv is missing")

    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider,
                      post_hooks=[merge_rows])
    assert res.state == TaskState.RETRY_WAIT
    [a] = db.attempts(new.spec.id)
    assert a["outcome"] == "failed"
    assert "post-hook merge_rows failed: output/proposed_rows.csv is missing" in a["end_reason"]


def test_passing_post_hooks_run_in_order(cfg, db, env):  # noqa: F811
    seen = []
    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider,
                      post_hooks=[lambda f, r: seen.append(1), lambda f, r: seen.append(2)])
    assert res.state == TaskState.DONE and seen == [1, 2]


def test_agent_reported_failure_is_not_retried(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_RESULT", "failed")
    hooks = []
    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider,
                      post_hooks=[lambda f, r: hooks.append(1)])
    # The agent finished and judged its own work failed: no crash, no retry.
    assert res.state == TaskState.FAILED and hooks == []
    assert db.get_task(new.spec.id)["retries_used"] == 0


def test_crash_is_a_failed_attempt(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_EXIT", "3")
    env.setenv("FAKE_RESULT", "none")
    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    assert res.state == TaskState.RETRY_WAIT
    assert "crashed" in db.attempts(new.spec.id)[0]["end_reason"]
    assert "code 3" in db.transitions(new.spec.id)[-1]["reason"]


# --------------------------------------------------------------------------
# The retry schedule, with a fake clock
# --------------------------------------------------------------------------


def test_retries_wait_10_then_30_minutes_then_fail(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_EXIT", "1")
    env.setenv("FAKE_RESULT", "none")
    clock = Clock(datetime(2026, 10, 21, 2, 0, tzinfo=LA))
    host = FakeHost()
    new = create_task(cfg, db, "always crashes")
    tid = new.spec.id
    qr = QueueRunner(cfg, db, host, adapter_provider=fake_provider, clock=clock)

    assert qr.run_next() == tid  # Attempt 1 crashes
    row = db.get_task(tid)
    assert row["state"] == "retry_wait" and row["retries_used"] == 1
    assert row["retry_at"] == "2026-10-21T02:10:00-07:00"

    clock.advance(minutes=9, seconds=59)
    assert qr.run_next() is None and db.task_state(tid) == TaskState.RETRY_WAIT
    clock.advance(seconds=1)
    assert qr.run_next() == tid  # Attempt 2, after 10 minutes; crashes again
    row = db.get_task(tid)
    assert row["state"] == "retry_wait" and row["retries_used"] == 2
    assert row["retry_at"] == "2026-10-21T02:40:00-07:00"  # 30 minutes later

    clock.advance(minutes=29)
    assert qr.run_next() is None
    clock.advance(minutes=1)
    assert qr.run_next() == tid  # Attempt 3; budget of 2 retries spent
    row = db.get_task(tid)
    assert row["state"] == "failed" and row["retry_at"] is None
    assert len(db.attempts(tid)) == 3
    assert all(a["outcome"] == "failed" and a["agent"] == "claude" for a in db.attempts(tid))
    assert "no retries left (2 of 2 used)" in db.transitions(tid)[-1]["reason"]
    assert [t["to_state"] for t in db.transitions(tid)] == [
        "queued", "running", "retry_wait", "queued", "running", "retry_wait",
        "queued", "running", "failed",
    ]
    text = _report(new)
    assert "**failed**" in text and "no retries left" in text
    assert "| claude | 3 |" in text  # all three Attempts in the usage table
    # Nothing more to run, and the machine was held awake only during Attempts.
    clock.advance(hours=5)
    assert qr.run_next() is None
    assert host.awake_log == ["hold", "release"] * 3


def test_retry_that_succeeds_finishes_done(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_EXIT", "1")
    env.setenv("FAKE_RESULT", "none")
    clock = Clock(datetime(2026, 10, 21, 2, 0, tzinfo=LA))
    tid = create_task(cfg, db, "flaky").spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    qr.run_next()
    env.setenv("FAKE_EXIT", "0")
    env.setenv("FAKE_RESULT", "done")
    clock.advance(minutes=10)
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE
    assert db.get_task(tid)["retries_used"] == 1


def test_retry_wait_does_not_block_other_tasks(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_EXIT", "1")
    env.setenv("FAKE_RESULT", "none")
    clock = Clock(datetime(2026, 10, 21, 2, 0, tzinfo=LA))
    a = create_task(cfg, db, "crashes").spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    qr.run_next()
    env.setenv("FAKE_EXIT", "0")
    env.setenv("FAKE_RESULT", "done")
    b = create_task(cfg, db, "fine").spec.id
    assert qr.run_next() == b  # a is waiting, b runs meanwhile
    assert db.task_state(a) == TaskState.RETRY_WAIT


def test_wait_schedule_from_config(cfg):
    cfg.data["retries"] = {"max_retries": 4, "wait_minutes": [1, 5]}
    assert [failures.retry_wait(cfg, n).total_seconds() / 60 for n in (1, 2, 3, 4)] == [1, 5, 5, 5]


# --------------------------------------------------------------------------
# orch retry
# --------------------------------------------------------------------------


def _fail(db, tid, *, used=2):
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "Attempt started", run_id=run)
    db.finish_attempt(run, exit_code=1, outcome="failed", end_reason="crash", session_id=None)
    db.transition(tid, TaskState.FAILED, "no retries left", set_fields={"retries_used": used})


def test_orch_retry_failed_task_gets_fresh_budget(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    _fail(db, tid)
    r = _orch(config_dir, "retry", tid)
    assert r.exit_code == 0, r.output
    row = db.get_task(tid)
    assert row["state"] == "queued" and row["retries_used"] == 0
    assert "retried by hand from failed" in db.transitions(tid)[-1]["reason"]


def test_orch_retry_starts_a_waiting_retry_now(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    failures.after_failed_attempt(cfg, db, tid, "crash", at=datetime(2026, 10, 21, 2, tzinfo=LA))
    r = _orch(config_dir, "retry", tid)
    assert r.exit_code == 0, r.output
    row = db.get_task(tid)
    assert row["state"] == "queued" and row["retries_used"] == 1 and row["retry_at"] is None
    assert "started early" in r.output


def test_orch_retry_refuses_other_states(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    r = _orch(config_dir, "retry", tid)
    assert r.exit_code == 1 and "is queued" in r.output
    assert _orch(config_dir, "retry", "nope").exit_code == 1


def test_orch_show_reports_retry_wait(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    failures.after_failed_attempt(cfg, db, tid, "crash", at=datetime(2026, 10, 21, 2, tzinfo=LA))
    out = _orch(config_dir, "show", tid).output
    assert "retries_used" in out and "1 of 2" in out
    assert "Waiting: retry 1 at 2026-10-21T02:10:00-07:00" in out


# --------------------------------------------------------------------------
# orch cancel
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["queued", "retry_wait", "awaiting_approval"])
def test_orch_cancel_waiting_task(config_dir, cfg, db, state):
    new = create_task(cfg, db, "x", needs_approval=state == "awaiting_approval")
    tid = new.spec.id
    if state == "retry_wait":
        run = db.start_attempt(tid, "claude", ["claude"], "x")
        db.transition(tid, TaskState.RUNNING, "started", run_id=run)
        db.finish_attempt(run, exit_code=1, outcome="failed", end_reason="c", session_id=None)
        failures.after_failed_attempt(cfg, db, tid, "c", at=datetime(2026, 10, 21, tzinfo=LA))
    r = _orch(config_dir, "cancel", tid)
    assert r.exit_code == 0, r.output
    assert db.task_state(tid) == TaskState.CANCELLED
    assert f"cancelled by hand (orch cancel) while {state}" in db.transitions(tid)[-1]["reason"]
    assert "**cancelled**" in _report(new)
    # A cancelled retry never comes back.
    qr = QueueRunner(cfg, db, FakeHost(), clock=lambda: datetime(2030, 1, 1, tzinfo=LA))
    assert qr.promote_retries() == []


def test_orch_cancel_refuses_finished_task(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    db.transition(tid, TaskState.CANCELLED, "t")
    r = _orch(config_dir, "cancel", tid)
    assert r.exit_code == 1 and "already cancelled" in r.output


def test_orch_cancel_running_task_under_daemon(config_dir, cfg, db, env, monkeypatch):  # noqa: F811
    env.setenv("FAKE_SLEEP", "30")
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    new = create_task(cfg, db, "slow")
    tid = new.spec.id
    host = FakeHost()
    holder = {}

    def serve():  # the daemon has its own DB connection, as in production
        ddb = Database(cfg.db_path, tz=cfg.timezone)
        try:
            holder["qr"] = QueueRunner(cfg, ddb, host, adapter_provider=fake_provider)
            holder["qr"].run_next()
        finally:
            ddb.close()

    t = threading.Thread(target=serve)
    t.start()
    try:
        for _ in range(1000):
            qr = holder.get("qr")
            if qr is not None and qr.control is not None and qr.control.proc is not None:
                break
            time.sleep(0.01)
        start = time.monotonic()
        r = _orch(config_dir, "cancel", tid)
        assert r.exit_code == 0, r.output
        assert "cancelled (was running)" in r.output
        assert time.monotonic() - start < 10
    finally:
        if "qr" in holder:
            holder["qr"].stop()
        t.join(timeout=20)
    assert db.task_state(tid) == TaskState.CANCELLED
    [a] = db.attempts(tid)
    assert a["outcome"] == "cancelled" and a["ended_at"]
    assert "**cancelled**" in _report(new)
    assert not host.awake_held()


def test_orch_cancel_orphaned_running_task_without_daemon(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    r = _orch(config_dir, "cancel", tid)
    assert r.exit_code == 0, r.output
    assert db.task_state(tid) == TaskState.CANCELLED
    assert db.attempts(tid)[0]["outcome"] == "cancelled"


def test_cancel_request_survives_daemon_death(cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    db.request_cancel(tid)
    qr = QueueRunner(cfg, db, FakeHost())
    assert qr.recover() == [tid]
    assert db.task_state(tid) == TaskState.CANCELLED  # not retried
    assert db.get_task(tid)["retries_used"] == 0


def test_daemon_stop_after_cancel_request_cancels(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_SLEEP", "30")
    tid = create_task(cfg, db, "slow").spec.id
    db.request_cancel(tid)  # as if `orch cancel` raced the watchdog
    control = AttemptControl()
    control.request_stop()  # daemon stop first
    res = run_attempt(cfg, db, tid, adapter_provider=fake_provider, control=control)
    assert res.state == TaskState.CANCELLED
