"""Career page snapshots and their diffs (company-rescrape, §4 item 6).

A company-rescrape agent lists what each target company's career page
shows in ``output/career_pages.json``::

    {"pages": [{"url": "...", "company": "...", "loaded": true, "error": null,
                "postings": [{"title": "...", "location": "...", "url": "..."}]}]}

The ``snapshot_pages`` post-hook (``nightshift.templates``) hands it to
:func:`record`, which is Nightshift code (agents have no ``data/`` access):

* The snapshot is written to ``<data>/snapshots/<template>/<UTC stamp>_<task
  id>/snapshot.json``. A later Attempt of the same Task replaces its folder.
* Each page is diffed against the latest snapshot of an *earlier Task*
  where that page loaded: a page that failed this time is ``failed`` (none
  of its postings count as removed) and is compared again, against its last
  good snapshot, the next time it loads. A page never seen loading is
  ``first``. Postings are matched by normalized URL (``tracker.normalize_url``).
* The diff goes next to it as ``diff.json`` and ``diff.md``. Retention
  (Phase 4, §2.13) deletes old ``snapshot.json`` files and keeps the diffs.

The diff is a record of what changed on the pages, not the list of New
postings: those come only from the Tracker merge.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .timeutil import iso
from .tracker import normalize_url

SNAPSHOT, DIFF_JSON, DIFF_MD = "snapshot.json", "diff.json", "diff.md"
#: Most postings kept per page (a runaway agent can't fill the disk).
MAX_POSTINGS = 2000
#: Longest title, location or URL kept.
MAX_FIELD = 500
#: Page statuses in a diff. ``first``: no earlier snapshot where it loaded
#: (not to be confused with a New posting, which only the Tracker merge makes).
FIRST, COMPARED, FAILED = "first", "compared", "failed"


class SnapshotError(Exception):
    """The agent's career pages file can't be used."""


@dataclass
class Page:
    url: str
    company: str
    loaded: bool
    error: str | None = None
    postings: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PageDiff:
    url: str
    company: str
    status: str  # FIRST | COMPARED | FAILED
    #: The Task whose snapshot this page was compared with.
    since: str | None = None
    added: list[dict[str, str]] = field(default_factory=list)
    removed: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


@dataclass
class Recorded:
    folder: Path
    task_id: str
    taken_at: str
    #: The latest earlier Task with a snapshot, or None for the first one.
    previous: str | None
    pages: list[PageDiff]

    @property
    def added(self) -> int:
        return sum(len(p.added) for p in self.pages)

    @property
    def removed(self) -> int:
        return sum(len(p.removed) for p in self.pages)

    @property
    def failed(self) -> list[PageDiff]:
        return [p for p in self.pages if p.status == FAILED]

    def summary(self) -> str:
        """One line for the log and the report: the change since the
        previous Task's snapshot, and the pages that failed to load."""
        if self.previous is None:
            text = "the first snapshot: nothing to diff against yet"
        else:
            text = (f"{_plural(self.added, 'posting')} added, {self.removed} removed since "
                    f"Task {self.previous}")
        return text + (f"; {_plural(len(self.failed), 'page')} failed to load"
                       if self.failed else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'s' if n != 1 else ''}"


# --------------------------------------------------------------------------
# Parsing the agent's file
# --------------------------------------------------------------------------


def _text(value: Any) -> str:
    text = " ".join(str(value if value is not None else "").split())
    return text[:MAX_FIELD]


