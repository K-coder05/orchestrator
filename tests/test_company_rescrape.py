"""The company-rescrape template and Schedule (§4 item 6): Gemini re-scrapes
the target career pages every 2 days at 2am; Nightshift snapshots and diffs
them and merges the matching postings into the Tracker."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from nightshift import cli, schedules, snapshots, templates, tracker
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.states import TaskState

from test_gate2 import Mac, la
from test_gemini import genv  # noqa: F401  (pytest fixture)
from test_runner import env  # noqa: F401  (pytest fixture)
from test_tracker import (PAGES, _row, scan_env, scan_provider, scfg,  # noqa: F401  (fixtures)
                          scfg_dir)

ACME_SWE = _row(url="https://acme.com/jobs/1")
ACME_ML = _row(role="ML Engineer Intern", url="https://acme.com/jobs/2")


def _page(url, company, *rows, loaded=True, extra=()):
    """A career page as the agent lists it: every internship posting on it
    (``extra``: ones that don't match §1.8, so they are never proposed)."""
    return {"url": url, "company": company, "loaded": loaded,
            "error": None if loaded else "HTTP 403",
            "postings": [{"title": r["role"], "location": r["location"], "url": r["posting_url"]}
                         for r in rows] + [{"title": t, "location": "SF", "url": u}
                                           for t, u in extra]}


@pytest.fixture
def rescrape(scan_env, monkeypatch, tmp_path):  # noqa: F811
    """Shape the next Attempt: the agent's career pages and Proposed rows."""
    pages_file = tmp_path / "pages.json"

    def agent(pages, *rows):
        pages_file.write_text(json.dumps({"pages": pages}))
        monkeypatch.setenv("FAKE_PAGES", str(pages_file))
        scan_env(*rows)
    return agent


def _add(config_dir) -> str:
    res = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "add",
                                        "Re-scrape the target career pages", "--template",
                                        "company-rescrape"])
    assert res.exit_code == 0, res.output
    return re.search(r"Task (\w{4}) queued", res.output).group(1)


def _run(cfg, task_id, when=la(2026, 10, 22, 2, 0)):
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        QueueRunner(cfg, db, FakeHost(), adapter_provider=scan_provider,
                    clock=lambda: when).run_next()
        return db.get_task(task_id)
    finally:
        db.close()


def _report(row) -> str:
    return (Path(row["folder"]) / "report.md").read_text()


def test_template(scfg):
    tpl = templates.load(scfg, "company-rescrape")
    assert (tpl.task_type, tpl.agent, tpl.internet, tpl.long) == ("scraping", "gemini", "on", True)
    assert tpl.schedule == {"cron": "0 2 */2 * *", "catch_up": "next-wake", "overlap": "skip",
                            "request": "Re-scrape the target companies' career pages"}
    assert [h["hook"] for h in tpl.post_hooks] == [
        "require_outputs", "snapshot_pages", "merge_tracker"]
    p = templates.instantiate(scfg, tpl, "x").prompt
    for page in PAGES:
        assert f"- {page}" in p
    assert "output/career_pages.json" in p and "output/proposed_rows.csv" in p
    assert "Summer 2027 internship" in p and "LinkedIn" in p and "{{" not in p


def test_two_runs_snapshot_diff_and_merge(scfg, scfg_dir, rescrape):
    acme, globex = PAGES
    rescrape([_page(acme, "Acme", ACME_SWE, extra=[("Sales Intern", "https://acme.com/jobs/9")]),
              _page(globex, "Globex")], ACME_SWE)
    first = _add(scfg_dir)
    row1 = _run(scfg, first)
    assert row1["state"] == TaskState.DONE and row1["agent"] == "gemini"
    root = scfg.data_dir / "snapshots" / "company-rescrape"
    [snap1] = list(root.iterdir())
    assert snap1.name.endswith("_" + first)
    assert (snap1 / "snapshot.json").is_file() and (snap1 / "diff.md").is_file()
    rep1 = _report(row1)
    assert "1 new posting appended" in rep1.split("### New postings", 1)[1]
    assert "first snapshot" in rep1

    # Run 2: Acme adds the ML role, Globex fails to load.
    rescrape([_page(acme, "Acme", ACME_SWE, ACME_ML,
                    extra=[("Sales Intern", "https://acme.com/jobs/9")]),
              _page(globex, "Globex", loaded=False)], ACME_SWE, ACME_ML)
    second = _add(scfg_dir)
    row2 = _run(scfg, second, la(2026, 10, 24, 2, 0))
    assert row2["state"] == TaskState.DONE
    assert [r["role"] for r in tracker.read_tracker(scfg.tracker_path)] == [
        "Software Engineer Intern", "ML Engineer Intern"]
    rec = snapshots.load(root, second)
    assert rec.previous == first and (rec.added, rec.removed) == (1, 0)
    assert [p.status for p in rec.pages] == ["compared", "failed"]

    # The report lists only New postings; the diff is counts and a path.
    rep2 = _report(row2)
    new = rep2.split("### New postings", 1)[1]
    assert "1 new posting appended" in new and "ML Engineer Intern" in new
    assert "Software Engineer Intern" not in rep2.split("### New postings", 1)[1]
    assert "Sales Intern" not in rep2
    snap = rep2.split("### Career pages snapshot", 1)[1]
    assert f"1 posting added, 0 removed since Task {first}" in snap
    assert "Failed to load: Globex" in snap and str(rec.folder / "diff.md") in snap


