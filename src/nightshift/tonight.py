"""The "tonight's queue" preview (§4 item 8): what will run in tonight's
long-task window (midnight to 7am by default), in queue order, and roughly
how much usage it will take.

It replays the queue on a simulated clock, one Task at a time as the daemon
runs them, from now until the window ends:

* Tasks in the queue now, Tasks in ``retry_wait`` (from their ``retry_at``)
  and Tasks paused on a usage limit or the daily spend cap (from their
  ``resume_at``) are the candidates; the running Task holds the queue until
  its estimate runs out.
* Enabled Schedules add the Occurrences they will create, at each fire
  (after the Overlap rule ``skip``: a fire while the Schedule's previous
  Occurrence is still unfinished creates nothing).
* At each step the next Task is ``priority.pick_next``'s, so the order is
  the queue's own: Effective priority (deadlines coming within 24 hours
  turn urgent as the clock moves), long-Task gating and ``--at``.
* Each Task takes its ``estimated_minutes`` (``DEFAULT_MINUTES`` when it has
  none).

Usage holds (a Window at its threshold, the daytime reserve, an agent out
of usage), failures, retries and Tasks finishing early or late are not
predicted: the preview is a rough guide. Only Tasks that would be running
inside the window are listed.

The usage estimate of each entry is its minutes times the past cost per
minute of the same kind of work: finished Attempts' reported
cost-equivalent over their wall-clock minutes, on the same agent and
template, else agent and Task type, else the agent. Better forecasting is
Phase 4.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import pauses, planning, priority, schedules, templates, usage
from .config import Config, ConfigError
from .db import Database
from .states import TERMINAL, TaskState
from .timeutil import iso, parse_iso

#: Minutes assumed for a Task with no ``estimated_minutes``.
DEFAULT_MINUTES = 30

#: A guard on the replay; a night never holds this many steps.
MAX_STEPS = 1000


# --------------------------------------------------------------------------
# Past cost per minute
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rate:
    """Reported cost-equivalent per minute of Attempt, from history."""

    usd_per_minute: float
    #: What it was learned from, e.g. "claude, template posting-scan".
    basis: str
    attempts: int


class CostHistory:
    """Past cost per minute by agent and template, agent and Task type, and agent."""

    def __init__(self, rows: list[Any]):
        totals: dict[tuple, list[float]] = defaultdict(lambda: [0.0, 0.0, 0])
        for r in rows:
            minutes = (parse_iso(r["ended_at"]) - parse_iso(r["started_at"])).total_seconds() / 60
            if minutes <= 0:
                continue
            keys = [("agent", r["agent"]), ("type", r["agent"], r["task_type"])]
            if r["template"]:
                keys.append(("template", r["agent"], r["template"]))
            for k in keys:
                t = totals[k]
                t[0] += float(r["cost_usd"] or 0.0)
                t[1] += minutes
                t[2] += 1
        self._totals = dict(totals)

    @classmethod
    def load(cls, db: Database) -> "CostHistory":
        return cls(db.attempt_costs())

    def rate(self, agent: str, template: str | None, task_type: str) -> Rate | None:
        """The most specific past cost per minute there is; None with no history."""
        tries = []
        if template:
            tries.append((("template", agent, template), f"{agent}, template {template}"))
        tries += [(("type", agent, task_type), f"{agent}, {task_type} Tasks"),
                  (("agent", agent), f"{agent}, all Tasks")]
        for key, basis in tries:
            t = self._totals.get(key)
            if t and t[1] > 0:
                return Rate(usd_per_minute=t[0] / t[1], basis=basis, attempts=int(t[2]))
        return None


@dataclass(frozen=True)
class UsageEstimate:
    cost_usd: float
    rate: Rate


# --------------------------------------------------------------------------
# The preview
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TonightEntry:
    """One Task expected to run tonight."""

    #: None for an Occurrence a Schedule has not created yet.
    task_id: str | None
    title: str
    task_type: str
    agent: str
    priority: str
    #: The Effective priority when it is expected to start.
    effective_priority: str
    start: datetime
    end: datetime
    #: The Task's own estimate; None when it has none (``DEFAULT_MINUTES`` assumed).
    estimated_minutes: int | None
    #: Minutes it is expected to take from ``start`` (the rest, for the running Task).
    minutes: float
    #: The Schedule it is (or will be) an Occurrence of.
    schedule: str | None
    #: Short notes: "running now", "fires 02:00", "--at 03:00", "long Task", ...
    notes: list[str]
    #: None with no cost history for its agent.
    estimate: UsageEstimate | None


@dataclass(frozen=True)
class AgentTotal:
    agent: str
    cost_usd: float
    minutes: float
    #: Entries on this agent with a usage estimate (in ``cost_usd``) and without.
    estimated: int
    unestimated: int
    #: The estimate as a percent of the agent's shortest Window's Baseline;
    #: None with no Baseline.
    percent_of_baseline: float | None
    window: str | None


@dataclass(frozen=True)
class TonightPreview:
    #: Tonight's long-task window (the one under way, if it is now inside it).
    start: datetime
    end: datetime
    entries: list[TonightEntry]
    totals: list[AgentTotal]
    #: Tasks still waiting when the window ends.
    left_over: int


def window_bounds(cfg: Config, now: datetime) -> tuple[datetime, datetime]:
    """Tonight's long-task window: the one under way, else the next one."""
    tz = ZoneInfo(cfg.timezone)
    start_t, end_t = priority.long_window(cfg)
    local = now.astimezone(tz)
    start = datetime.combine(local.date(), start_t, tz)
    if priority.in_long_window(cfg, now):
        if start > local:
            start -= timedelta(days=1)
    elif start <= local:
        start += timedelta(days=1)
    end = datetime.combine(start.date(), end_t, tz)
    if end <= start:
        end += timedelta(days=1)
    return start, end


