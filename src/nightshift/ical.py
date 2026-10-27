"""School calendar feeds (§5 item 3): the bCourses calendar iCal feed and
the Google Calendar private iCal link.

Nightshift code, never an agent, fetches both feeds. Their URLs are
secrets (``.env`` or the Keychain, :data:`FEEDS`): loading one registers
it for redaction, it is never put in ``task.yaml``, a prompt, a log or a
report, and fetch errors are built from the feed's label and the HTTP
status, never from the URL.

What comes out of a fetch:

- :func:`parse`: a feed's events between two times, with all-day events,
  time zones (``TZID``, UTC, floating) and recurring events (``RRULE``,
  ``RDATE``, ``EXDATE``, ``RECURRENCE-ID``) expanded.
- :func:`classify`: which configured course an event names (its title,
  matched like a request, ``school.find_courses``) and whether it is a due
  date (:func:`is_due`).
- The weekly roundup's input file (:func:`calendar_markdown`), written into
  the Task folder by the ``school_calendar`` pre-hook (``templates``).
- A cache of the latest fetch in ``<agents_root>/data/school/calendar.json``
  (no URLs), which gives a Task tied to a course its next due date as its
  deadline (:func:`course_deadline`, §2.3).
"""

from __future__ import annotations

import html
import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import school
from .timeutil import iso, parse_iso

if TYPE_CHECKING:
    from .config import Config


@dataclass(frozen=True)
class Feed:
    key: str
    label: str
    secret: str  # the name of the secret holding its URL


FEEDS = (
    Feed("bcourses", "bCourses calendar", "BCOURSES_ICAL_URL"),
    Feed("google", "Google Calendar", "GOOGLE_CALENDAR_ICAL_URL"),
)

#: Defaults of ``school.calendar`` (config).
DEFAULTS = {"lookahead_days": 14, "refresh_hours": 12, "timeout_seconds": 20}
#: A feed bigger than this is refused (a calendar is a few hundred KB).
MAX_BYTES = 10 * 1024 * 1024
#: Longest description kept for a course event (the rest is cut).
DESCRIPTION_CHARS = 300
#: A recurring event is expanded at most this many periods (a guard).
MAX_PERIODS = 5000


class FeedError(Exception):
    """A feed could not be fetched or read. Messages never hold the URL."""


def setting(cfg: Config, key: str) -> float:
    value = (((cfg.data.get("school") or {}).get("calendar")) or {}).get(key)
    return float(DEFAULTS[key] if value is None else value)


def validate(calendar: Any) -> str | None:
    """What is wrong with a ``school.calendar`` section, or None."""
    if calendar is None:
        return None
    if not isinstance(calendar, dict):
        return "school.calendar must be a mapping (lookahead_days, refresh_hours, timeout_seconds)"
    unknown = sorted(set(calendar) - set(DEFAULTS))
    if unknown:
        return f"school.calendar has unknown keys {', '.join(unknown)} (known: {', '.join(DEFAULTS)})"
    for key in DEFAULTS:
        v = calendar.get(key, DEFAULTS[key])
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            return f"school.calendar.{key} must be a positive number"
    return None


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    source: str  # Feed.key
    uid: str
    summary: str
    start: datetime  # aware; an all-day event starts at local midnight
    end: datetime
    all_day: bool = False
    location: str = ""
    description: str = ""
    recurring: bool = False
    #: Filled in by :func:`classify`.
    course: str | None = None
    due: bool = False

    @property
    def due_at(self) -> datetime:
        """When it is due: a timed event's start; an all-day event's day,
        at its end (23:59:59 local), as a posting's close date (§2.3)."""
        if self.all_day:
            return datetime.combine(self.start.date(), time(23, 59, 59), self.start.tzinfo)
        return self.start

    def to_json(self) -> dict[str, Any]:
        d = asdict(self)
        d["start"], d["end"] = iso(self.start), iso(self.end)
        return d

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Event:
        return cls(**(d | {"start": parse_iso(d["start"]), "end": parse_iso(d["end"])}))


