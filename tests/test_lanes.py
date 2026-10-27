"""Two agents in parallel (§5 item 5, #45): the Claude lane (Claude, or Codex
when it has taken over for Claude) and the Gemini lane (Gemini and its paid
API path) each run one Task at a time, side by side.

The queue runner runs on its own thread with its own DB connection, as in
production (``test_daemon._Daemon``). The Attempts are fakes the test
releases, so every overlap is deterministic; where a real Attempt matters
(a limit hit, paid calls) it runs through ``run_attempt`` with the fake
CLIs, its lane partner held open by the test.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import yaml

from nightshift import usage
from nightshift.adapters import lane_of
from nightshift.adapters.base import RateLimit
from nightshift.host import FakeHost
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import iso

from test_daemon import _Daemon, _Result
from test_gemini import genv  # noqa: F401  (fixture)
from test_gemini_api import cfg, config_dir, db, provider as gemini_provider  # noqa: F401
from test_runner import env, fake_provider as fake_claude_provider  # noqa: F401  (fixture)

LA = ZoneInfo("America/Los_Angeles")
#: A fixed night in Los Angeles: inside the long-task window, so no daytime reserve.
NIGHT = datetime(2026, 10, 22, 2, 0, tzinfo=LA)


def provider(name, settings):
    """The fake Claude CLI, and the fake Gemini CLI for both Gemini paths."""
    return fake_claude_provider(name, settings) if name == "claude" else gemini_provider(
        name, settings)


class Clock:
    def __init__(self, t: datetime = NIGHT):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class LaneAttempts:
    """Stands in for run_attempt. Each Attempt runs until the test releases
    it, on the runner's Attempt thread with that thread's own connection,
    and checks the lane rule as it starts: no other Attempt in its lane.

    ``real``: agents whose Attempts run through the real ``run_attempt``
    (with the fake CLIs); they are watched for the lane rule too.
    ``on_start``: per Task, work the Attempt does once running (e.g.
    recording usage) before it waits for its release; ``before_running``,
    work before it moves its Task to ``running``."""

    def __init__(self, *, real: frozenset[str] = frozenset(), on_start=None,
                 before_running=None):
        self.real = real
        self.on_start = on_start or {}
        self.before_running = before_running or {}
        self.cond = threading.Condition()
        self.gates: dict[str, threading.Event] = {}
        self.started: list[tuple[str, str]] = []  # (Task, agent), in start order
        self.active: dict[str, str] = {}  # Task -> lane, running now
        self.overlaps: set[frozenset[str]] = set()  # lanes seen running together
        self.violations: list[str] = []

    def gate(self, task_id: str) -> threading.Event:
        with self.cond:
            return self.gates.setdefault(task_id, threading.Event())

    def _enter(self, task_id: str, agent: str) -> None:
        lane = lane_of(agent)
        with self.cond:
            if lane in self.active.values():
                self.violations.append(f"{task_id} ({agent}) started in the busy {lane} lane")
            for other in self.active.values():
                self.overlaps.add(frozenset({lane, other}))
            self.active[task_id] = lane
            self.started.append((task_id, agent))
            self.cond.notify_all()

    def _leave(self, task_id: str) -> None:
        with self.cond:
            self.active.pop(task_id, None)
            self.cond.notify_all()

    def __call__(self, cfg_, db_, task_id, *, control=None, **kw):
        agent = db_.get_task(task_id)["agent"]
        self._enter(task_id, agent)
        try:
            if agent in self.real:
                kw["adapter_provider"] = provider
                return run_attempt(cfg_, db_, task_id, control=control, **kw)
            if task_id in self.before_running:
                self.before_running[task_id](db_, task_id)
            run = db_.start_attempt(task_id, agent, ["fake"], "logs/fake.jsonl")
            db_.transition(task_id, TaskState.RUNNING, "Attempt started", run_id=run)
            control.started.set()
            if task_id in self.on_start:
                self.on_start[task_id](db_, task_id, run)
            stopped = not self.gate(task_id).wait(20) or control.stop_requested
            state = TaskState.QUEUED if stopped else TaskState.DONE
            db_.finish_attempt(run, exit_code=0, outcome="interrupted" if stopped else "completed",
                               end_reason="fake", session_id=None)
            db_.transition(task_id, state, "fake stopped" if stopped else "fake finished",
                           run_id=run)
            return _Result(state)
        finally:
            self._leave(task_id)

    def wait_for(self, what, predicate) -> None:
        with self.cond:
            assert self.cond.wait_for(predicate, 20), (what, self.started, self.active)

    def wait_started(self, *task_ids: str) -> None:
        self.wait_for(f"start of {task_ids}",
                      lambda: set(task_ids) <= {t for t, _ in self.started})

    def running(self) -> set[str]:
        with self.cond:
            return set(self.active)


def _wait_state(db, task_id, state, timeout=20.0):
    for _ in range(int(timeout * 100)):
        if db.task_state(task_id) == state:
            return
        threading.Event().wait(0.01)
    raise AssertionError(f"{task_id} is {db.task_state(task_id)}, not {state}")


def _new(cfg, db, title, agent="claude", task_type="general", **kw):
    """A Task on ``agent`` as its own (as a template puts it, not --agent),
    so it may fail over."""
    kw.setdefault("long", False)
    return create_task(cfg, db, title, agent=agent, agent_override=False, task_type=task_type,
                       **kw).spec.id


# --------------------------------------------------------------------------
# The lane rule
# --------------------------------------------------------------------------


def test_a_claude_and_a_gemini_task_run_at_once_but_two_in_the_claude_lane_never_do(cfg, db):
    c1 = _new(cfg, db, "claude one")
    cx = _new(cfg, db, "codex fix", agent="codex", task_type="coding")
    g1 = _new(cfg, db, "gemini one", agent="gemini", task_type="research")
    c2 = _new(cfg, db, "claude two")
    g2 = _new(cfg, db, "gemini api", agent="gemini-api", task_type="summary")
    fake = LaneAttempts()
    host = FakeHost()
    d = _Daemon(cfg, host, run_attempt_fn=fake)
    try:
        d.__enter__()
        # The oldest Task in each lane: Codex and the second Claude Task wait
        # behind the first, though the Gemini lane is busy too.
        fake.wait_started(c1, g1)
        assert fake.running() == {c1, g1}
        assert {t: db.task_state(t) for t in (cx, c2, g2)} == dict.fromkeys(
            (cx, c2, g2), TaskState.QUEUED)

        fake.gate(g1).set()  # the Gemini lane frees: its next Task (the paid path) starts
        fake.wait_started(g2)
        assert fake.running() == {c1, g2} and db.task_state(cx) == TaskState.QUEUED

        fake.gate(c1).set()  # the Claude lane frees: Codex, the older of the two
        fake.wait_started(cx)
        assert fake.running() == {cx, g2} and db.task_state(c2) == TaskState.QUEUED

        fake.gate(cx).set()
        fake.wait_started(c2)
        assert fake.running() == {c2, g2}
    finally:
        for t in (c1, cx, g1, c2, g2):
            fake.gate(t).set()
        d.__exit__(None, None, None)
    assert fake.violations == []
    assert fake.overlaps == {frozenset({"claude", "gemini"})}
    assert [t for t, _ in fake.started] == [c1, g1, g2, cx, c2]
    assert all(db.task_state(t) == TaskState.DONE for t in (c1, cx, g1, c2, g2))
    assert host.awake_log == ["hold", "release"]  # held throughout, released once


def test_a_cap_of_one_runs_one_task_at_a_time_across_lanes(cfg, db):
    c = _new(cfg, db, "claude")
    g = _new(cfg, db, "gemini", agent="gemini")
    fake = LaneAttempts()
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake, max_running=1)
    try:
        d.__enter__()
        fake.wait_started(c)
        assert fake.running() == {c} and db.task_state(g) == TaskState.QUEUED
        fake.gate(c).set()
        fake.wait_started(g)
    finally:
        for t in (c, g):
            fake.gate(t).set()
        d.__exit__(None, None, None)
    assert fake.overlaps == set()


# --------------------------------------------------------------------------
# Failover moves a Task between lanes (§2.8)
# --------------------------------------------------------------------------


def test_a_task_failed_over_before_it_starts_is_picked_by_its_new_lane(cfg, db):
    clock = Clock()
    usage.record_limit_hit(cfg, db, "claude",
                           RateLimit("five_hour", iso(clock() + timedelta(hours=3))),
                           at=clock(), source="probe")
    g = _new(cfg, db, "gemini first", agent="gemini", task_type="research")
    cx = _new(cfg, db, "fix the build", task_type="coding")  # Claude out: to Codex
    r = _new(cfg, db, "read up", task_type="research")  # Claude out: to Gemini
    c = _new(cfg, db, "plain Claude work")  # no failover: waits for Claude
    fake = LaneAttempts()
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake, clock=clock)
    try:
        d.__enter__()
        fake.wait_started(g, cx)
        # Codex holds the Claude lane; the research Task now waits in the
        # Gemini lane (busy), not in the Claude lane.
        assert dict(fake.started) == {g: "gemini", cx: "codex"}
        assert db.get_task(r)["agent"] == "gemini" and db.task_state(r) == TaskState.QUEUED
        assert [(f["from_agent"], f["to_agent"]) for f in db.failovers(r)] == [("claude", "gemini")]

        fake.gate(cx).set()  # the Claude lane frees, but Claude is out: nothing starts there
        _wait_state(db, cx, TaskState.DONE)
        assert fake.running() == {g} and db.task_state(c) == TaskState.QUEUED

        fake.gate(g).set()  # the Gemini lane frees: the failed-over Task starts there
        fake.wait_started(r)
        assert dict(fake.started)[r] == "gemini"
    finally:
        for t in (g, cx, r, c):
            fake.gate(t).set()
        d.runner.stop()
        d.__exit__(None, None, None)
    assert fake.violations == []
    assert db.task_state(c) == TaskState.QUEUED and db.get_task(c)["agent"] == "claude"


def test_a_task_that_fails_over_mid_run_waits_for_the_other_lane(cfg, db, env, genv):  # noqa: F811
    g = _new(cfg, db, "long gemini", agent="gemini")
    r = _new(cfg, db, "read up on photosynthesis", task_type="research",
             simulate_rate_limit_after=2)  # its Claude Attempt hits a (simulated) limit
    c2 = _new(cfg, db, "next Claude Task")
    fake = LaneAttempts(real=frozenset({"claude"}))  # c2 too runs on the fake Claude CLI
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake)
    try:
        d.__enter__()
        fake.wait_started(g, r)
        # The Claude Attempt fails over to Gemini and is requeued there; the
        # Claude lane then runs the next Claude Task, while the moved Task
        # waits for the Gemini lane.
        _wait_state(db, c2, TaskState.DONE)
        row = db.get_task(r)
        assert row["agent"] == "gemini" and row["state"] == TaskState.QUEUED
        assert [(a["agent"], a["outcome"]) for a in db.attempts(r)] == [("claude", "paused")]
        assert fake.running() == {g}

        fake.gate(g).set()
        fake.wait_for("r in the Gemini lane", lambda: [t for t, _ in fake.started].count(r) == 2)
        assert [a for t, a in fake.started if t == r] == ["claude", "gemini"]
        fake.gate(r).set()
        _wait_state(db, r, TaskState.DONE)
    finally:
        for t in (g, r):
            fake.gate(t).set()
        d.__exit__(None, None, None)
    assert fake.violations == []
    assert fake.overlaps == {frozenset({"claude", "gemini"})}


# --------------------------------------------------------------------------
# Spend caps and the paid-credit rule (§1.5) with both lanes busy
# --------------------------------------------------------------------------


def _earlier_spend(cfg, db, usd, at):
    other = _new(cfg, db, "earlier paid Task", agent="gemini-api", task_type="summary")
    db.transition(other, TaskState.CANCELLED, "test: only its spend matters")
    db.record_spend(other, run_id=None, agent="gemini-api", model="gemini-3.1-pro",
                    at=iso(at - timedelta(hours=1)), input_tokens=0, cached_tokens=0,
                    output_tokens=0, thoughts_tokens=0, cost_usd=usd, raw=None)


def _recording_usage(stop: threading.Event, rows: list[int]):
    """A Claude-lane Attempt that keeps writing usage rows from its own
    thread until ``stop``: SQLite writes from two Attempts at once."""
    def work(db_, task_id, run):
        while not stop.is_set():
            db_.record_usage(run, task_id, "claude", [{"model": "m", "cost_usd": 0.001}])
            rows.append(run)
            stop.wait(0.005)
    return work


def test_spend_caps_hold_while_the_claude_lane_records_usage(cfg, db, genv):  # noqa: F811
    clock = Clock(datetime(2026, 10, 22, 12, 0, tzinfo=LA))
    _earlier_spend(cfg, db, 4.0, clock())  # $4 of the $5 day already spent
    # $1.00 a paid call: the first call reaches the daily cap.
    genv.setenv("FAKE_GEMINI_CALL_TOKENS", json.dumps(
        {"promptTokenCount": 0, "cachedContentTokenCount": 0, "candidatesTokenCount": 100_000}))
    genv.setenv("FAKE_GEMINI_STEP_DELAY", "0.3")
    c = _new(cfg, db, "Claude work")
    p1 = _new(cfg, db, "paid one", agent="gemini-api", task_type="summary")
    p2 = _new(cfg, db, "paid two", agent="gemini-api", task_type="summary")
    g = _new(cfg, db, "free Gemini", agent="gemini", task_type="summary")
    stop, rows = threading.Event(), []
    fake = LaneAttempts(real=frozenset({"gemini-api", "gemini"}),
                        on_start={c: _recording_usage(stop, rows)})
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake, clock=clock)
    try:
        d.__enter__()
        fake.wait_started(c, p1)
        # The paid Attempt crosses the daily cap while the Claude lane writes usage.
        _wait_state(db, p1, TaskState.PAUSED_SPEND)
        # The next paid Task is paused at the next pick, without a paid call;
        # the subscription path in the same lane runs.
        _wait_state(db, g, TaskState.DONE)
        assert db.task_state(p2) == TaskState.PAUSED_SPEND and db.attempts(p2) == []
        fake.wait_for("only the Claude lane busy", lambda: set(fake.active) == {c})
    finally:
        stop.set()
        fake.gate(c).set()
        d.__exit__(None, None, None)
    assert fake.violations == []
    assert db.task_state(c) == TaskState.DONE
    assert db.task_spend(p1) == pytest.approx(1.0)  # stopped at the call that reached it
    assert sum(r["cost_usd"] for r in db.spend_events()) == pytest.approx(5.0)
    why = db.transitions(p1)[-1]["reason"]
    assert "daily spend cap" in why
    assert db.get_task(p2)["resume_at"] == "2026-10-23T00:00:00-07:00"
    # Every usage row the Claude lane wrote meanwhile is there.
    assert len(rows) > 0 and len(db.usage_for_task(c)) == len(rows)


class HoldingClock(Clock):
    """A fixed clock that, once ``hold()`` is called, stops the queue
    runner's thread at its next reading until ``release()``: a way to order
    the runner's next pick against an Attempt thread."""

    def __init__(self, t: datetime):
        super().__init__(t)
        self._held: threading.Event | None = None
        self.blocked = threading.Event()

    def __call__(self) -> datetime:
        held = self._held
        if held is not None and not threading.current_thread().name.startswith("attempt-"):
            self.blocked.set()
            held.wait(20)
        return self.t

    def hold(self) -> None:
        self._held = threading.Event()

    def release(self) -> None:
        held, self._held = self._held, None
        held.set()


