"""The queue runner, daemon lock and launchd plist, against the fake host."""

from __future__ import annotations

import plistlib
import threading
from dataclasses import dataclass

import pytest
from click.testing import CliRunner

from nightshift import cli, daemon
from nightshift.daemon import DaemonLock, QueueRunner
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState

from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)


@dataclass
class _Result:
    state: TaskState


class FakeAttempts:
    """Stands in for run_attempt: records order, concurrency and wakefulness."""

    def __init__(self, host: FakeHost, outcome=TaskState.DONE, raise_for: set[str] | None = None):
        self.host = host
        self.outcome = outcome
        self.raise_for = raise_for or set()
        self.order: list[str] = []
        self.active = 0
        self.max_active = 0
        self.awake_during: list[bool] = []

    def __call__(self, cfg, db, task_id, *, adapter_provider=None, control=None):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            self.order.append(task_id)
            self.awake_during.append(self.host.awake_held())
            if task_id in self.raise_for:
                raise RuntimeError("boom")
            db.transition(task_id, TaskState.RUNNING, "Attempt 1 started")
            db.transition(task_id, self.outcome, "fake finished")
            return _Result(self.outcome)
        finally:
            self.active -= 1


def test_runs_one_task_at_a_time_in_priority_order_and_holds_awake_only_while_running(cfg, db):
    host = FakeHost()
    bg = create_task(cfg, db, "background job", priority="background").spec.id
    n1 = create_task(cfg, db, "normal one").spec.id
    urgent = create_task(cfg, db, "urgent one", priority="urgent").spec.id
    n2 = create_task(cfg, db, "normal two").spec.id
    fake = FakeAttempts(host)
    runner = QueueRunner(cfg, db, host, run_attempt_fn=fake)

    assert not host.awake_held()
    assert runner.drain() == [urgent, n1, n2, bg]
    assert fake.order == [urgent, n1, n2, bg]
    assert fake.max_active == 1
    assert fake.awake_during == [True] * 4
    assert not host.awake_held()
    assert host.awake_log == ["hold", "release"] * 4
    assert all(db.task_state(t) == TaskState.DONE for t in fake.order)
    # Nothing left: running the queue again does nothing and never holds awake.
    assert runner.run_next() is None
    assert host.awake_log == ["hold", "release"] * 4


def test_empty_queue_never_holds_awake(cfg, db):
    host = FakeHost()
    QueueRunner(cfg, db, host, run_attempt_fn=FakeAttempts(host)).serve(exit_when_idle=True)
    assert host.awake_log == []


def test_crash_in_nightshift_fails_task_releases_awake_and_continues(cfg, db):
    host = FakeHost()
    bad = create_task(cfg, db, "bad").spec.id
    good = create_task(cfg, db, "good").spec.id
    fake = FakeAttempts(host, raise_for={bad})
    runner = QueueRunner(cfg, db, host, run_attempt_fn=fake)
    assert runner.drain() == [bad, good]
    assert db.task_state(bad) == TaskState.FAILED
    assert "RuntimeError: boom" in db.transitions(bad)[-1]["reason"]
    assert db.task_state(good) == TaskState.DONE
    assert not host.awake_held()


def test_real_attempts_through_runner(cfg, db, env):  # noqa: F811
    host = FakeHost()
    a = create_task(cfg, db, "first").spec.id
    b = create_task(cfg, db, "second").spec.id
    runner = QueueRunner(cfg, db, host, adapter_provider=fake_provider)
    runner.serve(exit_when_idle=True, poll=0.01)
    for tid in (a, b):
        assert db.task_state(tid) == TaskState.DONE
        assert [r["to_state"] for r in db.transitions(tid)] == ["queued", "running", "done"]
    # The two Attempts did not overlap.
    first, second = db.attempts(a)[0], db.attempts(b)[0]
    assert first["ended_at"] <= second["started_at"]
    assert host.awake_log == ["hold", "release", "hold", "release"]
    assert not daemon.state_path(cfg).exists()  # removed on clean exit


def test_stop_mid_attempt_requeues_and_releases_awake(cfg, db, env):  # noqa: F811
    env.setenv("FAKE_SLEEP", "30")
    host = FakeHost()
    tid = create_task(cfg, db, "slow").spec.id
    runner = QueueRunner(cfg, db, host, adapter_provider=fake_provider)

    def stop_when_running():
        for _ in range(500):
            if runner.control is not None and runner.control.proc is not None:
                break
            threading.Event().wait(0.01)
        runner.stop()

    t = threading.Thread(target=stop_when_running)
    t.start()
    runner.serve(poll=0.01)
    t.join()
    assert db.task_state(tid) == TaskState.QUEUED
    [attempt] = db.attempts(tid)
    assert attempt["outcome"] == "interrupted" and attempt["ended_at"]
    assert "requeued" in db.transitions(tid)[-1]["reason"]
    assert host.awake_log == ["hold", "release"] and not host.awake_held()


