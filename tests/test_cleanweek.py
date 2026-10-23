"""Gate 2 (§4): the clean-week check (`orch clean-week`), on fixture data.

Rows are written straight into the database with the times a real week
would have left behind, so the check is tested without waiting a week.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightshift import cleanweek, cli, report
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import iso

LA = ZoneInfo("America/Los_Angeles")
END = datetime(2026, 10, 22, 22, 0, tzinfo=LA)  # the week: Oct 15 22:00 to Oct 22 22:00


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


def fire(day: int) -> datetime:
    return la(2026, 10, day, 6, 30)


# --------------------------------------------------------------------------
# Fixture data
# --------------------------------------------------------------------------


def add_schedule(db, name="scan", cron="30 6 * * *", catch_up="within", hours=12.0,
                 created=la(2026, 10, 1), enabled=True):
    db.insert_schedule({
        "name": name, "template": "scan", "request": "", "cron": cron, "catch_up": catch_up,
        "catch_up_hours": hours if catch_up == "within" else None, "overlap": "skip",
        "priority": None, "agent": None, "enabled": enabled, "created_at": iso(created),
        "updated_at": iso(created), "last_fire_at": iso(created),
    })


def move(db, tid, state, reason, at, run_id=None):
    db.transition(tid, state, reason, run_id=run_id)
    db.conn.execute("UPDATE transitions SET at = ? WHERE id = (SELECT MAX(id) FROM transitions)",
                    (iso(at),))
    db.conn.commit()


def task(cfg, db, title, at, **kw):
    tid = create_task(cfg, db, title, **kw).spec.id
    db.conn.execute("UPDATE transitions SET at = ? WHERE task_id = ?", (iso(at), tid))
    db.conn.commit()
    return tid


def attempt(db, tid, start, end, outcome, reason):
    run = db.start_attempt(tid, "claude", [], "log")
    db.finish_attempt(run, exit_code=1, outcome=outcome, end_reason=reason, session_id=None)
    db.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?",
                    (iso(start), iso(end), run))
    db.conn.commit()
    return run


def occurrence(cfg, db, name, fire_at, decided, *, fires=1, state=TaskState.DONE,
               trigger="poll", outcome="created"):
    """A Schedule decision that created an Occurrence, which then ran."""
    tid = task(cfg, db, f"{name} {fire_at:%Y-%m-%d}", decided, schedule_name=name,
               occurrence_date=fire_at.date())
    db.record_schedule_run(schedule_name=name, trigger=trigger, fire_at=iso(fire_at),
                           fires=fires, decided_at=iso(decided), outcome=outcome, task_id=tid,
                           detail=None)
    if state != TaskState.QUEUED:
        move(db, tid, TaskState.RUNNING, "picked", decided + timedelta(minutes=1))
        if state != TaskState.RUNNING:
            move(db, tid, state, "agent finished; report says done",
                 decided + timedelta(minutes=20))
    return tid


def decision(db, name, fire_at, decided, outcome, detail="", fires=1):
    db.record_schedule_run(schedule_name=name, trigger="poll", fire_at=iso(fire_at),
                           fires=fires, decided_at=iso(decided), outcome=outcome, task_id=None,
                           detail=detail or None)


def on_time_week(cfg, db, days=range(16, 23)):
    add_schedule(db)
    return [occurrence(cfg, db, "scan", fire(d), fire(d) + timedelta(seconds=5)) for d in days]


def by_day(week):
    return {c.fire_at.day: c for c in week.fires}


# --------------------------------------------------------------------------
# A clean week
# --------------------------------------------------------------------------


def test_a_week_of_on_time_runs_with_an_explained_retry_is_clean(cfg, db):
    ids = on_time_week(cfg, db)
    # One Failed Attempt that says why, retried by the daemon.
    tid = task(cfg, db, "haiku", la(2026, 10, 18, 1, 0))
    move(db, tid, TaskState.RUNNING, "picked", la(2026, 10, 18, 1, 0))
    run = attempt(db, tid, la(2026, 10, 18, 1, 0), la(2026, 10, 18, 3, 0), "failed",
                  "Attempt 1 hit its runtime cap of 120 min")
    move(db, tid, TaskState.RETRY_WAIT, "Attempt 1 hit its runtime cap of 120 min; retry 1 of 2",
         la(2026, 10, 18, 3, 0), run_id=run)

    week = cleanweek.check(cfg, db, END)

    assert [c.status for c in week.fires] == ["ran"] * 7
    assert [c.task_id for c in week.fires] == ids
    assert all(c.ok for c in week.fires) and week.schedules_ok
    assert len(week.failures) == 1 and week.failures[0].explained
    assert week.failures[0].attempt_no == 1 and "runtime cap" in week.failures[0].explanation
    assert week.interventions == [] and week.clean
    text = cleanweek.format_check(week, cfg.timezone)
    assert "1. Scheduled runs: PASS (7 fires" in text and "2. Failures: PASS (1, all explained)" in text
    assert "3. Manual interventions: PASS (none)" in text and text.endswith("Clean week: yes.")


def test_only_the_period_counts(cfg, db):
    on_time_week(cfg, db, days=range(10, 23))
    tid = task(cfg, db, "old", la(2026, 10, 12))
    move(db, tid, TaskState.CANCELLED, "cancelled by hand (orch cancel) while queued",
         la(2026, 10, 12, 1))
    week = cleanweek.check(cfg, db, END)
    assert sorted(by_day(week)) == list(range(16, 23)) and week.clean
    assert [c.fire_at.day for c in cleanweek.check(cfg, db, END, days=3).fires] == [20, 21, 22]


def test_a_fire_the_daemon_has_not_polled_yet_is_not_missed(cfg, db):
    on_time_week(cfg, db, days=range(16, 22))
    week = cleanweek.check(cfg, db, fire(22) + timedelta(minutes=2))
    assert sorted(by_day(week)) == list(range(16, 22)) and week.clean


# --------------------------------------------------------------------------
# 1. Scheduled runs: ran, caught up, skipped by the rules
# --------------------------------------------------------------------------


def test_catch_up_and_skips_by_the_rules_are_correct(cfg, db):
    add_schedule(db)
    occurrence(cfg, db, "scan", fire(16), fire(16) + timedelta(seconds=5))
    # The Mac slept from the 16th to 8am on the 18th: two fires, one Occurrence.
    collapsed = occurrence(cfg, db, "scan", fire(18), la(2026, 10, 18, 8, 0), fires=2)
    # Asleep until the 20th at 6am: the 19th's fire is 23.5h old, past 12h.
    decision(db, "scan", fire(19), la(2026, 10, 20, 6, 0), "skipped_catch_up",
             "missed by 23.5h; Catch-up rule is within 12h, else skip")
    stuck = occurrence(cfg, db, "scan", fire(20), fire(20) + timedelta(seconds=3),
                       state=TaskState.RUNNING)
    decision(db, "scan", fire(21), fire(21) + timedelta(seconds=3), "skipped_overlap",
             f"previous Occurrence {stuck} is still running")
    move(db, stuck, TaskState.DONE, "agent finished; report says done", fire(21) + timedelta(hours=1))
    occurrence(cfg, db, "scan", fire(22), fire(22) + timedelta(seconds=3))

    week = cleanweek.check(cfg, db, END)
    f = by_day(week)

    assert f[16].status == "ran"
    assert f[17].status == "caught up" and f"collapsed into Occurrence {collapsed}" in f[17].detail
    assert "2 fires made one" in f[17].detail
    assert f[18].status == "caught up" and "1.5h late" in f[18].detail
    assert f[19].status == "skipped" and "Catch-up rule" in f[19].detail
    assert f[20].status == "ran" and f[21].status == "skipped" and "Overlap rule" in f[21].detail
    assert week.schedules_ok and week.clean


def test_run_on_next_wake_catches_up_however_late(cfg, db):
    add_schedule(db, name="rescrape", cron="0 2 */2 * *", catch_up="next_wake")
    # Fires on the 17th, 19th and 21st at 2am; the Mac woke at noon on the 19th.
    tid = occurrence(cfg, db, "rescrape", la(2026, 10, 19, 2, 0), la(2026, 10, 19, 12, 0), fires=2)
    occurrence(cfg, db, "rescrape", la(2026, 10, 21, 2, 0), la(2026, 10, 21, 2, 0, 4))
    week = cleanweek.check(cfg, db, END)
    assert [(c.fire_at.day, c.status) for c in week.fires] == [
        (17, "caught up"), (19, "caught up"), (21, "ran")]
    assert week.fires[1].task_id == tid and "run on next wake" in week.fires[1].detail
    assert week.clean


def test_overlap_replace_cancelling_an_occurrence_is_not_a_problem(cfg, db):
    on_time_week(cfg, db, days=[16, 18, 19, 20, 21, 22])
    old = occurrence(cfg, db, "scan", fire(17), fire(17) + timedelta(seconds=3),
                     state=TaskState.RUNNING)
    move(db, old, TaskState.CANCELLED, "replaced by a new Occurrence of Schedule scan "
         "(Overlap rule: replace) while running", fire(18))
    week = cleanweek.check(cfg, db, END)
    assert by_day(week)[17].ok and "replaced" in by_day(week)[17].detail and week.clean


def test_scheduled_runs_that_went_wrong_are_each_listed(cfg, db):
    add_schedule(db)
    occurrence(cfg, db, "scan", fire(16), fire(16) + timedelta(seconds=5))
    # Created 14h late although the rule is within 12h.
    occurrence(cfg, db, "scan", fire(17), fire(17) + timedelta(hours=14))
    # Skipped as too late although it was only 2h late.
    decision(db, "scan", fire(18), fire(18) + timedelta(hours=2), "skipped_catch_up", "missed")
    decision(db, "scan", fire(19), fire(19) + timedelta(seconds=2), "error",
             "TemplateError: no template 'scan'")
    gone = occurrence(cfg, db, "scan", fire(20), fire(20) + timedelta(seconds=2),
                      state=TaskState.QUEUED)
    move(db, gone, TaskState.CANCELLED, "cancelled by hand (orch cancel) while queued",
         fire(20) + timedelta(hours=1))
    stuck = occurrence(cfg, db, "scan", fire(21), fire(21) + timedelta(seconds=2),
                       state=TaskState.QUEUED)
    # The 22nd: the daemon never decided it (it was down).

    week = cleanweek.check(cfg, db, END)
    f = by_day(week)

    assert f[16].ok
    assert (f[17].status, f[17].ok) == ("caught up late", False) and "14.0h late" in f[17].detail
    assert (f[18].status, f[18].ok) == ("error", False) and "although" in f[18].detail
    assert (f[19].status, f[19].ok) == ("error", False) and "TemplateError" in f[19].detail
    assert (f[20].status, f[20].ok) == ("did not run", False) and "cancelled" in f[20].detail
    assert (f[21].status, f[21].ok) == ("did not run", False)
    assert f[21].task_id == stuck and "still queued" in f[21].detail
    assert (f[22].status, f[22].ok) == ("missed", False)
    assert not week.schedules_ok and not week.clean
    text = cleanweek.format_check(week, cfg.timezone)
    assert "1. Scheduled runs: FAIL (6 of 7 fires not handled correctly)" in text
    assert "! 2026-10-22 06:30  scan: missed" in text


def test_an_unfinished_occurrence_under_a_day_old_is_fine(cfg, db):
    on_time_week(cfg, db, days=range(16, 22))
    occurrence(cfg, db, "scan", fire(22), fire(22) + timedelta(seconds=2), state=TaskState.RUNNING)
    week = cleanweek.check(cfg, db, END)
    assert by_day(week)[22].ok and "running" in by_day(week)[22].detail


def test_fires_while_disabled_do_not_count_but_disabling_is_an_intervention(cfg, db):
    on_time_week(cfg, db, days=[16, 17, 18, 21, 22])
    db.record_intervention(at=iso(la(2026, 10, 18, 12)), command="orch schedule disable",
                           schedule_name="scan", detail="Schedule scan disabled")
    db.record_intervention(at=iso(la(2026, 10, 20, 12)), command="orch schedule enable",
                           schedule_name="scan", detail="Schedule scan enabled")
    week = cleanweek.check(cfg, db, END)
    f = by_day(week)
    assert f[19].status == f[20].status == "disabled" and f[19].ok
    assert week.schedules_ok and not week.hands_off
    assert [i.command for i in week.interventions] == [
        "orch schedule disable", "orch schedule enable"]
    assert "1. Scheduled runs: PASS (5 fires" in cleanweek.format_check(week, cfg.timezone)


def test_a_schedule_still_disabled_counts_nothing_since(cfg, db):
    add_schedule(db, enabled=False, created=la(2026, 10, 1))
    db.conn.execute("UPDATE schedules SET updated_at = ?", (iso(la(2026, 10, 2)),))
    db.conn.commit()
    week = cleanweek.check(cfg, db, END)
    assert {c.status for c in week.fires} == {"disabled"} and week.schedules_ok


def test_a_schedule_added_midweek_counts_from_then(cfg, db):
    add_schedule(db, created=la(2026, 10, 20, 9))
    for d in (21, 22):
        occurrence(cfg, db, "scan", fire(d), fire(d) + timedelta(seconds=2))
    week = cleanweek.check(cfg, db, END)
    assert sorted(by_day(week)) == [21, 22] and week.clean


# --------------------------------------------------------------------------
# 2. Failures
# --------------------------------------------------------------------------


def _failed_attempt(cfg, db, title, reason, at, *, final=False):
    tid = task(cfg, db, title, at)
    move(db, tid, TaskState.RUNNING, "picked", at)
    run = attempt(db, tid, at, at + timedelta(minutes=5), "failed", reason)
    move(db, tid, TaskState.FAILED if final else TaskState.RETRY_WAIT,
         f"{reason}; no retries left (2 of 2 used)" if final else f"{reason}; retry 1 of 2",
         at + timedelta(minutes=5), run_id=run)
    return tid


def _agent_failed(cfg, db, title, result_line, at):
    tid = task(cfg, db, title, at)
    folder = Path(db.get_task(tid)["folder"])
    draft = report.skeleton(title).replace(f"## 1. Result\n\n{report.PLACEHOLDER}",
                                           f"## 1. Result\n\n{result_line}")
    (folder / "report.md").write_text(draft)
    report.finalize(folder, title=title, result="failed", result_detail=None, attempts=[],
                    usage_rows=[])
    move(db, tid, TaskState.RUNNING, "picked", at)
    run = attempt(db, tid, at, at + timedelta(minutes=5), "completed",
                  "agent finished; report says failed")
    move(db, tid, TaskState.FAILED, "agent finished; report says failed",
         at + timedelta(minutes=5), run_id=run)
    return tid


def test_every_failure_needs_an_explanation(cfg, db):
    at = la(2026, 10, 18, 1)
    ok_crash = _failed_attempt(cfg, db, "a", "Attempt 1 crashed: agent exited with code 1: "
                               "API Error: 500", at)
    bare = _failed_attempt(cfg, db, "b", "Attempt 1 crashed: agent exited with code 1",
                           at + timedelta(hours=1), final=True)
    died = _failed_attempt(cfg, db, "c", "Attempt 1 crashed: the daemon stopped unexpectedly "
                           "while an Attempt was running", at + timedelta(hours=2))
    hook = _failed_attempt(cfg, db, "d", "Attempt 1: post-hook merge_tracker failed: bad CSV",
                           at + timedelta(hours=3))
    said = _agent_failed(cfg, db, "e", "failed: the careers site returned 503 all night",
                         at + timedelta(hours=4))
    silent = _agent_failed(cfg, db, "f", "failed", at + timedelta(hours=5))
    nightshift = task(cfg, db, "g", at + timedelta(hours=6))
    move(db, nightshift, TaskState.FAILED, "Nightshift error: OSError: disk full",
         at + timedelta(hours=6))

    week = cleanweek.check(cfg, db, END)
    got = {f.task_id: f for f in week.failures}

    assert len(week.failures) == 7  # b's final transition isn't a second entry
    assert got[ok_crash].explained and "API Error: 500" in got[ok_crash].explanation
    assert not got[bare].explained and "no error message" in got[bare].explanation
    assert not got[died].explained
    assert got[hook].explained
    assert got[said].explained and "503" in got[said].explanation
    assert not got[silent].explained and "without saying why" in got[silent].explanation
    assert got[nightshift].explained and got[nightshift].attempt_no is None
    assert not week.failures_ok and not week.clean and week.schedules_ok and week.hands_off
    text = cleanweek.format_check(week, cfg.timezone)
    assert "2. Failures: FAIL (3 of 7 without an explanation)" in text
    assert f"! 2026-10-18 02:05  {bare} Attempt 1 (b): UNEXPLAINED" in text
    assert text.endswith("Clean week: no (failures).")


def test_open_issues_explain_an_agent_reported_failure(cfg, db, tmp_path):
    tid = task(cfg, db, "x", la(2026, 10, 18))
    folder = Path(db.get_task(tid)["folder"])
    draft = report.skeleton("x").replace(f"## 1. Result\n\n{report.PLACEHOLDER}",
                                         "## 1. Result\n\n**failed**")
    draft = draft.replace(f"## 7. Open issues\n\n{report.PLACEHOLDER}",
                          "## 7. Open issues\n\nThe posting needs a login.")
    (folder / "report.md").write_text(draft)
    assert cleanweek.report_explanation(folder) == "Open issues: The posting needs a login."
    assert cleanweek.report_explanation(tmp_path / "nowhere") is None


def test_failures_outside_the_period_are_not_listed(cfg, db):
    _failed_attempt(cfg, db, "old", "Attempt 1 crashed: agent exited with code 1",
                    la(2026, 10, 10))
    assert cleanweek.check(cfg, db, END).failures == []


# --------------------------------------------------------------------------
# 3. Manual interventions
# --------------------------------------------------------------------------


def test_cli_changes_approvals_and_run_now_are_listed(cfg, db):
    at = la(2026, 10, 18, 9)
    t = task(cfg, db, "x", at)
    move(db, t, TaskState.PAUSED_USAGE, "paused by hand (orch pause) while queued", at)
    move(db, t, TaskState.QUEUED, "resumed by hand (orch resume) from a by hand pause",
         at + timedelta(minutes=1))
    move(db, t, TaskState.CANCELLED, "cancelled by hand (orch cancel) while queued",
         at + timedelta(minutes=2))
    r = task(cfg, db, "retried", at)
    move(db, r, TaskState.FAILED, "Nightshift error: boom", at)
    move(db, r, TaskState.QUEUED, "retried by hand from failed (orch retry); retry budget reset",
         at + timedelta(minutes=3))
    a = task(cfg, db, "approved", at, needs_approval=True)
    move(db, a, TaskState.QUEUED, "plan approved (orch approve)", at + timedelta(minutes=4))
    d = task(cfg, db, "denied", at, needs_approval=True)
    move(db, d, TaskState.CANCELLED, "plan denied (orch deny)", at + timedelta(minutes=5))
    # Approving the plan while adding the Task is part of asking for it.
    n = task(cfg, db, "added", at, needs_approval=True)
    move(db, n, TaskState.QUEUED, "plan approved (orch add)", at + timedelta(minutes=6))
    # The daemon's own transitions are never interventions.
    move(db, n, TaskState.RUNNING, "picked", at + timedelta(minutes=7))
    add_schedule(db, created=la(2026, 10, 20, 9))
    occurrence(cfg, db, "scan", at + timedelta(minutes=8), at + timedelta(minutes=8),
               trigger="run_now")
    db.record_intervention(at=iso(at + timedelta(minutes=9)), command="orch priority",
                           task_id=n, detail="priority normal -> urgent")
    db.record_intervention(at=iso(at + timedelta(minutes=10)), command="orch raise-cap",
                           detail="monthly spend cap $15.00 -> $25.00 for 2026-10")
    db.record_intervention(at=iso(la(2026, 10, 1)), command="orch raise-cap", detail="old")

    week = cleanweek.check(cfg, db, END)

    assert [i.command for i in week.interventions] == [
        "orch pause", "orch resume", "orch cancel", "orch retry", "orch approve", "orch deny",
        "orch schedule run-now", "orch priority", "orch raise-cap"]
    assert week.interventions[0].task_id == t and "queued -> paused_usage" in week.interventions[0].detail
    # A run-now handles no cron fire: the 21st's and 22nd's are still missed.
    assert [(c.fire_at.day, c.status) for c in week.fires] == [(21, "missed"), (22, "missed")]
    assert not week.hands_off and not week.clean
    text = cleanweek.format_check(week, cfg.timezone)
    assert "3. Manual interventions: FAIL (9 interventions)" in text
    assert "orch priority: priority normal -> urgent" in text


def test_a_cancel_of_a_running_task_by_the_daemon_is_listed(cfg, db):
    t = task(cfg, db, "x", la(2026, 10, 19))
    move(db, t, TaskState.RUNNING, "picked", la(2026, 10, 19))
    move(db, t, TaskState.CANCELLED, "cancelled by hand (orch cancel) during Attempt 1",
         la(2026, 10, 19, 0, 5))
    move(db, task(cfg, db, "y", la(2026, 10, 19)), TaskState.CANCELLED,
         "interrupted from the terminal (Ctrl-C)", la(2026, 10, 19, 1))
    assert [i.command for i in cleanweek.check(cfg, db, END).interventions] == [
        "orch cancel", "by hand"]


# --------------------------------------------------------------------------
# orch clean-week, and the commands that record an intervention
# --------------------------------------------------------------------------


def run(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: END)


def test_orch_clean_week_exits_zero_on_a_clean_week(cfg, db, config_dir, clock):
    on_time_week(cfg, db)
    res = run(config_dir, "clean-week")
    assert res.exit_code == 0, res.output
    assert "Clean week check (Gate 2): 2026-10-15 22:00 to 2026-10-22 22:00" in res.output
    assert "Clean week: yes." in res.output


@pytest.mark.parametrize("spoil", ["schedule", "failure", "intervention"])
def test_orch_clean_week_exits_non_zero_when_any_condition_fails(cfg, db, config_dir, clock,
                                                                  spoil):
    on_time_week(cfg, db, days=range(16, 22) if spoil == "schedule" else range(16, 23))
    if spoil == "failure":
        _failed_attempt(cfg, db, "x", "Attempt 1 crashed: agent exited with code 2",
                        la(2026, 10, 20))
    if spoil == "intervention":
        t = task(cfg, db, "x", la(2026, 10, 20))
        move(db, t, TaskState.CANCELLED, "cancelled by hand (orch cancel) while queued",
             la(2026, 10, 20))
    res = run(config_dir, "clean-week")
    assert res.exit_code == 1, res.output
    want = {"schedule": "scheduled runs", "failure": "failures",
            "intervention": "manual interventions"}[spoil]
    assert f"Clean week: no ({want})." in res.output


def test_orch_clean_week_until_and_days(cfg, db, config_dir, clock):
    on_time_week(cfg, db, days=range(16, 20))  # nothing after the 19th
    assert run(config_dir, "clean-week").exit_code == 1
    res = run(config_dir, "clean-week", "--until", "2026-10-19T12:00", "--days", "3")
    assert res.exit_code == 0, res.output
    assert "2026-10-16 12:00 to 2026-10-19 12:00" in res.output
    assert run(config_dir, "clean-week", "--until", "soon").exit_code == 2


def test_cli_commands_without_a_transition_record_an_intervention(cfg, db, config_dir, clock):
    add_schedule(db)
    tid = create_task(cfg, db, "x").spec.id
    assert run(config_dir, "priority", tid, "urgent").exit_code == 0
    assert run(config_dir, "schedule", "disable", "scan").exit_code == 0
    assert run(config_dir, "schedule", "disable", "scan").exit_code == 0  # no change: no record
    assert run(config_dir, "schedule", "enable", "scan").exit_code == 0
    assert run(config_dir, "raise-cap", "25").exit_code == 0
    assert run(config_dir, "schedule", "remove", "scan").exit_code == 0
    rows = db.interventions()
    assert [r["command"] for r in rows] == [
        "orch priority", "orch schedule disable", "orch schedule enable", "orch raise-cap",
        "orch schedule remove"]
    assert rows[0]["task_id"] == tid and rows[0]["detail"] == "priority normal -> urgent"
    assert rows[1]["schedule_name"] == "scan" and rows[0]["at"] == iso(END)
    assert "$25.00" in rows[3]["detail"]