@dataclass
class _Item:
    row: dict[str, Any]
    available_at: datetime
    minutes: float
    estimated_minutes: int | None
    notes: list[str] = field(default_factory=list)
    projected: bool = False


def _minutes(estimated: int | None) -> float:
    return float(estimated) if estimated else float(DEFAULT_MINUTES)


def _local_hm(dt: datetime, tz: ZoneInfo) -> str:
    return dt.astimezone(tz).strftime("%H:%M")


def _candidates(cfg: Config, db: Database, now: datetime) -> list[_Item]:
    tz = ZoneInfo(cfg.timezone)
    items = []
    for r in db.tasks_in_state(TaskState.QUEUED):
        items.append(_Item(dict(r), now, _minutes(r["estimated_minutes"]),
                           r["estimated_minutes"]))
    for r in db.tasks_in_state(TaskState.RETRY_WAIT):
        at = max(now, parse_iso(r["retry_at"])) if r["retry_at"] else now
        items.append(_Item(dict(r), at, _minutes(r["estimated_minutes"]), r["estimated_minutes"],
                           [f"retry from {_local_hm(at, tz)}"]))
    for state in (TaskState.PAUSED_USAGE, TaskState.PAUSED_SPEND):
        for r in db.tasks_in_state(state):
            if not r["resume_at"] or r["pause_kind"] == pauses.KIND_BY_HAND:
                continue  # waits for you, not for the clock
            at = max(now, parse_iso(r["resume_at"]))
            row = dict(r)
            if state == TaskState.PAUSED_USAGE:
                row["resume_pending"] = 1
            items.append(_Item(row, at, _minutes(r["estimated_minutes"]),
                               r["estimated_minutes"], [f"resumes {_local_hm(at, tz)}"]))
    items.sort(key=lambda i: (i.row["created_at"]))
    return items