# --------------------------------------------------------------------------
# Parsing (RFC 5545, the parts calendars use)
# --------------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw.strip():
            lines.append(raw)
    return lines


@dataclass
class Prop:
    name: str
    params: dict[str, str]
    value: str


def _content_line(line: str) -> Prop | None:
    """``NAME;PARAM=x;PARAM="y:z":value``."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            head, value = line[:i], line[i + 1:]
            break
    else:
        return None
    parts, buf, quoted = [], "", False
    for ch in head:
        if ch == '"':
            quoted = not quoted
        if ch == ";" and not quoted:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    params = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        params[k.strip().upper()] = v.strip().strip('"')
    return Prop(parts[0].strip().upper(), params, value)


def _text(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out).strip()


#: TZIDs some calendars (Outlook, Exchange) use instead of IANA names.
WINDOWS_ZONES = {
    "pacific standard time": "America/Los_Angeles",
    "mountain standard time": "America/Denver",
    "us mountain standard time": "America/Phoenix",
    "central standard time": "America/Chicago",
    "eastern standard time": "America/New_York",
    "alaskan standard time": "America/Anchorage",
    "hawaiian standard time": "Pacific/Honolulu",
    "gmt standard time": "Europe/London",
    "utc": "UTC",
    "coordinated universal time": "UTC",
}


def _zone(tzid: str | None, default: ZoneInfo) -> ZoneInfo:
    """An IANA zone for a ``TZID``: the name itself, a known Windows name,
    or the IANA name at its end (``/mozilla.org/20050126_1/America/New_York``);
    anything else is the default zone."""
    if not tzid:
        return default
    name = tzid.strip().strip('"')
    candidates = [name, WINDOWS_ZONES.get(name.lower(), "")]
    parts = [p for p in name.split("/") if p]
    candidates += ["/".join(parts[i:]) for i in range(len(parts))]
    for c in candidates:
        if not c:
            continue
        try:
            return ZoneInfo(c)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return default


_DT = re.compile(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?(Z)?)?")


def _when(value: str, params: dict[str, str], default: ZoneInfo) -> datetime | date | None:
    """A DATE (``date``) or DATE-TIME (aware ``datetime``) value."""
    m = _DT.fullmatch(value.strip())
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        if m.group(4) is None or params.get("VALUE", "").upper() == "DATE":
            return date(y, mo, d)
        naive = datetime(y, mo, d, int(m.group(4)), int(m.group(5)), int(m.group(6) or 0))
    except ValueError:
        return None
    if m.group(7):
        return naive.replace(tzinfo=timezone.utc)
    return naive.replace(tzinfo=_zone(params.get("TZID"), default))


_DURATION = re.compile(
    r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")


def _duration(value: str) -> timedelta | None:
    m = _DURATION.fullmatch(value.strip())
    if not m or value.strip() in ("P", "PT", "-P", "+P"):
        return None
    w, d, h, mi, s = (int(g or 0) for g in m.groups()[1:])
    delta = timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)
    return -delta if m.group(1) == "-" else delta


def _components(lines: list[str]) -> tuple[list[list[Prop]], dict[str, str]]:
    """The VEVENTs (their own properties; VALARMs inside are skipped) and
    the calendar's own properties."""
    events: list[list[Prop]] = []
    cal: dict[str, str] = {}
    stack: list[str] = []
    for line in lines:
        p = _content_line(line)
        if p is None:
            continue
        if p.name == "BEGIN":
            stack.append(p.value.strip().upper())
            if stack == ["VCALENDAR", "VEVENT"]:
                events.append([])
            continue
        if p.name == "END":
            if stack:
                stack.pop()
            continue
        if stack == ["VCALENDAR", "VEVENT"]:
            events[-1].append(p)
        elif stack == ["VCALENDAR"]:
            cal.setdefault(p.name, p.value)
    return events, cal


