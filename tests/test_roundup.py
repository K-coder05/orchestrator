"""The weekly school deadline roundup (§5 item 3): the bCourses and Google
Calendar iCal feeds (``nightshift.ical``), the ``school-roundup`` template
and Schedule, course due dates as Task deadlines (§2.3), and ``orch doctor``
checking the two secrets. No real network: a fake fetcher serves the sample
feeds in ``tests/fixtures/ical/``."""

from __future__ import annotations

import json
import re
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, ical, schedules, templates
from nightshift.config import ConfigError, load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.doctor import format_report, run_doctor
from nightshift.host import FakeHost
from nightshift.protocol import agent_protocol
from nightshift.states import TaskState
from nightshift.workspace import read_task_yaml

from conftest import FIXTURES, TEST_GEMINI_KEY
from test_doctor import matching_host
from test_gate2 import Mac
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
ICS = FIXTURES / "ical"
BC_URL = "https://bcourses.berkeley.edu/feeds/calendars/user_BCsecretTOKEN4411.ics"
GC_URL = ("https://calendar.google.com/calendar/ical/me%40example.com/"
          "private-GCsecretTOKEN9902/basic.ics")
TOKENS = ("BCsecretTOKEN4411", "GCsecretTOKEN9902")

COURSES = {
    "cs61a": {"name": "CS 61A", "aliases": ["61A", "COMPSCI 61A"],
              "policy": "AI may explain lecture concepts and make practice material.",
              "limits": ["No solutions to homework, lab or project problems"]},
    "cs70": {"name": "CS 70", "aliases": ["COMPSCI 70"],
             "policy": "AI may explain proofs from lecture."},
    "data8": {"name": "Data 8", "aliases": ["DATA C8"]},  # no policy
    "ee16a": {"name": "EE 16A", "aliases": ["EECS 16A"], "policy": "No AI use.",
              "ai_allowed": False},
}


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


SUNDAY_8PM = la(2026, 10, 25, 20, 0)


def _parse(name: str, start: datetime = SUNDAY_8PM, days: int = 14) -> ical.ParseResult:
    return ical.parse((ICS / f"{name}.ics").read_text(), source=name, tz_name="America/Los_Angeles",
                      start=start, end=start + timedelta(days=days))


def _by_title(events, title):
    return [e for e in events if e.summary == title]


# --------------------------------------------------------------------------
# Parsing sample feeds
# --------------------------------------------------------------------------


def test_bcourses_feed():
    res = _parse("bcourses")
    titles = [e.summary for e in res.events]
    # HW 04 was due before the window; the VALARM inside an event is skipped.
    assert titles == ["Office hours [COMPSCI 61A]", "HW 05 [COMPSCI 61A]",
                      "Problem Set 6 [EECS 16A]", "Midterm 2 [COMPSCI 70]",
                      "Project 2: Ants [COMPSCI 61A]", "Lab 7 [DATA C8]"]
    [hw] = _by_title(res.events, "HW 05 [COMPSCI 61A]")
    # UTC times, shown in local time: 06:59Z is 23:59 the day before (PDT).
    assert hw.start == la(2026, 10, 27, 23, 59) and not hw.all_day
    assert hw.uid == "event-assignment-1001"
    # Folded lines unfolded, escapes undone.
    assert hw.description == ("<p>Recursion &amp; trees, part 2. Submit on Gradescope. "
                              "This description is folded across two lines.</p>")
    [proj] = _by_title(res.events, "Project 2: Ants [COMPSCI 61A]")
    assert proj.start.astimezone(LA) == la(2026, 11, 3, 23, 59)  # after DST ends: PST
    assert proj.start.astimezone(LA).utcoffset() == timedelta(hours=-8)
    assert res.notes == []


def test_all_day_events():
    [mid] = _by_title(_parse("bcourses").events, "Midterm 2 [COMPSCI 70]")
    assert mid.all_day and mid.start == la(2026, 10, 30) and mid.end == la(2026, 10, 31)
    # An all-day event is due at the end of its day.
    assert mid.due_at == la(2026, 10, 30, 23, 59, 59)
    [trip] = _by_title(_parse("google").events, "Family trip")
    assert trip.all_day and (trip.start, trip.end) == (la(2026, 11, 6), la(2026, 11, 9))


