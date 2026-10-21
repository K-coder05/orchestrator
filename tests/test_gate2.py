"""Gate Test 2 (§3.3), simulated: the daily 6:30am `posting-scan` Schedule.

The real gate needs several mornings and a real lid-close sleep. Here the
days pass on a fake clock and the sleep is a `FakeHost` whose awake clock
stops while the wall clock jumps (what `CLOCK_UPTIME_RAW` does on a Mac).
Everything else is real: the built-in posting-scan template and its
standard Schedule, the scheduler, the daemon's queue runner, the fake
`claude` CLI streaming through the real adapter, the merge_tracker
post-hook and report.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightshift import cli, schedules, templates, tracker
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.states import TaskState
from nightshift.workspace import read_task_yaml

from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)
from test_tracker import _row, scan_env, scfg, scfg_dir  # noqa: F401  (pytest fixtures)

LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


class Mac:
    """Wall clock plus awake clock: awake time stops while the lid is shut."""

    def __init__(self, t: datetime):
        self.clock = Clock(t)
        self.host = FakeHost(awake_clock=1000.0)

    def awake(self, **kw) -> None:
        d = timedelta(**kw)
        self.clock.t += d
        self.host.awake_clock += d.total_seconds()

    def sleep_until(self, t: datetime) -> None:
        self.clock.t = t
        self.host.awake_clock += 1  # a second of awake time to wake up


@pytest.fixture
def gate(scfg, scfg_dir, scan_env, monkeypatch):  # noqa: F811
    """The posting-scan Schedule set up the standard way on Oct 20, noon."""
    mac = Mac(la(2026, 10, 20, 12, 0))
    monkeypatch.setattr(cli, "clock", mac.clock)
    res = CliRunner().invoke(cli.main, ["--config-dir", str(scfg_dir), "schedule", "add",
                                        "posting-scan"])
    assert res.exit_code == 0, res.output
    db = Database(scfg.db_path, tz=scfg.timezone)
    logs: list[str] = []
    runner = QueueRunner(scfg, db, mac.host, adapter_provider=fake_provider, clock=mac.clock,
                         log=logs.append)
    runner.check_wake()  # the daemon's first look only sets the mark
    yield mac, db, runner, logs, res.output
    db.close()


def _report_new(row) -> str:
    return (Path(row["folder"]) / "report.md").read_text().split("### New postings", 1)[1]


def test_standard_schedule_is_the_spec_rules(gate):
    _, db, _, _, out = gate
    assert ("Schedule posting-scan added (enabled): 30 6 * * * (America/Los_Angeles), "
            "template posting-scan, catch-up within 12h, else skip, overlap skip") in out
    assert "Next fire: 2026-10-21T06:30:00-07:00" in out
    s = schedules.get(db, "posting-scan")
    assert (s.cron.expr, s.catch_up.kind, s.catch_up.hours, s.overlap) == (
        "30 6 * * *", schedules.CATCH_UP_WITHIN, 12.0, schedules.OVERLAP_SKIP)
    assert s.request == "Daily scan for new internship postings"


def test_gate_two_runs_and_a_fire_missed_while_asleep(gate, scfg, scan_env):  # noqa: F811
    mac, db, runner, logs, _ = gate
    a = _row()
    b = _row(company="Globex", role="Backend Engineer Intern", url="https://globex.com/jobs/7",
             location="Austin, TX", notes="low-priority location")
    c = _row(company="Initech", url="https://initech.com/jobs/swe-intern", location="Remote (US)")

    # Day 1, awake: nothing before 6:30; the 6:30 fire runs on the next poll.
    mac.sleep_until(la(2026, 10, 21, 6, 0))
    runner.check_wake()
    scan_env(a, b)
    assert runner.run_next() is None
    mac.awake(minutes=31)
    first = runner.run_next()
    assert first is not None
    row1 = db.get_task(first)
    assert row1["state"] == TaskState.DONE and row1["schedule_name"] == "posting-scan"
    assert Path(row1["folder"]) == scfg.recurring_dir / "posting-scan" / "2026-10-21"
    assert read_task_yaml(Path(row1["folder"])).approved is True
    new1 = _report_new(row1)
    assert "2 new postings appended" in new1 and "Acme" in new1 and "Globex" in new1

    # Night 1 -> day 2: the lid is shut at 23:00 and opened at 8:15, after
    # the 6:30 fire. The daemon sees the wake and catches the fire up once.
    mac.awake(hours=16)
    runner.check_wake()
    scan_env({**a, "posting_url": a["posting_url"] + "?utm_source=github"}, b, c)
    mac.sleep_until(la(2026, 10, 22, 8, 15))
    assert runner.check_wake() is True
    assert any("woke from sleep" in line for line in logs)
    caught = [line for line in logs if "after wake: Schedule posting-scan" in line]
    assert len(caught) == 1 and "fire at 2026-10-22T06:30:00-07:00" in caught[0]
    second = runner.run_next()
    assert second is not None and second != first
    row2 = db.get_task(second)
    assert row2["state"] == TaskState.DONE
    assert Path(row2["folder"]) == scfg.recurring_dir / "posting-scan" / "2026-10-22"

    # No duplicate Tracker rows after two runs in a row.
    rows = tracker.read_tracker(scfg.tracker_path)
    assert [r["company"] for r in rows] == ["Acme", "Globex", "Initech"]
    assert len({tracker.normalize_url(r["posting_url"]) for r in rows}) == len(rows)
    assert len({tracker.row_key(r) for r in rows}) == len(rows)

    # The second run reports only New postings: Initech, with the two
    # known postings as a count.
    new2 = _report_new(row2)
    assert "1 new posting appended" in new2 and "Initech" in new2
    assert "Acme" not in new2 and "Globex" not in new2
    assert "Dropped as duplicates: 2." in new2

    # Exactly once: more polls, another wake check and a daemon restart
    # later that morning create nothing.
    mac.awake(minutes=30)
    assert runner.run_next() is None
    assert runner.check_wake() is False
    QueueRunner(scfg, db, mac.host, adapter_provider=fake_provider,
                clock=mac.clock).serve(exit_when_idle=True)
    occ = db.occurrences("posting-scan")
    assert [r["id"] for r in occ] == [first, second]
    runs = [(r["trigger"], r["outcome"]) for r in db.schedule_runs("posting-scan")]
    assert runs == [("poll", "created"), ("wake", "created")]


def test_gate_days_asleep_collapse_into_one_occurrence(gate, scan_env):  # noqa: F811
    mac, db, runner, logs, _ = gate
    scan_env(_row())
    # Shut on Oct 20 at 13:00, opened Oct 23 at 7:00: three 6:30 fires missed.
    mac.awake(hours=1)
    runner.check_wake()
    mac.sleep_until(la(2026, 10, 23, 7, 0))
    assert runner.check_wake() is True
    assert runner.run_next() is not None
    assert runner.run_next() is None
    [run] = db.schedule_runs("posting-scan")
    assert (run["trigger"], run["outcome"], run["fires"]) == ("wake", "created", 3)
    assert run["fire_at"] == "2026-10-23T06:30:00-07:00"
    assert len(db.occurrences("posting-scan")) == 1


def test_gate_wake_more_than_12h_late_skips(gate, scan_env):  # noqa: F811
    mac, db, runner, logs, _ = gate
    scan_env(_row())
    mac.awake(hours=1)
    runner.check_wake()
    mac.sleep_until(la(2026, 10, 21, 19, 0))  # 12.5h after the 6:30 fire
    runner.check_wake()
    assert runner.run_next() is None
    [run] = db.schedule_runs("posting-scan")
    assert run["outcome"] == schedules.SKIPPED_CATCH_UP and run["task_id"] is None
    assert "missed by 12.5h" in run["detail"]
    # The next morning fires normally.
    mac.awake(hours=11, minutes=31)
    assert runner.run_next() is not None
    assert [r["outcome"] for r in db.schedule_runs("posting-scan")] == [
        schedules.SKIPPED_CATCH_UP, schedules.CREATED]


def test_gate_fire_while_previous_scan_unfinished_is_skipped(gate, scan_env):  # noqa: F811
    mac, db, runner, logs, _ = gate
    mac.sleep_until(la(2026, 10, 21, 6, 31))
    assert runner.check_wake() is True
    [run] = db.schedule_runs("posting-scan")
    assert run["outcome"] == schedules.CREATED
    # Never run (e.g. the queue was busy all day): still unfinished next morning.
    mac.sleep_until(la(2026, 10, 22, 6, 31))
    runner.check_wake()
    runs = db.schedule_runs("posting-scan")
    assert runs[-1]["outcome"] == schedules.SKIPPED_OVERLAP
    assert len(db.occurrences("posting-scan")) == 1


def test_template_schedule_block_is_validated(scfg, tmp_path):  # noqa: F811
    base = {"task_type": "general", "agent": "claude", "estimated_minutes": 5,
            "max_runtime_minutes": 10, "long": False, "internet": "off", "prompt": "x"}
    for bad, msg in ((["30 6 * * *"], "schedule is"),
                     ({"cron": "30 6 * * *"}, "schedule is"),
                     ({"cron": "nope", "catch_up": "12h"}, "schedule:"),
                     ({"cron": "30 6 * * *", "catch_up": "soon"}, "Catch-up"),
                     ({"cron": "30 6 * * *", "catch_up": "12h", "overlap": "stack"}, "overlap"),
                     ({"cron": "30 6 * * *", "catch_up": "12h", "when": 1}, "schedule is")):
        with pytest.raises(templates.TemplateError, match=msg):
            templates.parse(scfg, "t", tmp_path / "t.yaml", {**base, "schedule": bad})
    tpl = templates.parse(scfg, "t", tmp_path / "t.yaml",
                          {**base, "schedule": {"cron": "@daily", "catch_up": "next-wake"}})
    assert tpl.schedule == {"cron": "@daily", "catch_up": "next-wake", "overlap": "skip",
                            "request": ""}


def test_schedule_add_without_rules_needs_a_standard_schedule(scfg, tmp_path):  # noqa: F811
    db = Database(scfg.db_path, tz=scfg.timezone)
    try:
        with pytest.raises(schedules.ScheduleError, match="--cron and --catch-up are required"):
            schedules.add(scfg, db, "tailor", template="resume-tailor", at=la(2026, 10, 20, 12))
        with pytest.raises(schedules.ScheduleError, match="no template 'nothing'"):
            schedules.add(scfg, db, "nothing", at=la(2026, 10, 20, 12))
        # Flags override the template's standard rules.
        s = schedules.add(scfg, db, "posting-scan", cron_rule="0 7 * * 1-5", overlap="replace",
                          request="Weekday scan", at=la(2026, 10, 20, 12))
        assert (s.template, s.cron.expr, str(s.catch_up), s.overlap, s.request) == (
            "posting-scan", "0 7 * * 1-5", "within 12h, else skip", "replace", "Weekday scan")
    finally:
        db.close()
