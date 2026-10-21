"""Priority, Effective priority, deadlines and time windows (§2.3), on a fake clock."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli
from nightshift import priority as prio
from nightshift.config import ConfigError, load_config
from nightshift.daemon import QueueRunner
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.workspace import read_task_yaml

from test_daemon import FakeAttempts
from test_planning import planner  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


def row(**kw):
    base = {
        "id": "t", "priority": "normal", "deadline": None, "run_at": None, "long": 0,
        "created_at": "2026-10-20T10:00:00-07:00", "state": "queued",
    }
    return base | kw


# -- Effective priority -------------------------------------------------------


@pytest.mark.parametrize(
    "assigned, deadline, expected",
    [
        ("normal", None, "normal"),
        ("background", None, "background"),
        ("urgent", None, "urgent"),
        ("normal", la(2026, 10, 22, 12, 0, 1), "normal"),  # 24h and 1s away
        ("normal", la(2026, 10, 22, 12, 0, 0), "urgent"),  # exactly 24h
        ("background", la(2026, 10, 21, 18, 0), "urgent"),
        ("normal", la(2026, 10, 20, 9, 0), "urgent"),  # already passed
        ("urgent", la(2026, 12, 1), "urgent"),  # never lowered
    ],
)
def test_effective_priority(assigned, deadline, expected):
    now = la(2026, 10, 21, 12, 0)
    d = deadline.isoformat() if deadline else None
    assert prio.effective_priority(assigned, d, now) == expected


def test_effective_priority_is_recalculated_as_time_passes(cfg):
    r = row(deadline=la(2026, 10, 23, 9, 0).isoformat())
    assert prio.evaluate(cfg, r, la(2026, 10, 22, 8, 59)).effective == "normal"
    assert prio.evaluate(cfg, r, la(2026, 10, 22, 9, 0)).effective == "urgent"


def test_deadline_compares_absolute_time_across_zones(cfg):
    # Deadline given in UTC; the clock in LA. 23h apart -> urgent.
    r = row(deadline="2026-10-22T18:00:00+00:00")
    assert prio.evaluate(cfg, r, la(2026, 10, 21, 12, 0)).effective == "urgent"


# -- Ordering -----------------------------------------------------------------


def _ids(cfg, rows, now):
    return [ev.task_id for ev in prio.order(cfg, rows, now)]


def test_urgent_jumps_normal_in_order_background_last(cfg):
    now = la(2026, 10, 21, 3, 0)
    rows = [
        row(id="bg", priority="background", created_at="2026-10-20T01:00:00-07:00"),
        row(id="n1", created_at="2026-10-20T02:00:00-07:00"),
        row(id="n2", created_at="2026-10-20T03:00:00-07:00"),
        row(id="u", priority="urgent", created_at="2026-10-20T04:00:00-07:00"),
        row(id="n3", created_at="2026-10-20T05:00:00-07:00"),
    ]
    assert _ids(cfg, rows, now) == ["u", "n1", "n2", "n3", "bg"]


def test_deadline_raises_into_urgent_lane_nearest_deadline_first(cfg):
    now = la(2026, 10, 21, 3, 0)
    rows = [
        row(id="u-old", priority="urgent", created_at="2026-10-19T00:00:00-07:00"),
        row(id="n-far", deadline=la(2026, 10, 30).isoformat()),
        row(id="bg-soon", priority="background", deadline=la(2026, 10, 21, 20, 0).isoformat()),
        row(id="n-sooner", deadline=la(2026, 10, 21, 10, 0).isoformat()),
    ]
    # Urgent lane: nearest deadline first, no deadline last; then normal in order.
    assert _ids(cfg, rows, now) == ["n-sooner", "bg-soon", "u-old", "n-far"]


def test_background_runs_only_when_nothing_else_waits(cfg):
    now = la(2026, 10, 21, 3, 0)
    bg = row(id="bg", priority="background", created_at="2026-10-01T00:00:00-07:00")
    n = row(id="n", created_at="2026-10-21T02:00:00-07:00")
    assert prio.pick_next(cfg, [bg, n], now)["id"] == "n"
    assert prio.pick_next(cfg, [bg], now)["id"] == "bg"
    assert prio.pick_next(cfg, [], now) is None


# -- Gating -------------------------------------------------------------------


@pytest.mark.parametrize(
    "hour, minute, allowed",
    [(0, 0, True), (3, 30, True), (6, 59, True), (7, 0, False), (12, 0, False), (23, 59, False)],
)
def test_long_tasks_start_only_between_midnight_and_7am(cfg, hour, minute, allowed):
    long_row = row(id="L", long=1)
    now = la(2026, 10, 21, hour, minute)
    assert (prio.evaluate(cfg, long_row, now).blocked is None) is allowed
    assert (prio.pick_next(cfg, [long_row], now) is not None) is allowed
    # Short Tasks are never gated.
    assert prio.pick_next(cfg, [row(id="S")], now)["id"] == "S"


def test_urgent_long_tasks_ignore_the_window(cfg):
    noon = la(2026, 10, 21, 12, 0)
    assert prio.pick_next(cfg, [row(long=1, priority="urgent")], noon) is not None
    # Raised to urgent by a deadline counts too.
    near = row(long=1, deadline=la(2026, 10, 21, 20, 0).isoformat())
    assert prio.pick_next(cfg, [near], noon) is not None


def test_window_uses_los_angeles_time_whatever_zone_the_clock_is_in(cfg):
    assert cfg.timezone == "America/Los_Angeles"
    # 09:00 UTC is 02:00 in LA (PDT): inside the window.
    utc = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
    assert prio.in_long_window(cfg, utc)
    # 03:00 UTC is 20:00 the previous day in LA: outside.
    assert not prio.in_long_window(cfg, datetime(2026, 10, 21, 3, 0, tzinfo=timezone.utc))
    # Winter (PST, UTC-8): 07:30 UTC is 23:30 in LA.
    assert not prio.in_long_window(cfg, datetime(2026, 12, 1, 7, 30, tzinfo=timezone.utc))
    assert prio.in_long_window(cfg, datetime(2026, 12, 1, 8, 30, tzinfo=timezone.utc))


def test_gated_task_does_not_block_others(cfg):
    noon = la(2026, 10, 21, 12, 0)
    rows = [
        row(id="L", long=1, created_at="2026-10-01T00:00:00-07:00"),
        row(id="bg", priority="background"),
    ]
    assert prio.pick_next(cfg, rows, noon)["id"] == "bg"


def test_run_at_holds_a_task_until_its_time(cfg):
    r = row(id="A", priority="urgent", run_at=la(2026, 10, 21, 14, 0).isoformat())
    assert prio.pick_next(cfg, [r], la(2026, 10, 21, 13, 59)) is None
    assert "waiting until" in prio.evaluate(cfg, r, la(2026, 10, 21, 13, 59)).blocked
    assert prio.pick_next(cfg, [r], la(2026, 10, 21, 14, 0))["id"] == "A"


def test_run_at_and_long_window_both_apply(cfg):
    r = row(long=1, run_at=la(2026, 10, 21, 22, 0).isoformat())
    assert prio.pick_next(cfg, [r], la(2026, 10, 21, 22, 30)) is None  # past --at, not in window
    assert prio.pick_next(cfg, [r], la(2026, 10, 22, 0, 0)) is not None


def test_window_is_configurable(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "a")},
        "queue": {"long_window_start": "22:00", "long_window_end": "06:00"},
    }))
    c = load_config(d, host=FakeHost())
    assert prio.in_long_window(c, la(2026, 10, 21, 23, 0))
    assert prio.in_long_window(c, la(2026, 10, 21, 5, 59))
    assert not prio.in_long_window(c, la(2026, 10, 21, 6, 0))
    (d / "config.yaml").write_text(yaml.safe_dump({"queue": {"long_window_end": "7am"}}))
    with pytest.raises(ConfigError, match="long_window_end"):
        load_config(d, host=FakeHost())


# -- Parsing ------------------------------------------------------------------

NOW = la(2026, 10, 21, 15, 0)


def test_parse_deadline():
    tz = "America/Los_Angeles"
    assert prio.parse_deadline("2026-10-25", NOW, tz) == "2026-10-25T23:59:59-07:00"
    # After the DST change the offset follows LA.
    assert prio.parse_deadline("2026-11-02 09:30", NOW, tz) == "2026-11-02T09:30:00-08:00"
    assert prio.parse_deadline("2026-10-22T12:00:00Z", NOW, tz) == "2026-10-22T05:00:00-07:00"
    for bad in ("tomorrow", "2026-13-01", "2026-10-20"):
        with pytest.raises(ConfigError):
            prio.parse_deadline(bad, NOW, tz)


def test_parse_at():
    tz = "America/Los_Angeles"
    assert prio.parse_at("23:30", NOW, tz) == "2026-10-21T23:30:00-07:00"  # later today
    assert prio.parse_at("02:00", NOW, tz) == "2026-10-22T02:00:00-07:00"  # tomorrow
    assert prio.parse_at("15:00", NOW, tz) == "2026-10-22T15:00:00-07:00"  # now -> tomorrow
    # A UTC clock still means LA wall-clock time.
    utc_now = NOW.astimezone(timezone.utc)
    assert prio.parse_at("02:00", utc_now, tz) == "2026-10-22T02:00:00-07:00"
    assert prio.parse_at("2026-10-30 01:00", NOW, tz) == "2026-10-30T01:00:00-07:00"
    for bad in ("2am", "25:00", "2026-10-30", "2026-10-01 01:00"):
        with pytest.raises(ConfigError):
            prio.parse_at(bad, NOW, tz)


# -- Tasks, daemon and CLI ----------------------------------------------------


def test_long_defaults_from_task_type(cfg, db):
    assert create_task(cfg, db, "build it", task_type="coding").spec.long is True
    assert create_task(cfg, db, "read up", task_type="research").spec.long is True
    assert create_task(cfg, db, "haiku", task_type="comedy").spec.long is False
    assert create_task(cfg, db, "x", task_type="coding", long=False).spec.long is False


def test_daemon_orders_and_gates_with_fake_clock(cfg, db):
    host = FakeHost()
    clock = Clock(la(2026, 10, 21, 12, 0))  # midday
    long_t = create_task(cfg, db, "refactor", task_type="coding").spec.id
    bg = create_task(cfg, db, "tidy", priority="background").spec.id
    soon = create_task(
        cfg, db, "apply", deadline=la(2026, 10, 22, 11, 0).isoformat()
    ).spec.id
    later = create_task(cfg, db, "at four", run_at=la(2026, 10, 21, 16, 0).isoformat()).spec.id
    n = create_task(cfg, db, "normal").spec.id
    fake = FakeAttempts(host)
    runner = QueueRunner(cfg, db, host, run_attempt_fn=fake, clock=clock)

    assert runner.drain() == [soon, n, bg]  # deadline-urgent first; long and --at held back
    assert db.task_state(long_t) == TaskState.QUEUED and db.task_state(later) == TaskState.QUEUED
    clock.t = la(2026, 10, 21, 16, 0)
    assert runner.drain() == [later]
    clock.t = la(2026, 10, 22, 0, 5)
    assert runner.drain() == [long_t]
    assert host.awake_log == ["hold", "release"] * 5


def test_effective_priority_is_never_stored(cfg, db):
    new = create_task(cfg, db, "apply", deadline=la(2026, 10, 21, 20, 0).isoformat())
    host = FakeHost()
    runner = QueueRunner(cfg, db, host, run_attempt_fn=FakeAttempts(host),
                         clock=Clock(la(2026, 10, 21, 12, 0)))
    assert runner.drain() == [new.spec.id]
    assert db.get_task(new.spec.id)["priority"] == "normal"
    assert read_task_yaml(new.folder).priority == "normal"


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


@pytest.fixture
def fake_clock(monkeypatch):
    c = Clock(la(2026, 10, 21, 15, 0))
    monkeypatch.setattr(cli, "clock", c)
    return c


def test_add_with_priority_deadline_and_at(config_dir, db, planner, fake_clock):  # noqa: F811
    r = _orch(config_dir, "add", "Write a haiku", "--priority", "background",
              "--deadline", "2026-10-25", "--at", "02:00", "--yes")
    assert r.exit_code == 0, r.output
    [t] = db.list_tasks()
    assert t["priority"] == "background"
    assert t["deadline"] == "2026-10-25T23:59:59-07:00"
    assert t["run_at"] == "2026-10-22T02:00:00-07:00"
    spec = read_task_yaml(Path(t["folder"]))
    assert (spec.priority, spec.deadline, spec.run_at) == (
        "background", t["deadline"], t["run_at"])
    assert "no earlier than 2026-10-22T02:00:00-07:00" in r.output


def test_add_rejects_bad_times_before_planning(config_dir, db, planner, fake_clock):  # noqa: F811
    r = _orch(config_dir, "add", "x", "--deadline", "someday", "--yes")
    assert r.exit_code == 1 and "--deadline" in r.output and "Nothing was queued" in r.output
    r = _orch(config_dir, "add", "x", "--at", "2026-10-01 01:00", "--yes")
    assert r.exit_code == 1 and "already passed" in r.output
    assert db.list_tasks() == [] and db.planning_usage() == []


def test_orch_priority(config_dir, cfg, db, fake_clock):
    new = create_task(cfg, db, "apply", deadline=la(2026, 10, 22, 9, 0).isoformat())
    tid = new.spec.id
    r = _orch(config_dir, "priority", tid, "background")
    assert r.exit_code == 0, r.output
    assert "normal -> background" in r.output and "effective urgent" in r.output
    assert db.get_task(tid)["priority"] == "background"
    assert read_task_yaml(new.folder).priority == "background"
    assert "via orch priority" in (new.folder / "logs" / "nightshift.log").read_text()
    # Not a state change.
    assert [t["to_state"] for t in db.transitions(tid)] == ["queued"]

    r = _orch(config_dir, "show", tid)
    assert "effective_priority" in r.output and "urgent" in r.output
    r = _orch(config_dir, "list")
    assert "background>urgent" in r.output

    assert _orch(config_dir, "priority", tid, "high").exit_code == 2
    assert "no Task" in _orch(config_dir, "priority", "zzzz", "urgent").output
    db.transition(tid, TaskState.CANCELLED, "test")
    r = _orch(config_dir, "priority", tid, "urgent")
    assert r.exit_code == 1 and "final" in r.output


def test_show_explains_why_a_queued_task_waits(config_dir, cfg, db, fake_clock):
    tid = create_task(cfg, db, "refactor", task_type="coding").spec.id
    r = _orch(config_dir, "show", tid)
    assert "Waiting: long Task: starts only between 00:00 and 07:00" in r.output


def test_unknown_timezone_is_a_config_error(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({"timezone": "Mars/Olympus"}))
    with pytest.raises(ConfigError, match="timezone"):
        load_config(d, host=FakeHost())