def _running(cfg: Config, db: Database, now: datetime) -> tuple[_Item, datetime] | None:
    """The running Task (the oldest, were there several) and when its estimate runs out."""
    rows = db.tasks_in_state(TaskState.RUNNING)
    if not rows:
        return None
    r = rows[0]
    attempts = db.attempts(r["id"])
    started = (parse_iso(attempts[-1]["started_at"]) if attempts and not attempts[-1]["ended_at"]
               else parse_iso(r["state_changed_at"]))
    total = _minutes(r["estimated_minutes"])
    left = max(total - (now - started).total_seconds() / 60, 1.0)
    return _Item(dict(r), now, left, r["estimated_minutes"], ["running now"]), now + timedelta(
        minutes=left)


@dataclass
class _Fire:
    at: datetime
    schedule: schedules.Schedule
    template: templates.Template
    #: A fire already due, which the daemon creates on its next poll.
    due: bool = False


def _fires(cfg: Config, db: Database, now: datetime, end: datetime) -> list[_Fire]:
    """Every fire of an enabled Schedule from now until ``end``; a fire
    already due (the daemon handles it on its next poll) counts as now
    when its Catch-up rule still allows it."""
    tz = ZoneInfo(cfg.timezone)
    out = []
    for s in schedules.all_schedules(db):
        if not s.enabled:
            continue
        try:
            tpl = templates.load(cfg, s.template)
        except (templates.TemplateError, ConfigError):
            continue  # the fire would be an error; nothing runs
        count, latest = schedules.due(cfg, s, now)
        if count and latest is not None and s.catch_up.allows(latest, now):
            out.append(_Fire(now, s, tpl, due=True))
        t = max(now, parse_iso(s.last_fire_at))
        while True:
            nxt = s.cron.next_after(t, tz)
            if nxt is None or nxt >= end:
                break
            out.append(_Fire(nxt, s, tpl))
            t = nxt
    out.sort(key=lambda f: f.at)
    return out


def _occurrence(cfg: Config, f: _Fire) -> _Item:
    tz = ZoneInfo(cfg.timezone)
    tpl, s = f.template, f.schedule
    try:
        estimated: int | None = planning.from_template(tpl.as_dict()).estimated_minutes
    except planning.PlanningError:
        estimated = None
    day = f.at.astimezone(tz).date()
    row = {
        "id": None, "title": f"{s.name} {day:%Y-%m-%d}", "task_type": tpl.task_type,
        "agent": s.agent or tpl.agent, "priority": s.priority or cfg.default("priority"),
        "deadline": None, "run_at": None, "long": int(bool(tpl.long)),
        "created_at": iso(f.at), "template": tpl.name, "schedule_name": s.name,
        "resume_pending": 0,
    }
    return _Item(row, f.at, _minutes(estimated), estimated,
                 ["fire due now" if f.due else f"fires {_local_hm(f.at, tz)}"], projected=True)