def test_time_zones():
    events = _parse("google").events
    # TZID with a Windows name, with a mozilla.org prefix, another zone
    # (New York), and a floating time (X-WR-TIMEZONE).
    [group] = _by_title(events, "Study group CS 70")
    assert group.start == la(2026, 10, 26, 19, 0) and group.end == la(2026, 10, 26, 20, 30)
    [hw8] = _by_title(events, "CS 70 HW 8 due")
    assert hw8.start == la(2026, 10, 29, 22, 0)
    [club] = _by_title(events, "Club call")
    assert club.start.astimezone(LA) == la(2026, 10, 30, 9, 0)  # noon in New York
    [gym] = _by_title(events, "Gym")
    assert gym.start == la(2026, 10, 28, 18, 0)
    [dentist] = _by_title(events, "Dentist")
    assert dentist.start.astimezone(LA) == la(2026, 10, 27, 10, 0)


def test_recurring_events():
    events = _parse("google").events
    lectures = [e for e in events if e.summary.startswith("CS 61A Lecture")]
    # Weekly MO/WE/FR since Aug 31: Fri Oct 30 is an EXDATE, Mon Nov 2 was
    # moved to 2pm (RECURRENCE-ID), and 10am stays 10am across the DST change.
    assert [(e.start.astimezone(LA), e.summary) for e in lectures] == [
        (la(2026, 10, 26, 10, 0), "CS 61A Lecture"),
        (la(2026, 10, 28, 10, 0), "CS 61A Lecture"),
        (la(2026, 11, 2, 14, 0), "CS 61A Lecture (moved)"),
        (la(2026, 11, 4, 10, 0), "CS 61A Lecture"),
        (la(2026, 11, 6, 10, 0), "CS 61A Lecture"),
    ]
    assert all(e.recurring for e in lectures)
    assert lectures[0].location == "Wheeler 150" and lectures[2].location == "Dwinelle 155"
    # DAILY;COUNT=4 from Oct 30: four mornings at 8am local, DST or not.
    runs = [e.start for e in _by_title(events, "Morning run")]
    assert runs == [la(2026, 10, d, 8, 0) for d in (30, 31)] + [
        la(2026, 11, d, 8, 0) for d in (1, 2)]
    # MONTHLY;BYDAY=-1FR;COUNT=3 from Sep 25: the last Friday, Oct 30 in the window.
    assert len(_by_title(events, "Club call")) == 1
    later = _parse("google", start=la(2026, 11, 20), days=40).events
    assert [e.start.astimezone(LA).date().isoformat() for e in _by_title(later, "Club call")] \
        == ["2026-11-27"]
    # The lectures stop at UNTIL (Dec 10 is the last Thursday... Friday Dec 11
    # at 10am is after UNTIL 07:59Z Dec 11).
    tail = [e.start.date().isoformat() for e in later if e.summary == "CS 61A Lecture"]
    assert tail[-1] == "2026-12-09"


def test_cancelled_events_are_left_out():
    assert _by_title(_parse("google").events, "Cancelled party") == []


def test_an_unsupported_rule_keeps_its_first_instance_and_says_so():
    res = _parse("google")
    assert len(_by_title(res.events, "Hourly thing")) == 1
    assert res.notes == ["recurring event 'Hourly thing': rule HOURLY not supported, only its "
                         "first instance is listed"]


@pytest.mark.parametrize("rule, days", [
    ("FREQ=WEEKLY;INTERVAL=2", ["2026-11-02", "2026-11-16", "2026-11-30", "2026-12-14"]),
    ("FREQ=WEEKLY;COUNT=2", ["2026-11-02", "2026-11-09"]),
    ("FREQ=MONTHLY;BYMONTHDAY=-1", ["2026-11-30"]),
    ("FREQ=MONTHLY;BYDAY=MO;BYSETPOS=2", ["2026-11-09", "2026-12-14"]),
    ("FREQ=DAILY;BYDAY=SA,SU;UNTIL=20261110", ["2026-11-07", "2026-11-08"]),
    ("FREQ=YEARLY;BYMONTH=11,12", ["2026-11-02", "2026-12-02"]),
])
def test_more_rules(rule, days):
    text = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:r\nSUMMARY:R\n"
            "DTSTART;TZID=America/Los_Angeles:20261102T090000\n"
            "DTEND;TZID=America/Los_Angeles:20261102T100000\n"
            f"RRULE:{rule}\nEND:VEVENT\nEND:VCALENDAR\n")
    res = ical.parse(text, source="google", tz_name="America/Los_Angeles",
                     start=la(2026, 11, 1), end=la(2026, 12, 15))
    got = [e.start.date().isoformat() for e in res.events]
    # DTSTART is always the first instance, even when the rule skips it.
    assert got == (days if days[0] == "2026-11-02" else ["2026-11-02", *days])


