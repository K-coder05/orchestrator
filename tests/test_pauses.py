"""Usage pauses, resume, expiry, orch pause/resume and fault injection (§2.6)."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightshift import cli, daemon, pauses
from nightshift import priority as prio
from nightshift.adapters.base import Event, EventKind
from nightshift.adapters.claude import ClaudeAdapter
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import iso, now
from nightshift.workspace import read_task_yaml

from conftest import FIXTURES
from test_planning import planner  # noqa: F401  (pytest fixture)
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
SESSION = "f5e745a1-aea4-451e-bef1-64b1544eb8de"  # the recorded stream's session


class Clock:
    """A fake clock starting at real time (DB timestamps are real time)."""

    def __init__(self):
        self.t = now("America/Los_Angeles")

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _invocations(folder):
    path = folder / "logs" / "fake_invocations.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


@pytest.fixture
def slow(env):  # noqa: F811
    # A short pause between stream lines, so a stop lands mid-stream.
    env.setenv("FAKE_STEP_DELAY", "0.05")
    return env


# --------------------------------------------------------------------------
# Fault injection and the pause itself
# --------------------------------------------------------------------------


def test_simulated_limit_pauses_after_n_steps_once_the_step_finishes(cfg, db, slow):
    clock = Clock()
    new = create_task(cfg, db, "haiku", simulate_rate_limit_after=2)
    tid = new.spec.id
    res = run_attempt(cfg, db, tid, adapter_provider=fake_provider, clock=clock)

    assert res.state == TaskState.PAUSED_USAGE
    [a] = db.attempts(tid)
    assert a["outcome"] == "paused" and a["session_id"] == SESSION
    assert "simulated rate limit after 2 Steps (--simulate-rate-limit-after 2)" in a["end_reason"]
    # The 2nd Step ran to its result, then the agent was stopped (SIGTERM).
    lines = [json.loads(x) for x in open(a["log_path"])]
    tool_uses = [b for m in lines if m.get("type") == "assistant"
                 for b in m["message"]["content"] if b.get("type") == "tool_use"]
    assert len(tool_uses) == 2
    assert lines[-1]["type"] == "user"  # the 2nd Step's tool_result
    assert a["exit_code"] == -15
    row = db.get_task(tid)
    assert row["pause_kind"] == "simulated" and row["resume_pending"] == 1
    assert row["pause_reset_at"] == iso(clock() + timedelta(minutes=5))
    assert row["resume_at"] == row["pause_reset_at"] and row["simulated_limit_at"]
    assert row["retries_used"] == 0  # a usage pause is not a Failed Attempt
    assert "resets at" in db.transitions(tid)[-1]["reason"]
    assert "**" not in (new.folder / "report.md").read_text()  # not finalized


def test_pause_then_resume_same_session_through_the_daemon(cfg, db, slow):
    clock = Clock()
    cfg.data["retries"]["max_retries"] = 0  # a pause must not need retry budget
    new = create_task(cfg, db, "haiku", simulate_rate_limit_after=2)
    tid = new.spec.id
    host = FakeHost()
    qr = QueueRunner(cfg, db, host, adapter_provider=fake_provider, clock=clock)

    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.PAUSED_USAGE
    clock.advance(minutes=4, seconds=59)
    assert qr.run_next() is None and db.task_state(tid) == TaskState.PAUSED_USAGE
    clock.advance(seconds=1)
    assert qr.run_next() == tid  # reset time reached: resumed

    assert db.task_state(tid) == TaskState.DONE
    first, second = _invocations(new.folder)
    assert "--resume" not in first["argv"]
    argv = second["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION
    assert second["stdin"] == pauses.RESUME_PROMPT
    assert "--append-system-prompt" in argv  # the protocol rides along on resume
    attempts = db.attempts(tid)
    assert [a["outcome"] for a in attempts] == ["paused", "completed"]
    assert db.get_task(tid)["retries_used"] == 0 and db.get_task(tid)["resume_pending"] == 0
    assert [t["to_state"] for t in db.transitions(tid)] == [
        "queued", "running", "paused_usage", "queued", "running", "done"]
    assert "resuming session" in db.transitions(tid)[4]["reason"]
    text = (new.folder / "report.md").read_text()
    assert "**done**" in text and "| claude | 2 |" in text
    assert host.awake_log == ["hold", "release"] * 2


def test_old_session_resumes_fresh_from_plan_and_progress(cfg, db, slow):
    clock = Clock()
    new = create_task(cfg, db, "haiku", simulate_rate_limit_after=1)
    tid = new.spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    qr.run_next()
    clock.advance(hours=25)  # past the 24h session limit, under 72h
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE
    second = _invocations(new.folder)[1]
    assert "--resume" not in second["argv"]
    assert second["stdin"].startswith("haiku") and "plan.md" in second["stdin"]
    assert "progress.md" in second["stdin"]
    reason = db.transitions(tid)[4]["reason"]
    assert "fresh session from plan.md + progress.md" in reason and "over 24h" in reason


def test_failed_resume_falls_back_to_a_fresh_session(cfg, db, slow):
    clock = Clock()
    new = create_task(cfg, db, "haiku", simulate_rate_limit_after=2)
    tid = new.spec.id
    run_attempt(cfg, db, tid, adapter_provider=fake_provider, clock=clock)
    slow.setenv("FAKE_RESUME_FAIL", "1")
    clock.advance(minutes=5)
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE
    outcomes = [a["outcome"] for a in db.attempts(tid)]
    assert outcomes == ["paused", "resume_failed", "completed"]
    assert "No conversation found" in db.attempts(tid)[1]["end_reason"]
    third = _invocations(new.folder)[2]
    assert "--resume" not in third["argv"] and "plan.md" in third["stdin"]
    assert db.get_task(tid)["retries_used"] == 0  # not a Failed Attempt either


def test_real_limit_hit_pauses_with_reset_time_and_holds_the_agent(cfg, db, env):  # noqa: F811
    clock = Clock()
    reset = clock() + timedelta(minutes=40)
    env.setenv("FAKE_LIMIT_AT", "6")  # after the first Step's result
    env.setenv("FAKE_RESETS_AT", str(int(reset.timestamp())))
    a = create_task(cfg, db, "first").spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    assert qr.run_next() == a
    row = db.get_task(a)
    assert row["state"] == "paused_usage" and row["pause_kind"] == "usage_limit"
    assert row["pause_limit_type"] == "five_hour"
    assert row["pause_reset_at"] == iso(reset.replace(microsecond=0))
    [att] = db.attempts(a)
    assert att["outcome"] == "paused" and "claude's usage limit (five_hour)" in att["end_reason"]
    assert row["retries_used"] == 0

    # Claude is out: nothing else starts on it until the reset.
    b = create_task(cfg, db, "second").spec.id
    assert qr.run_next() is None
    assert db.task_state(b) == TaskState.QUEUED
    env.delenv("FAKE_LIMIT_AT")
    clock.advance(minutes=41)
    assert qr.run_next() == a  # resumed first, ahead of the fresh Task
    assert qr.run_next() == b
    assert db.task_state(a) == TaskState.DONE and db.task_state(b) == TaskState.DONE


def test_limit_without_reset_time_tries_again_after_probe_interval(cfg, db):
    clock = Clock()
    tid = create_task(cfg, db, "x").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    pauses.pause_after_attempt(cfg, db, tid, kind=pauses.KIND_LIMIT, limit=None,
                               reason="limit", at=clock(), run_id=run)
    row = db.get_task(tid)
    assert row["pause_reset_at"] is None
    assert row["resume_at"] == iso(clock() + timedelta(minutes=30))
    assert "no reset time given" in db.transitions(tid)[-1]["reason"]


def test_step_that_never_finishes_is_cut_off_after_the_grace(cfg, db, env, tmp_path):  # noqa: F811
    stream = tmp_path / "stuck.jsonl"
    lines = (FIXTURES / "claude_stream.jsonl").read_text()
    stream.write_text("\n".join(lines.splitlines()[:3]) + "\n")  # a tool_use, no result
    env.setenv("FAKE_STREAM", str(stream))
    env.setenv("FAKE_SLEEP", "30")
    cfg.data["usage"]["step_grace_seconds"] = 0.5
    tid = create_task(cfg, db, "x", simulate_rate_limit_after=1).spec.id
    start = time.monotonic()
    res = run_attempt(cfg, db, tid, adapter_provider=fake_provider)
    assert time.monotonic() - start < 10
    assert res.state == TaskState.PAUSED_USAGE


def test_simulate_value_is_validated(cfg, db):
    for bad in (0, -1, True, "3"):
        with pytest.raises(ValueError):
            create_task(cfg, db, "x", simulate_rate_limit_after=bad)


# --------------------------------------------------------------------------
# Resume order and expiry
# --------------------------------------------------------------------------


def _paused(cfg, db, prompt, at, **kw):
    tid = create_task(cfg, db, prompt, **kw).spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.set_attempt_session(run, f"s-{tid}")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    db.finish_attempt(run, exit_code=-15, outcome="paused", end_reason="sim", session_id=None)
    pauses.pause_after_attempt(cfg, db, tid, kind=pauses.KIND_SIMULATED,
                               limit=pauses.simulated_limit(cfg, at), reason="sim", at=at,
                               run_id=run)
    return tid


def test_resume_order_priority_then_deadline_then_oldest(cfg, db):
    clock = Clock()
    t = clock()
    soon = iso(t + timedelta(days=3))
    later = iso(t + timedelta(days=5))
    old = _paused(cfg, db, "old normal", t)
    far = _paused(cfg, db, "far deadline", t, deadline=later)
    near = _paused(cfg, db, "near deadline", t, deadline=soon)
    bg = _paused(cfg, db, "background", t, priority="background", deadline=soon)
    urgent = _paused(cfg, db, "urgent", t, priority="urgent")
    rows = db.tasks_in_state(TaskState.PAUSED_USAGE)
    assert [r["id"] for r in prio.resume_order(cfg, rows, t)] == [urgent, near, far, old, bg]

    fresh_normal = create_task(cfg, db, "fresh normal").spec.id  # newest, never paused
    fresh_urgent = create_task(cfg, db, "fresh urgent", priority="urgent").spec.id
    clock.advance(minutes=5)
    seen = []

    def fake_attempt(cfg, db, task_id, **_kw):
        seen.append(task_id)
        db.transition(task_id, TaskState.RUNNING, "fake")
        db.transition(task_id, TaskState.DONE, "fake")
        return type("R", (), {"state": TaskState.DONE})()

    qr = QueueRunner(cfg, db, FakeHost(), run_attempt_fn=fake_attempt, clock=clock)
    qr.drain()
    # Urgent first (resumed before fresh), then resumed normal Tasks in resume
    # order, then the fresh normal Task, then background.
    assert seen == [urgent, fresh_urgent, near, far, old, fresh_normal, bg]


def test_paused_over_72h_needs_reapproval(config_dir, cfg, db, slow):
    clock = Clock()
    new = create_task(cfg, db, "haiku", simulate_rate_limit_after=2)
    tid = new.spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    qr.run_next()
    _orch(config_dir, "pause", tid)  # by hand: no automatic resume at all
    clock.advance(hours=73)
    assert qr.run_next() is None
    row = db.get_task(tid)
    assert row["state"] == "awaiting_approval" and row["approved"] == 0
    assert read_task_yaml(new.folder).approved is False
    assert "needs re-approval" in db.transitions(tid)[-1]["reason"]
    r = _orch(config_dir, "resume", tid)
    assert r.exit_code == 1 and "orch approve" in r.output
    assert _orch(config_dir, "approve", tid).exit_code == 0
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE
    # The session was 73h old: a fresh session from plan.md + progress.md.
    assert "--resume" not in _invocations(new.folder)[1]["argv"]


def test_passed_deadline_cancels_a_paused_task_and_reports_it(cfg, db):
    clock = Clock()
    deadline = iso(clock() + timedelta(hours=2))
    tid = _paused(cfg, db, "due soon", clock(), deadline=deadline)
    db.conn.execute("UPDATE tasks SET resume_at = NULL, pause_kind = 'by_hand' WHERE id = ?",
                    (tid,))
    db.conn.commit()
    qr = QueueRunner(cfg, db, FakeHost(), clock=clock)
    clock.advance(hours=1)
    assert qr.promote_paused().cancelled == []
    clock.advance(hours=1)
    assert qr.promote_paused().cancelled == [tid]
    assert db.task_state(tid) == TaskState.CANCELLED
    text = open(f"{db.get_task(tid)['folder']}/report.md").read()
    assert "**cancelled**" in text and "passed while paused" in text


# --------------------------------------------------------------------------
# orch pause / orch resume
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["queued", "retry_wait"])
def test_orch_pause_and_resume_waiting_task(config_dir, cfg, db, state):
    clock = Clock()
    tid = create_task(cfg, db, "x").spec.id
    if state == "retry_wait":
        run = db.start_attempt(tid, "claude", ["claude"], "x")
        db.transition(tid, TaskState.RUNNING, "started", run_id=run)
        db.finish_attempt(run, exit_code=1, outcome="failed", end_reason="c", session_id=None)
        db.transition(tid, TaskState.RETRY_WAIT, "c", set_fields={"retries_used": 1,
                                                                    "retry_at": iso(clock())})
    r = _orch(config_dir, "pause", tid)
    assert r.exit_code == 0, r.output
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["pause_kind"] == "by_hand"
    assert row["resume_at"] is None
    assert "paused by hand until `orch resume" in _orch(config_dir, "show", tid).output
    # The daemon never resumes a pause by hand on its own.
    clock.advance(hours=10)
    qr = QueueRunner(cfg, db, FakeHost(), clock=clock)
    assert qr.promote_paused().resumed == []
    assert _orch(config_dir, "pause", tid).exit_code == 1  # already paused
    r = _orch(config_dir, "resume", tid)
    assert r.exit_code == 0, r.output
    assert db.task_state(tid) == TaskState.QUEUED
    assert "resumed by hand (orch resume)" in db.transitions(tid)[-1]["reason"]
    assert db.get_task(tid)["retries_used"] == (1 if state == "retry_wait" else 0)


def test_orch_resume_ends_a_usage_pause_early(config_dir, cfg, db):
    clock = Clock()
    tid = _paused(cfg, db, "x", clock())
    r = _orch(config_dir, "resume", tid)
    assert r.exit_code == 0, r.output
    row = db.get_task(tid)
    assert row["state"] == "queued" and row["resume_pending"] == 1 and row["resume_at"] is None


def test_orch_pause_keeps_a_usage_pause_until_resumed(config_dir, cfg, db):
    clock = Clock()
    tid = _paused(cfg, db, "x", clock())
    assert _orch(config_dir, "pause", tid).exit_code == 0
    clock.advance(minutes=10)
    assert pauses.promote_due(cfg, db, clock()).resumed == []
    assert db.get_task(tid)["pause_kind"] == "by_hand"


def test_orch_pause_resume_refuse_other_states(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    assert _orch(config_dir, "resume", tid).exit_code == 1
    db.transition(tid, TaskState.CANCELLED, "t")
    r = _orch(config_dir, "pause", tid)
    assert r.exit_code == 1 and "cancelled" in r.output
    assert _orch(config_dir, "pause", "nope").exit_code == 1


def test_orch_pause_running_task_under_daemon(config_dir, cfg, db, env, monkeypatch):  # noqa: F811
    env.setenv("FAKE_STEP_DELAY", "0.3")
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    new = create_task(cfg, db, "slow")
    tid = new.spec.id
    holder = {}

    def serve():  # the daemon has its own DB connection, as in production
        ddb = Database(cfg.db_path, tz=cfg.timezone)
        try:
            holder["qr"] = QueueRunner(cfg, ddb, FakeHost(), adapter_provider=fake_provider)
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
        time.sleep(0.5)
        r = _orch(config_dir, "pause", tid)
        assert r.exit_code == 0, r.output
        assert "paused (was running)" in r.output
    finally:
        if "qr" in holder:
            holder["qr"].stop()
        t.join(timeout=20)
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["pause_kind"] == "by_hand"
    assert row["resume_pending"] == 1 and row["pause_requested_at"] is None
    [a] = db.attempts(tid)
    assert a["outcome"] == "paused" and "paused by hand (orch pause)" in a["end_reason"]
    # The stop came between Steps: every tool call in the log has its result.
    lines = [json.loads(x) for x in open(a["log_path"])]
    uses = {b["id"] for m in lines if m.get("type") == "assistant"
            for b in m["message"]["content"] if b.get("type") == "tool_use"}
    results = {b.get("tool_use_id") for m in lines if m.get("type") == "user"
               for b in m["message"]["content"] if isinstance(b, dict)}
    assert uses and uses <= results
    assert _orch(config_dir, "resume", tid).exit_code == 0
    assert db.task_state(tid) == TaskState.QUEUED


def test_orch_pause_orphaned_running_task(config_dir, cfg, db):
    tid = create_task(cfg, db, "x").spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "started", run_id=run)
    r = _orch(config_dir, "pause", tid)
    assert r.exit_code == 0, r.output
    assert db.task_state(tid) == TaskState.PAUSED_USAGE
    assert db.attempts(tid)[0]["outcome"] == "paused"
    assert db.get_task(tid)["resume_pending"] == 1


def test_orch_add_simulate_rate_limit_after(config_dir, db, planner):  # noqa: F811
    r = _orch(config_dir, "add", "write a haiku", "--yes", "--simulate-rate-limit-after", "3")
    assert r.exit_code == 0, r.output
    assert "simulated rate limit after 3 Steps" in r.output
    [row] = db.list_tasks()
    assert row["simulate_rate_limit_after"] == 3
    assert "simulate_rate_limit_after" in _orch(config_dir, "show", row["id"]).output
    r = _orch(config_dir, "add", "x", "--yes", "--simulate-rate-limit-after", "0")
    assert r.exit_code == 2


# --------------------------------------------------------------------------
# Claude adapter: rate-limit detection and Step results
# --------------------------------------------------------------------------


def test_claude_limit_detection():
    a = ClaudeAdapter()
    info = EventKind.RATE_LIMIT_INFO
    assert a.detect_rate_limit(Event(info, {"status": "allowed_warning"})) is None
    hit = a.detect_rate_limit(Event(info, {"status": "rejected", "rateLimitType": "seven_day"}))
    assert hit.limit_type == "seven_day" and hit.reset_at is None
    err = Event(EventKind.ERROR, {"message": "Claude AI usage limit reached"})
    assert a.detect_rate_limit(err) is not None
    assert a.detect_rate_limit(Event(EventKind.ERROR, {"message": "x", "api_error_status": 429}))
    tool = Event(EventKind.ERROR, {"message": "API rate limit exceeded", "tool_error": True})
    assert a.detect_rate_limit(tool) is None  # a tool's output, not our limit
    assert a.detect_rate_limit(Event(EventKind.ERROR, {"message": "model_not_found"})) is None


def test_claude_emits_tool_results():
    a = ClaudeAdapter()
    line = json.dumps({"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})
    [ev] = a.parse_stream(line)
    assert ev.kind == EventKind.TOOL_RESULT and ev.data == {"tool_use_id": "t1", "is_error": False}