def _first(props: list[Prop], name: str) -> Prop | None:
    return next((p for p in props if p.name == name), None)


def _all(props: list[Prop], name: str) -> list[Prop]:
    return [p for p in props if p.name == name]


def _as_dt(v: datetime | date, tz: ZoneInfo) -> datetime:
    return v if isinstance(v, datetime) else datetime.combine(v, time(0), tz)


def _key(v: datetime | date) -> str:
    """An instance's identity (for EXDATE and RECURRENCE-ID): its start in UTC."""
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return v.strftime("%Y%m%d")


@dataclass
class _Master:
    props: list[Prop]
    start: datetime | date
    length: timedelta
    rule: dict[str, str] | None
    extra: list[datetime | date] = field(default_factory=list)
    excluded: set[str] = field(default_factory=set)


@dataclass
class ParseResult:
    events: list[Event]
    #: Things the parser could not fully follow (shown in the input file).
    notes: list[str] = field(default_factory=list)


def parse(text: str, *, source: str, tz_name: str, start: datetime,
          end: datetime) -> ParseResult:
    """The events of an iCal feed that overlap ``[start, end)``, sorted by
    start; recurring events are expanded into one Event per instance in the
    window, cancelled events and instances left out."""
    default = ZoneInfo(tz_name)
    lines = _unfold(text)
    if not lines or _content_line(lines[0]) is None or lines[0].strip().upper() != "BEGIN:VCALENDAR":
        raise FeedError("is not an iCal feed (no BEGIN:VCALENDAR)")
    comps, cal = _components(lines)
    floating = _zone(cal.get("X-WR-TIMEZONE"), default)
    notes: list[str] = []
    masters: dict[str, _Master] = {}
    overrides: list[tuple[str, list[Prop]]] = []
    singles: list[list[Prop]] = []
    for props in comps:
        uid = (_first(props, "UID").value.strip() if _first(props, "UID") else "")
        rid = _first(props, "RECURRENCE-ID")
        if rid is not None and uid:
            overrides.append((uid, props))
        elif _first(props, "RRULE") is not None or _first(props, "RDATE") is not None:
            m = _master(props, floating, notes)
            if m is not None:
                masters[uid or f"(no uid {len(masters)})"] = m
        else:
            singles.append(props)

    out: list[Event] = []

    def keep(ev: Event | None) -> None:
        if ev is not None and ev.end > start and ev.start < end:
            out.append(ev)

    for props in singles:
        keep(_event(props, source, floating, notes))
    replaced: dict[str, set[str]] = {}
    for uid, props in overrides:
        rid = _first(props, "RECURRENCE-ID")
        when = _when(rid.value, rid.params, floating)
        if when is not None:
            replaced.setdefault(uid, set()).add(_key(when))
        keep(_event(props, source, floating, notes, recurring=True))
    for uid, m in masters.items():
        skip = m.excluded | replaced.get(uid, set())
        if _status(m.props) == "CANCELLED":
            continue
        for inst in _instances(m, floating, end, notes):
            if _key(inst) in skip:
                continue
            s = _as_dt(inst, floating)
            keep(_build(m.props, source, s, s + m.length, not isinstance(inst, datetime),
                        recurring=True))
    out.sort(key=lambda e: (e.start, e.summary))
    return ParseResult(out, notes)


def _status(props: list[Prop]) -> str:
    p = _first(props, "STATUS")
    return p.value.strip().upper() if p else ""


def _span(props: list[Prop], tz: ZoneInfo, notes: list[str]
          ) -> tuple[datetime | date, timedelta] | None:
    ds = _first(props, "DTSTART")
    if ds is None:
        return None
    s = _when(ds.value, ds.params, tz)
    if s is None:
        notes.append(f"skipped an event with a DTSTART that is not a date ({ds.value[:20]!r})")
        return None
    de, du = _first(props, "DTEND"), _first(props, "DURATION")
    length = None
    if de is not None:
        e = _when(de.value, de.params, tz)
        if e is not None:
            length = _as_dt(e, tz) - _as_dt(s, tz)
    elif du is not None:
        length = _duration(du.value)
    if length is None or length < timedelta(0):
        length = timedelta(days=1) if not isinstance(s, datetime) else timedelta(0)
    return s, length


