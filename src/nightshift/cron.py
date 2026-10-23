"""Cron-style timing rules for Schedules (§2.5).

The standard five fields (minute, hour, day of month, month, day of week)
with ``*``, numbers, ``a-b`` ranges, ``/step``, comma lists, month and
weekday names (``jan``, ``mon``), ``7`` for Sunday, and the macros
``@hourly``, ``@daily``/``@midnight``, ``@weekly``, ``@monthly`` and
``@yearly``/``@annually``. As in Vixie cron, when both day fields are
restricted (neither starts with ``*``) a day matches if either does.

Fire times are wall-clock times in the configured timezone, so a 6:30am
Schedule fires at 6:30 on both sides of a DST change. A wall time that
doesn't exist (the spring-forward gap) fires at the same instant read with
the old offset, i.e. just after the gap (2:30 -> 3:30 PDT); a repeated wall
time (fall back) fires once, the first time.

Pure: every function takes the times it compares, so the Schedule rules are
unit tested with a fake clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = {m: i for i, m in enumerate(
    "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
WEEKDAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
FIELDS = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, MONTHS),
    ("day of week", 0, 7, WEEKDAYS),
)

#: How far ahead ``next_after`` looks (covers Feb 29 rules).
SEARCH_DAYS = 366 * 5
#: ``fires_between`` looks back at most this far; older fires are ignored.
MAX_SPAN_DAYS = 400


class CronError(ValueError):
    """A timing rule that can't be parsed or never fires."""


def _value(token: str, lo: int, hi: int, names: dict[str, int], fname: str) -> int:
    t = token.lower()
    if t in names:
        return names[t]
    if not re.fullmatch(r"\d+", t):
        raise CronError(f"{fname}: {token!r} is not a number"
                        + (f" or a name ({', '.join(names)})" if names else ""))
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"{fname}: {v} is outside {lo}-{hi}")
    return v


def _field(text: str, lo: int, hi: int, names: dict[str, int], fname: str) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"{fname}: empty list item in {text!r}")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not re.fullmatch(r"\d+", step_s) or int(step_s) == 0:
                raise CronError(f"{fname}: bad step {step_s!r}")
            step = int(step_s)
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            x, _, y = rng.partition("-")
            a, b = _value(x, lo, hi, names, fname), _value(y, lo, hi, names, fname)
            if a > b:
                raise CronError(f"{fname}: range {rng!r} runs backwards")
        else:
            a = _value(rng, lo, hi, names, fname)
            b = hi if step_s else a  # "5/10" means 5, 15, 25, ...
        out.update(range(a, b + 1, step))
    return out


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]  # 0 = Sunday
    dom_star: bool
    dow_star: bool

    def matches_day(self, d: date) -> bool:
        if d.month not in self.months:
            return False
        dom_ok = d.day in self.doms
        dow_ok = d.isoweekday() % 7 in self.dows
        # Vixie cron: a day field starting with "*" (even "*/2") restricts
        # together with the other; two restricted day fields match either.
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok

    def day_fires(self, d: date, tz: ZoneInfo) -> list[datetime]:
        """The fire instants of local day ``d``, in order, without repeats."""
        if not self.matches_day(d):
            return []
        seen: dict[datetime, None] = {}
        for h in self.hours:
            for m in self.minutes:
                # fold=0: a gap time reads with the old offset (lands just
                # after the gap); a repeated time fires the first time.
                inst = datetime.combine(d, time(h, m), tzinfo=tz).astimezone(timezone.utc)
                seen[inst] = None
        return [i.astimezone(tz) for i in sorted(seen)]

    def next_after(self, t: datetime, tz: ZoneInfo) -> datetime | None:
        """The first fire strictly after ``t`` (aware), or None."""
        day = t.astimezone(tz).date() - timedelta(days=1)
        best: datetime | None = None
        found_on: int | None = None
        for i in range(SEARCH_DAYS):
            for inst in self.day_fires(day + timedelta(days=i), tz):
                if inst > t:
                    best = inst if best is None or inst < best else best
                    break
            if best is not None:
                # Look one day past the first hit, in case a DST gap pushed
                # a late fire past the next day's first one.
                if found_on is None:
                    found_on = i
                else:
                    break
        return best

    def fires_in(self, after: datetime, until: datetime, tz: ZoneInfo) -> list[datetime]:
        """Every fire in ``(after, until]``, in order (for a bounded span,
        e.g. the clean-week check's 7 days)."""
        out: list[datetime] = []
        d = after.astimezone(tz).date() - timedelta(days=1)
        last = until.astimezone(tz).date() + timedelta(days=1)
        while d <= last:
            out += [i for i in self.day_fires(d, tz) if after < i <= until]
            d += timedelta(days=1)
        return out

    def fires_between(self, after: datetime, until: datetime,
                      tz: ZoneInfo) -> tuple[int, datetime | None]:
        """How many fires fall in ``(after, until]`` and the latest of them.

        Only the last ``MAX_SPAN_DAYS`` are looked at, so a machine that
        slept for years still answers quickly (older fires collapse anyway).
        """
        if until <= after:
            return 0, None
        start = max(after, until - timedelta(days=MAX_SPAN_DAYS))
        first = start.astimezone(tz).date() - timedelta(days=1)
        last = until.astimezone(tz).date() + timedelta(days=1)
        count, latest = 0, None
        d = first
        while d <= last:
            for inst in self.day_fires(d, tz):
                if start < inst <= until:
                    count += 1
                    if latest is None or inst > latest:
                        latest = inst
            d += timedelta(days=1)
        return count, latest


def parse(expr: str) -> Cron:
    """Parse a five-field cron rule (or macro). Raises :class:`CronError`."""
    if not isinstance(expr, str) or not expr.strip():
        raise CronError("a cron rule is five fields: minute hour day-of-month month day-of-week")
    text = " ".join(expr.split())
    body = MACROS.get(text.lower(), text)
    if body.startswith("@"):
        raise CronError(f"unknown macro {text!r} (known: {', '.join(MACROS)})")
    parts = body.split(" ")
    if len(parts) != 5:
        raise CronError(f"{text!r} has {len(parts)} fields; a cron rule has 5: "
                        "minute hour day-of-month month day-of-week (e.g. '30 6 * * *')")
    sets = [_field(p, lo, hi, names, fname) for p, (fname, lo, hi, names) in zip(parts, FIELDS)]
    dows = {d % 7 for d in sets[4]}
    c = Cron(expr=text, minutes=tuple(sorted(sets[0])), hours=tuple(sorted(sets[1])),
             doms=frozenset(sets[2]), months=frozenset(sets[3]), dows=frozenset(dows),
             dom_star=parts[2].startswith("*"), dow_star=parts[4].startswith("*"))
    # e.g. "0 0 31 2 *" (Feb 31) would never fire.
    if c.next_after(datetime(2000, 1, 1, tzinfo=timezone.utc), ZoneInfo("UTC")) is None:
        raise CronError(f"{text!r} never fires")
    return c