def test_the_next_pick_never_touches_a_task_whose_attempt_is_still_starting(cfg, db):
    """Until its Attempt moves it to `running`, a just-picked Task is still
    `queued`: the runner waits for that before its next pick, whose
    pre-pick work (spend caps here) would otherwise pause it under its own
    Attempt."""
    clock = HoldingClock(datetime(2026, 10, 22, 12, 0, tzinfo=LA))
    p = _new(cfg, db, "paid", agent="gemini-api", task_type="summary")
    c = _new(cfg, db, "Claude work")

    def slow_start(db_, task_id):
        # Stop the runner at its next pick, if it makes one now; the daily
        # cap is reached (by another Task) while this Attempt is starting.
        clock.hold()
        clock.blocked.wait(1.0)  # without the wait in start_next, it is blocked by now
        _earlier_spend(cfg, db_, 5.0, clock())
        clock.release()

    fake = LaneAttempts(before_running={p: slow_start})
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake, clock=clock)
    try:
        d.__enter__()
        fake.wait_started(p, c)
    finally:
        for t in (p, c):
            fake.gate(t).set()
        d.__exit__(None, None, None)
    assert not clock.blocked.is_set()  # no pick while it was starting
    assert [t["to_state"] for t in db.transitions(p)] == ["queued", "running", "done"]
    assert db.task_state(c) == TaskState.DONE