def _build(props: list[Prop], source: str, s: datetime, e: datetime, all_day: bool, *,
           recurring: bool) -> Event:
    def text(name: str) -> str:
        p = _first(props, name)
        return _text(p.value) if p else ""
    return Event(source=source, uid=text("UID"), summary=text("SUMMARY") or "(no title)",
                 start=s, end=e, all_day=all_day, location=text("LOCATION"),
                 description=text("DESCRIPTION"), recurring=recurring)


def _event(props: list[Prop], source: str, tz: ZoneInfo, notes: list[str], *,
           recurring: bool = False) -> Event | None:
    if _status(props) == "CANCELLED":
        return None
    span = _span(props, tz, notes)
    if span is None:
        return None
    s, length = span
    start = _as_dt(s, tz)
    return _build(props, source, start, start + length, not isinstance(s, datetime),
                  recurring=recurring)


def _master(props: list[Prop], tz: ZoneInfo, notes: list[str]) -> _Master | None:
    span = _span(props, tz, notes)
    if span is None:
        return None
    s, length = span
    rule = None
    rr = _first(props, "RRULE")
    if rr is not None:
        rule = {}
        for part in rr.value.split(";"):
            k, _, v = part.partition("=")
            if k.strip():
                rule[k.strip().upper()] = v.strip().upper()
    m = _Master(props, s, length, rule)
    for name, target in (("RDATE", m.extra), ("EXDATE", None)):
        for p in _all(props, name):
            if p.params.get("VALUE", "").upper() == "PERIOD":
                continue
            for v in p.value.split(","):
                w = _when(v, p.params, tz)
                if w is None:
                    continue
                if isinstance(s, date) and not isinstance(s, datetime) and isinstance(w, datetime):
                    w = w.date()
                if target is None:
                    m.excluded.add(_key(w))
                else:
                    target.append(w)
    return m


WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
#: RRULE parts :func:`_instances` follows; with any other only DTSTART counts.
RULE_PARTS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "BYMONTH",
              "BYSETPOS", "WKST"}