def test_not_a_feed_is_an_error():
    with pytest.raises(ical.FeedError, match="is not an iCal feed"):
        ical.parse("<html>Please log in</html>", source="bcourses", tz_name="UTC",
                   start=SUNDAY_8PM, end=SUNDAY_8PM + timedelta(days=1))


# --------------------------------------------------------------------------
# Config, courses and due dates
# --------------------------------------------------------------------------


def _config(tmp_path: Path, *, env_urls: bool = True, **extra) -> Path:
    d = tmp_path / "rconfig"
    d.mkdir(exist_ok=True)
    root = tmp_path / "school"
    root.mkdir(exist_ok=True)
    data = {
        "paths": {"agents_root": str(tmp_path / "agents")},
        "retries": {"max_retries": 1, "wait_minutes": [0]},
        "school": {"dir": str(root), "courses": COURSES},
        "permissions": {"read_only_dirs": [str(root)]},
    }
    data.update(extra)
    (d / "config.yaml").write_text(yaml.safe_dump(data))
    lines = [f"GEMINI_API_KEY={TEST_GEMINI_KEY}"]
    if env_urls:
        lines += [f"BCOURSES_ICAL_URL={BC_URL}", f'GOOGLE_CALENDAR_ICAL_URL="{GC_URL}"']
    (d / ".env").write_text("\n".join(lines) + "\n")
    (d / ".env").chmod(0o600)
    return d


@pytest.fixture
def rcfg_dir(tmp_path):
    return _config(tmp_path)


@pytest.fixture
def rcfg(rcfg_dir):
    return load_config(rcfg_dir, host=FakeHost())


class Feeds:
    """The fake fetcher: serves the sample feeds by URL, counts calls, and
    can fail one feed."""

    def __init__(self):
        self.calls: list[str] = []
        self.fail: dict[str, Exception] = {}
        self.files = {BC_URL: ICS / "bcourses.ics", GC_URL: ICS / "google.ics"}

    def __call__(self, url: str, timeout: float) -> bytes:
        self.calls.append(url)
        if url in self.fail:
            raise self.fail[url]
        return self.files[url].read_bytes()


@pytest.fixture
def feeds(monkeypatch):
    f = Feeds()
    monkeypatch.setattr(ical, "fetch_url", f)
    return f


def test_calendar_config_is_validated(tmp_path):
    d = _config(tmp_path, school={"dir": "~/school", "calendar": {"lookahead": 7}})
    with pytest.raises(ConfigError, match="school.calendar has unknown keys lookahead"):
        load_config(d, host=FakeHost())
    d = _config(tmp_path, school={"dir": "~/school", "calendar": {"lookahead_days": 0}})
    with pytest.raises(ConfigError, match="school.calendar.lookahead_days must be a positive"):
        load_config(d, host=FakeHost())


def test_events_get_their_course_and_due_flag(rcfg, feeds):
    snap = ical.fetch(rcfg, SUNDAY_8PM)
    assert feeds.calls == [BC_URL, GC_URL]
    assert [(f.label, f.events, f.error) for f in snap.feeds] == [
        ("bCourses calendar", 6, None), ("Google Calendar", 16, None)]
    got = {e.summary: (e.course, e.due) for e in snap.events}
    assert got["HW 05 [COMPSCI 61A]"] == ("cs61a", True)
    assert got["Project 2: Ants [COMPSCI 61A]"] == ("cs61a", True)  # an assignment UID
    assert got["Office hours [COMPSCI 61A]"] == ("cs61a", False)
    assert got["CS 61A Lecture"] == ("cs61a", False)
    assert got["Midterm 2 [COMPSCI 70]"] == ("cs70", True)
    assert got["CS 70 HW 8 due"] == ("cs70", True)
    assert got["Study group CS 70"] == ("cs70", False)
    assert got["Lab 7 [DATA C8]"] == ("data8", True)
    assert got["Problem Set 6 [EECS 16A]"] == ("ee16a", True)
    assert got["Dentist"] == (None, False)


