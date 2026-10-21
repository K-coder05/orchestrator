"""Schedules (§2.5): cron rules, Catch-up and Overlap rules, the daemon's
catch-up on start and wake, and `orch schedule`. All with a fake clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, cron, schedules
from nightshift.config import load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.states import TaskState
from nightshift.workspace import read_task_yaml

from test_daemon import FakeAttempts

LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> datetime:
        self.t = self.t + timedelta(**kw)
        return self.t


TEMPLATE = {
    "description": "test scan",
    "task_type": "scraping",
    "agent": "claude",
    # Over the 30-minute plan threshold: Occurrences are pre-approved anyway.
    "estimated_minutes": 45,
    "max_runtime_minutes": 30,
    "long": False,
    "internet": "on",
    "prompt": "Scan the postings. Request: {{request}}",
}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "scan.yaml").write_text(yaml.safe_dump(TEMPLATE))
    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "templates": {"dir": str(tdir)},
    }))
    return d


@pytest.fixture
def cfg(config_dir):
    return load_config(config_dir, host=FakeHost())


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path, tz=cfg.timezone)
    yield d
    d.close()


def add(cfg, db, at, name="scan", **kw):
    kw.setdefault("template", "scan")
    kw.setdefault("cron_rule", "30 6 * * *")
    kw.setdefault("catch_up", "within 12h")
    return schedules.add(cfg, db, name, at=at, **kw)


# --------------------------------------------------------------------------
# Cron rules
# --------------------------------------------------------------------------


def test_cron_parses_fields_names_steps_and_macros():
    c = cron.parse("*/15 9-17 * jan,MAR mon-fri")
    assert c.minutes == (0, 15, 30, 45)
    assert c.hours == tuple(range(9, 18))
    assert c.months == {1, 3}
    assert c.dows == {1, 2, 3, 4, 5}
    assert cron.parse("0 0 * * 7").dows == {0}  # 7 is Sunday too
    assert cron.parse("5/20 * * * *").minutes == (5, 25, 45)
    assert cron.parse("@daily").minutes == (0,) and cron.parse("@daily").hours == (0,)
    assert cron.parse("  30   6 * *  * ").expr == "30 6 * * *"


@pytest.mark.parametrize("bad", [
    "30 6 * *", "60 6 * * *", "30 24 * * *", "* * 0 * *", "*/0 * * * *", "5-1 * * * *",
    "@sometimes", "x 6 * * *", "", "0 0 31 2 *",  # Feb 31 never fires
])
def test_cron_rejects_bad_rules(bad):
    with pytest.raises(cron.CronError):
        cron.parse(bad)


def test_cron_next_after_daily_and_dst():
    c = cron.parse("30 6 * * *")
    assert c.next_after(la(2026, 10, 21, 6, 0), LA) == la(2026, 10, 21, 6, 30)
    # Strictly after: a fire exactly now is not next.
    assert c.next_after(la(2026, 10, 21, 6, 30), LA) == la(2026, 10, 22, 6, 30)
    # 6:30 wall time on both sides of the November DST change.
    nov2 = c.next_after(la(2026, 11, 1, 7, 0), LA)
    assert (nov2.hour, nov2.minute, nov2.utcoffset()) == (6, 30, timedelta(hours=-8))
    # Spring-forward gap: 2:30 doesn't exist on 2026-03-08; it fires just after.
    gap = cron.parse("30 2 * * *").next_after(la(2026, 3, 8, 0, 0), LA)
    assert gap.astimezone(LA).strftime("%H:%M %z") == "03:30 -0700"
    # Fall back: 1:30 happens twice on 2026-11-01; it fires once.
    n, _ = cron.parse("30 1 * * *").fires_between(la(2026, 10, 31, 12, 0), la(2026, 11, 1, 12, 0),
                                                  LA)
    assert n == 1


def test_cron_day_of_month_or_day_of_week_when_both_restricted():
    c = cron.parse("0 9 1 * mon")  # the 1st, or any Monday
    assert c.matches_day(la(2026, 10, 1).date())  # Thursday the 1st
    assert c.matches_day(la(2026, 10, 19).date())  # a Monday
    assert not c.matches_day(la(2026, 10, 20).date())
    every_2_days = cron.parse("0 2 */2 * *")
    assert every_2_days.matches_day(la(2026, 10, 21).date())
    assert not every_2_days.matches_day(la(2026, 10, 22).date())


def test_cron_fires_between_counts_and_latest():
    c = cron.parse("30 6 * * *")
    n, latest = c.fires_between(la(2026, 10, 18, 7, 0), la(2026, 10, 21, 8, 0), LA)
    assert (n, latest) == (3, la(2026, 10, 21, 6, 30))
    assert c.fires_between(la(2026, 10, 21, 7, 0), la(2026, 10, 21, 8, 0), LA) == (0, None)
    # Inclusive of `until`, exclusive of `after`.
    assert c.fires_between(la(2026, 10, 21, 6, 30), la(2026, 10, 22, 6, 30), LA)[0] == 1


# --------------------------------------------------------------------------
# Catch-up rules
# --------------------------------------------------------------------------


@pytest.mark.parametrize("text,kind,hours", [
    ("within 12h", "within", 12), ("within:12", "within", 12), ("12h", "within", 12),
    ("within 12 hours, else skip", "within", 12), ("1.5h", "within", 1.5),
    ("next-wake", "next_wake", None), ("next wake", "next_wake", None),
    ("run on next wake", "next_wake", None), ("NEXT_WAKE", "next_wake", None),
])
def test_parse_catch_up(text, kind, hours):
    assert schedules.parse_catch_up(text) == schedules.CatchUp(kind, hours)


@pytest.mark.parametrize("bad", ["", "soon", "within 0h", "within -3h", "12 days"])
def test_parse_catch_up_rejects(bad):
    with pytest.raises(schedules.ScheduleError):
        schedules.parse_catch_up(bad)


# --------------------------------------------------------------------------
# Adding, enabling, removing
# --------------------------------------------------------------------------


def test_add_validates_everything_up_front(cfg, db):
    at = la(2026, 10, 21, 7, 0)
    for kw, msg in [
        ({"name": "Bad Name"}, "bad Schedule name"),
        ({"cron_rule": "30 6 * *"}, "bad cron rule"),
        ({"catch_up": "sometime"}, "bad Catch-up rule"),
        ({"template": "nope"}, "no template 'nope'"),
        ({"overlap": "stack"}, "Overlap rule"),
        ({"agent": "gpt"}, "unknown agent"),
    ]:
        with pytest.raises(schedules.ScheduleError, match=msg):
            add(cfg, db, at, **kw)
    assert db.schedules() == []
    add(cfg, db, at)
    with pytest.raises(schedules.ScheduleError, match="already exists"):
        add(cfg, db, at)


def test_fires_before_the_schedule_was_added_never_count(cfg, db):
    add(cfg, db, la(2026, 10, 21, 7, 0))  # 6:30 today is already past
    assert schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 23, 0)) == []
    assert db.list_tasks() == []
    s = schedules.get(db, "scan")
    assert schedules.next_fire(cfg, s, la(2026, 10, 21, 23, 0)) == la(2026, 10, 22, 6, 30)


# --------------------------------------------------------------------------
# Firing: Occurrences
# --------------------------------------------------------------------------


def test_on_time_fire_creates_one_pre_approved_occurrence(cfg, db):
    add(cfg, db, la(2026, 10, 21, 7, 0), request="interns 2027")
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 30, 4))
    assert [d.outcome for d in done] == ["created"]
    row = db.get_task(done[0].task_id)
    assert row["state"] == "queued"
    assert row["approved"] == 1 and row["approved_at"]  # pre-approved (§2.4), despite 45 min
    assert row["schedule_name"] == "scan" and row["template"] == "scan"
    folder = Path(row["folder"])
    assert folder == cfg.agents_root / "recurring" / "scan" / "2026-10-22"
    spec = read_task_yaml(folder)
    assert spec.schedule_name == "scan" and spec.approved is True
    assert spec.prompt == "Scan the postings. Request: interns 2027"
    assert spec.title == "scan 2026-10-22"
    for name in ("task.yaml", "plan.md", "progress.md", "report.md", "output", "logs"):
        assert (folder / name).exists()
    assert "Occurrence of Schedule scan" in (folder / "logs" / "nightshift.log").read_text()
    # Same time again (another poll): nothing more.
    assert schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 30, 9)) == []
    assert len(db.list_tasks()) == 1
    assert db.transitions(row["id"])[0]["reason"].startswith("Occurrence of Schedule scan")


def test_several_missed_fires_collapse_into_one_occurrence(cfg, db):
    add(cfg, db, la(2026, 10, 18, 7, 0), catch_up="next-wake")
    # The laptop slept from the 18th to 8am on the 21st: fires on 19, 20, 21.
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 8, 0), trigger="wake")
    assert len(done) == 1
    d = done[0]
    assert (d.outcome, d.fires, d.fire_at) == ("created", 3, "2026-10-21T06:30:00-07:00")
    assert len(db.list_tasks()) == 1
    run = db.schedule_runs("scan")[-1]
    assert (run["fires"], run["trigger"], run["outcome"]) == (3, "wake", "created")
    assert schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 9, 0)) == []


def test_within_rule_runs_a_recent_missed_fire(cfg, db):
    add(cfg, db, la(2026, 10, 20, 7, 0), catch_up="within 12h")
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 18, 29))  # 11h59m late
    assert [d.outcome for d in done] == ["created"]


def test_within_rule_skips_a_fire_missed_by_too_long(cfg, db):
    add(cfg, db, la(2026, 10, 20, 7, 0), catch_up="within 12h")
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 18, 31))  # 12h01m late
    assert [d.outcome for d in done] == ["skipped_catch_up"]
    assert "missed by 12.0h" in done[0].detail
    assert db.list_tasks() == []
    # Skipped once, then the next fire runs normally.
    assert schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 20, 0)) == []
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 31))
    assert [d.outcome for d in done] == ["created"]


def test_within_rule_judges_the_latest_missed_fire(cfg, db):
    # Asleep 3 days, woken 2h after the latest fire: the older fires are
    # far beyond 12h but collapse into the recent one, which runs.
    add(cfg, db, la(2026, 10, 17, 7, 0), catch_up="within 12h")
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 8, 30))
    assert [(d.outcome, d.fires) for d in done] == [("created", 4)]


def test_next_wake_runs_however_late(cfg, db):
    add(cfg, db, la(2026, 10, 1, 7, 0), catch_up="next-wake", cron_rule="0 2 */2 * *")
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 18, 0), trigger="wake")
    assert [d.outcome for d in done] == ["created"]
    assert done[0].fire_at == "2026-10-21T02:00:00-07:00"


# --------------------------------------------------------------------------
# Overlap rules
# --------------------------------------------------------------------------


def test_overlap_skip_logs_the_skipped_fire(cfg, db):
    add(cfg, db, la(2026, 10, 21, 7, 0))
    first = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 31))[0].task_id
    # Still queued a day later (e.g. held back): the next fire is skipped.
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 23, 6, 31))
    assert [d.outcome for d in done] == ["skipped_overlap"]
    assert f"{first} is still queued" in done[0].detail
    assert [r["id"] for r in db.list_tasks()] == [first]
    log = (Path(db.get_task(first)["folder"]) / "logs" / "nightshift.log").read_text()
    assert "the new fire was skipped (Overlap rule: skip)" in log
    # Once it finished, the next fire creates an Occurrence again.
    db.transition(first, TaskState.RUNNING, "test")
    db.transition(first, TaskState.DONE, "test")
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 24, 6, 31))
    assert [d.outcome for d in done] == ["created"]


@pytest.mark.parametrize("state", ["needs_you", "paused_usage", "retry_wait"])
def test_overlap_counts_every_non_terminal_state_as_unfinished(cfg, db, state):
    add(cfg, db, la(2026, 10, 21, 7, 0))
    first = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 31))[0].task_id
    db.transition(first, TaskState.RUNNING, "test")
    db.transition(first, TaskState(state), "test")
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 23, 6, 31))
    assert [d.outcome for d in done] == ["skipped_overlap"]


@pytest.mark.parametrize("state", ["done", "partial", "failed", "cancelled"])
def test_terminal_occurrences_never_block(cfg, db, state):
    add(cfg, db, la(2026, 10, 21, 7, 0))
    first = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 31))[0].task_id
    if state != "cancelled":
        db.transition(first, TaskState.RUNNING, "test")
    db.transition(first, TaskState(state), "test")
    assert [d.outcome for d in schedules.apply_catch_up(cfg, db, la(2026, 10, 23, 6, 31))] == [
        "created"]


def test_overlap_replace_cancels_the_unfinished_occurrence(cfg, db):
    add(cfg, db, la(2026, 10, 21, 7, 0), overlap="replace", cron_rule="0 */6 * * *")
    first = schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 12, 1))[0].task_id
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 18, 1))
    assert [d.outcome for d in done] == ["replaced"]
    second = done[0].task_id
    assert db.task_state(first) == TaskState.CANCELLED
    assert "replaced by a new Occurrence of Schedule scan" in db.transitions(first)[-1]["reason"]
    assert db.task_state(second) == TaskState.QUEUED
    assert done[0].detail == f"replaced {first}"
    # Same date: the second Occurrence gets its own folder.
    f1, f2 = Path(db.get_task(first)["folder"]), Path(db.get_task(second)["folder"])
    assert f1.name == "2026-10-21" and f2.name == f"2026-10-21_{second}"
    assert "cancelled" in (f1 / "report.md").read_text()


def test_overlap_replace_of_a_running_occurrence_requests_a_cancel(cfg, db):
    add(cfg, db, la(2026, 10, 21, 7, 0), overlap="replace")
    first = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 31))[0].task_id
    db.transition(first, TaskState.RUNNING, "test")
    d = schedules.run_now(cfg, db, "scan", now=la(2026, 10, 22, 9, 0), daemon_up=True)
    assert d.outcome == "replaced"
    assert db.task_state(first) == TaskState.RUNNING and db.cancel_requested(first)
    assert db.task_state(d.task_id) == TaskState.QUEUED


# --------------------------------------------------------------------------
# run-now, enable/disable, remove, errors
# --------------------------------------------------------------------------


def test_run_now_fires_without_moving_the_cron_state(cfg, db):
    add(cfg, db, la(2026, 10, 21, 7, 0))
    d = schedules.run_now(cfg, db, "scan", now=la(2026, 10, 21, 9, 0), daemon_up=False)
    assert d.outcome == "created" and d.trigger == "run_now"
    assert schedules.get(db, "scan").last_fire_at == "2026-10-21T07:00:00-07:00"
    # The Overlap rule applies to run-now too.
    again = schedules.run_now(cfg, db, "scan", now=la(2026, 10, 21, 9, 5), daemon_up=False)
    assert again.outcome == "skipped_overlap"
    assert len(db.list_tasks()) == 1


def test_disabled_schedules_dont_fire_and_dont_catch_up_when_enabled(cfg, db):
    add(cfg, db, la(2026, 10, 18, 7, 0), catch_up="next-wake")
    assert schedules.set_enabled(db, "scan", False, at=la(2026, 10, 18, 8, 0))
    assert not schedules.set_enabled(db, "scan", False, at=la(2026, 10, 18, 8, 0))
    assert schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 8, 0)) == []
    schedules.set_enabled(db, "scan", True, at=la(2026, 10, 21, 8, 0))
    assert schedules.apply_catch_up(cfg, db, la(2026, 10, 21, 9, 0)) == []
    assert [d.outcome for d in schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 30))] == [
        "created"]


def test_remove_keeps_occurrences(cfg, db):
    add(cfg, db, la(2026, 10, 21, 7, 0))
    tid = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 31))[0].task_id
    assert schedules.remove(db, "scan") == [tid]
    assert db.get_schedule("scan") is None
    assert db.task_state(tid) == TaskState.QUEUED
    with pytest.raises(schedules.ScheduleError, match="no Schedule"):
        schedules.remove(db, "scan")
    # Re-adding the name starts a fresh history.
    s = add(cfg, db, la(2026, 10, 22, 7, 0))
    assert schedules.last_run(db, s) is None


def test_a_template_broken_after_add_is_recorded_once(cfg, db, config_dir):
    add(cfg, db, la(2026, 10, 21, 7, 0))
    (Path(cfg.data["templates"]["dir"]) / "scan.yaml").unlink()
    done = schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 31))
    assert [d.outcome for d in done] == ["error"]
    assert "no template 'scan'" in done[0].detail
    # The fire is handled: no retry storm on every poll.
    assert schedules.apply_catch_up(cfg, db, la(2026, 10, 22, 6, 32)) == []
    assert db.list_tasks() == []


def test_claimed_fires_are_never_handled_twice(cfg, db):
    """Two lookers at the same fires (or a crash right after the claim): the
    conditional claim lets only one of them act."""
    add(cfg, db, la(2026, 10, 21, 7, 0))
    s = schedules.get(db, "scan")
    assert db.claim_schedule_fires("scan", expect_last=s.last_fire_at,
                                   through="2026-10-22T06:30:00-07:00", checked_at="x")
    assert not db.claim_schedule_fires("scan", expect_last=s.last_fire_at,
                                       through="2026-10-22T06:30:00-07:00", checked_at="x")


# --------------------------------------------------------------------------
# The daemon: catch-up on every start and every wake
# --------------------------------------------------------------------------


def test_daemon_start_catches_up_a_missed_fire_exactly_once(cfg, db):
    host = FakeHost()
    add(cfg, db, la(2026, 10, 20, 7, 0), catch_up="within 12h")
    clock = Clock(la(2026, 10, 21, 8, 15))  # the Mac slept through 6:30
    fake = FakeAttempts(host)
    QueueRunner(cfg, db, host, run_attempt_fn=fake, clock=clock).serve(exit_when_idle=True)
    assert len(fake.order) == 1
    occ = db.get_task(fake.order[0])
    assert occ["schedule_name"] == "scan" and occ["state"] == "done"
    runs = db.schedule_runs("scan")
    assert [(r["trigger"], r["outcome"]) for r in runs] == [("start", "created")]
    # A second daemon start (launchd restart) doesn't catch it up again.
    fake2 = FakeAttempts(host)
    QueueRunner(cfg, db, host, run_attempt_fn=fake2, clock=clock).serve(exit_when_idle=True)
    assert fake2.order == [] and len(db.list_tasks()) == 1


def test_daemon_applies_on_time_fires_before_each_pick(cfg, db):
    host = FakeHost()
    add(cfg, db, la(2026, 10, 21, 7, 0))
    clock = Clock(la(2026, 10, 22, 6, 0))
    fake = FakeAttempts(host)
    runner = QueueRunner(cfg, db, host, run_attempt_fn=fake, clock=clock)
    assert runner.run_next() is None
    clock.advance(minutes=31)
    tid = runner.run_next()
    assert tid is not None and fake.order == [tid]
    assert db.schedule_runs("scan")[-1]["trigger"] == "poll"


def test_daemon_detects_wake_and_applies_catch_up(cfg, db):
    host = FakeHost(awake_clock=1000.0)
    add(cfg, db, la(2026, 10, 21, 22, 0), catch_up="within 12h")
    clock = Clock(la(2026, 10, 21, 23, 0))
    logs: list[str] = []
    runner = QueueRunner(cfg, db, host, run_attempt_fn=FakeAttempts(host), clock=clock,
                         log=logs.append)
    assert runner.check_wake() is False  # first look only sets the mark
    clock.advance(seconds=30)
    host.awake_clock += 30
    assert runner.check_wake() is False  # awake the whole time
    # Lid closed at 23:00:30, opened at 8:00: wall time moved 9h, awake time didn't.
    clock.t = la(2026, 10, 22, 8, 0)
    host.awake_clock += 5
    assert runner.check_wake() is True
    assert any("woke from sleep" in line for line in logs)
    assert any("after wake: Schedule scan" in line and "created Occurrence" in line
               for line in logs)
    assert [r["trigger"] for r in db.schedule_runs("scan")] == ["wake"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


@pytest.fixture
def run_cli(config_dir, monkeypatch):
    clock = Clock(la(2026, 10, 21, 7, 0))
    monkeypatch.setattr(cli, "clock", clock)

    def run(*args, ok=True):
        res = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])
        if ok:
            assert res.exit_code == 0, res.output
        return res

    run.clock = clock
    return run


def test_cli_schedule_commands(run_cli, cfg):
    out = run_cli("schedule", "add", "posting-scan", "--template", "scan", "--cron", "30 6 * * *",
                  "--catch-up", "within 12h").output
    assert "Schedule posting-scan added (enabled): 30 6 * * *" in out
    assert "catch-up within 12h, else skip, overlap skip" in out
    assert "Next fire: 2026-10-22T06:30:00-07:00" in out

    listing = run_cli("schedule", "list").output
    for line in ("posting-scan  enabled", "rule       30 6 * * *", "catch-up   within 12h, else skip",
                 "overlap    skip", "next fire  2026-10-22 06:30", "last       never fired",
                 "open       none"):
        assert line in listing

    res = run_cli("schedule", "add", "posting-scan", "--template", "scan", "--cron", "30 6 * * *",
                  "--catch-up", "12h", ok=False)
    assert res.exit_code != 0 and "already exists" in res.output
    res = run_cli("schedule", "add", "x", "--template", "scan", "--cron", "bad",
                  "--catch-up", "12h", ok=False)
    assert "bad cron rule" in res.output

    out = run_cli("schedule", "run-now", "posting-scan").output
    assert "Occurrence " in out and "recurring/posting-scan/2026-10-21" in out
    res = run_cli("schedule", "run-now", "posting-scan", ok=False)
    assert res.exit_code != 0 and "Overlap rule: skip" in res.output
    listing = run_cli("schedule", "list").output
    assert "run now by hand: skipped (Overlap rule)" in listing and "(queued)" in listing

    assert "disabled." in run_cli("schedule", "disable", "posting-scan").output
    assert "already disabled" in run_cli("schedule", "disable", "posting-scan").output
    assert "posting-scan  disabled" in run_cli("schedule", "list").output
    assert "fires while it was disabled don't catch up" in run_cli(
        "schedule", "enable", "posting-scan").output

    out = run_cli("schedule", "remove", "posting-scan").output
    assert "Schedule posting-scan removed." in out and "will still run" in out
    assert "No Schedules." in run_cli("schedule", "list").output
    res = run_cli("schedule", "enable", "posting-scan", ok=False)
    assert "no Schedule 'posting-scan'" in res.output