_BYDAY = re.compile(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")


def _ints(text: str | None) -> list[int]:
    return [int(x) for x in (text or "").split(",") if re.fullmatch(r"[+-]?\d+", x.strip())]


def _month_days(year: int, month: int) -> int:
    nxt = date(year + month // 12, month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


def _in_month(year: int, month: int, rule: dict[str, str], day0: int) -> list[int]:
    """The days of a month a MONTHLY/YEARLY rule picks."""
    n = _month_days(year, month)
    days: set[int] = set()
    if "BYMONTHDAY" in rule:
        for d in _ints(rule["BYMONTHDAY"]):
            d = d if d > 0 else n + d + 1
            if 1 <= d <= n:
                days.add(d)
    if "BYDAY" in rule:
        picked: set[int] = set()
        for m in _BYDAY.finditer(rule["BYDAY"]):
            wd = WEEKDAYS.index(m.group(2))
            matches = [d for d in range(1, n + 1) if date(year, month, d).weekday() == wd]
            if m.group(1):
                k = int(m.group(1))
                if 0 < abs(k) <= len(matches):
                    picked.add(matches[k - 1] if k > 0 else matches[k])
            else:
                picked.update(matches)
        days = (days & picked) if "BYMONTHDAY" in rule else picked
    if "BYMONTHDAY" not in rule and "BYDAY" not in rule and day0 <= n:
        days.add(day0)
    ordered = sorted(days)
    if "BYSETPOS" in rule and ordered:
        ordered = sorted({ordered[p - 1] if p > 0 else ordered[p]
                          for p in _ints(rule["BYSETPOS"]) if 0 < abs(p) <= len(ordered)})
    return ordered


def _instances(m: _Master, tz: ZoneInfo, end: datetime, notes: list[str]
               ) -> Iterator[datetime | date]:
    """The starts of a recurring event up to ``end`` (DTSTART first), in
    wall-clock time of its own zone, so a DST change keeps the local time."""
    s = m.start
    timed = isinstance(s, datetime)
    zone = s.tzinfo if timed else tz
    wall0 = s.replace(tzinfo=None) if timed else datetime.combine(s, time(0))
    end_wall = end.astimezone(zone).replace(tzinfo=None)  # type: ignore[arg-type]

    def out(w: datetime) -> datetime | date:
        return w.replace(tzinfo=zone) if timed else w.date()

    seen: set[str] = set()
    emitted = [out(wall0)] + list(m.extra)
    rule = m.rule
    if rule is not None:
        summary = _first(m.props, "SUMMARY")
        title = _text(summary.value) if summary else "(no title)"
        unknown = sorted(set(rule) - RULE_PARTS)
        freq = rule.get("FREQ")
        if unknown or freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
            notes.append(f"recurring event {title!r}: rule {';'.join(unknown) or freq} "
                         "not supported, only its first instance is listed")
            rule = None
    if rule is not None:
        interval = max(1, (_ints(rule.get("INTERVAL")) or [1])[0])
        count = (_ints(rule.get("COUNT")) or [None])[0]
        until = None
        if "UNTIL" in rule:
            until = _when(rule["UNTIL"], {}, zone)  # type: ignore[arg-type]
        months = _ints(rule.get("BYMONTH"))
        made = 0

        def stop(w: datetime) -> bool:
            if until is None:
                return False
            if isinstance(until, datetime):
                return out(w) > until if timed else w > until.replace(tzinfo=None)
            return w.date() > until

        def candidates(k: int) -> list[datetime]:
            if freq == "DAILY":
                w = wall0 + timedelta(days=k * interval)
                ok = (not months or w.month in months) and (
                    "BYDAY" not in rule or WEEKDAYS[w.weekday()] in rule["BYDAY"])
                return [w] if ok else []
            if freq == "WEEKLY":
                wkst = WEEKDAYS.index(rule.get("WKST", "MO")) if rule.get("WKST") in WEEKDAYS else 0
                week0 = wall0 - timedelta(days=(wall0.weekday() - wkst) % 7)
                base = week0 + timedelta(weeks=k * interval)
                days = [WEEKDAYS.index(x.group(2)) for x in _BYDAY.finditer(rule.get("BYDAY", ""))]
                if not days:
                    days = [wall0.weekday()]
                ws = sorted({(d - wkst) % 7 for d in days})
                return [base + timedelta(days=o) for o in ws
                        if not months or (base + timedelta(days=o)).month in months]
            if freq == "MONTHLY":
                mi = wall0.month - 1 + k * interval
                y, mo = wall0.year + mi // 12, mi % 12 + 1
                if months and mo not in months:
                    return []
                return [datetime.combine(date(y, mo, d), wall0.time())
                        for d in _in_month(y, mo, rule, wall0.day)]
            y = wall0.year + k * interval  # YEARLY
            got = []
            for mo in months or [wall0.month]:
                if "BYDAY" in rule or "BYMONTHDAY" in rule:
                    ds = _in_month(y, mo, rule, wall0.day)
                else:
                    ds = [wall0.day] if wall0.day <= _month_days(y, mo) else []
                got += [datetime.combine(date(y, mo, d), wall0.time()) for d in ds]
            return sorted(got)

        generated: list[datetime | date] = []
        done = False
        for k in range(MAX_PERIODS):
            for w in candidates(k):
                if w < wall0:
                    continue
                if stop(w) or w >= end_wall + timedelta(days=1) or (
                        count is not None and made >= count):
                    done = True
                    break
                made += 1
                generated.append(out(w))
            if done:
                break
        first = out(wall0)
        # DTSTART is always the first instance, even if the rule skips it.
        emitted = ([] if any(_key(g) == _key(first) for g in generated) else [first]) + (
            generated + list(m.extra))
    for e in emitted:
        k = _key(e)
        if k not in seen:
            seen.add(k)
            yield e


# --------------------------------------------------------------------------
# Courses and due dates
# --------------------------------------------------------------------------

#: Words in an event title that make it a due date (whole words, any case).
DUE_WORDS = ("due", "deadline", "homework", "hw", "assignment", "pset", "problem set",
             "quiz", "exam", "midterm", "final", "essay")
_DUE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(w).replace(r"\ ", r"\s+")
                                                 for w in DUE_WORDS)
                  + r")s?(?:\s*\d+)?(?![a-z])", re.IGNORECASE)