@pytest.mark.parametrize("title, due", [
    ("HW 5", True), ("hw5", True), ("Homework 3 due", True), ("Midterm 2", True),
    ("Final Exam", True), ("Exams review", True), ("Quiz 4", True), ("pset 2", True),
    ("Problem set 6", True), ("Essay draft", True), ("Project 2 deadline", True),
    ("Lecture 12", False), ("Finally free", False), ("Hwy 1 drive", False),
    ("Discussion section", False),
])
def test_what_counts_as_a_due_date(title, due):
    ev = ical.Event(source="google", uid="x", summary=title, start=SUNDAY_8PM, end=SUNDAY_8PM)
    assert ical.is_due(ev) is due


def test_course_deadline_is_the_next_due_date(rcfg, feeds):
    d = ical.course_deadline(rcfg, "cs61a", SUNDAY_8PM)
    assert d.at == "2026-10-27T23:59:00-07:00" and d.event.summary == "HW 05 [COMPSCI 61A]"
    assert d.describe(rcfg) == ("2026-10-27T23:59:00-07:00 (HW 05 [COMPSCI 61A], from your "
                                "bCourses calendar)")
    # Google's "CS 70 HW 8 due" (Thu 10pm) comes before the all-day midterm (Fri).
    assert ical.course_deadline(rcfg, "cs70", SUNDAY_8PM).event.summary == "CS 70 HW 8 due"
    # The first fetch was cached: none again within refresh_hours.
    assert len(feeds.calls) == 2 and ical.cache_path(rcfg).is_file()
    # After HW 05 is due, the project is next.
    assert ical.course_deadline(rcfg, "cs61a", la(2026, 10, 28, 0, 0)).at == \
        "2026-11-03T23:59:00-08:00"
    # An all-day due date counts until the end of its day.
    assert ical.course_deadline(rcfg, "cs70", la(2026, 10, 30, 12, 0)).at == \
        "2026-10-30T23:59:59-07:00"


def test_course_deadline_beyond_the_lookahead_is_none(rcfg, feeds):
    assert ical.course_deadline(rcfg, "cs61a", la(2026, 11, 4, 0, 0)) is None


def test_a_stale_cache_is_fetched_again_and_a_failed_fetch_falls_back(rcfg, feeds):
    assert ical.course_deadline(rcfg, "cs61a", SUNDAY_8PM) is not None
    feeds.fail[BC_URL] = urllib.error.HTTPError(BC_URL, 503, "busy", None, None)
    later = SUNDAY_8PM + timedelta(hours=13)
    d = ical.course_deadline(rcfg, "cs61a", later)
    assert len(feeds.calls) == 4  # fetched again: the cache was 13h old
    assert d is not None and d.event.summary == "HW 05 [COMPSCI 61A]"  # from the cache
    assert d.fetched_at == SUNDAY_8PM


def test_no_feeds_no_deadline(tmp_path, feeds):
    cfg = load_config(_config(tmp_path, env_urls=False), host=FakeHost())
    assert ical.course_deadline(cfg, "cs61a", SUNDAY_8PM) is None
    assert feeds.calls == []


# --------------------------------------------------------------------------
# Course-linked Tasks get due-date deadlines; --deadline wins
# --------------------------------------------------------------------------


@pytest.fixture
def slides(tmp_path):
    d = tmp_path / "school" / "cs61a" / "week9"
    d.mkdir(parents=True)
    (d / "lecture20.pdf").write_text("%PDF fake\n")
    return d


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _task(cfg, task_id):
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        return db.get_task(task_id)
    finally:
        db.close()


