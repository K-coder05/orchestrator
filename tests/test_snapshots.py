"""Career page snapshots and diffs (company-rescrape, §4 item 6)."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from nightshift import snapshots

LA = ZoneInfo("America/Los_Angeles")


def _pages(*pages: dict) -> str:
    return json.dumps({"pages": list(pages)})


def _page(url="https://acme.com/careers", company="Acme", postings=(), loaded=True, error=None):
    return {"url": url, "company": company, "loaded": loaded, "error": error,
            "postings": [{"title": t, "location": "SF", "url": u} for t, u in postings]}


SWE = ("Software Engineer Intern", "https://acme.com/jobs/1")
ML = ("ML Engineer Intern", "https://acme.com/jobs/2")
DATA = ("Data Science Intern", "https://acme.com/jobs/3")


def _record(root, task_id, text, day):
    return snapshots.record(root, task_id, snapshots.parse_pages(text),
                            taken_at=datetime(2026, 10, day, 2, 5, tzinfo=LA))


def test_first_snapshot_has_nothing_to_diff_against(tmp_path):
    rec = _record(tmp_path, "aaaa", _pages(_page(postings=[SWE, ML])), 20)
    assert rec.previous is None
    assert rec.folder.parent == tmp_path and rec.folder.name.endswith("_aaaa")
    saved = json.loads((rec.folder / "snapshot.json").read_text())
    assert saved["task_id"] == "aaaa"
    assert [p["title"] for p in saved["pages"][0]["postings"]] == [SWE[0], ML[0]]
    [page] = rec.pages
    assert (page.status, page.added, page.removed) == ("first", [], [])
    assert "first snapshot" in (rec.folder / "diff.md").read_text()


def test_diff_against_the_previous_run(tmp_path):
    _record(tmp_path, "aaaa", _pages(_page(postings=[SWE, ML])), 20)
    # The same SWE posting under a tracking URL, ML gone, DATA new.
    swe2 = (SWE[0], SWE[1] + "?utm_source=x")
    rec = _record(tmp_path, "bbbb", _pages(_page(postings=[swe2, DATA])), 22)
    assert rec.previous == "aaaa"
    [page] = rec.pages
    assert page.status == "compared"
    assert [p["title"] for p in page.added] == [DATA[0]]
    assert [p["title"] for p in page.removed] == [ML[0]]
    assert (rec.added, rec.removed) == (1, 1)
    md = (rec.folder / "diff.md").read_text()
    assert "+ Data Science Intern" in md and "- ML Engineer Intern" in md
    assert snapshots.load(tmp_path, "bbbb").added == 1


def test_a_page_that_failed_is_diffed_against_its_last_good_snapshot(tmp_path):
    other = "https://globex.com/jobs"
    _record(tmp_path, "aaaa", _pages(_page(postings=[SWE]), _page(other, "Globex", [ML])), 20)
    # Task 2: Globex fails to load. Not a removal of its postings.
    rec2 = _record(tmp_path, "bbbb", _pages(
        _page(postings=[SWE]), _page(other, "Globex", loaded=False, error="403")), 22)
    assert [(p.company, p.status, p.removed) for p in rec2.pages] == [
        ("Acme", "compared", []), ("Globex", "failed", [])]
    # Task 3: Globex is back; compared with Task 1, the last time it loaded.
    rec3 = _record(tmp_path, "cccc", _pages(
        _page(postings=[SWE]), _page(other, "Globex", [ML, DATA])), 24)
    globex = rec3.pages[1]
    assert (globex.status, globex.since) == ("compared", "aaaa")
    assert [p["title"] for p in globex.added] == [DATA[0]]


def test_a_rerun_of_the_same_task_replaces_its_snapshot(tmp_path):
    _record(tmp_path, "aaaa", _pages(_page(postings=[SWE])), 20)
    _record(tmp_path, "bbbb", _pages(_page(postings=[SWE, ML])), 22)
    rec = _record(tmp_path, "bbbb", _pages(_page(postings=[SWE, ML, DATA])), 22)
    assert rec.previous == "aaaa"  # never diffed against its own earlier Attempt
    assert sorted(p.name.split("_")[1] for p in tmp_path.iterdir()) == ["aaaa", "bbbb"]
    assert rec.added == 2


@pytest.mark.parametrize("text, why", [
    ("not json", "not JSON"),
    (json.dumps([]), "\"pages\""),
    (_pages({"company": "Acme", "loaded": True, "postings": []}), "url"),
    (_pages(_page(url="ftp://acme.com")), r"not an http\(s\) URL"),
    (_pages({**_page(), "postings": [{"title": "x"}]}), "url"),
    (_pages({**_page(), "postings": "lots"}), "postings"),
    # A page whose load state is unclear would diff as "everything removed".
    (_pages({"url": "https://acme.com/careers", "postings": []}), "loaded must be true or false"),
    (_pages({**_page(), "loaded": "false"}), "loaded must be true or false"),
])
def test_parse_pages_rejects_bad_files(text, why):
    with pytest.raises(snapshots.SnapshotError, match=why):
        snapshots.parse_pages(text)


def test_parse_pages_keeps_a_failed_page_without_postings():
    [page] = snapshots.parse_pages(_pages({"url": "https://acme.com/careers", "company": "Acme",
                                           "loaded": False, "error": "timeout"}))
    assert (page.loaded, page.error, page.postings) == (False, "timeout", [])
