"""The queue runner, daemon lock and launchd plist, against the fake host."""

from __future__ import annotations

import json
import plistlib
import threading
from dataclasses import dataclass

import pytest
from click.testing import CliRunner

from nightshift import cli, daemon
from nightshift.daemon import DaemonLock, QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState

from test_gemini import fake_gemini, genv  # noqa: F401  (pytest fixture)
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)


def lane_provider(name, settings):
    """The fake Claude, and the fake Gemini for the Gemini lane."""
    return fake_gemini(settings) if name == "gemini" else fake_provider(name, settings)


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

    def __call__(self, cfg, db, task_id, *, adapter_provider=None, control=None, **_kw):
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
    assert db.get_task(tid)["retries_used"] == 0  # a daemon stop isn't a Failed Attempt
    assert "requeued" in db.transitions(tid)[-1]["reason"]
    assert host.awake_log == ["hold", "release"] and not host.awake_held()


def test_recover_retries_tasks_left_running(cfg, db):
    tid = create_task(cfg, db, "orphan").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "logs/attempt-1.claude.jsonl")
    db.transition(tid, TaskState.RUNNING, "Attempt 1 started", run_id=run)
    runner = QueueRunner(cfg, db, FakeHost(), run_attempt_fn=FakeAttempts(FakeHost()))
    assert runner.recover() == [tid]
    # A daemon dying mid-Attempt is a crash: a Failed Attempt, so it is retried.
    assert db.task_state(tid) == TaskState.RETRY_WAIT
    assert db.get_task(tid)["retries_used"] == 1
    assert db.attempts(tid)[0]["outcome"] == "failed"


def test_recover_fails_tasks_left_running_with_no_budget(cfg, db):
    cfg.data["retries"]["max_retries"] = 0
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
    # launchd's stdout is a file: unbuffered, or daemon.log stays empty.
    assert env["PYTHONUNBUFFERED"] == "1"
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


def test_install_refuses_when_daemon_files_are_privacy_protected(config_dir, fake_host, cfg, tmp_path):
    # Real launchd on macOS: a LaunchAgent whose Python lives under ~/Desktop
    # hung in dyld before Python started (TCC). Install must refuse up front.
    fake_host.protected = [cfg.agents_root.parent]
    r = _orch(config_dir, "daemon", "install")
    assert r.exit_code == 1, r.output
    assert "not installing" in r.output and "agents_root" in r.output
    assert "--allow-protected" in r.output and "Full Disk Access" in r.output
    assert fake_host.plists == {}

    r = _orch(config_dir, "daemon", "install", "--allow-protected")
    assert r.exit_code == 0, r.output
    assert "com.nightshift.daemon" in fake_host.plists
    assert "warning: agents_root" in r.output


def test_install_warns_but_installs_for_protected_task_inputs(config_dir, fake_host, tmp_path):
    docs = tmp_path / "Documents"
    (config_dir / "config.yaml").write_text(
        (config_dir / "config.yaml").read_text()
        + f"permissions:\n  read_only_dirs: [{docs / 'career'}]\n"
        + f"templates:\n  resume_tailor:\n    master_tex: {docs / 'career' / 'master.tex'}\n"
    )
    fake_host.protected = [docs]
    r = _orch(config_dir, "daemon", "install")
    assert r.exit_code == 0, r.output
    assert "warning: permissions.read_only_dirs" in r.output
    assert "warning: templates.resume_tailor.master_tex" in r.output


def test_access_problems_classify_blocking(cfg, tmp_path):
    host = FakeHost(protected=[tmp_path])
    found = {p.what: p.blocking for p in daemon.launchd_access_problems(cfg, host, python="/usr/bin/python3")}
    assert found["agents_root"] is True and found["config dir"] is True
    assert "Python" not in found  # /usr/bin is not protected
    assert daemon.launchd_access_problems(cfg, FakeHost(), python="/x") == []


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


# --------------------------------------------------------------------------
# A set of running Attempts (#44): driven at a cap of 2, one Task in each
# lane (#45)
# --------------------------------------------------------------------------


class GatedAttempts:
    """Fake Attempts that run until the test releases them, each on the
    runner's Attempt thread with that thread's own DB connection. Like
    run_attempt, each sets ``control.started`` once its Task is running."""

    def __init__(self, host: FakeHost):
        self.host = host
        self.gates: dict[str, threading.Event] = {}
        self.started: list[str] = []
        self.dbs: set[int] = set()
        self.cond = threading.Condition()

    def gate(self, task_id: str) -> threading.Event:
        with self.cond:
            return self.gates.setdefault(task_id, threading.Event())

    def __call__(self, cfg, db, task_id, *, control=None, **_kw):
        db.transition(task_id, TaskState.RUNNING, "Attempt 1 started")
        control.started.set()
        with self.cond:
            self.started.append(task_id)
            self.dbs.add(id(db))
            self.cond.notify_all()
        stopped = not self.gate(task_id).wait(10) or control.stop_requested
        state = TaskState.QUEUED if stopped else TaskState.DONE
        db.transition(task_id, state, "fake stopped" if stopped else "fake finished")
        return _Result(state)

    def wait_started(self, n: int) -> None:
        with self.cond:
            assert self.cond.wait_for(lambda: len(self.started) >= n, 10), self.started