def test_paid_credit_rule_moves_an_urgent_task_within_the_gemini_lane(cfg, db):
    clock = Clock()
    usage.record_limit_hit(cfg, db, "gemini",
                           RateLimit("daily", iso(clock() + timedelta(hours=5))),
                           at=clock(), source="probe")
    c = _new(cfg, db, "Claude work")
    u = create_task(cfg, db, "Summarize lecture 3", agent="gemini", agent_override=False,
                    task_type="general", priority="urgent", long=False).spec.id
    w = create_task(cfg, db, "Summarize lecture 4", agent="gemini", agent_override=False,
                    task_type="general", long=False).spec.id
    fake = LaneAttempts()
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake, clock=clock)
    try:
        d.__enter__()
        fake.wait_started(c, u)
        assert dict(fake.started) == {c: "claude", u: "gemini-api"}
        # The one that can wait waits for Gemini's reset, on the subscription.
        assert db.get_task(w)["agent"] == "gemini" and db.task_state(w) == TaskState.QUEUED
        [dw] = db.paid_credit_decisions(w)
        assert dw["use_paid"] == 0
    finally:
        for t in (c, u, w):
            fake.gate(t).set()
        d.runner.stop()
        d.__exit__(None, None, None)
    assert fake.violations == []


# --------------------------------------------------------------------------
# Usage thresholds (§1.5) with both lanes busy
# --------------------------------------------------------------------------