def test_a_course_task_gets_the_due_date_as_deadline(rcfg_dir, rcfg, slides, feeds, monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: SUNDAY_8PM)
    res = _orch(rcfg_dir, "add", f"Study guide for CS 61A from {slides}", "--template",
                "study-guide")
    assert res.exit_code == 0, res.output
    assert ("Due date:  2026-10-27T23:59:00-07:00 (HW 05 [COMPSCI 61A], from your bCourses "
            "calendar): the Task's deadline") in res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    row = _task(rcfg, tid)
    assert row["deadline"] == "2026-10-27T23:59:00-07:00"
    assert read_task_yaml(Path(row["folder"])).deadline == "2026-10-27T23:59:00-07:00"


def test_deadline_flag_wins(rcfg_dir, rcfg, slides, feeds, monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: SUNDAY_8PM)
    res = _orch(rcfg_dir, "add", f"Study guide for CS 61A from {slides}", "--template",
                "study-guide", "--deadline", "2026-11-20")
    assert res.exit_code == 0, res.output
    assert "Due date:" not in res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    assert _task(rcfg, tid)["deadline"].startswith("2026-11-20T")
    assert feeds.calls == []  # not even fetched


def test_feeds_down_the_task_is_still_added(rcfg_dir, rcfg, slides, feeds, monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: SUNDAY_8PM)
    feeds.fail[BC_URL] = urllib.error.URLError("nodename nor servname provided")
    feeds.fail[GC_URL] = TimeoutError()
    res = _orch(rcfg_dir, "add", f"Study guide for CS 61A from {slides}", "--template",
                "study-guide")
    assert res.exit_code == 0, res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    assert _task(rcfg, tid)["deadline"] is None


def test_a_course_occurrence_gets_a_deadline_too(rcfg, slides, feeds):
    db = Database(rcfg.db_path, tz=rcfg.timezone)
    try:
        schedules.add(rcfg, db, "cs61a-guide", template="study-guide",
                      request=f"Study guide for CS 61A from {slides}", cron_rule="0 21 * * 0",
                      catch_up="next-wake", at=la(2026, 10, 25, 12, 0))
        [d] = schedules.apply_catch_up(rcfg, db, SUNDAY_8PM + timedelta(hours=1, minutes=5))
        assert db.get_task(d.task_id)["deadline"] == "2026-10-27T23:59:00-07:00"
    finally:
        db.close()


# --------------------------------------------------------------------------
# The template and its Schedule
# --------------------------------------------------------------------------


def test_template(rcfg):
    tpl = templates.load(rcfg, "school-roundup")
    assert (tpl.task_type, tpl.agent, tpl.internet, tpl.long) == ("study", "claude", "off", False)
    assert tpl.estimated_minutes <= 30  # no plan approval (§2.4)
    assert tpl.schedule == {"cron": "0 20 * * 0", "catch_up": "next-wake", "overlap": "skip",
                            "request": "Weekly school deadline roundup and study plan"}
    assert tpl.courses == "all"
    assert tpl.pre_hooks == [{"hook": "school_calendar", "to": "input/calendar.md"}]
    assert rcfg.failover("study") == (["claude", "gemini"], "always")
    p = templates.instantiate(rcfg, tpl, "x").prompt
    assert "input/calendar.md" in p and "output/roundup.md" in p and "{{" not in p
    assert BC_URL not in p and GC_URL not in p


@pytest.mark.parametrize("extra, message", [
    ({"courses": "some"}, "courses must be all"),
    ({"pre_hooks": [{"hook": "fetch_web"}]}, "must name one of school_calendar"),
    ({"pre_hooks": [{"hook": "school_calendar", "to": "../x.md"}]}, "must stay inside"),
    ({"pre_hooks": [{"hook": "school_calendar", "to": "a.md", "url": "x"}]}, "unknown keys url"),
    ({"courses": "all", "inputs": {"c": {"from": "course"}}}, "exclude each other"),
])
def test_bad_template_keys(rcfg, tmp_path, extra, message):
    data = {"task_type": "study", "agent": "claude", "estimated_minutes": 5,
            "max_runtime_minutes": 10, "long": False, "internet": "off", "prompt": "x"} | extra
    with pytest.raises(templates.TemplateError, match=re.escape(message)):
        templates.parse(rcfg, "t", tmp_path / "t.yaml", data)


