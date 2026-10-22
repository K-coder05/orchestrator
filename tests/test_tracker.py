"""The Tracker merge (§1.7) and the posting-scan template (§3.1 item 9)."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, templates, tracker
from nightshift.config import load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.permissions import build_policy
from nightshift.states import TaskState

from test_gemini import fake_gemini
from test_gemini import genv  # noqa: F401  (pytest fixture)
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
TODAY = date(2026, 10, 21)
HEADER = ",".join(tracker.COLUMNS)


def _csv(*rows: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=tracker.COLUMNS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in tracker.COLUMNS})
    return buf.getvalue()


def _row(company="Acme", role="Software Engineer Intern", url="https://boards.greenhouse.io/acme/jobs/1",
         **kw) -> dict:
    base = {"company": company, "role": role, "location": "San Francisco, CA",
            "season": "Summer 2027", "posting_url": url,
            "source": "https://github.com/example/Summer2027-Internships"}
    base.update(kw)
    return base


def _merge(path: Path, *rows: dict, own=(), text: str | None = None) -> tracker.MergeResult:
    return tracker.merge(path, text if text is not None else _csv(*rows), season="Summer 2027",
                         today=TODAY, denied_domains=["linkedin.com", "lnkd.in"], own_ids=own)


def _tracker_rows(path: Path) -> list[dict]:
    return tracker.read_tracker(path)


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


@pytest.mark.parametrize("a, b", [
    ("https://boards.greenhouse.io/acme/jobs/123", "https://boards.greenhouse.io/acme/jobs/123/"),
    ("https://boards.greenhouse.io/acme/jobs/123", "HTTPS://Boards.Greenhouse.IO/Acme/Jobs/123"),
    ("https://boards.greenhouse.io/acme/jobs/123", "http://www.boards.greenhouse.io/acme/jobs/123"),
    ("https://boards.greenhouse.io/acme/jobs/123",
     "https://boards.greenhouse.io/acme/jobs/123?utm_source=github&utm_medium=list&gh_src=abc"),
    ("https://jobs.lever.co/acme/abc", "https://jobs.lever.co/acme/abc?lever-source=Simplify&ref=x#apply"),
    ("https://acme.com/careers?jid=5&team=eng", "https://acme.com:443/careers/?team=eng&jid=5"),
    ("https://acme.com/jobs/1?gclid=1&fbclid=2", "https://acme.com/jobs/1"),
    ("acme.com/jobs/1", "https://acme.com/jobs/1"),
    ("https://acme.com//jobs//1", "https://acme.com/jobs/1"),
])
def test_normalize_url_same_posting(a, b):
    assert tracker.normalize_url(a) == tracker.normalize_url(b)


@pytest.mark.parametrize("a, b", [
    # The job id in a query parameter is not tracking.
    ("https://acme.com/careers?gh_jid=1", "https://acme.com/careers?gh_jid=2"),
    ("https://boards.greenhouse.io/acme/jobs/1", "https://boards.greenhouse.io/acme/jobs/2"),
    ("https://acme.com/jobs/1", "https://other.com/jobs/1"),
    ("https://acme.com:8443/jobs/1", "https://acme.com/jobs/1"),
])
def test_normalize_url_different_postings(a, b):
    assert tracker.normalize_url(a) != tracker.normalize_url(b)


def test_normalize_url_empty():
    assert tracker.normalize_url("") == "" and tracker.normalize_url("   ") == ""


@pytest.mark.parametrize("a, b", [
    ("Acme", "acme"), ("Acme, Inc.", "Acme"), ("Acme Inc", "ACME LLC"), ("The Acme Co.", "Acme"),
    ("Jane Street", "Jane  Street"), ("AT&T", "AT and T"),
])
def test_normalize_company(a, b):
    assert tracker.normalize_company(a) == tracker.normalize_company(b)


def test_normalize_company_keeps_distinct_names():
    assert tracker.normalize_company("Acme") != tracker.normalize_company("Acme Robotics")
    # A suffix word alone is still a name.
    assert tracker.normalize_company("Labs") == "labs"


@pytest.mark.parametrize("a, b", [
    ("Software Engineer Intern", "software engineering internship"),
    ("SWE Intern", "Software Engineer Intern"),
    ("Software Engineer Intern - Backend", "Software Engineer Intern, Backend"),
    ("ML Engineer Intern", "Machine Learning Engineering Intern"),
    ("Infra Engineer Intern", "Infrastructure Engineer Intern"),
    ("Software Developer Intern", "Software Engineer Intern"),
])
def test_normalize_role(a, b):
    assert tracker.normalize_role(a) == tracker.normalize_role(b)


def test_normalize_role_keeps_distinct_roles():
    assert tracker.normalize_role("Backend Engineer Intern") != tracker.normalize_role(
        "Frontend Engineer Intern")


@pytest.mark.parametrize("text", ["Summer 2027", "summer 2027", "2027 Summer", "Summer '27",
                                  "SU27", "Summer-2027", "summer'27"])
def test_normalize_season(text):
    assert tracker.normalize_season(text) == "summer 2027"


def test_normalize_season_other_seasons_differ():
    assert tracker.normalize_season("Fall 2027") == "fall 2027"
    assert tracker.normalize_season("Summer 2026") != tracker.normalize_season("Summer 2027")


def test_row_key():
    assert tracker.row_key(_row(company="Acme, Inc.", role="SWE Internship", season="2027 Summer")) \
        == tracker.row_key(_row()) == ("acme", "software engineer intern", "summer 2027")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _valid(raw):
    return tracker.validate_row(raw, season="Summer 2027", today=TODAY,
                                denied_domains=["linkedin.com"])


def test_validate_sets_nightshift_fields():
    row, why = _valid(_row(id="99", status="applied", resume_version="k3f9"))
    assert why is None
    assert row["id"] == "" and row["status"] == "new" and row["resume_version"] == ""
    assert row["date_found"] == "2026-10-21"


@pytest.mark.parametrize("change, reason", [
    ({"company": ""}, "missing company"),
    ({"posting_url": " "}, "missing posting_url"),
    ({"source": ""}, "missing source"),
    ({"posting_url": "ftp://acme.com/x"}, "not an http"),
    ({"posting_url": "acme jobs page"}, "not an http"),
    ({"posting_url": "https://www.linkedin.com/jobs/view/1"}, "denied domain"),
    ({"season": "Summer 2026"}, "is not Summer 2027"),
    ({"season": "Fall 2027"}, "is not Summer 2027"),
    ({"role": "Software Engineer Intern (PhD)"}, "MS/PhD-only"),
    ({"role": "Software Engineer, New Grad 2027"}, "new grad"),
    ({"role": "Software Engineer (Full-Time)"}, "full-time"),
    ({"role": "Software Engineer Intern - TS/SCI Clearance"}, "security clearance"),
    ({"date_found": "Oct 21"}, "date_found"),
    ({"date_found": "2026-02-30"}, "date_found"),
])
def test_validate_rejects(change, reason):
    row, why = _valid(_row(**change))
    assert row is None and reason in why


def test_validate_bad_deadline_moves_to_notes():
    row, why = _valid(_row(deadline="rolling", notes="low-priority location"))
    assert why is None and row["deadline"] == ""
    assert row["notes"] == "low-priority location; deadline: rolling"
    row, _ = _valid(_row(deadline="2026-12-01"))
    assert row["deadline"] == "2026-12-01"


def test_validate_cleans_values():
    row, _ = _valid(_row(company="=HYPERLINK(\"x\")", notes="a\nb\tc", role="SWE Intern " + "x" * 600))
    assert row["company"].startswith("'=")  # never a spreadsheet formula
    assert row["notes"] == "a b c"
    assert len(row["role"]) == tracker.MAX_FIELD


# --------------------------------------------------------------------------
# Reading Proposed rows
# --------------------------------------------------------------------------


def test_read_proposed_header_must_match():
    with pytest.raises(tracker.TrackerError, match="missing notes"):
        tracker.read_proposed("id,company,role,location,season,posting_url,source,date_found,"
                              "deadline,status,resume_version\n")
    with pytest.raises(tracker.TrackerError, match="unknown salary"):
        tracker.read_proposed(HEADER + ",salary\n")
    with pytest.raises(tracker.TrackerError, match="empty"):
        tracker.read_proposed("")


def test_read_proposed_any_column_order_and_case():
    cols = list(reversed(tracker.COLUMNS))
    text = ",".join(c.upper() for c in cols) + "\n" + ",".join(
        {"company": "Acme", "role": "SWE Intern"}.get(c, "") for c in cols) + "\n"
    p = tracker.read_proposed("﻿" + text)
    [(line, raw)] = p.rows
    assert line == 2 and raw["company"] == "Acme" and raw["role"] == "SWE Intern"


def test_read_proposed_rejects_ragged_rows_and_skips_blank_lines():
    p = tracker.read_proposed(HEADER + "\n\n,Acme,SWE\n" + ",,,,,,,,,,,\n" + ",Acme" + "," * 10 + "\n")
    assert len(p.rows) == 1 and len(p.invalid) == 1 and "3 fields" in p.invalid[0].reason
    assert p.rows[0][0] == 5 and p.invalid[0].line == 3


# --------------------------------------------------------------------------
# Merge and dedupe
# --------------------------------------------------------------------------


def test_merge_creates_tracker_and_appends_new(tmp_path):
    path = tmp_path / "data" / "applications.csv"
    res = _merge(path, _row(), _row(company="Globex", url="https://globex.com/jobs/7"))
    assert [r["id"] for r in res.appended] == ["1", "2"]
    assert res.dropped_duplicates == 0 and not res.invalid and res.proposed == 2
    text = path.read_text()
    assert text.splitlines()[0] == HEADER
    rows = _tracker_rows(path)
    assert [(r["id"], r["company"], r["status"]) for r in rows] == [
        ("1", "Acme", "new"), ("2", "Globex", "new")]


def test_merge_dedupes_by_normalized_url_then_key(tmp_path):
    path = tmp_path / "applications.csv"
    _merge(path, _row())
    res = _merge(
        path,
        # Same posting URL, different wording: dupe by URL.
        _row(company="ACME Inc", role="Backend Intern",
             url="https://boards.greenhouse.io/acme/jobs/1/?utm_source=simplify"),
        # Same company+role+season, another URL (e.g. the career page): dupe by key.
        _row(company="Acme, Inc.", role="SWE Internship", url="https://acme.com/careers/swe-intern"),
        # New.
        _row(role="Backend Engineer Intern", url="https://boards.greenhouse.io/acme/jobs/2"),
    )
    assert [d.by for d in res.duplicates] == ["posting_url", "company, role, season"]
    assert all(d.matches == "1" and not d.own for d in res.duplicates)
    assert [r["role"] for r in res.appended] == ["Backend Engineer Intern"]
    assert res.appended[0]["id"] == "2" and res.dropped_duplicates == 2
    assert len(_tracker_rows(path)) == 2


def test_merge_dedupes_within_the_batch(tmp_path):
    path = tmp_path / "applications.csv"
    res = _merge(path, _row(), _row(url="https://boards.greenhouse.io/acme/jobs/1?gh_src=x"),
                 _row(company="acme", url="https://acme.com/other"))
    assert len(res.appended) == 1
    assert [(d.by, d.matches) for d in res.duplicates] == [
        ("posting_url", ""), ("company, role, season", "")]
    assert res.dropped_duplicates == 2


def test_merge_prefers_a_matching_location_for_one_role(tmp_path):
    path = tmp_path / "applications.csv"
    res = _merge(path,
                 _row(url="https://acme.com/jobs/austin", location="Austin, TX",
                      notes="low-priority location"),
                 _row(url="https://acme.com/jobs/sf"),
                 _row(company="Globex", url="https://globex.com/1"))
    assert [r["posting_url"] for r in res.appended] == ["https://acme.com/jobs/sf",
                                                        "https://globex.com/1"]
    [dup] = res.duplicates
    assert dup.row["location"] == "Austin, TX" and dup.by == "company, role, season"


def test_merge_twice_adds_nothing(tmp_path):
    path = tmp_path / "applications.csv"
    rows = [_row(), _row(company="Globex", url="https://globex.com/jobs/7")]
    _merge(path, *rows)
    before = path.read_bytes()
    res = _merge(path, *rows)
    assert not res.appended and res.dropped_duplicates == 2
    assert path.read_bytes() == before  # untouched


def test_merge_keeps_existing_rows_byte_for_byte(tmp_path):
    path = tmp_path / "applications.csv"
    existing = (HEADER + "\n"
                + "7,Initech,\"SWE Intern, Platform\",NYC,Summer 2027,https://initech.com/j/1,me,"
                  "2026-10-01,,applied,k3f9,\"my note, with comma\"")  # no final newline
    path.write_text(existing)
    res = _merge(path, _row())
    assert res.appended[0]["id"] == "8"  # after the highest numeric id
    text = path.read_text()
    assert text.startswith(existing + "\n")
    rows = _tracker_rows(path)
    assert rows[0]["status"] == "applied" and rows[0]["resume_version"] == "k3f9"
    assert rows[1]["id"] == "8" and rows[1]["status"] == "new"


def test_merge_ignores_non_numeric_ids(tmp_path):
    path = tmp_path / "applications.csv"
    path.write_text(_csv({**_row(company="Old", url="https://old.com/1"), "id": "abc"}))
    assert _merge(path, _row()).appended[0]["id"] == "1"


def test_merge_refuses_a_tracker_with_other_columns(tmp_path):
    path = tmp_path / "applications.csv"
    path.write_text("company,role\nAcme,SWE\n")
    with pytest.raises(tracker.TrackerError, match="not touching it"):
        _merge(path, _row())
    assert path.read_text() == "company,role\nAcme,SWE\n"


def test_merge_header_only_proposed_file_creates_empty_tracker(tmp_path):
    path = tmp_path / "applications.csv"
    res = _merge(path, text=HEADER + "\n")
    assert res.proposed == 0 and not res.appended
    assert path.read_text() == HEADER + "\n"


def test_merge_rejected_rows_are_never_appended(tmp_path):
    path = tmp_path / "applications.csv"
    res = _merge(path, _row(season="Summer 2026"), _row(role="New Grad SWE"),
                 _row(posting_url="https://linkedin.com/jobs/1"), _row(company=""))
    assert not res.appended and len(res.invalid) == 4
    assert [r.line for r in res.invalid] == [2, 3, 4, 5]
    assert _tracker_rows(path) == []


def test_merge_own_rows_from_an_earlier_attempt(tmp_path):
    path = tmp_path / "applications.csv"
    first = _merge(path, _row())
    res = _merge(path, _row(), _row(company="Globex", url="https://globex.com/jobs/7"),
                 own=[r["id"] for r in first.appended])
    assert [d.own for d in res.duplicates] == [True]
    assert res.dropped_duplicates == 0 and len(res.already_appended) == 1
    assert [r["company"] for r in res.appended] == ["Globex"]


def test_merge_result_roundtrips_through_json(tmp_path):
    res = _merge(tmp_path / "a.csv", _row(), _row(), _row(season="x"))
    back = tracker.MergeResult.from_dict(json.loads(json.dumps(res.as_dict())))
    assert back == res


def test_new_postings_section():
    text = tracker.new_postings_section(
        [{"company": "A|B", "role": "SWE Intern", "location": "NYC", "deadline": "",
          "posting_url": "https://a.com/(1)", "notes": ""}],
        dropped_duplicates=3, invalid=1, tracker_path=Path("/x/applications.csv"))
    assert "1 new posting appended" in text and "A\\|B" in text
    assert "[link](https://a.com/(1%29)" in text
    assert "Dropped as duplicates: 3." in text and "Rejected as invalid or screened out: 1" in text
    empty = tracker.new_postings_section([], dropped_duplicates=2, invalid=0,
                                         tracker_path=Path("/x"))
    assert "No new postings" in empty and "Dropped as duplicates: 2." in empty
    assert "Rejected" not in empty


# --------------------------------------------------------------------------
# The posting-scan template
# --------------------------------------------------------------------------

REPOS = ["https://github.com/example/Summer2027-Internships"]
PAGES = ["https://acme.com/careers", "https://globex.com/jobs"]


@pytest.fixture
def scfg_dir(tmp_path):
    d = tmp_path / "sconfig"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "templates": {"posting_scan": {"tracker_repos": REPOS, "career_pages": PAGES}},
        "retries": {"max_retries": 1, "wait_minutes": [0]},
    }))
    return d


@pytest.fixture
def scfg(scfg_dir):
    return load_config(scfg_dir, host=FakeHost())


def scan_provider(name, settings):
    """posting-scan runs on Gemini; Claude is its failover (task_types.scraping)."""
    return fake_gemini(settings) if name == "gemini" else fake_provider(name, settings)


@pytest.fixture
def scan_env(env, genv, monkeypatch, tmp_path):  # noqa: F811
    for k in ("FAKE_RESUME", "FAKE_PROGRESS", "FAKE_STEP_DELAY"):
        monkeypatch.delenv(k, raising=False)
    proposed = tmp_path / "proposed.csv"

    def propose(*rows, text=None):
        proposed.write_text(text if text is not None else _csv(*rows))
        monkeypatch.setenv("FAKE_PROPOSED", str(proposed))
    return propose


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _add_scan(config_dir) -> str:
    res = _orch(config_dir, "add", "Scan for new internship postings", "--template", "posting-scan")
    assert res.exit_code == 0, res.output
    return re.search(r"Task (\w{4}) queued", res.output).group(1)


def _run(cfg, task_id):
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        clock = lambda: datetime(2026, 10, 21, 6, 30, tzinfo=LA)  # noqa: E731
        QueueRunner(cfg, db, FakeHost(), adapter_provider=scan_provider, clock=clock).run_next()
        return db.get_task(task_id)
    finally:
        db.close()


def test_posting_scan_template_loads(scfg):
    tpl = templates.load(scfg, "posting-scan")
    assert (tpl.task_type, tpl.agent, tpl.internet, tpl.long) == ("scraping", "gemini", "on", False)
    assert tpl.estimated_minutes <= 30  # a daily scan never waits for plan approval
    assert [h["hook"] for h in tpl.post_hooks] == ["require_outputs", "merge_tracker"]
    assert tpl.post_hooks[-1] == {"hook": "merge_tracker", "proposed": "output/proposed_rows.csv"}


def test_posting_scan_prompt_has_sources_rules_and_columns(scfg):
    inst = templates.instantiate(scfg, templates.load(scfg, "posting-scan"), "Daily scan")
    p = inst.prompt
    for src in REPOS + PAGES:
        assert f"- {src}" in p
    assert "Summer 2027 internship" in p and "low-priority location" in p
    for rule in ("MS/PhD-only", "new-grad or full-time", "security clearance", "LinkedIn"):
        assert rule in p
    assert HEADER in p and "output/proposed_rows.csv" in p
    assert "{{" not in p


def test_posting_scan_prompt_without_sources(cfg):
    inst = templates.instantiate(cfg, templates.load(cfg, "posting-scan"), "x")
    assert inst.values["tracker_repos"] == inst.values["career_pages"] == "(none configured)"
    assert "If both lists say \"(none configured)\", write `needs_you.json`" in inst.prompt


def test_template_values_are_validated(cfg, tmp_path):
    base = {"task_type": "general", "agent": "claude", "estimated_minutes": 5,
            "max_runtime_minutes": 10, "long": False, "internet": "off", "prompt": "{{x}}"}
    with pytest.raises(templates.TemplateError, match="needs config"):
        templates.parse(cfg, "t", tmp_path / "t.yaml", {**base, "values": {"x": "a.b"}})
    with pytest.raises(templates.TemplateError, match="defined twice"):
        templates.parse(cfg, "t", tmp_path / "t.yaml",
                        {**base, "values": {"request": {"config": "a"}, "x": {"config": "b"}}})
    with pytest.raises(templates.TemplateError, match="merge_tracker has unknown keys"):
        templates.parse(cfg, "t", tmp_path / "t.yaml", {**base, "values": {"x": {"config": "a"}},
                        "post_hooks": [{"hook": "merge_tracker", "proposed": "o.csv", "to": "x"}]})
    assert templates.render_value(["a", {"company": "Acme", "url": "u"}]) == \
        "- a\n- company: Acme, url: u"
    assert templates.render_value([]) == templates.render_value(None) == "(none configured)"
    assert templates.render_value("Summer 2027") == "Summer 2027"


def _report(row) -> str:
    return (Path(row["folder"]) / "report.md").read_text()


def test_posting_scan_end_to_end_two_runs_no_duplicates(scfg, scfg_dir, scan_env):
    a = _row()
    b = _row(company="Globex", role="Backend Engineer Intern", url="https://globex.com/jobs/7",
             location="Austin, TX", notes="low-priority location")
    scan_env(a, b, _row(role="Software Engineer, New Grad"))
    first = _add_scan(scfg_dir)
    row = _run(scfg, first)
    assert row["state"] == TaskState.DONE
    rows = _tracker_rows(scfg.tracker_path)
    assert [(r["id"], r["company"], r["status"]) for r in rows] == [
        ("1", "Acme", "new"), ("2", "Globex", "new")]
    rep = _report(row)
    new = rep.split("### New postings", 1)[1]
    assert "2 new postings appended" in new and "Acme" in new and "Globex" in new
    assert "Dropped as duplicates: 0." in new and "Rejected as invalid or screened out: 1" in new
    assert "New Grad" not in new
    log = (Path(row["folder"]) / "logs" / "nightshift.log").read_text()
    assert "tracker merge: 3 proposed row(s): 2 appended" in log
    assert "rejected line 4: screened out: new grad" in log

    # Second run: the same two postings (one under a tracking URL) plus one new.
    c = _row(company="Initech", url="https://initech.com/jobs/swe-intern", location="Remote (US)")
    scan_env({**a, "posting_url": a["posting_url"] + "?utm_source=github"}, b, c)
    second = _add_scan(scfg_dir)
    row2 = _run(scfg, second)
    assert row2["state"] == TaskState.DONE
    rows = _tracker_rows(scfg.tracker_path)
    assert [r["company"] for r in rows] == ["Acme", "Globex", "Initech"]
    assert len({tracker.normalize_url(r["posting_url"]) for r in rows}) == 3
    new2 = _report(row2).split("### New postings", 1)[1]
    assert "1 new posting appended" in new2 and "Initech" in new2
    assert "Acme" not in new2 and "Globex" not in new2
    assert "Dropped as duplicates: 2." in new2

    db = Database(scfg.db_path, tz=scfg.timezone)
    try:
        [m] = db.tracker_merges(second)
        assert (m["proposed"], m["appended"], m["duplicates"], m["invalid"]) == (3, 1, 2, 0)
        assert m["run_id"] == db.attempts(second)[-1]["id"]
        assert [a["agent"] for a in db.attempts(first) + db.attempts(second)] == [
            "gemini", "gemini"]
    finally:
        db.close()


def test_posting_scan_out_of_gemini_fails_over_to_claude_and_merges(scfg, scfg_dir, scan_env,
                                                                     monkeypatch):
    scan_env(_row())
    monkeypatch.setenv("FAKE_GEMINI_LIMIT", "1")
    task = _add_scan(scfg_dir)
    assert _run(scfg, task)["state"] == TaskState.QUEUED
    row = _run(scfg, task)
    assert row["state"] == TaskState.DONE and row["agent"] == "claude"
    assert [r["company"] for r in _tracker_rows(scfg.tracker_path)] == ["Acme"]
    assert "1 new posting appended" in _report(row).split("### New postings", 1)[1]


def test_posting_scan_nothing_new(scfg, scfg_dir, scan_env):
    scan_env(_row())
    _run(scfg, _add_scan(scfg_dir))
    second = _add_scan(scfg_dir)
    row = _run(scfg, second)
    new = _report(row).split("### New postings", 1)[1]
    assert "No new postings" in new and "Dropped as duplicates: 1." in new
    assert "Acme" not in new


def test_posting_scan_missing_or_bad_file_is_a_failed_attempt(scfg, scfg_dir, scan_env):
    scan_env(text="company,role\nAcme,SWE\n")
    task = _add_scan(scfg_dir)
    row = _run(scfg, task)
    assert row["state"] == TaskState.RETRY_WAIT
    log = (Path(row["folder"]) / "logs" / "nightshift.log").read_text()
    assert "merge_tracker failed: proposed rows header must be the Tracker's columns" in log
    assert not scfg.tracker_path.exists()


def test_posting_scan_agent_failure_never_merges(scfg, scfg_dir, scan_env, monkeypatch):
    scan_env(_row())
    monkeypatch.setenv("FAKE_GEMINI_RESULT", "failed")
    task = _add_scan(scfg_dir)
    row = _run(scfg, task)
    assert row["state"] == TaskState.FAILED
    assert not scfg.tracker_path.exists()
    assert "The Tracker merge did not run" in _report(row)


def test_merge_hook_rerun_keeps_own_rows_as_new_postings(scfg, scfg_dir, scan_env):
    """A second merge of the same Task (e.g. after a retry) appends nothing
    twice, and the report still lists the rows this Task appended."""
    scan_env(_row(), _row(company="Globex", url="https://globex.com/jobs/7"))
    task = _add_scan(scfg_dir)
    row = _run(scfg, task)
    [hook] = [h for h in templates.post_hooks(scfg, row, FakeHost())
              if h.__name__ == "merge_tracker"]
    hook(Path(row["folder"]), row)
    assert len(_tracker_rows(scfg.tracker_path)) == 2
    text = templates.report_appendix(scfg, row)
    assert "2 new postings appended" in text and "Dropped as duplicates: 0." in text


def test_tracker_path_config_and_agent_access(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "tracker": {"path": str(tmp_path / "private" / "applications.csv")},
        "permissions": {"read_only_dirs": []}}))
    cfg = load_config(d, host=FakeHost())
    assert cfg.tracker_path == tmp_path / "private" / "applications.csv"
    policy = build_policy(cfg, task_id="k3f9", workspace=cfg.tasks_dir / "t", internet="on")
    assert (tmp_path / "private").resolve() in policy.no_access_dirs


def test_default_tracker_path(cfg):
    assert cfg.tracker_path == cfg.data_dir / "applications.csv"
    policy = build_policy(cfg, task_id="k3f9", workspace=cfg.tasks_dir / "t", internet="on")
    assert policy.no_access_dirs.count(cfg.data_dir.resolve()) == 1