def _ids(state):
    return [t["id"] for t in daemon.running_tasks(state)]


class _Daemon(threading.Thread):
    """The queue runner on its own thread with its own DB connection, as
    in production (SQLite connections stay in their thread)."""

    def __init__(self, cfg, host, *, serve=False, max_running=2, **kw):
        super().__init__()
        self.cfg, self.host, self.serve, self.kw = cfg, host, serve, kw
        self.max_running = max_running
        self.ready = threading.Event()
        self.ran: list[str] = []
        self.runner: QueueRunner | None = None

    def run(self):
        ddb = Database(self.cfg.db_path, tz=self.cfg.timezone)
        try:
            self.runner = QueueRunner(self.cfg, ddb, self.host, max_running=self.max_running,
                                      **self.kw)
            self.ready.set()
            if self.serve:
                self.runner.serve(poll=0.01)
            else:
                self.ran = self.runner.drain()
        finally:
            self.ready.set()
            ddb.close()

    def __enter__(self):
        self.start()
        assert self.ready.wait(10) and self.runner is not None
        return self.runner

    def __exit__(self, *exc):
        if exc[0] is not None and self.runner is not None:
            self.runner.stop()
        self.join(60)
        assert not self.is_alive()


def test_cap_two_runs_two_at_once_holds_awake_until_none_runs_and_lists_both(cfg, db):
    host = FakeHost()
    a = create_task(cfg, db, "a").spec.id
    b = create_task(cfg, db, "b", agent="gemini").spec.id
    c = create_task(cfg, db, "c").spec.id
    fake = GatedAttempts(host)
    d = _Daemon(cfg, host, run_attempt_fn=fake)
    try:
        runner = d.__enter__()
        fake.wait_started(2)
        assert fake.started == [a, b] and runner.current == a
        assert host.awake_held()
        state = daemon.read_state(cfg)
        assert _ids(state) == [a, b] and state["current_task"] == a
        assert state["max_running"] == 2 and state["awake_held"] is True
        assert [t_["agent"] for t_ in state["running_tasks"]] == ["claude", "gemini"]
        assert [t_["lane"] for t_ in state["running_tasks"]] == ["claude", "gemini"]
        assert db.task_state(c) == TaskState.QUEUED  # no free slot

        fake.gate(a).set()  # a ends: c takes its slot; the Mac stays awake
        fake.wait_started(3)
        assert host.awake_log == ["hold"]
        assert _ids(daemon.read_state(cfg)) == [b, c]
    finally:
        for g in (a, b, c):
            fake.gate(g).set()
        d.__exit__(None, None, None)
    assert d.ran == [a, b, c]
    assert all(db.task_state(x) == TaskState.DONE for x in (a, b, c))
    assert host.awake_log == ["hold", "release"] and not host.awake_held()
    after = daemon.read_state(cfg)
    assert after["running_tasks"] == [] and after["current_task"] is None
    assert after["awake_held"] is False
    assert len(fake.dbs) >= 2  # each Attempt thread had its own connection
    assert runner.running == {} and runner.control is None


def test_cap_two_a_crash_in_one_attempt_leaves_the_other_running(cfg, db):
    host = FakeHost()
    good = create_task(cfg, db, "good").spec.id
    bad = create_task(cfg, db, "bad", agent="gemini").spec.id
    fake = GatedAttempts(host)

    def attempt(cfg_, db_, task_id, **kw):
        if task_id == bad:  # `good` is running by now
            raise RuntimeError("boom")
        return fake(cfg_, db_, task_id, **kw)

    d = _Daemon(cfg, host, run_attempt_fn=attempt)
    try:
        runner = d.__enter__()
        fake.wait_started(1)
        for _ in range(500):
            if db.task_state(bad) == TaskState.FAILED:
                break
            threading.Event().wait(0.01)
        assert db.task_state(bad) == TaskState.FAILED
        assert runner.current == good and host.awake_held()
    finally:
        fake.gate(good).set()
        d.__exit__(None, None, None)
    assert db.task_state(good) == TaskState.DONE
    assert host.awake_log == ["hold", "release"]


def _wait_running(runner, n):
    for _ in range(1000):
        live = list(runner.running.values())
        if len(live) >= n and all(r.control.proc is not None for r in live):
            return
        threading.Event().wait(0.01)
    raise AssertionError("Attempts did not start")