def test_all_policies_head_the_protocol(rcfg, tmp_path):
    from nightshift import school

    text = agent_protocol(tmp_path, course_policy=school.all_courses_section(rcfg))
    assert text.index("## Course AI policies") < text.index("1. Work only inside")
    assert "### CS 61A\n\n   Policy (CS 61A, from Nightshift config):" in text
    assert "   - No solutions to homework, lab or project problems" in text
    assert "### Data 8\n\n   No AI policy configured: no study work." in text
    assert "### EE 16A\n\n   Its AI policy allows no AI use: no study work." in text
    assert "Never do, solve or write up graded work" in text


@pytest.fixture
def agent_writes(env, monkeypatch, tmp_path):  # noqa: F811
    """The roundup agent's output files (FAKE_STUDY copies them to output/)."""
    for k in ("FAKE_PROPOSED", "FAKE_PAGES", "FAKE_RESEARCH", "FAKE_PROGRESS"):
        monkeypatch.delenv(k, raising=False)
    out = tmp_path / "roundup-out"
    out.mkdir()
    (out / "roundup.md").write_text("# School roundup: Oct 26 to Nov 1\n\n## Due soon\n- HW 05\n")
    (out / "study_plan.md").write_text("# Study plan\n\n## Monday\n- CS 61A: review trees\n")
    monkeypatch.setenv("FAKE_STUDY", str(out))


@pytest.fixture
def sched(rcfg, rcfg_dir, agent_writes, feeds, monkeypatch):
    """`orch schedule add school-roundup` on Saturday Oct 24, noon."""
    mac = Mac(la(2026, 10, 24, 12, 0))
    monkeypatch.setattr(cli, "clock", mac.clock)
    res = _orch(rcfg_dir, "schedule", "add", "school-roundup")
    assert res.exit_code == 0, res.output
    db = Database(rcfg.db_path, tz=rcfg.timezone)
    logs: list[str] = []
    runner = QueueRunner(rcfg, db, mac.host, adapter_provider=fake_provider, clock=mac.clock,
                         log=logs.append)
    runner.check_wake()
    yield mac, db, runner, logs, res.output
    db.close()


def test_standard_schedule(sched, rcfg):
    _, db, _, _, out = sched
    assert ("Schedule school-roundup added (enabled): 0 20 * * 0 (America/Los_Angeles), "
            "template school-roundup, catch-up run on next wake, overlap skip") in out
    assert "Next fire: 2026-10-25T20:00:00-07:00" in out  # Sunday 8pm
    s = schedules.get(db, "school-roundup")
    assert s.catch_up.kind == schedules.CATCH_UP_NEXT_WAKE and s.overlap == "skip"
    assert schedules.next_fire(rcfg, s, la(2026, 10, 25, 20, 1)) == la(2026, 11, 1, 20, 0)


def _files_under(root: Path):
    for p in root.rglob("*"):
        if p.is_file() and not p.is_symlink():
            yield p


def _assert_no_urls(*roots: Path) -> None:
    for root in roots:
        for p in _files_under(root):
            data = p.read_bytes()
            for secret in (BC_URL, GC_URL, *TOKENS):
                assert secret.encode() not in data, f"{secret} found in {p}"


