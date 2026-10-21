"""``orch add|list|show|logs|report``."""

from __future__ import annotations

import time

from click.testing import CliRunner

from nightshift import cli, views
from nightshift.daemon import DaemonLock, lock_path
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState

from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def test_add_queues_and_returns_immediately(config_dir, cfg, db):
    start = time.monotonic()
    r = _orch(config_dir, "add", "Write a haiku", "--priority", "urgent")
    assert r.exit_code == 0, r.output
    assert time.monotonic() - start < 5
    [row] = db.list_tasks()
    assert row["state"] == "queued" and row["priority"] == "urgent"
    assert db.attempts(row["id"]) == []  # nothing ran
    assert f"Task {row['id']} queued" in r.output
    assert "daemon isn't running" in r.output


def test_add_without_note_when_daemon_up(config_dir, cfg):
    lock = DaemonLock(lock_path(cfg))
    assert lock.acquire()
    try:
        r = _orch(config_dir, "add", "x")
    finally:
        lock.release()
    assert r.exit_code == 0 and "daemon isn't running" not in r.output


def test_list_filters_by_status(config_dir, cfg, db):
    a = create_task(cfg, db, "alpha task").spec.id
    b = create_task(cfg, db, "beta task").spec.id
    db.transition(b, TaskState.CANCELLED, "test")
    r = _orch(config_dir, "list")
    assert r.exit_code == 0 and a in r.output and b in r.output
    assert r.output.splitlines()[0].split()[:2] == ["ID", "STATE"]
    r = _orch(config_dir, "list", "--status", "queued")
    assert a in r.output and b not in r.output
    r = _orch(config_dir, "list", "--status", "queued", "--status", "cancelled")
    assert a in r.output and b in r.output
    assert _orch(config_dir, "list", "--status", "bogus").exit_code == 2
    assert "No Tasks." in _orch(config_dir, "list", "--status", "running").output


def test_show_report_and_logs_after_a_run(config_dir, cfg, db, env):  # noqa: F811
    new = create_task(cfg, db, "Write a haiku to output/haiku.txt")
    tid = new.spec.id
    run_attempt(cfg, db, tid, adapter_provider=fake_provider)

    r = _orch(config_dir, "show", tid)
    assert r.exit_code == 0, r.output
    assert "state" in r.output and "done" in r.output
    assert str(new.folder) in r.output
    assert "Attempts:" in r.output and "completed" in r.output
    assert "(new) -> queued" in r.output and "running -> done" in r.output

    r = _orch(config_dir, "report", tid)
    assert r.exit_code == 0 and "## 1. Result" in r.output and "**done**" in r.output

    r = _orch(config_dir, "logs", tid)
    assert r.exit_code == 0, r.output
    assert "[nightshift]" in r.output and "Attempt 1 started" in r.output
    assert "== Attempt 1 (claude) ==" in r.output
    assert "[step] Write" in r.output and "[result] ok" in r.output

    raw = _orch(config_dir, "logs", tid, "--raw").output
    assert '"type":"system"' in raw.replace(" ", "")

    # -f on a finished Task prints everything and exits.
    r = _orch(config_dir, "logs", tid, "-f")
    assert r.exit_code == 0 and r.output.rstrip().endswith(f"Task {tid} is done.")


def test_follow_waits_for_running_task(cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    folder = cfg.tasks_dir.glob(f"*_{tid}").__next__()
    log = folder / "logs" / "nightshift.log"
    steps = iter(range(10))

    def sleep(_):
        i = next(steps)
        if i == 0:
            log.write_text("line one\npartial")
        elif i == 1:
            with open(log, "a") as f:
                f.write(" now whole\n")
            db.transition(tid, TaskState.RUNNING, "start")
            db.transition(tid, TaskState.DONE, "end")

    out = list(views.task_logs(cfg, db, tid, follow=True, sleep=sleep))
    assert out == ["[nightshift] line one", "[nightshift] partial now whole", f"Task {tid} is done."]


def test_unknown_task(config_dir, cfg):
    for cmd in ("show", "logs", "report"):
        r = _orch(config_dir, cmd, "zzzz")
        assert r.exit_code == 1 and "no Task 'zzzz'" in r.output