def test_cap_two_stop_requeues_every_running_task(cfg, db, env, genv):  # noqa: F811
    env.setenv("FAKE_SLEEP", "30")
    genv.setenv("FAKE_GEMINI_STEP_DELAY", "5")
    host = FakeHost()
    a = create_task(cfg, db, "slow a").spec.id
    b = create_task(cfg, db, "slow b", agent="gemini").spec.id
    runner = QueueRunner(cfg, db, host, adapter_provider=lane_provider, max_running=2)
    seen = {}

    def stop_when_both_run():
        try:
            _wait_running(runner, 2)
            seen.update(daemon.read_state(cfg))
        finally:
            runner.stop()

    t = threading.Thread(target=stop_when_both_run)
    t.start()
    runner.serve(poll=0.01)
    t.join()
    assert _ids(seen) == [a, b] and seen["awake_held"] is True
    for tid in (a, b):
        assert db.task_state(tid) == TaskState.QUEUED
        [attempt] = db.attempts(tid)
        assert attempt["outcome"] == "interrupted" and attempt["ended_at"]
        assert "requeued" in db.transitions(tid)[-1]["reason"]
    assert host.awake_log == ["hold", "release"] and not host.awake_held()
    assert not daemon.state_path(cfg).exists()


def test_cap_two_cancel_and_pause_reach_the_right_attempt(cfg, db, env, genv,  # noqa: F811
                                                         monkeypatch):
    from datetime import datetime

    from nightshift import control

    env.setenv("FAKE_SLEEP", "30")
    genv.setenv("FAKE_GEMINI_STEP_DELAY", "5")
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    host = FakeHost()
    a = create_task(cfg, db, "slow a").spec.id
    b = create_task(cfg, db, "slow b", agent="gemini").spec.id
    d = _Daemon(cfg, host, adapter_provider=lane_provider)
    try:
        runner = d.__enter__()
        _wait_running(runner, 2)
        at = datetime.now()
        assert control.cancel(cfg, db, b, via="test", at=at).action == control.CANCEL_REQUESTED
        assert control.wait_for_cancel(cfg, db, b, timeout=20).action == control.CANCELLED
        assert db.task_state(a) == TaskState.RUNNING and runner.current == a
        assert _ids(daemon.read_state(cfg)) == [a] and host.awake_held()
        assert control.pause(cfg, db, a, via="test", at=at).action == control.PAUSE_REQUESTED
        assert control.wait_for_pause(cfg, db, a, timeout=20).action == control.PAUSED
    finally:
        runner.stop()
        d.__exit__(None, None, None)
    assert db.task_state(b) == TaskState.CANCELLED
    assert db.get_task(a)["pause_kind"] == "by_hand"
    assert host.awake_log == ["hold", "release"]


def test_recover_settles_several_orphaned_running_tasks(cfg, db):
    orphans = []
    for n in range(2):
        tid = create_task(cfg, db, f"orphan {n}").spec.id
        run = db.start_attempt(tid, "claude", ["claude"], "logs/attempt-1.claude.jsonl")
        db.transition(tid, TaskState.RUNNING, "Attempt 1 started", run_id=run)
        orphans.append(tid)
    host = FakeHost()
    runner = QueueRunner(cfg, db, host, run_attempt_fn=GatedAttempts(host), max_running=2)
    assert sorted(runner.recover()) == sorted(orphans)
    for tid in orphans:
        assert db.task_state(tid) == TaskState.RETRY_WAIT
        assert db.attempts(tid)[0]["outcome"] == "failed"
    assert db.tasks_in_state(TaskState.RUNNING) == []
    assert host.awake_log == []


def test_shipped_cap_runs_both_lanes(cfg, db):
    assert cfg.data["daemon"]["max_running"] == 2
    assert QueueRunner(cfg, db, FakeHost()).max_running == 2


def test_daemon_status_lists_every_running_task(config_dir, fake_host, cfg, monkeypatch):
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    path = daemon.state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": 1, "started_at": "s", "awake_held": True, "current_task": "t1",
        "running_tasks": [{"id": "t1", "agent": "codex", "lane": "claude", "started_at": "x"},
                          {"id": "t2", "agent": "gemini", "lane": "gemini", "started_at": "y"}]}))
    out = _orch(config_dir, "daemon", "status").output
    assert "running       t1 (codex, claude lane, since x)" in out
    assert "running       t2 (gemini, gemini lane, since y)" in out
    # A state file from a daemon before lanes.
    path.write_text(json.dumps({
        "pid": 1, "started_at": "s", "current_task": "t1",
        "running_tasks": [{"id": "t1", "agent": "claude", "started_at": "x"}]}))
    assert "running       t1 (claude, since x)" in _orch(config_dir, "daemon", "status").output
    # A state file from a daemon before the list.
    path.write_text(json.dumps({"pid": 1, "started_at": "s", "current_task": "t9"}))
    assert "running       t9" in _orch(config_dir, "daemon", "status").output
    path.write_text(json.dumps({"pid": 1, "started_at": "s", "current_task": None}))
    assert "running       none" in _orch(config_dir, "daemon", "status").output
