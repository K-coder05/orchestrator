"""The "tonight's queue" preview (§4 item 8): queue order on a simulated
clock and the rough usage estimate. All with a fake clock."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

from nightshift import schedules, tonight
from nightshift.config import load_config
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import iso

LA = ZoneInfo("America/Los_Angeles")


def la(*args) -> datetime:
    return datetime(*args, tzinfo=LA)


EVENING = la(2026, 10, 21, 22, 0)

TEMPLATE = {
    "description": "test rescrape",
    "task_type": "scraping",
    "agent": "gemini",
    "estimated_minutes": 40,
    "max_runtime_minutes": 60,
    "long": True,
    "internet": "on",
    "prompt": "Re-scrape. Request: {{request}}",
}


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    tdir = tmp_path / "templates"
    tdir.mkdir()
    (tdir / "rescrape.yaml").write_text(yaml.safe_dump(TEMPLATE))
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


def task(cfg, db, title, *, minutes=30, **kw):
    kw.setdefault("agent", "claude")
    kw.setdefault("task_type", "research")
    kw.setdefault("long", False)
    return create_task(cfg, db, title, title=title, estimated_minutes=minutes, **kw).spec.id


def past_attempt(cfg, db, *, agent="claude", minutes=10, cost=0.5, template=None,
                 task_type="research"):
    """A finished Attempt that used ``cost`` over ``minutes``."""
    tid = create_task(cfg, db, "old work", agent=agent, task_type=task_type,
                      template=template).spec.id
    run = db.start_attempt(tid, agent, ["x"], "log")
    db.finish_attempt(run, exit_code=0, outcome="done", end_reason="done", session_id=None)
    start = la(2026, 10, 20, 2, 0)
    db.conn.execute("UPDATE runs SET started_at = ?, ended_at = ? WHERE id = ?",
                    (iso(start), iso(start + timedelta(minutes=minutes)), run))
    db.conn.commit()
    db.record_usage(run, tid, agent, [{"cost_usd": cost}])
    db.transition(tid, TaskState.RUNNING, "picked")
    db.transition(tid, TaskState.DONE, "finished")
    return tid


def ids(preview):
    return [e.task_id for e in preview.entries]


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------


@pytest.mark.parametrize("now, start", [
    (la(2026, 10, 21, 22, 0), la(2026, 10, 22, 0, 0)),   # evening: the coming night
    (la(2026, 10, 21, 9, 0), la(2026, 10, 22, 0, 0)),    # morning: tonight, not last night
    (la(2026, 10, 22, 3, 0), la(2026, 10, 22, 0, 0)),    # inside it: the one under way
    (la(2026, 10, 22, 7, 0), la(2026, 10, 23, 0, 0)),    # 7am: the window just ended
])
def test_window_is_tonights_long_task_window(cfg, now, start):
    assert tonight.window_bounds(cfg, now) == (start, start + timedelta(hours=7))


def test_window_follows_the_configured_long_window(config_dir):
    data = yaml.safe_load((config_dir / "config.yaml").read_text())
    data["queue"] = {"long_window_start": "23:00", "long_window_end": "05:00"}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(config_dir, host=FakeHost())
    assert tonight.window_bounds(cfg, la(2026, 10, 21, 22, 0)) == (
        la(2026, 10, 21, 23, 0), la(2026, 10, 22, 5, 0))


# --------------------------------------------------------------------------
# Queue order
# --------------------------------------------------------------------------


def test_lists_only_what_runs_between_midnight_and_7am_in_queue_order(cfg, db):
    # At 10pm the short Tasks run right away (gone by midnight); long ones
    # wait for the window.
    task(cfg, db, "short now", minutes=60)
    a = task(cfg, db, "long normal", minutes=90, long=True)
    b = task(cfg, db, "long background", minutes=60, long=True, priority="background")
    c = task(cfg, db, "long urgent", minutes=30, long=True, priority="urgent")

    p = tonight.preview(cfg, db, EVENING)

    # Urgent jumps the long-Task gate and runs at 10pm, right after "short now".
    assert ids(p) == [a, b]
    assert [(e.start, e.end) for e in p.entries] == [
        (la(2026, 10, 22, 0, 0), la(2026, 10, 22, 1, 30)),
        (la(2026, 10, 22, 1, 30), la(2026, 10, 22, 2, 30)),
    ]
    assert c not in ids(p)
    assert "long Task" in p.entries[0].notes
    assert p.left_over == 0


def test_a_deadline_turns_a_task_urgent_as_the_night_goes_on(cfg, db):
    first = task(cfg, db, "normal first", minutes=120, long=True)
    later = task(cfg, db, "due tomorrow 1am", minutes=30, long=True,
                 deadline=iso(la(2026, 10, 23, 1, 0)))
    third = task(cfg, db, "normal third", minutes=30, long=True)

    p = tonight.preview(cfg, db, EVENING)

    # By 2am the deadline is within 24 hours: urgent, ahead of "normal third".
    assert ids(p) == [first, later, third]
    assert p.entries[1].effective_priority == "urgent"
    assert p.entries[1].priority == "normal"


def test_at_holds_a_task_until_its_time(cfg, db):
    late = task(cfg, db, "at 3am", minutes=30, run_at=iso(la(2026, 10, 22, 3, 0)))
    bg = task(cfg, db, "long background", minutes=30, long=True, priority="background")

    p = tonight.preview(cfg, db, EVENING)

    assert ids(p) == [bg, late]
    assert p.entries[1].start == la(2026, 10, 22, 3, 0)
    assert "--at 03:00" in p.entries[1].notes


def test_the_queue_after_7am_is_left_over(cfg, db):
    for n in range(4):
        task(cfg, db, f"long {n}", minutes=150, long=True)
    p = tonight.preview(cfg, db, EVENING)
    # 0:00, 2:30, 5:00 (runs past 7am, still listed); the fourth waits.
    assert len(p.entries) == 3 and p.left_over == 1
    assert p.entries[-1].start == la(2026, 10, 22, 5, 0)


def test_the_running_task_holds_the_queue_until_its_estimate_runs_out(cfg, db):
    running = task(cfg, db, "running", minutes=180)
    db.transition(running, TaskState.RUNNING, "picked")
    run = db.start_attempt(running, "claude", ["x"], "log")
    db.conn.execute("UPDATE runs SET started_at = ? WHERE id = ?",
                    (iso(la(2026, 10, 21, 22, 0)), run))
    db.conn.commit()
    nxt = task(cfg, db, "long next", minutes=30, long=True)

    p = tonight.preview(cfg, db, la(2026, 10, 21, 23, 0))

    assert ids(p) == [running, nxt]
    assert p.entries[0].end == la(2026, 10, 22, 1, 0) and p.entries[0].minutes == 120
    assert "running now" in p.entries[0].notes
    assert p.entries[1].start == la(2026, 10, 22, 1, 0)


def test_retry_waits_and_usage_pauses_join_when_due(cfg, db):
    retry = task(cfg, db, "retrying", minutes=30)
    db.transition(retry, TaskState.RUNNING, "picked")
    db.transition(retry, TaskState.RETRY_WAIT, "crashed",
                  set_fields={"retry_at": iso(la(2026, 10, 22, 1, 0))})
    paused = task(cfg, db, "paused on limit", minutes=30)
    db.transition(paused, TaskState.RUNNING, "picked")
    db.transition(paused, TaskState.PAUSED_USAGE, "limit",
                  set_fields={"pause_kind": "usage_limit", "resume_at": iso(la(2026, 10, 22, 2, 0))})
    by_hand = task(cfg, db, "paused by hand", minutes=30)
    db.transition(by_hand, TaskState.PAUSED_USAGE, "orch pause", set_fields={"pause_kind": "by_hand"})

    p = tonight.preview(cfg, db, EVENING)

    assert ids(p) == [retry, paused]
    assert [e.start for e in p.entries] == [la(2026, 10, 22, 1, 0), la(2026, 10, 22, 2, 0)]


def test_a_task_without_an_estimate_assumes_the_default(cfg, db):
    tid = create_task(cfg, db, "no estimate", long=True, task_type="research").spec.id
    [e] = tonight.preview(cfg, db, EVENING).entries
    assert e.task_id == tid and e.estimated_minutes is None
    assert e.minutes == tonight.DEFAULT_MINUTES
    assert any("no estimate" in n for n in e.notes)


def test_nothing_queued_means_an_empty_preview(cfg, db):
    p = tonight.preview(cfg, db, EVENING)
    assert p.entries == [] and p.totals == [] and p.left_over == 0


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------


def add_schedule(cfg, db, name="rescrape", cron_rule="0 2 * * *", **kw):
    return schedules.add(cfg, db, name, template="rescrape", cron_rule=cron_rule,
                         catch_up="next-wake", at=la(2026, 10, 21, 21, 0), **kw)


def test_a_schedule_firing_tonight_adds_its_occurrence(cfg, db):
    add_schedule(cfg, db)
    first = task(cfg, db, "long first", minutes=180, long=True, agent="gemini")

    p = tonight.preview(cfg, db, EVENING)

    # It fires at 2am, while "long first" runs in the Gemini lane, and goes next.
    assert ids(p) == [first, None]
    occ = p.entries[1]
    assert occ.title == "rescrape 2026-10-22" and occ.schedule == "rescrape"
    assert occ.agent == "gemini" and occ.task_type == "scraping" and occ.minutes == 40
    assert occ.start == la(2026, 10, 22, 3, 0)
    assert "fires 02:00" in occ.notes


def test_disabled_schedules_and_fires_after_7am_add_nothing(cfg, db):
    add_schedule(cfg, db, enabled=False)
    add_schedule(cfg, db, name="morning", cron_rule="30 7 * * *")
    assert tonight.preview(cfg, db, EVENING).entries == []


def test_overlap_skip_drops_a_fire_while_the_last_occurrence_is_unfinished(cfg, db):
    add_schedule(cfg, db, cron_rule="0 1,2 * * *")
    p = tonight.preview(cfg, db, EVENING)
    # 1:00-1:40 runs; at 2:00 it is done, so the 2am fire runs too.
    assert [e.start for e in p.entries] == [la(2026, 10, 22, 1, 0), la(2026, 10, 22, 2, 0)]

    stuck = task(cfg, db, "old occurrence", minutes=30, schedule_name="rescrape")
    db.transition(stuck, TaskState.RUNNING, "picked")
    db.transition(stuck, TaskState.NEEDS_YOU, "asked")
    assert tonight.preview(cfg, db, EVENING).entries == []


def test_a_fire_already_due_counts_as_now(cfg, db):
    add_schedule(cfg, db, cron_rule="0 1 * * *")
    # The 1am fire is due but the daemon hasn't polled yet.
    p = tonight.preview(cfg, db, la(2026, 10, 22, 1, 0, 30))
    assert [e.start for e in p.entries] == [la(2026, 10, 22, 1, 0, 30)]
    assert "fire due now" in p.entries[0].notes


# --------------------------------------------------------------------------
# Usage estimate
# --------------------------------------------------------------------------


def test_estimate_is_minutes_times_past_cost_per_minute(cfg, db):
    past_attempt(cfg, db, minutes=10, cost=0.5)
    past_attempt(cfg, db, minutes=30, cost=1.5)  # $0.05/min overall
    task(cfg, db, "long", minutes=60, long=True)

    [e] = tonight.preview(cfg, db, EVENING).entries

    assert e.estimate.rate.usd_per_minute == pytest.approx(0.05)
    assert e.estimate.cost_usd == pytest.approx(3.0)
    assert e.estimate.rate.attempts == 2


def test_rate_prefers_the_same_template_then_task_type_then_agent(cfg, db):
    past_attempt(cfg, db, minutes=10, cost=1.0, task_type="coding")         # $0.10/min
    past_attempt(cfg, db, minutes=10, cost=0.2, task_type="research")       # $0.02/min
    past_attempt(cfg, db, minutes=10, cost=0.5, template="scan")            # $0.05/min
    h = tonight.CostHistory.load(db)

    assert h.rate("claude", "scan", "research").usd_per_minute == pytest.approx(0.05)
    assert h.rate("claude", "other", "coding").usd_per_minute == pytest.approx(0.10)
    assert h.rate("claude", None, "comedy").usd_per_minute == pytest.approx(1.7 / 30)
    assert h.rate("codex", None, "coding") is None


def test_no_history_means_no_estimate(cfg, db):
    task(cfg, db, "long", minutes=60, long=True)
    p = tonight.preview(cfg, db, EVENING)
    assert p.entries[0].estimate is None
    [t] = p.totals
    assert t.estimated == 0 and t.unestimated == 1 and t.percent_of_baseline is None


def test_totals_per_agent_against_the_shortest_window_baseline(cfg, db):
    past_attempt(cfg, db, minutes=10, cost=1.0)  # $0.10/min
    for cost in (20.0, 25.0):
        db.insert_limit_observation({
            "agent": "claude", "window": "five_hour", "hit_at": iso(la(2026, 10, 19, 12, 0)),
            "source": "test", "window_start": iso(la(2026, 10, 19, 7, 0)), "cost_usd": cost})
    db.insert_limit_observation({
        "agent": "claude", "window": "seven_day", "hit_at": iso(la(2026, 10, 19, 12, 0)),
        "source": "test", "window_start": iso(la(2026, 10, 13, 12, 0)), "cost_usd": 200.0})
    task(cfg, db, "a", minutes=60, long=True)
    task(cfg, db, "b", minutes=40, long=True)
    task(cfg, db, "c", minutes=30, long=True, agent="codex", task_type="coding")

    totals = {t.agent: t for t in tonight.preview(cfg, db, EVENING).totals}

    claude = totals["claude"]
    assert claude.cost_usd == pytest.approx(10.0) and claude.minutes == 100
    assert claude.window == "five_hour" and claude.percent_of_baseline == pytest.approx(50.0)
    assert totals["codex"].estimated == 0 and totals["codex"].percent_of_baseline is None


# --------------------------------------------------------------------------
# Two lanes (#45): the Claude lane and the Gemini lane run side by side
# --------------------------------------------------------------------------


def test_each_lane_runs_its_own_tasks_side_by_side(cfg, db):
    c1 = task(cfg, db, "claude one", minutes=120, long=True)
    c2 = task(cfg, db, "codex two", minutes=60, long=True, agent="codex", task_type="coding")
    g1 = task(cfg, db, "gemini one", minutes=90, long=True, agent="gemini")
    g2 = task(cfg, db, "gemini two", minutes=30, long=True, agent="gemini")

    p = tonight.preview(cfg, db, EVENING)

    # Codex shares Claude's lane: it waits for "claude one"; the Gemini
    # Tasks don't wait for either.
    by_id = {e.task_id: e for e in p.entries}
    assert ids(p) == [c1, g1, g2, c2]
    assert (by_id[c1].start, by_id[c1].end) == (la(2026, 10, 22, 0, 0), la(2026, 10, 22, 2, 0))
    assert (by_id[g1].start, by_id[g1].end) == (la(2026, 10, 22, 0, 0), la(2026, 10, 22, 1, 30))
    assert by_id[g2].start == la(2026, 10, 22, 1, 30)
    assert by_id[c2].start == la(2026, 10, 22, 2, 0)
    assert [e.lane for e in p.entries] == ["claude", "gemini", "gemini", "claude"]


def test_both_running_tasks_hold_their_own_lanes(cfg, db):
    at = la(2026, 10, 21, 23, 0)
    started = la(2026, 10, 21, 22, 0)
    running = {}
    for agent, minutes in (("claude", 180), ("gemini", 150)):
        tid = task(cfg, db, f"running {agent}", minutes=minutes, agent=agent)
        db.transition(tid, TaskState.RUNNING, "picked")
        run = db.start_attempt(tid, agent, ["x"], "log")
        db.conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (iso(started), run))
        db.conn.commit()
        running[agent] = tid
    nc = task(cfg, db, "next claude", minutes=30, long=True)
    ng = task(cfg, db, "next gemini", minutes=30, long=True, agent="gemini")

    p = tonight.preview(cfg, db, at)

    by_id = {e.task_id: e for e in p.entries}
    assert ids(p) == [running["claude"], running["gemini"], ng, nc]
    assert all("running now" in by_id[t].notes for t in running.values())
    assert by_id[running["claude"]].end == la(2026, 10, 22, 1, 0)
    assert by_id[running["gemini"]].end == la(2026, 10, 22, 0, 30)
    assert by_id[ng].start == la(2026, 10, 22, 0, 30)  # each waits for its own lane
    assert by_id[nc].start == la(2026, 10, 22, 1, 0)


def test_a_cap_of_one_runs_one_task_at_a_time(config_dir):
    data = yaml.safe_load((config_dir / "config.yaml").read_text())
    data["daemon"] = {"max_running": 1}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(config_dir, host=FakeHost())
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        c = task(cfg, db, "claude", minutes=60, long=True)
        g = task(cfg, db, "gemini", minutes=60, long=True, agent="gemini")
        p = tonight.preview(cfg, db, EVENING)
    finally:
        db.close()
    assert ids(p) == [c, g]
    assert p.entries[1].start == la(2026, 10, 22, 1, 0)
