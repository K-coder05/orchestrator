"""The Tracker (``applications.csv``, §1.7) and the merge of Proposed rows.

Agents never write the Tracker. A posting-scan agent writes Proposed rows to
``output/proposed_rows.csv`` in its Task folder, with the Tracker's columns;
the ``merge_tracker`` post-hook (``nightshift.templates``) hands them to
:func:`merge`, which is deterministic Nightshift code:

1. **Validate** each row (:func:`validate_row`): required fields, an
   http(s) ``posting_url`` not on a denied domain, the configured season
   (§1.8: Summer 2027), no screened-out role words (MS/PhD-only, new grad,
   full-time, clearance), dates as ``YYYY-MM-DD``. Nightshift sets ``id``,
   ``status=new`` and an empty ``resume_version`` itself; whatever the agent
   wrote there is ignored.
2. **Dedupe** against the Tracker and against earlier rows of the same file:
   first by normalized ``posting_url`` (:func:`normalize_url`), then by
   normalized ``(company, role, season)`` (:func:`row_key`).
3. **Append** only the rows left, atomically (the whole file is rewritten
   to a temp file and renamed over the old one, under a lock). Existing
   rows are copied byte for byte.

The :class:`MergeResult` is what the report's New postings section is
written from: only rows actually appended are New postings.
"""

from __future__ import annotations

import csv
import fcntl
import io
import os
import re
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: The Tracker's columns, in order (§1.7).
COLUMNS: tuple[str, ...] = ("id", "company", "role", "location", "season", "posting_url",
                            "source", "date_found", "deadline", "status", "resume_version",
                            "notes")
STATUSES = ("new", "applying", "applied", "OA", "interview", "offer", "rejected", "skipped")
#: Fields a Proposed row must fill in.
REQUIRED = ("company", "role", "location", "season", "posting_url", "source")
#: The note the agent puts on a posting outside the wanted locations (§1.8).
LOW_PRIORITY = "low-priority location"
#: Longest value kept in any field (a runaway agent can't bloat the Tracker).
MAX_FIELD = 500


class TrackerError(Exception):
    """The Tracker or the Proposed rows file can't be used at all."""


# --------------------------------------------------------------------------
# Normalization (the dedupe keys)
# --------------------------------------------------------------------------

#: Query parameters that only track where a click came from. Stripped from
#: the dedupe key; any other parameter (e.g. Greenhouse's ``gh_jid``, the job
#: id itself) is kept.
TRACKING_PARAMS = frozenset({
    "gclid", "fbclid", "msclkid", "dclid", "yclid", "mc_cid", "mc_eid", "_hsenc", "_hsmi",
    "ref", "ref_src", "referrer", "referer", "source", "src", "gh_src", "lever-source",
    "lever-origin", "lever-via", "trk", "trackingid", "tracking_id", "igshid", "si",
    "campaign", "medium", "share", "_ga", "_gl", "hsa_acc", "hsa_cam", "hsa_grp", "hsa_ad",
    "hsa_src", "hsa_tgt", "hsa_kw", "hsa_mt", "hsa_net", "hsa_ver", "iis", "iisn", "ccuid",
})
_TRACKING_PREFIXES = ("utm_", "pk_", "mtm_", "hsa_")


