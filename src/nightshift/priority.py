"""Priority, Effective priority, deadlines and time windows (§2.3).

Everything here is pure: it takes Task rows (or plain values) and a ``now``
from the caller's clock, so ordering and gating are testable with a fake
clock. All wall-clock rules (the long-task window, ``--at HH:MM``, a date-only
``--deadline``) are evaluated in the configured timezone, America/Los_Angeles
by default, whatever zone ``now`` arrives in.

``task.yaml`` and the ``tasks.priority`` column hold only the Priority the
user assigned. The Effective priority is never stored; :func:`pick_next`
recalculates it every time the queue is evaluated.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import Config, ConfigError
from .timeutil import iso, parse_iso

PRIORITIES = ("urgent", "normal", "background")
RANK = {p: i for i, p in enumerate(PRIORITIES)}

Row = Mapping[str, Any]


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def _hhmm(value: str) -> time:
    h, m = (int(x) for x in value.split(":"))
    return time(h, m)


def long_window(cfg: Config) -> tuple[time, time]:
    q = cfg.data.get("queue") or {}
    return _hhmm(str(q.get("long_window_start", "00:00"))), _hhmm(
        str(q.get("long_window_end", "07:00"))
    )


def deadline_urgent_hours(cfg: Config) -> float:
    return float((cfg.data.get("queue") or {}).get("deadline_urgent_hours", 24))


# --------------------------------------------------------------------------
# Effective priority and gating
# --------------------------------------------------------------------------


def effective_priority(
    assigned: str, deadline: str | None, now: datetime, *, urgent_within_hours: float = 24
) -> str:
    """The assigned Priority, raised to urgent once the deadline is within
    ``urgent_within_hours`` (a passed deadline counts as within)."""
    if assigned not in RANK:
        raise ValueError(f"unknown priority {assigned!r}")
    if deadline and parse_iso(deadline) - now <= timedelta(hours=urgent_within_hours):
        return "urgent"
    return assigned


def in_long_window(cfg: Config, now: datetime) -> bool:
    """Whether ``now`` (in the configured timezone) is inside the long-task
    window, by default midnight to 7am. Start inclusive, end exclusive."""
    start, end = long_window(cfg)
    local = now.astimezone(ZoneInfo(cfg.timezone)).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end  # a window that wraps midnight


@dataclass(frozen=True)
class Evaluation:
    """One queued Task as the queue sees it at one moment."""

    row: Row
    effective: str
    #: Why the Task may not start now; None when it may.
    blocked: str | None

    @property
    def task_id(self) -> str:
        return str(self.row["id"])


def evaluate(cfg: Config, row: Row, now: datetime) -> Evaluation:
    eff = effective_priority(
        row["priority"], row["deadline"], now, urgent_within_hours=deadline_urgent_hours(cfg)
    )
    blocked = None
    if row["run_at"] and parse_iso(row["run_at"]) > now:
        blocked = f"waiting until {row['run_at']} (--at)"
    elif row["long"] and eff != "urgent" and not in_long_window(cfg, now):
        start, end = long_window(cfg)
        blocked = f"long Task: starts only between {start:%H:%M} and {end:%H:%M} unless urgent"
    return Evaluation(row=row, effective=eff, blocked=blocked)


def _order_key(ev: Evaluation) -> tuple:
    row = ev.row
    deadline = parse_iso(row["deadline"]).timestamp() if row["deadline"] else float("inf")
    # Urgent Tasks: nearest deadline first. Normal and background run in order.
    return (
        RANK[ev.effective],
        deadline if ev.effective == "urgent" else 0.0,
        parse_iso(row["created_at"]).timestamp(),
    )


def order(cfg: Config, rows: Iterable[Row], now: datetime) -> list[Evaluation]:
    """Queued Tasks in queue order (startable or not), Effective priority
    first. ``rows`` must already be oldest-first so equal keys keep order."""
    return sorted((evaluate(cfg, r, now) for r in rows), key=_order_key)


def pick_next(cfg: Config, rows: Iterable[Row], now: datetime) -> Row | None:
    """The queued Task to start now, or None if none may start.

    Urgent jumps the queue, normal runs in order, and background runs only
    when nothing else is waiting: since only one Task runs at a time, it is
    picked only when no urgent or normal Task may start now. A Task held back
    by ``--at`` or the long-task window isn't waiting to run yet, so it
    doesn't block the queue.
    """
    for ev in order(cfg, rows, now):
        if ev.blocked is None:
            return ev.row
    return None


# --------------------------------------------------------------------------
# Parsing --deadline and --at
# --------------------------------------------------------------------------

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TIME = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")


def _localize(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def _parse_datetime(text: str, tz: ZoneInfo) -> datetime | None:
    try:
        return _localize(datetime.fromisoformat(text.strip().replace(" ", "T", 1)), tz)
    except ValueError:
        return None


def parse_deadline(text: str, now: datetime, tz_name: str) -> str:
    """``--deadline``: ``YYYY-MM-DD`` (the end of that day), ``YYYY-MM-DD
    HH:MM`` or an ISO 8601 timestamp. Naive values are local time. Returns
    ISO 8601 in the configured timezone; a deadline already passed is an
    error."""
    tz = ZoneInfo(tz_name)
    text = text.strip()
    if _DATE.fullmatch(text):
        try:
            day = datetime.fromisoformat(text).date()
        except ValueError:
            raise ConfigError(f"--deadline {text!r} is not a valid date") from None
        dt = datetime.combine(day, time(23, 59, 59), tz)
    else:
        dt = _parse_datetime(text, tz)
        if dt is None:
            raise ConfigError(
                f"--deadline {text!r}: use YYYY-MM-DD, 'YYYY-MM-DD HH:MM' or ISO 8601"
            )
    if dt <= now:
        raise ConfigError(f"--deadline {iso(dt)} has already passed")
    return iso(dt)


def parse_at(text: str, now: datetime, tz_name: str) -> str:
    """``--at``: ``HH:MM`` (its next occurrence, today or tomorrow), ``YYYY-MM-DD
    HH:MM`` or an ISO 8601 timestamp. Naive values are local time. Returns ISO
    8601 in the configured timezone; an explicit time already passed is an
    error."""
    tz = ZoneInfo(tz_name)
    text = text.strip()
    m = _TIME.fullmatch(text)
    if m:
        local_now = now.astimezone(tz)
        dt = datetime.combine(local_now.date(), time(int(m[1]), int(m[2])), tz)
        if dt <= local_now:
            dt = datetime.combine(local_now.date() + timedelta(days=1), dt.time(), tz)
        return iso(dt)
    dt = _parse_datetime(text, tz)
    if dt is None or _DATE.fullmatch(text):
        raise ConfigError(f"--at {text!r}: use HH:MM, 'YYYY-MM-DD HH:MM' or ISO 8601")
    if dt <= now:
        raise ConfigError(f"--at {iso(dt)} has already passed")
    return iso(dt)