def preview(cfg: Config, db: Database, now: datetime,
            history: CostHistory | None = None) -> TonightPreview:
    """What is expected to run in tonight's long-task window, in queue order."""
    tz = ZoneInfo(cfg.timezone)
    start, end = window_bounds(cfg, now)
    history = history if history is not None else CostHistory.load(db)
    pending = _candidates(cfg, db, now)
    fires = _fires(cfg, db, now, end)
    ran: list[tuple[_Item, datetime, datetime, str]] = []

    # Occurrences that never finish tonight as far as the replay knows
    # (waiting on you, paused by hand, ...): their Schedule's fires skip.
    in_replay = {i.row["id"] for i in pending}
    stuck = {r["schedule_name"] for r in db.list_tasks()
             if r["schedule_name"] and TaskState(r["state"]) not in TERMINAL
             and r["id"] not in in_replay and r["state"] != str(TaskState.RUNNING)}

    t = now
    running = _running(cfg, db, now)
    if running is not None:
        item, until = running
        ev = priority.evaluate(cfg, item.row, now)
        ran.append((item, now, until, ev.effective))
        t = until

    def unfinished(name: str, at: datetime) -> bool:
        if name in stuck:
            return True
        if any(i.row.get("schedule_name") == name for i in pending):
            return True
        return any(i.row.get("schedule_name") == name and e > at for i, _, e, _ in ran)

    for _ in range(MAX_STEPS):
        while fires and fires[0].at <= t:
            f = fires.pop(0)
            if f.schedule.overlap == schedules.OVERLAP_SKIP and unfinished(f.schedule.name, f.at):
                continue
            if f.schedule.overlap == schedules.OVERLAP_REPLACE:
                pending = [i for i in pending if i.row.get("schedule_name") != f.schedule.name]
            pending.append(_occurrence(cfg, f))
        if t >= end:
            break
        ready = [i for i in pending if i.available_at <= t]
        order = priority.order(cfg, [i.row for i in ready], t)
        pick = next((ev for ev in order if ev.blocked is None), None)
        if pick is not None:
            item = next(i for i in ready if i.row is pick.row)
            pending.remove(item)
            finish = t + timedelta(minutes=item.minutes)
            ran.append((item, t, finish, pick.effective))
            t = finish
            continue
        # Nothing may start now: jump to the next moment something changes.
        wake = [i.available_at for i in pending if i.available_at > t]
        wake += [parse_iso(i.row["run_at"]) for i in ready
                 if i.row["run_at"] and parse_iso(i.row["run_at"]) > t]
        if fires:
            wake.append(fires[0].at)
        if start > t and any(i.row["long"] for i in pending):
            wake.append(start)
        if not wake:
            break
        t = min(wake)

    entries = []
    for item, s, e, eff in ran:
        if e <= start or s >= end:
            continue
        row = item.row
        notes = list(item.notes)
        if row.get("run_at") and not item.projected:
            notes.append(f"--at {_local_hm(parse_iso(row['run_at']), tz)}")
        if row["long"]:
            notes.append("long Task")
        if item.estimated_minutes is None:
            notes.append(f"no estimate ({DEFAULT_MINUTES} min assumed)")
        rate = history.rate(row["agent"], row.get("template"), row["task_type"])
        entries.append(TonightEntry(
            task_id=row["id"], title=row["title"], task_type=row["task_type"],
            agent=row["agent"], priority=row["priority"], effective_priority=eff,
            start=s.astimezone(tz), end=e.astimezone(tz),
            estimated_minutes=item.estimated_minutes, minutes=item.minutes,
            schedule=row.get("schedule_name"), notes=notes,
            estimate=(UsageEstimate(cost_usd=item.minutes * rate.usd_per_minute, rate=rate)
                      if rate is not None else None),
        ))
    return TonightPreview(start=start, end=end, entries=entries,
                          totals=_totals(cfg, db, entries),
                          left_over=sum(i.available_at < end for i in pending))


def _shortest_baseline(cfg: Config, db: Database, agent: str) -> tuple[str, float] | None:
    best = None
    for w in usage.windows_of(cfg, db, agent):
        base = usage.baseline(db, agent, w)
        if base is None:
            continue
        hours = cfg.window_hours(agent, w)
        if best is None or hours < best[0]:
            best = (hours, w, base.cost_usd)
    return (best[1], best[2]) if best is not None else None


def _totals(cfg: Config, db: Database, entries: list[TonightEntry]) -> list[AgentTotal]:
    out = []
    for agent in dict.fromkeys(e.agent for e in entries):
        mine = [e for e in entries if e.agent == agent]
        cost = sum(e.estimate.cost_usd for e in mine if e.estimate is not None)
        base = _shortest_baseline(cfg, db, agent)
        estimated = sum(e.estimate is not None for e in mine)
        out.append(AgentTotal(
            agent=agent, cost_usd=cost, minutes=sum(e.minutes for e in mine),
            estimated=estimated, unestimated=len(mine) - estimated,
            percent_of_baseline=(cost / base[1] * 100 if base and estimated else None),
            window=base[0] if base and estimated else None,
        ))
    return out