def _http(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme.lower() in ("http", "https") and bool(parts.hostname)


def parse_pages(text: str) -> list[Page]:
    """The pages of an agent's ``career_pages.json``; raises
    :class:`SnapshotError` naming the first problem."""
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise SnapshotError(f"career pages file is not JSON: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("pages"), list):
        raise SnapshotError('career pages file must be {"pages": [...]}')
    pages: list[Page] = []
    for i, raw in enumerate(data["pages"], 1):
        where = f"page {i}"
        if not isinstance(raw, dict):
            raise SnapshotError(f"{where} is not an object")
        url = _text(raw.get("url"))
        if not url:
            raise SnapshotError(f"{where} has no url")
        if not _http(url):
            raise SnapshotError(f"{where} url {url!r} is not an http(s) URL")
        loaded = raw.get("loaded")
        if not isinstance(loaded, bool):
            # Guessing would diff an unread page as "every posting removed".
            raise SnapshotError(f"{where} ({url}): loaded must be true or false")
        postings_raw = raw.get("postings") or []
        if not isinstance(postings_raw, list):
            raise SnapshotError(f"{where} ({url}): postings must be a list")
        postings: list[dict[str, str]] = []
        for j, p in enumerate(postings_raw[:MAX_POSTINGS], 1):
            if not isinstance(p, dict) or not _text(p.get("title")) or not _text(p.get("url")):
                raise SnapshotError(f"{where} ({url}): posting {j} needs a title and a url")
            postings.append({"title": _text(p["title"]), "location": _text(p.get("location")),
                             "url": _text(p["url"])})
        pages.append(Page(url=url, company=_text(raw.get("company")), loaded=loaded,
                          error=_text(raw.get("error")) or None,
                          postings=postings if loaded else []))
    return pages


# --------------------------------------------------------------------------
# Recording and diffing
# --------------------------------------------------------------------------


def _folders(root: Path) -> list[Path]:
    """Snapshot folders, oldest first (names start with a UTC stamp)."""
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and "_" in p.name)


def _task_of(folder: Path) -> str:
    return folder.name.rsplit("_", 1)[1]


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _last_good(root: Path, task_id: str) -> tuple[str | None, dict[str, tuple[str, list]]]:
    """The latest earlier Task with a snapshot, and for each page URL
    (normalized) the latest earlier snapshot where it loaded."""
    latest: str | None = None
    good: dict[str, tuple[str, list]] = {}
    for folder in _folders(root):
        other = _task_of(folder)
        if other == task_id:
            continue
        snap = _read(folder / SNAPSHOT)
        if snap is None:
            continue
        latest = other
        for page in snap.get("pages") or []:
            if page.get("loaded"):
                good[normalize_url(page.get("url", ""))] = (other, page.get("postings") or [])
    return latest, good


def _diff(page: Page, before: tuple[str, list] | None) -> PageDiff:
    d = PageDiff(url=page.url, company=page.company, status=COMPARED, error=page.error)
    if not page.loaded:
        d.status = FAILED
        return d
    if before is None:
        d.status = FIRST
        return d
    d.since, old = before
    old_urls = {normalize_url(p.get("url", "")) for p in old}
    new_urls = {normalize_url(p["url"]) for p in page.postings}
    d.added = [p for p in page.postings if normalize_url(p["url"]) not in old_urls]
    d.removed = [p for p in old if normalize_url(p.get("url", "")) not in new_urls]
    return d


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def record(root: Path, task_id: str, pages: list[Page], *, taken_at: datetime) -> Recorded:
    """Write this Task's snapshot under ``root`` and diff it against the
    earlier ones (see the module docstring)."""
    for old in _folders(root):
        if _task_of(old) == task_id:
            shutil.rmtree(old)
    previous, good = _last_good(root, task_id)
    stamp = taken_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = root / f"{stamp}_{task_id}"
    folder.mkdir(parents=True)
    _write(folder / SNAPSHOT, json.dumps(
        {"task_id": task_id, "taken_at": iso(taken_at), "pages": [asdict(p) for p in pages]},
        indent=1))
    rec = Recorded(folder=folder, task_id=task_id, taken_at=iso(taken_at), previous=previous,
                   pages=[_diff(p, good.get(normalize_url(p.url))) for p in pages])
    _write(folder / DIFF_JSON, json.dumps(
        {"task_id": task_id, "taken_at": rec.taken_at, "previous": previous,
         "pages": [asdict(p) for p in rec.pages]}, indent=1))
    _write(folder / DIFF_MD, diff_markdown(rec))
    return rec


def load(root: Path, task_id: str) -> Recorded | None:
    """This Task's recorded diff, or None when it has none."""
    for folder in reversed(_folders(root)):
        if _task_of(folder) != task_id:
            continue
        data = _read(folder / DIFF_JSON)
        if data is None:
            return None
        return Recorded(folder=folder, task_id=task_id, taken_at=str(data.get("taken_at")),
                        previous=data.get("previous"),
                        pages=[PageDiff(**p) for p in data.get("pages") or []])
    return None


def _name(p: PageDiff) -> str:
    return f"{p.company} ({p.url})" if p.company else p.url


def diff_markdown(rec: Recorded) -> str:
    lines = [f"# Career pages diff, {rec.taken_at} (Task {rec.task_id})", ""]
    lines.append(f"Against the snapshot of Task {rec.previous}." if rec.previous else
                 "No earlier snapshot: this is the first snapshot.")
    for p in rec.pages:
        lines += ["", f"## {_name(p)}", ""]
        if p.status == FAILED:
            lines.append("Did not load" + (f": {p.error}" if p.error else "") +
                         "; compared again the next time it loads.")
        elif p.status == FIRST:
            lines.append("First snapshot of this page.")
        elif not p.added and not p.removed:
            lines.append(f"No change since Task {p.since}.")
        else:
            lines.append(f"Since Task {p.since}:")
            lines += [f"+ {x['title']} | {x.get('location') or '-'} | {x['url']}" for x in p.added]
            lines += [f"- {x.get('title')} | {x.get('location') or '-'} | {x.get('url')}"
                      for x in p.removed]
    return "\n".join(lines) + "\n"