def _learn_baseline(cfg, db, agent, at, cost):
    db.record_planning_usage(None, "probe", agent, [{"model": "m", "cost_usd": cost}],
                             at=iso(at - timedelta(minutes=5)))
    usage.record_limit_hit(cfg, db, agent, RateLimit("five_hour", iso(at + timedelta(minutes=30))),
                           at=at, source="probe")


def test_80_percent_on_claude_holds_only_the_claude_lane(cfg, db):
    clock = Clock()
    _learn_baseline(cfg, db, "claude", clock() - timedelta(days=1), 1.0)
    c1 = _new(cfg, db, "Claude first")
    g1 = _new(cfg, db, "Gemini first", agent="gemini")
    c2 = _new(cfg, db, "Claude second")
    g2 = _new(cfg, db, "Gemini second", agent="gemini")

    def cross_80(db_, task_id, run):  # the running Claude Task's own usage crosses 80%
        db_.record_usage(run, task_id, "claude", [{"model": "m", "cost_usd": 0.85}],
                         at=iso(clock()))

    fake = LaneAttempts(on_start={c1: cross_80})
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake, clock=clock)
    try:
        d.__enter__()
        fake.wait_started(c1, g1)
        fake.gate(c1).set()  # the running Task finishes; no new Claude Task starts
        _wait_state(db, c1, TaskState.DONE)
        fake.gate(g1).set()  # the Gemini lane carries on
        fake.wait_started(g2)
        assert db.task_state(c2) == TaskState.QUEUED
        assert usage.threshold_holds(cfg, db, clock()).all.keys() == {"claude"}
        fake.gate(g2).set()
        _wait_state(db, g2, TaskState.DONE)
        assert db.task_state(c2) == TaskState.QUEUED
    finally:
        for t in (c1, g1, c2, g2):
            fake.gate(t).set()
        d.runner.stop()
        d.__exit__(None, None, None)
    assert fake.violations == []