def test_bad_pages_file_is_a_failed_attempt_and_nothing_merges(scfg, scfg_dir, rescrape,
                                                                tmp_path, monkeypatch):
    rescrape([], ACME_SWE)
    (tmp_path / "pages.json").write_text('{"pages": "all of them"}')
    row = _run(scfg, _add(scfg_dir))
    assert row["state"] == TaskState.RETRY_WAIT
    log = (Path(row["folder"]) / "logs" / "nightshift.log").read_text()
    assert 'snapshot_pages failed: career pages file must be {"pages": [...]}' in log
    assert not scfg.tracker_path.exists()
    assert not (scfg.data_dir / "snapshots").exists()


def test_long_task_waits_for_the_overnight_window(scfg, scfg_dir, rescrape):
    rescrape([_page(PAGES[0], "Acme", ACME_SWE)], ACME_SWE)
    task = _add(scfg_dir)
    assert _run(scfg, task, la(2026, 10, 22, 9, 0))["state"] == TaskState.QUEUED
    assert _run(scfg, task, la(2026, 10, 23, 0, 30))["state"] == TaskState.DONE


# --------------------------------------------------------------------------
# The Schedule
# --------------------------------------------------------------------------


@pytest.fixture
def sched(scfg, scfg_dir, rescrape, monkeypatch):  # noqa: F811
    """`orch schedule add company-rescrape` on Oct 20 (an even day), noon."""
    mac = Mac(la(2026, 10, 20, 12, 0))
    monkeypatch.setattr(cli, "clock", mac.clock)
    res = CliRunner().invoke(cli.main, ["--config-dir", str(scfg_dir), "schedule", "add",
                                        "company-rescrape"])
    assert res.exit_code == 0, res.output
    db = Database(scfg.db_path, tz=scfg.timezone)
    logs: list[str] = []
    runner = QueueRunner(scfg, db, mac.host, adapter_provider=scan_provider, clock=mac.clock,
                         log=logs.append)
    runner.check_wake()
    yield mac, db, runner, logs, res.output
    db.close()


def test_standard_schedule(sched):
    _, db, _, _, out = sched
    assert ("Schedule company-rescrape added (enabled): 0 2 */2 * * (America/Los_Angeles), "
            "template company-rescrape, catch-up run on next wake, overlap skip") in out
    assert "Next fire: 2026-10-21T02:00:00-07:00" in out
    s = schedules.get(db, "company-rescrape")
    assert s.catch_up.kind == schedules.CATCH_UP_NEXT_WAKE
    fires = [s.cron.next_after(la(2026, 10, d, 3, 0), la(2026, 1, 1).tzinfo).day
             for d in (21, 23)]
    assert fires == [23, 25]


def test_a_run_missed_asleep_catches_up_once_on_wake(sched, scfg, rescrape):
    mac, db, runner, logs, _ = sched
    rescrape([_page(PAGES[0], "Acme", ACME_SWE)], ACME_SWE)
    # Lid shut Oct 20 at 23:00, opened Oct 21 at 8:15: the 2am fire was missed.
    mac.awake(hours=11)
    runner.check_wake()
    mac.sleep_until(la(2026, 10, 21, 8, 15))
    assert runner.check_wake() is True
    caught = [line for line in logs if "after wake: Schedule company-rescrape" in line]
    assert len(caught) == 1 and "fire at 2026-10-21T02:00:00-07:00" in caught[0]
    [occ] = db.occurrences("company-rescrape")
    # A long Task: queued now, runs in the overnight window.
    assert runner.run_next() is None
    assert db.get_task(occ["id"])["state"] == TaskState.QUEUED
    mac.awake(hours=16, minutes=30)  # 00:45 on Oct 22
    assert runner.check_wake() is False
    assert runner.run_next() == occ["id"]
    assert db.get_task(occ["id"])["state"] == TaskState.DONE
    # Exactly once: no second Occurrence for that fire.
    assert len(db.occurrences("company-rescrape")) == 1
    assert [(r["trigger"], r["outcome"]) for r in db.schedule_runs("company-rescrape")] == [
        ("wake", "created")]


def test_days_asleep_collapse_into_one_occurrence(sched, rescrape):
    mac, db, runner, logs, _ = sched
    rescrape([_page(PAGES[0], "Acme", ACME_SWE)], ACME_SWE)
    mac.awake(hours=1)
    runner.check_wake()
    mac.sleep_until(la(2026, 10, 26, 1, 0))  # fires on the 21st, 23rd, 25th missed
    assert runner.check_wake() is True
    [run] = db.schedule_runs("company-rescrape")
    assert (run["outcome"], run["fires"], run["fire_at"]) == (
        "created", 3, "2026-10-25T02:00:00-07:00")
    assert runner.run_next() is not None and runner.run_next() is None