def is_due(ev: Event) -> bool:
    """A due date: a bCourses assignment (its UID says ``assignment``), or
    an event whose title has one of :data:`DUE_WORDS` (``HW 5``, ``hw5``,
    ``Midterm 2``, ``Project 3 due``)."""
    if ev.source == "bcourses" and "assignment" in ev.uid.lower():
        return True
    return bool(_DUE.search(ev.summary))


def classify(cfg: Config, events: list[Event]) -> list[Event]:
    """Each event with its course (the one configured course its title
    names; none if it names none or several) and whether it is a due date."""
    out = []
    for ev in events:
        found = school.find_courses(cfg, ev.summary)
        out.append(replace(ev, course=found[0].id if len(found) == 1 else None, due=is_due(ev)))
    return out


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def _urlopen(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "nightshift (calendar feed)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (http(s) only)
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise FeedError(f"is larger than {MAX_BYTES // (1024 * 1024)} MB")
    return data


#: How a feed's URL is read: ``fetch_url(url, timeout_seconds) -> bytes``.
#: Tests replace it with a fake fetcher (no real network in tests).
fetch_url: Callable[[str, float], bytes] = _urlopen


def configured(cfg: Config) -> dict[str, bool]:
    """Whether each feed's secret is set (``orch doctor``)."""
    return {f.key: bool((cfg.secrets.get(f.secret) or "").strip()) for f in FEEDS}


@dataclass
class FeedResult:
    key: str
    label: str
    configured: bool
    events: int = 0
    error: str | None = None


@dataclass
class Snapshot:
    fetched_at: datetime
    start: datetime
    end: datetime
    feeds: list[FeedResult]
    events: list[Event]
    notes: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        return [f"{f.label}: {f.error}" for f in self.feeds if f.error]

    def to_json(self) -> dict[str, Any]:
        return {"fetched_at": iso(self.fetched_at), "start": iso(self.start),
                "end": iso(self.end), "feeds": [asdict(f) for f in self.feeds],
                "events": [e.to_json() for e in self.events], "notes": self.notes}

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Snapshot:
        return cls(fetched_at=parse_iso(d["fetched_at"]), start=parse_iso(d["start"]),
                   end=parse_iso(d["end"]), feeds=[FeedResult(**f) for f in d["feeds"]],
                   events=[Event.from_json(e) for e in d["events"]],
                   notes=list(d.get("notes") or []))


def _read_feed(cfg: Config, feed: Feed, url: str) -> str:
    """The feed's text. Errors name the feed, never its URL."""
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not re.match(r"https?://", url, re.IGNORECASE):
        raise FeedError(f"secret {feed.secret} is not an http(s) or webcal URL")
    try:
        data = fetch_url(url, setting(cfg, "timeout_seconds"))
    except FeedError:
        raise
    except urllib.error.HTTPError as exc:
        raise FeedError(f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else type(exc.reason).__name__
        raise FeedError(f"could not connect ({cfg.secrets.redact(str(reason))})") from None
    except TimeoutError:
        raise FeedError(f"timed out after {setting(cfg, 'timeout_seconds'):g}s") from None
    except OSError as exc:
        raise FeedError(f"could not be read ({type(exc).__name__})") from None
    return data.decode("utf-8", errors="replace")


def fetch(cfg: Config, at: datetime) -> Snapshot:
    """Fetch and parse every configured feed: the events from ``at`` to
    ``school.calendar.lookahead_days`` ahead, classified. A feed whose secret
    is unset is skipped (and so marked); one that fails carries its error."""
    tz = ZoneInfo(cfg.timezone)
    start = at.astimezone(tz)
    end = start + timedelta(days=setting(cfg, "lookahead_days"))
    results, events, notes = [], [], []
    for feed in FEEDS:
        url = cfg.secrets.get(feed.secret)  # loading it registers it for redaction
        res = FeedResult(feed.key, feed.label, configured=bool((url or "").strip()))
        results.append(res)
        if not res.configured:
            continue
        try:
            parsed = parse(_read_feed(cfg, feed, url or ""), source=feed.key,
                           tz_name=cfg.timezone, start=start, end=end)
        except FeedError as exc:
            res.error = cfg.secrets.redact(str(exc))
            continue
        except Exception as exc:  # a parser bug must not leak the feed's text
            res.error = f"could not be parsed ({type(exc).__name__})"
            continue
        res.events = len(parsed.events)
        events += parsed.events
        notes += [f"{feed.label}: {cfg.secrets.redact(n)}" for n in parsed.notes]
    events.sort(key=lambda e: (e.start, e.summary))
    return Snapshot(at, start, end, results, classify(cfg, events), notes)


# --------------------------------------------------------------------------
# The cache (agents can't reach the data dir)
# --------------------------------------------------------------------------


def cache_path(cfg: Config) -> Path:
    return cfg.data_dir / "school" / "calendar.json"


def save(path: Path, snap: Snapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(snap.to_json(), indent=1))
    tmp.replace(path)


def load(path: Path) -> Snapshot | None:
    try:
        return Snapshot.from_json(json.loads(path.read_text()))
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store(cfg: Config, snap: Snapshot) -> None:
    """Keep ``snap`` as the cache, unless a feed failed in it (a failed
    feed would hide its due dates until the next fetch)."""
    if not snap.errors and any(f.configured for f in snap.feeds):
        save(cache_path(cfg), snap)


@dataclass(frozen=True)
class Deadline:
    at: str  # ISO 8601
    event: Event
    fetched_at: datetime

    def describe(self, cfg: Config) -> str:
        label = next(f.label for f in FEEDS if f.key == self.event.source)
        return f"{self.at} ({self.event.summary}, from your {label})"


def course_deadline(cfg: Config, course_id: str, at: datetime) -> Deadline | None:
    """:func:`find_deadline`, best effort: the feeds being down, or a bad
    cache, never stops a Task from being added; it just gets no deadline."""
    try:
        return find_deadline(cfg, course_id, at)
    except Exception:  # noqa: BLE001
        return None


def find_deadline(cfg: Config, course_id: str, at: datetime) -> Deadline | None:
    """The next due date of ``course_id`` after ``at`` (§2.3: "a course due
    date from a calendar feed"): the earliest due-date event naming the
    course, due after ``at`` and within ``lookahead_days``. Uses the cached
    fetch; one older than ``refresh_hours`` (or none) is fetched again
    first if a feed is configured, and a failed fetch falls back to the
    cache. None: no feed, or no such event."""
    snap = load(cache_path(cfg))
    stale = snap is None or at - snap.fetched_at > timedelta(hours=setting(cfg, "refresh_hours"))
    if stale and any(configured(cfg).values()):
        fresh = fetch(cfg, at)
        store(cfg, fresh)
        if not fresh.errors:
            snap = fresh
        elif snap is None:
            snap = fresh  # whatever the feeds that worked had
    if snap is None:
        return None
    horizon = at + timedelta(days=setting(cfg, "lookahead_days"))
    due = sorted((e for e in classify(cfg, snap.events)
                  if e.course == course_id and e.due and at < e.due_at <= horizon),
                 key=lambda e: (e.due_at, e.summary))
    if not due:
        return None
    return Deadline(at=iso(due[0].due_at.astimezone(ZoneInfo(cfg.timezone))), event=due[0],
                    fetched_at=snap.fetched_at)


# --------------------------------------------------------------------------
# The roundup's input file
# --------------------------------------------------------------------------


def _plain(text: str) -> str:
    """A description as one plain line: tags dropped, entities decoded, cut."""
    flat = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())
    return flat if len(flat) <= DESCRIPTION_CHARS else flat[:DESCRIPTION_CHARS].rstrip() + "..."


def _when_text(ev: Event, tz: ZoneInfo) -> str:
    s, e = ev.start.astimezone(tz), ev.end.astimezone(tz)
    if ev.all_day:
        last = (e - timedelta(days=1)).date() if e > s else s.date()
        day = f"{s:%a %b} {s.day}"
        return day + (f" to {last:%a %b} {last.day}" if last > s.date() else "") + " (all day)"
    if ev.due:
        return f"{s:%a %b} {s.day}, {s:%H:%M}"
    if e.date() == s.date():
        return f"{s:%a %b} {s.day}, {s:%H:%M}-{e:%H:%M}"
    return f"{s:%a %b} {s.day}, {s:%H:%M} to {e:%a %b} {e.day}, {e:%H:%M}"


def _label(key: str) -> str:
    return next(f.label for f in FEEDS if f.key == key)


def calendar_markdown(cfg: Config, snap: Snapshot) -> str:
    """The roundup's ``input/calendar.md``: the coming events per course,
    due dates first, then everything else on the calendars (titles and times
    only). Never the feed URLs."""
    tz = ZoneInfo(cfg.timezone)
    courses = school.courses(cfg)
    lines = [
        "# My calendar for the coming days",
        "",
        f"Fetched by Nightshift at {snap.fetched_at.astimezone(tz):%a %b %d %Y, %H:%M} "
        f"({cfg.timezone}), covering {snap.start.astimezone(tz):%a %b %d} to "
        f"{snap.end.astimezone(tz):%a %b %d}. All times are {cfg.timezone}.",
        "",
        "Feeds:",
    ]
    for f in snap.feeds:
        state = (f"{f.events} event{'s' if f.events != 1 else ''}" if f.configured and not f.error
                 else f"FAILED ({f.error})" if f.error else "not configured")
        lines.append(f"- {f.label}: {state}")
    for n in snap.notes:
        lines.append(f"- Note: {n}")
    for cid, c in courses.items():
        evs = [e for e in snap.events if e.course == cid]
        if c.policy is None:
            rule = "no AI policy configured: list its due dates only, plan no study work for it"
        elif not c.ai_allowed:
            rule = "its AI policy allows no AI use: list its due dates only, plan no study work for it"
        else:
            rule = "AI policy in the protocol above"
        lines += ["", f"## {c.label}", "", f"Course rule: {rule}."]
        due = [e for e in evs if e.due]
        other = [e for e in evs if not e.due]
        lines += ["", "Due dates:"]
        lines += [f"- {_when_text(e, tz)}: {e.summary} [{_label(e.source)}]"
                  + (f"\n  Details: {_plain(e.description)}" if e.description else "")
                  for e in due] or ["- none in this window"]
        if other:
            lines += ["", "Other course events:"]
            lines += [f"- {_when_text(e, tz)}: {e.summary}"
                      + (f" ({e.location})" if e.location else "") for e in other]
    rest = [e for e in snap.events if e.course is None]
    lines += ["", "## Not tied to a configured course", "",
              "Titles and times only. Due dates here belong to no configured course: list "
              "them, but plan no study work for them."]
    lines += [f"- {_when_text(e, tz)}: {e.summary} [{_label(e.source)}"
              + (", due date" if e.due else "") + "]" for e in rest] or ["- none"]
    return "\n".join(lines) + "\n"