# --------------------------------------------------------------------------
# A free lane doesn't wait for the busy one
# --------------------------------------------------------------------------


def test_a_gemini_task_added_while_a_claude_task_runs_starts_at_once(cfg, db):
    c = _new(cfg, db, "long Claude Task")
    fake = LaneAttempts()
    d = _Daemon(cfg, FakeHost(), run_attempt_fn=fake, serve=True)
    try:
        d.__enter__()
        fake.wait_started(c)
        later = _new(cfg, db, "queued later", agent="gemini")  # as `orch add` would
        fake.wait_started(later)  # on the next poll, not once the Claude Task ends
        assert fake.running() == {c, later}
        c2 = _new(cfg, db, "another Claude Task")
        fake.gate(later).set()
        _wait_state(db, later, TaskState.DONE)
        assert db.task_state(c2) == TaskState.QUEUED  # its lane is still busy
    finally:
        for t in list(fake.gates) + [c]:
            fake.gate(t).set()
        d.runner.stop()
        d.__exit__(None, None, None)
    assert fake.violations == []


# --------------------------------------------------------------------------
# A real overlapping run (opt-in)
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("NIGHTSHIFT_REAL_AGENT") != "1"
    or shutil.which("claude") is None or shutil.which("gemini") is None,
    reason="real-agent test; set NIGHTSHIFT_REAL_AGENT=1 (needs claude, gemini and "
           "GEMINI_API_KEY)")