def test_recover_fails_tasks_left_running(cfg, db):
    tid = create_task(cfg, db, "orphan").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "logs/attempt-1.claude.jsonl")
    db.transition(tid, TaskState.RUNNING, "Attempt 1 started", run_id=run)
    queued = create_task(cfg, db, "waiting").spec.id
    runner = QueueRunner(cfg, db, FakeHost(), run_attempt_fn=FakeAttempts(FakeHost()))
    assert runner.recover() == [tid]
    assert db.task_state(tid) == TaskState.FAILED
    assert db.attempts(tid)[0]["outcome"] == "failed"
    assert db.task_state(queued) == TaskState.QUEUED
    report = (cfg.tasks_dir.glob(f"*_{tid}").__next__() / "report.md").read_text()
    assert "**failed**" in report and "daemon stopped unexpectedly" in report


def test_daemon_lock_is_exclusive(cfg):
    path = daemon.lock_path(cfg)
    assert not daemon.daemon_running(cfg)
    first = DaemonLock(path)
    assert first.acquire()
    assert daemon.daemon_running(cfg)
    assert not DaemonLock(path).acquire()
    first.release()
    assert not daemon.daemon_running(cfg)


def test_state_file_reports_current_task_and_awake(cfg, db):
    host = FakeHost()
    tid = create_task(cfg, db, "x").spec.id
    seen = {}

    def attempt(cfg_, db_, task_id, **kw):
        seen.update(daemon.read_state(cfg))
        db_.transition(task_id, TaskState.RUNNING, "start")
        db_.transition(task_id, TaskState.DONE, "end")
        return _Result(TaskState.DONE)

    QueueRunner(cfg, db, host, run_attempt_fn=attempt).run_next()
    assert seen["current_task"] == tid and seen["awake_held"] is True
    after = daemon.read_state(cfg)
    assert after["current_task"] is None and after["awake_held"] is False


# --------------------------------------------------------------------------
# launchd
# --------------------------------------------------------------------------


def test_plist_runs_daemon_under_keepalive(cfg):
    data = plistlib.loads(daemon.build_plist(cfg, python="/venv/bin/python", path_env="/usr/bin"))
    assert data["Label"] == "com.nightshift.daemon"
    assert data["KeepAlive"] is True and data["RunAtLoad"] is True
    assert data["ProgramArguments"] == [
        "/venv/bin/python", "-m", "nightshift", "--config-dir", str(cfg.config_dir), "daemon", "run",
    ]
    env = data["EnvironmentVariables"]
    assert env["PATH"].split(":")[0] == "/venv/bin"
    assert env["NIGHTSHIFT_CONFIG_DIR"] == str(cfg.config_dir)
    assert env["PYTHONPATH"].endswith("src")
    assert data["StandardOutPath"] == str(daemon.log_path(cfg))


@pytest.fixture
def fake_host(monkeypatch):
    host = FakeHost()
    monkeypatch.setattr(cli, "host_factory", lambda: host)
    return host


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def test_daemon_install_status_uninstall(config_dir, fake_host):
    r = _orch(config_dir, "daemon", "status")
    assert r.exit_code == 0, r.output
    assert "not installed" in r.output and "not running" in r.output

    r = _orch(config_dir, "daemon", "install")
    assert r.exit_code == 0, r.output
    plist = plistlib.loads(fake_host.plists["com.nightshift.daemon"])
    assert plist["KeepAlive"] is True
    assert "Installed com.nightshift.daemon" in r.output

    r = _orch(config_dir, "daemon", "status")
    assert "loaded, pid 4242" in r.output and "present" in r.output

    r = _orch(config_dir, "daemon", "uninstall")
    assert r.exit_code == 0, r.output
    assert fake_host.plists == {}
    assert "not installed" in _orch(config_dir, "daemon", "status").output


def test_daemon_install_failure_exits_nonzero(config_dir, fake_host):
    fake_host.launchd_error = "launchctl bootstrap failed: Input/output error"
    r = _orch(config_dir, "daemon", "install")
    assert r.exit_code == 1 and "bootstrap failed" in r.output


def test_daemon_run_refuses_second_daemon(config_dir, fake_host, cfg):
    lock = DaemonLock(daemon.lock_path(cfg))
    assert lock.acquire()
    try:
        r = _orch(config_dir, "daemon", "run", "--exit-when-idle")
        assert r.exit_code == 1 and "already running" in r.output
    finally:
        lock.release()


def test_daemon_run_exit_when_idle(config_dir, fake_host):
    r = _orch(config_dir, "daemon", "run", "--exit-when-idle")
    assert r.exit_code == 0, r.output
    assert "daemon started" in r.output and "daemon stopped" in r.output
    assert fake_host.awake_log == []