def _tracking(param: str) -> bool:
    p = param.lower()
    return p in TRACKING_PARAMS or p.startswith(_TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """The dedupe key of a posting URL (§1.7): case-folded, ``http``/``https``
    and a leading ``www.`` treated alike, default ports, fragments, tracking
    query parameters and trailing slashes dropped, the other parameters
    sorted. Not a URL to open, only a key."""
    text = (url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    port = parts.port if parts.port not in (None, 80, 443) else None
    netloc = host + (f":{port}" if port else "")
    path = re.sub(r"/{2,}", "/", parts.path or "").rstrip("/")
    query = sorted((k.lower(), v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not _tracking(k))
    key = urlunsplit(("https", netloc, path, urlencode(query), ""))
    return key.lower()


_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_COMPANY_SUFFIXES = frozenset({"inc", "incorporated", "llc", "ltd", "limited", "corp",
                               "corporation", "co", "company", "plc", "gmbh", "technologies",
                               "technology", "labs", "the"})
#: Word-level synonyms in role titles, so "Software Engineering Internship"
#: and "Software Engineer Intern" are one role.
_ROLE_WORDS = {"internship": "intern", "interns": "intern", "engineering": "engineer",
               "engineers": "engineer", "developer": "engineer", "dev": "engineer",
               "swe": "software engineer", "sde": "software engineer",
               "ml": "machine learning", "ai": "artificial intelligence",
               "infra": "infrastructure", "and": "", "of": "", "the": "", "a": ""}
_SEASONS = {"summer": "summer", "su": "summer", "fall": "fall", "autumn": "fall",
            "winter": "winter", "spring": "spring"}


def _words(text: str) -> list[str]:
    t = (text or "").lower().replace("&", " and ").replace("'", "")
    return [w for w in _NON_ALNUM.split(t) if w]


def normalize_company(text: str) -> str:
    words = _words(text)
    while len(words) > 1 and words[-1] in _COMPANY_SUFFIXES:
        words.pop()
    if len(words) > 1 and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def normalize_role(text: str) -> str:
    out: list[str] = []
    for w in _words(text):
        out.extend(_ROLE_WORDS.get(w, w).split())
    return " ".join(out)


def normalize_season(text: str) -> str:
    """``Summer 2027``, ``2027 Summer``, ``summer '27`` and ``SU27`` -> ``summer 2027``."""
    raw = (text or "").lower()
    m = re.fullmatch(r"\s*([a-z]+)\s*'?(\d{2})\s*", raw)  # SU27, summer'27
    if m and m.group(1) in _SEASONS:
        return f"{_SEASONS[m.group(1)]} 20{m.group(2)}"
    words = _words(raw)
    season = next((_SEASONS[w] for w in words if w in _SEASONS), None)
    year = next((w if len(w) == 4 else f"20{w}" for w in words
                 if w.isdigit() and len(w) in (2, 4)), None)
    if season and year:
        return f"{season} {year}"
    return " ".join(words)


def row_key(row: Mapping[str, Any]) -> tuple[str, str, str]:
    """The secondary dedupe key: normalized ``(company, role, season)``."""
    return (normalize_company(str(row.get("company") or "")),
            normalize_role(str(row.get("role") or "")),
            normalize_season(str(row.get("season") or "")))


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

#: Role words that screen a posting out (§1.8). Matched on the role title
#: only; the agent applies the full rules, this is Nightshift's backstop.
SCREEN_OUT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("MS/PhD-only", re.compile(r"\b(ph\.?\s?d|doctoral|ms\s*/\s*ph\.?d|master'?s\s+(only|students?|degree))\b", re.I)),
    ("new grad", re.compile(r"\bnew[\s-]*grad(uate)?s?\b|\bentry[\s-]level\b|\buniversity\s+grad", re.I)),
    ("full-time", re.compile(r"\bfull[\s-]*time\b", re.I)),
    ("security clearance", re.compile(r"\bclearance\b|\bts\s*/\s*sci\b|\bsecret\b", re.I)),
)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _clean(value: Any) -> str:
    text = " ".join(str(value if value is not None else "").split())  # no newlines/tabs
    if len(text) > MAX_FIELD:
        text = text[: MAX_FIELD - 1] + "…"
    # The Tracker syncs to a spreadsheet later: never let a value be a formula.
    if text[:1] in ("=", "+", "@"):
        text = "'" + text
    return text


def _is_date(text: str) -> bool:
    if not _DATE.fullmatch(text):
        return False
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def _denied(host: str, denied_domains: Iterable[str]) -> str | None:
    host = host.lower().rstrip(".")
    for dom in denied_domains:
        dom = str(dom).lower().strip()
        if dom and (host == dom or host.endswith("." + dom)):
            return dom
    return None


def validate_row(raw: Mapping[str, Any], *, season: str, today: date,
                 denied_domains: Iterable[str] = ()) -> tuple[dict[str, str] | None, str | None]:
    """A Proposed row made ready for the Tracker, or ``(None, why)``.

    ``id`` is left empty (set at append), ``status`` is always ``new`` and
    ``resume_version`` empty. A blank ``date_found`` is today; a ``deadline``
    that isn't ``YYYY-MM-DD`` is moved into ``notes`` rather than dropped.
    """
    row = {c: _clean(raw.get(c)) for c in COLUMNS}
    missing = [c for c in REQUIRED if not row[c]]
    if missing:
        return None, f"missing {', '.join(missing)}"
    url = row["posting_url"]
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None, f"posting_url {url!r} is not an http(s) URL"
    dom = _denied(parts.hostname, denied_domains)
    if dom:
        return None, f"posting_url is on a denied domain ({dom}, §1.6)"
    if normalize_season(row["season"]) != normalize_season(season):
        return None, f"season {row['season']!r} is not {season} (§1.8)"
    for label, pattern in SCREEN_OUT:
        if pattern.search(row["role"]):
            return None, f"screened out: {label} role (§1.8)"
    if not row["date_found"]:
        row["date_found"] = today.isoformat()
    elif not _is_date(row["date_found"]):
        return None, f"date_found {row['date_found']!r} is not YYYY-MM-DD"
    if row["deadline"] and not _is_date(row["deadline"]):
        row["notes"] = "; ".join(x for x in (row["notes"], f"deadline: {row['deadline']}") if x)
        row["notes"] = _clean(row["notes"])
        row["deadline"] = ""
    row["id"] = ""
    row["status"] = "new"
    row["resume_version"] = ""
    return row, None


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _header(fields: Sequence[str] | None) -> list[str]:
    return [(f or "").strip().lower() for f in (fields or [])]


def read_tracker(path: Path) -> list[dict[str, str]]:
    """The Tracker's rows; [] when it doesn't exist yet. Raises
    :class:`TrackerError` when its header isn't the Tracker's columns (it is
    then never written: a hand-edited file is not Nightshift's to fix)."""
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    if _header(reader.fieldnames) != [c.lower() for c in COLUMNS]:
        raise TrackerError(f"{path} has columns {reader.fieldnames}, not the Tracker's "
                           f"({', '.join(COLUMNS)}); not touching it")
    return [{c: (r.get(c) or "") for c in COLUMNS} for r in reader]


@dataclass
class Proposed:
    rows: list[tuple[int, dict[str, Any]]]  # (line number, raw row)
    invalid: list["Rejected"]


def read_proposed(text: str) -> Proposed:
    """Parse ``proposed_rows.csv``. The header must be the Tracker's columns
    (any order, any case); a file without it raises :class:`TrackerError`.
    Rows with the wrong number of fields are rejected one by one."""
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    try:
        header = _header(next(reader))
    except StopIteration:
        raise TrackerError("proposed rows file is empty (it needs at least the header)") from None
    except csv.Error as exc:
        raise TrackerError(f"proposed rows file is not CSV: {exc}") from None
    want = {c.lower() for c in COLUMNS}
    if set(header) != want or len(header) != len(want):
        missing = sorted(want - set(header))
        extra = sorted(set(header) - want)
        raise TrackerError(
            "proposed rows header must be the Tracker's columns ("
            + ", ".join(COLUMNS) + ")"
            + (f"; missing {', '.join(missing)}" if missing else "")
            + (f"; unknown {', '.join(extra)}" if extra else "")
            + ("; a column is repeated" if not missing and not extra else ""))
    names = {c.lower(): c for c in COLUMNS}
    rows: list[tuple[int, dict[str, Any]]] = []
    invalid: list[Rejected] = []
    try:
        for values in reader:
            line = reader.line_num
            if not any(v.strip() for v in values):
                continue
            if len(values) != len(header):
                invalid.append(Rejected(line, {}, f"has {len(values)} fields, not {len(header)}"))
                continue
            rows.append((line, {names[h]: v for h, v in zip(header, values)}))
    except csv.Error as exc:
        raise TrackerError(f"proposed rows file is not valid CSV: {exc}") from None
    return Proposed(rows, invalid)


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


@dataclass
class Rejected:
    line: int
    row: dict[str, Any]
    reason: str


@dataclass
class Duplicate:
    row: dict[str, str]
    #: "posting_url" or "company, role, season"
    by: str
    #: The Tracker id it duplicates, or "" for an earlier row of the same file.
    matches: str
    #: True when the matching Tracker row was appended by this same Task in
    #: an earlier Attempt (it is still one of this Task's New postings).
    own: bool = False


@dataclass
class MergeResult:
    proposed: int
    appended: list[dict[str, str]] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)
    invalid: list[Rejected] = field(default_factory=list)

    @property
    def dropped_duplicates(self) -> int:
        """Duplicates of postings already in the Tracker before this Task."""
        return sum(1 for d in self.duplicates if not d.own)

    @property
    def already_appended(self) -> list[Duplicate]:
        return [d for d in self.duplicates if d.own]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "MergeResult":
        return cls(proposed=int(d.get("proposed") or 0),
                   appended=list(d.get("appended") or []),
                   duplicates=[Duplicate(**x) for x in d.get("duplicates") or []],
                   invalid=[Rejected(**x) for x in d.get("invalid") or []])


def plan_merge(existing: Sequence[Mapping[str, Any]], proposed: Proposed, *, season: str,
               today: date, denied_domains: Iterable[str] = (),
               own_ids: Iterable[str] = ()) -> MergeResult:
    """Decide what to append (pure; :func:`merge` does the file work).
    ``own_ids``: Tracker ids this Task appended in earlier Attempts."""
    own = set(own_ids)
    by_url: dict[str, str] = {}
    by_key: dict[tuple[str, str, str], str] = {}
    for r in existing:
        by_url.setdefault(normalize_url(str(r.get("posting_url") or "")), str(r.get("id") or ""))
        by_key.setdefault(row_key(r), str(r.get("id") or ""))
    by_url.pop("", None)
    result = MergeResult(proposed=len(proposed.rows) + len(proposed.invalid),
                         invalid=list(proposed.invalid))
    batch_urls: set[str] = set()
    batch_keys: set[tuple[str, str, str]] = set()
    valid: list[dict[str, str]] = []
    for line, raw in proposed.rows:
        row, why = validate_row(raw, season=season, today=today, denied_domains=denied_domains)
        if row is None:
            result.invalid.append(Rejected(line, dict(raw), why or "invalid"))
        else:
            valid.append(row)
    # One role listed in several cities is one (company, role, season): keep
    # a matching-location listing over a low-priority one (stable otherwise).
    valid.sort(key=lambda r: LOW_PRIORITY in r["notes"].lower())
    for row in valid:
        u, k = normalize_url(row["posting_url"]), row_key(row)
        if u in by_url:
            result.duplicates.append(Duplicate(row, "posting_url", by_url[u], by_url[u] in own))
        elif k in by_key:
            result.duplicates.append(
                Duplicate(row, "company, role, season", by_key[k], by_key[k] in own))
        elif u in batch_urls:
            result.duplicates.append(Duplicate(row, "posting_url", ""))
        elif k in batch_keys:
            result.duplicates.append(Duplicate(row, "company, role, season", ""))
        else:
            batch_urls.add(u)
            batch_keys.add(k)
            result.appended.append(row)
    next_id = 1 + max((int(r["id"]) for r in existing if str(r.get("id") or "").isdigit()),
                      default=0)
    for i, row in enumerate(result.appended):
        row["id"] = str(next_id + i)
    return result


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Exclusive lock for read-modify-write of the Tracker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _csv_line(values: Sequence[str]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


def merge(tracker_path: Path, proposed_text: str, *, season: str, today: date,
          denied_domains: Iterable[str] = (), own_ids: Iterable[str] = ()) -> MergeResult:
    """Validate, dedupe and append Proposed rows to the Tracker (§1.7).
    Creates the Tracker (header only) if it doesn't exist."""
    proposed = read_proposed(proposed_text)
    with locked(tracker_path):
        existing = read_tracker(tracker_path)
        result = plan_merge(existing, proposed, season=season, today=today,
                            denied_domains=denied_domains, own_ids=own_ids)
        old = tracker_path.read_text(encoding="utf-8") if tracker_path.exists() else ""
        if not result.appended and old.strip():
            return result
        if not old.strip():
            old = _csv_line(COLUMNS)
        elif not old.endswith("\n"):
            old += "\n"
        new = old + "".join(_csv_line([r[c] for c in COLUMNS]) for r in result.appended)
        fd, tmp = tempfile.mkstemp(dir=tracker_path.parent, prefix=".applications.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(new)
                f.flush()
                os.fsync(f.fileno())
            if tracker_path.exists():
                os.chmod(tmp, tracker_path.stat().st_mode & 0o777)
            os.replace(tmp, tracker_path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    return result


# --------------------------------------------------------------------------
# The report's New postings section
# --------------------------------------------------------------------------


def _cell(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def new_postings_section(appended: Sequence[Mapping[str, Any]], *, dropped_duplicates: int,
                         invalid: int, tracker_path: Path) -> str:
    """The report's New postings section, from the merge result only: the
    rows actually appended, then duplicates and rejected rows as counts."""
    n = len(appended)
    lines = ["### New postings", ""]
    if n:
        lines += [f"{n} new posting{'s' if n != 1 else ''} appended to the Tracker "
                  f"(`{tracker_path}`) with status `new`:", "",
                  "| Company | Role | Location | Deadline | Posting | Notes |",
                  "|---|---|---|---|---|---|"]
        for r in appended:
            lines.append(f"| {_cell(r.get('company'))} | {_cell(r.get('role'))} | "
                         f"{_cell(r.get('location'))} | {_cell(r.get('deadline')) or '-'} | "
                         f"[link]({_cell(r.get('posting_url')).replace(')', '%29')}) | "
                         f"{_cell(r.get('notes')) or '-'} |")
    else:
        lines.append("No new postings: nothing was appended to the Tracker.")
    lines += ["", f"Dropped as duplicates: {dropped_duplicates}."]
    if invalid:
        lines.append(f"Rejected as invalid or screened out: {invalid} "
                     "(reasons in `logs/nightshift.log`).")
    return "\n".join(lines)