def test_two_lanes_real(tmp_path):
    """#45 on the real CLIs: a small Claude Task and a small Gemini Task
    queued together run at the same time, one in each lane; a second Claude
    Task waits for the first. Throwaway config and agents root.

        NIGHTSHIFT_REAL_AGENT=1 NIGHTSHIFT_CLAUDE_MODEL=haiku \\
          NIGHTSHIFT_GEMINI_MODEL=gemini-3.5-flash-lite \\
          .venv/bin/python -m pytest -s tests/test_lanes.py -k real
    """
    from nightshift.config import load_config
    from nightshift.daemon import QueueRunner
    from nightshift.db import Database
    from nightshift.timeutil import parse_iso

    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "agents": {"claude": {"model": os.environ.get("NIGHTSHIFT_CLAUDE_MODEL")},
                   "gemini": {"model": os.environ.get("NIGHTSHIFT_GEMINI_MODEL")}},
    }))
    (d / ".env").write_text(f"GEMINI_API_KEY={os.environ.get('GEMINI_API_KEY', '')}\n")
    (d / ".env").chmod(0o600)
    cfg_ = load_config(d, host=FakeHost())
    db_ = Database(cfg_.db_path, tz=cfg_.timezone)
    try:
        c1 = _new(cfg_, db_, "Write a three-line haiku about two lanes of traffic to "
                             "output/haiku.txt, then write report.md.")
        g1 = _new(cfg_, db_, "Write a two-sentence plain-text explanation of what a FIFO queue "
                             "is to output/notes.md, then write report.md.", agent="gemini")
        c2 = _new(cfg_, db_, "Write one sentence about patience to output/line.txt, then "
                             "write report.md.")
        log: list[str] = []
        runner = QueueRunner(cfg_, db_, FakeHost(), log=log.append, max_running=2)
        started = time.monotonic()
        ran = runner.drain(poll=1)
        took = time.monotonic() - started
        print("\n".join(log))
        runs = {t: db_.attempts(t) for t in (c1, g1, c2)}
        for t, attempts in runs.items():
            for a in attempts:
                print(f"{t} {a['agent']} {a['outcome']} {a['started_at']} -> {a['ended_at']} "
                      f"{a['end_reason']}")
        print(f"drain took {took:.0f}s; ran {ran}")
        assert ran[:2] == [c1, g1]
        first = {t: (parse_iso(runs[t][0]["started_at"]), parse_iso(runs[t][0]["ended_at"]))
                 for t in (c1, g1, c2)}
        # The Gemini Task ran while the first Claude Task did; the second
        # Claude Task only after the first ended.
        assert first[g1][0] < first[c1][1] and first[c1][0] < first[g1][1]
        assert first[c2][0] >= first[c1][1]
    finally:
        db_.close()