def test_missed_sunday_catches_up_on_wake_and_runs_end_to_end(sched, rcfg, feeds, tmp_path):
    mac, db, runner, logs, _ = sched
    # Lid shut Sunday 6pm, opened Monday 8:15am: the 8pm fire was missed.
    mac.awake(hours=30)
    runner.check_wake()
    mac.sleep_until(la(2026, 10, 26, 8, 15))
    assert runner.check_wake() is True
    [occ] = db.occurrences("school-roundup")
    assert db.get_task(occ["id"])["approved"]  # pre-approved: nothing waits for me
    assert runner.run_next() == occ["id"]
    row = db.get_task(occ["id"])
    assert row["state"] == TaskState.DONE, db.attempts(occ["id"])[-1]["end_reason"]
    assert (row["task_type"], row["agent"], row["internet"]) == ("study", "claude", "off")
    folder = Path(row["folder"])
    assert folder.parent.name == "school-roundup"  # recurring/school-roundup/<date>/
    assert feeds.calls == [BC_URL, GC_URL]

    # The input file: per course, due dates first, the course's rule.
    cal = (folder / "input" / "calendar.md").read_text()
    assert "- bCourses calendar: 6 events" in cal and "- Google Calendar: 16 events" in cal
    cs61a = cal.split("## CS 61A", 1)[1].split("\n## ", 1)[0]
    assert "Due dates:\n- Tue Oct 27, 23:59: HW 05 [COMPSCI 61A] [bCourses calendar]" in cs61a
    assert "Details: Recursion & trees, part 2." in cs61a
    assert "Mon Nov 2, 14:00-15:00: CS 61A Lecture (moved) (Dwinelle 155)" in cs61a
    data8 = cal.split("## Data 8", 1)[1].split("\n## ", 1)[0]
    assert "no AI policy configured: list its due dates only" in data8
    assert "Thu Nov 5, 23:59: Lab 7 [DATA C8]" in data8
    loose = cal.split("## Not tied to a configured course", 1)[1]
    assert "Dentist" in loose and "insurance card" not in cal  # titles and times only
    assert "Fri Nov 6 to Sun Nov 8 (all day): Family trip" in loose
    assert "Note: Google Calendar: recurring event 'Hourly thing'" in cal

    # Every course's policy headed the protocol the agent got.
    inv = json.loads((folder / "logs" / "fake_invocation.json").read_text())
    protocol = inv["argv"][inv["argv"].index("--append-system-prompt") + 1]
    assert protocol.index("## Course AI policies") < protocol.index("1. Work only")
    assert "AI may explain proofs from lecture." in protocol
    assert "input/calendar.md" in inv["stdin"]

    report = (folder / "report.md").read_text()
    assert "### Course AI policies" in report and \
        "Due dates only, no study work: Data 8 (no policy), EE 16A (no AI use)." in report
    cal_sec = report.split("### School calendar", 1)[1]
    assert "bCourses calendar: 6 events, Google Calendar: 16 events" in cal_sec
    assert "Due dates per course: CS 61A 2, EE 16A 1, CS 70 2, Data 8 1." in cal_sec
    log = (folder / "logs" / "nightshift.log").read_text()
    assert "calendar fetched: bCourses calendar 6 event(s), Google Calendar 16 event(s)" in log

    # The URLs never appear in any file Nightshift wrote: the Task folder,
    # the DB, the cache, the calendar record, the schedule state.
    assert ical.cache_path(rcfg).is_file()
    _assert_no_urls(rcfg.agents_root)
    assert len(db.occurrences("school-roundup")) == 1


def test_a_failed_feed_is_a_failed_attempt_and_never_leaks_the_url(
        rcfg_dir, rcfg, agent_writes, feeds, monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: SUNDAY_8PM)
    # An error whose text holds the URL (some libraries put it there).
    feeds.fail[GC_URL] = urllib.error.URLError(f"could not open {GC_URL}")
    res = _orch(rcfg_dir, "add", "Roundup", "--template", "school-roundup")
    assert res.exit_code == 0, res.output
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    db = Database(rcfg.db_path, tz=rcfg.timezone)
    try:
        runner = QueueRunner(rcfg, db, FakeHost(), adapter_provider=fake_provider,
                             clock=lambda: SUNDAY_8PM)
        runner.run_next()
        row = db.get_task(tid)
        [attempt] = db.attempts(tid)
        assert row["state"] == TaskState.RETRY_WAIT
        assert attempt["end_reason"].startswith(
            "Attempt 1: the agent did not start: pre-hook school_calendar failed: could not "
            "fetch the calendar: Google Calendar: could not connect (could not open [REDACTED])")
        folder = Path(row["folder"])
        assert not (folder / "logs" / "fake_invocation.json").exists()  # the agent never ran
        assert not (folder / "input" / "calendar.md").exists()
        assert not ical.cache_path(rcfg).exists()  # a failed fetch is never cached
        # The retry gets both feeds and runs.
        del feeds.fail[GC_URL]
        runner.run_next()
        assert db.get_task(tid)["state"] == TaskState.DONE
        assert (folder / "input" / "calendar.md").is_file()
    finally:
        db.close()
    _assert_no_urls(rcfg.agents_root)


def test_a_resumed_attempt_works_from_the_same_events(rcfg_dir, rcfg, agent_writes, feeds,
                                                      monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: SUNDAY_8PM)
    monkeypatch.setenv("FAKE_EXIT", "1")  # first Attempt crashes after the fetch
    res = _orch(rcfg_dir, "add", "Roundup", "--template", "school-roundup")
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    db = Database(rcfg.db_path, tz=rcfg.timezone)
    try:
        runner = QueueRunner(rcfg, db, FakeHost(), adapter_provider=fake_provider,
                             clock=lambda: SUNDAY_8PM)
        runner.run_next()
        assert db.get_task(tid)["state"] == TaskState.RETRY_WAIT
        monkeypatch.delenv("FAKE_EXIT")
        runner.run_next()
        assert db.get_task(tid)["state"] == TaskState.DONE
    finally:
        db.close()
    assert feeds.calls == [BC_URL, GC_URL]  # fetched once for the Task


def test_no_feed_set_up_fails_with_what_to_do(tmp_path, agent_writes, feeds, monkeypatch):
    d = _config(tmp_path, env_urls=False)
    cfg = load_config(d, host=FakeHost())
    monkeypatch.setattr(cli, "clock", lambda: SUNDAY_8PM)
    res = _orch(d, "add", "Roundup", "--template", "school-roundup")
    tid = re.search(r"Task (\w{4}) queued", res.output).group(1)
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider,
                    clock=lambda: SUNDAY_8PM).run_next()
        [attempt] = db.attempts(tid)
    finally:
        db.close()
    assert "no calendar feed is set up" in attempt["end_reason"]
    assert "BCOURSES_ICAL_URL and GOOGLE_CALENDAR_ICAL_URL" in attempt["end_reason"]
    assert feeds.calls == []


def test_keychain_urls_are_redacted_too(tmp_path, agent_writes, feeds, monkeypatch):
    """URLs kept in the Keychain (not .env) are masked once loaded."""
    d = _config(tmp_path, env_urls=False)
    host = FakeHost(keychain={("nightshift", "BCOURSES_ICAL_URL"): BC_URL,
                              ("nightshift", "GOOGLE_CALENDAR_ICAL_URL"): GC_URL})
    cfg = load_config(d, host=host)
    feeds.fail[BC_URL] = urllib.error.URLError(f"refused for {BC_URL}")
    snap = ical.fetch(cfg, SUNDAY_8PM)
    assert snap.errors == ["bCourses calendar: could not connect (refused for [REDACTED])"]
    assert cfg.secrets.redact(f"x {GC_URL} y") == "x [REDACTED] y"


# --------------------------------------------------------------------------
# orch doctor
# --------------------------------------------------------------------------


def test_doctor_reports_both_secrets(rcfg):
    r = run_doctor(rcfg, matching_host())
    assert r.calendar_secrets == [("bCourses calendar", "BCOURSES_ICAL_URL", True),
                                  ("Google Calendar", "GOOGLE_CALENDAR_ICAL_URL", True)]
    text = format_report(r)
    assert "Secrets (school calendar iCal URLs, in .env or the Keychain)" in text
    assert "  ok       bCourses calendar  BCOURSES_ICAL_URL" in text
    assert BC_URL not in text and GC_URL not in text


def test_doctor_flags_unset_secrets(tmp_path):
    d = _config(tmp_path, env_urls=False)
    host = FakeHost(keychain={("nightshift", "GOOGLE_CALENDAR_ICAL_URL"): GC_URL})
    cfg = load_config(d, host=host)
    r = run_doctor(cfg, matching_host())
    assert r.unset_secrets == ["BCOURSES_ICAL_URL"]
    text = format_report(r)
    assert ("  NOT SET  bCourses calendar  BCOURSES_ICAL_URL: the school-roundup Schedule can't "
            "read it") in text
    assert "  ok       Google Calendar    GOOGLE_CALENDAR_ICAL_URL" in text
    assert GC_URL not in text


def test_cli_doctor_shows_the_secrets(rcfg_dir, monkeypatch):
    monkeypatch.setattr(cli, "host_factory", matching_host)
    res = _orch(rcfg_dir, "doctor")
    assert res.exit_code == 0, res.output
    assert "BCOURSES_ICAL_URL" in res.output and "GOOGLE_CALENDAR_ICAL_URL" in res.output
    assert BC_URL not in res.output and GC_URL not in res.output
