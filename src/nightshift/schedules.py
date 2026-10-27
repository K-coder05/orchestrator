"""Schedules: Catch-up and Overlap rules (§2.5).

A Schedule is a named cron-style rule that creates Occurrences from a
template: ordinary Tasks, pre-approved (§2.4), whose folders live under
``recurring/<schedule-name>/YYYY-MM-DD/``. Its state lives in SQLite
(``schedules``, ``schedule_runs``): ``last_fire_at`` says every fire up to
then has been handled.

The daemon calls :func:`apply_catch_up` on every start, every wake and
every poll. It compares the time now with each Schedule's ``last_fire_at``;
all the fires in between (one when on time, several after a sleep) collapse
into **one** decision about the latest of them:

* Catch-up rule ``within N hours``: create one Occurrence if the latest
  fire is at most N hours ago, else skip it. ``next wake``: always create
  one.
* Overlap rule, when an earlier Occurrence hasn't reached a terminal state
  (done, partial, failed, cancelled): ``skip`` (default) logs the skipped
  fire and creates nothing; ``replace`` cancels the unfinished Occurrence
  and creates a fresh one.

`orch schedule edit` and the dashboard change the rules through
:func:`edit` (via ``nightshift.control``); the daemon uses them from its
next evaluation.

Every time comes from the caller, so all of it is unit tested with a fake
clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import cron, failures, ical, planning, templates
from .config import Config, ConfigError
from .db import Database
from .runner import create_task
from .states import TERMINAL, TaskState
from .timeutil import iso, parse_iso

CATCH_UP_WITHIN = "within"
CATCH_UP_NEXT_WAKE = "next_wake"
OVERLAP_SKIP = "skip"
OVERLAP_REPLACE = "replace"
OVERLAPS = (OVERLAP_SKIP, OVERLAP_REPLACE)

#: Why the Schedules were looked at. ``run_now`` is `orch schedule run-now`.
TRIGGER_START, TRIGGER_WAKE, TRIGGER_POLL, TRIGGER_RUN_NOW = "start", "wake", "poll", "run_now"

CREATED, REPLACED = "created", "replaced"
SKIPPED_OVERLAP, SKIPPED_CATCH_UP, ERROR = "skipped_overlap", "skipped_catch_up", "error"

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")


class ScheduleError(Exception):
    """A Schedule that can't be added or changed as asked."""


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CatchUp:
    kind: str  # CATCH_UP_WITHIN | CATCH_UP_NEXT_WAKE
    hours: float | None = None

    def __str__(self) -> str:
        if self.kind == CATCH_UP_NEXT_WAKE:
            return "run on next wake"
        return f"within {self.hours:g}h, else skip"

    def allows(self, fire_at: datetime, now: datetime) -> bool:
        if self.kind == CATCH_UP_NEXT_WAKE:
            return True
        return now - fire_at <= timedelta(hours=float(self.hours or 0))


def parse_catch_up(text: str) -> CatchUp:
    """``next-wake`` / ``next wake``, or ``within 12h`` / ``within:12`` /
    ``12h`` / ``within 12 hours, else skip``."""
    t = " ".join(str(text or "").strip().lower().replace("_", " ").split())
    if t.replace("-", " ") in ("next wake", "on next wake", "run on next wake", "wake"):
        return CatchUp(CATCH_UP_NEXT_WAKE)
    m = re.fullmatch(
        r"(?:run )?(?:within ?:? ?)?(\d+(?:\.\d+)?) ?(?:h|hr|hrs|hour|hours)?(?:,? ?else skip)?", t)
    if not m or float(m.group(1)) <= 0:
        raise ScheduleError(
            f"bad Catch-up rule {text!r}: use 'within <N>h' (run within N hours, else skip) "
            "or 'next-wake'")
    return CatchUp(CATCH_UP_WITHIN, float(m.group(1)))


def parse_cron(text: str) -> cron.Cron:
    try:
        return cron.parse(text)
    except cron.CronError as exc:
        raise ScheduleError(f"bad cron rule: {exc}") from None


def check_overlap(text: str) -> str:
    if text not in OVERLAPS:
        raise ScheduleError(f"Overlap rule must be one of {', '.join(OVERLAPS)}, not {text!r}")
    return text


def catch_up_arg(cu: CatchUp) -> str:
    """``cu`` as ``--catch-up`` takes it (what an edit form starts from)."""
    return "next-wake" if cu.kind == CATCH_UP_NEXT_WAKE else f"within {cu.hours:g}h"


@dataclass(frozen=True)
class Schedule:
    name: str
    template: str
    request: str
    cron: cron.Cron
    catch_up: CatchUp
    overlap: str
    priority: str | None
    agent: str | None
    enabled: bool
    created_at: str
    last_fire_at: str
    last_checked_at: str | None

    @classmethod
    def from_row(cls, row: Any) -> "Schedule":
        return cls(
            name=row["name"], template=row["template"], request=row["request"] or "",
            cron=cron.parse(row["cron"]),
            catch_up=CatchUp(row["catch_up"], row["catch_up_hours"]),
            overlap=row["overlap"], priority=row["priority"], agent=row["agent"],
            enabled=bool(row["enabled"]), created_at=row["created_at"],
            last_fire_at=row["last_fire_at"], last_checked_at=row["last_checked_at"],
        )


def validate_name(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise ScheduleError(
            f"bad Schedule name {name!r}: lowercase letters, digits and '-', up to 40 "
            "characters (it names the folder recurring/<name>/)")
    return name


def get(db: Database, name: str) -> Schedule:
    row = db.get_schedule(name)
    if row is None:
        raise ScheduleError(f"no Schedule {name!r}")
    return Schedule.from_row(row)


def all_schedules(db: Database) -> list[Schedule]:
    return [Schedule.from_row(r) for r in db.schedules()]


def _check_template(cfg: Config, template: str, request: str, agent: str | None) -> None:
    """Load and fill in the template now, so a typo or a missing input file
    fails `orch schedule add`, never a 2am fire."""
    try:
        tpl = templates.load(cfg, template)
        templates.instantiate(cfg, tpl, request)
        cfg.agent(agent or tpl.agent)
    except (templates.TemplateError, ConfigError) as exc:
        raise ScheduleError(str(exc)) from None


def standard_rules(cfg: Config, template: str) -> dict | None:
    """The template's standard Schedule rules (its ``schedule:`` block):
    {cron, catch_up, overlap}, or None when it has none."""
    try:
        return templates.load(cfg, template).schedule
    except templates.TemplateError as exc:
        raise ScheduleError(str(exc)) from None


def add(cfg: Config, db: Database, name: str, *, template: str | None = None,
        cron_rule: str | None = None, catch_up: str | CatchUp | None = None,
        overlap: str | None = None, request: str = "", priority: str | None = None,
        agent: str | None = None, enabled: bool = True, at: datetime) -> Schedule:
    """Add a Schedule. Fires before ``at`` never count as missed.

    ``template`` defaults to the Schedule's name; a rule left out comes from
    the template's ``schedule:`` block (so ``orch schedule add posting-scan``
    sets up the daily 6:30am scan, §3.1 item 10), and so does an empty
    ``request``. Overlap defaults to skip.
    """
    validate_name(name)
    if db.get_schedule(name) is not None:
        raise ScheduleError(f"a Schedule named {name!r} already exists")
    template = template or name
    std = standard_rules(cfg, template) or {}
    request = request or std.get("request", "")
    if cron_rule is None or catch_up is None or overlap is None:
        missing = [flag for flag, v in (("--cron", cron_rule), ("--catch-up", catch_up))
                   if v is None and flag.strip("-").replace("-", "_") not in std]
        if missing:
            raise ScheduleError(
                f"template {template!r} has no standard Schedule (schedule: block), so "
                f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} required")
        cron_rule = std["cron"] if cron_rule is None else cron_rule
        catch_up = std["catch_up"] if catch_up is None else catch_up
        overlap = std.get("overlap", OVERLAP_SKIP) if overlap is None else overlap
    rule = parse_cron(cron_rule)
    cu = catch_up if isinstance(catch_up, CatchUp) else parse_catch_up(catch_up)
    check_overlap(overlap)
    if priority is not None and priority not in ("urgent", "normal", "background"):
        raise ScheduleError("priority must be urgent, normal or background")
    _check_template(cfg, template, request, agent)
    stamp = iso(at)
    db.insert_schedule({
        "name": name, "template": template, "request": request, "cron": rule.expr,
        "catch_up": cu.kind, "catch_up_hours": cu.hours, "overlap": overlap,
        "priority": priority, "agent": agent, "enabled": enabled,
        "created_at": stamp, "updated_at": stamp, "last_fire_at": stamp,
    })
    return get(db, name)


def remove(db: Database, name: str) -> list[str]:
    """Delete a Schedule; returns its unfinished Occurrences, which are left
    alone (cancel them with `orch cancel`)."""
    get(db, name)
    left = [r["id"] for r in unfinished_occurrences(db, name)]
    db.delete_schedule(name)
    return left


def set_enabled(db: Database, name: str, enabled: bool, *, at: datetime) -> bool:
    """Enable or disable; returns whether anything changed. Enabling starts
    counting fires from ``at``: fires while a Schedule was disabled are not
    missed fires, so they never catch up."""
    s = get(db, name)
    if s.enabled == enabled:
        return False
    db.set_schedule_enabled(name, enabled, at=iso(at), last_fire_at=iso(at) if enabled else None)
    return True


@dataclass(frozen=True)
class Edit:
    """What :func:`edit` did: the Schedule before and after, one line per
    rule changed (none: nothing to change) and how many fires of the old
    timing rule were still undecided and won't catch up."""

    old: Schedule
    new: Schedule
    changes: tuple[str, ...]
    dropped: int = 0


def edit(cfg: Config, db: Database, name: str, *, cron_rule: str | None = None,
         catch_up: str | None = None, overlap: str | None = None, at: datetime) -> Edit:
    """Change a Schedule's timing, Catch-up or Overlap rule (None: keep it),
    each checked as `orch schedule add` checks it. Nothing is changed unless
    every value given is valid.

    The daemon's next evaluation uses the new rules. A new Catch-up or
    Overlap rule also decides fires already due. A new timing rule counts
    fires from ``at``, as enabling does: its fires before the edit were
    never missed, so the edit never makes a fire that was handled run
    again. Fires of the old timing rule the daemon hadn't decided yet (it
    decides within seconds while it runs) are dropped and counted.
    """
    if cron_rule is None and catch_up is None and overlap is None:
        raise ScheduleError("nothing to change: give a timing (cron), Catch-up or Overlap rule")
    rule = parse_cron(cron_rule) if cron_rule is not None else None
    cu = parse_catch_up(catch_up) if catch_up is not None else None
    if overlap is not None:
        check_overlap(overlap)
    for _ in range(5):
        s = get(db, name)
        changes = []
        if rule is not None and rule.expr != s.cron.expr:
            changes.append(f"timing {s.cron.expr} -> {rule.expr}")
        if cu is not None and cu != s.catch_up:
            changes.append(f"catch-up {s.catch_up} -> {cu}")
        if overlap is not None and overlap != s.overlap:
            changes.append(f"overlap {s.overlap} -> {overlap}")
        if not changes:
            return Edit(s, s, ())
        retimed = rule is not None and rule.expr != s.cron.expr
        dropped, last = 0, s.last_fire_at
        if retimed:
            if s.enabled:
                dropped = due(cfg, s, at)[0]
            last = iso(max(parse_iso(s.last_fire_at), at))
        new_cu = cu or s.catch_up
        # Compare-and-set on last_fire_at: if the daemon claimed fires
        # meanwhile, look again rather than undo its claim.
        if db.update_schedule_rules(
                name, cron=(rule or s.cron).expr, catch_up=new_cu.kind,
                catch_up_hours=new_cu.hours, overlap=overlap or s.overlap, at=iso(at),
                last_fire_at=last, expect_last=s.last_fire_at):
            return Edit(s, get(db, name), tuple(changes), dropped)
    raise ScheduleError(f"Schedule {name!r} kept changing; try again")


def next_fire(cfg: Config, s: Schedule, now: datetime) -> datetime | None:
    tz = ZoneInfo(cfg.timezone)
    return s.cron.next_after(max(now, parse_iso(s.last_fire_at)), tz)


def due(cfg: Config, s: Schedule, now: datetime) -> tuple[int, datetime | None]:
    """Fires since ``last_fire_at``, up to ``now``: (count, latest)."""
    return s.cron.fires_between(parse_iso(s.last_fire_at), now, ZoneInfo(cfg.timezone))


def unfinished_occurrences(db: Database, name: str) -> list[Any]:
    return [r for r in db.occurrences(name) if TaskState(r["state"]) not in TERMINAL]


# --------------------------------------------------------------------------
# Firing
# --------------------------------------------------------------------------


@dataclass
class Decision:
    schedule: str
    trigger: str
    fire_at: str
    fires: int
    outcome: str
    task_id: str | None = None
    detail: str = ""
    replaced: list[str] = field(default_factory=list)
    folder: Path | None = None

    def describe(self) -> str:
        what = {
            CREATED: f"created Occurrence {self.task_id}",
            REPLACED: f"created Occurrence {self.task_id}",
            SKIPPED_OVERLAP: "skipped (Overlap rule)",
            SKIPPED_CATCH_UP: "skipped (Catch-up rule)",
            ERROR: "could not create an Occurrence",
        }[self.outcome]
        fires = "" if self.fires <= 1 else f" ({self.fires} missed fires collapsed into one)"
        when = "run now by hand" if self.trigger == TRIGGER_RUN_NOW else f"fire at {self.fire_at}"
        return (f"Schedule {self.schedule}: {when}{fires}: {what}"
                + (f": {self.detail}" if self.detail else ""))


def _log(cfg: Config, folder: str | Path, line: str) -> None:
    folder = Path(folder)
    if folder.exists():
        templates.log_line(folder, line, cfg.secrets.redact, cfg.timezone)


def _record(db: Database, d: Decision, now: datetime) -> Decision:
    db.record_schedule_run(schedule_name=d.schedule, trigger=d.trigger, fire_at=d.fire_at,
                           fires=d.fires, decided_at=iso(now), outcome=d.outcome,
                           task_id=d.task_id, detail=d.detail or None)
    return d


def _occurrence(cfg: Config, db: Database, s: Schedule, fire_at: datetime, *,
                trigger: str, fires: int, now: datetime) -> tuple[str, Path]:
    """Create one pre-approved Occurrence of ``s`` for ``fire_at``."""
    tpl = templates.load(cfg, s.template)
    instance = templates.instantiate(cfg, tpl, s.request)
    proposal = planning.from_template(tpl.as_dict())
    day = fire_at.astimezone(ZoneInfo(cfg.timezone)).date()
    why = ("run now by hand (orch schedule run-now)" if trigger == TRIGGER_RUN_NOW
           else f"fire at {iso(fire_at)}" + (f", {fires} missed fires collapsed" if fires > 1
                                              else ""))
    deadline = None
    if instance.course:
        # A course Occurrence's deadline: the course's next due date (§2.3).
        due = ical.course_deadline(cfg, instance.course, now)
        deadline = due.at if due is not None else None
    new = create_task(
        cfg, db, instance.prompt, title=f"{s.name} {day:%Y-%m-%d}", task_type=tpl.task_type,
        deadline=deadline,
        agent=s.agent or tpl.agent, agent_override=s.agent is not None, priority=s.priority,
        long=tpl.long, internet=tpl.internet, estimated_minutes=proposal.estimated_minutes,
        template=tpl.name, max_runtime_minutes=tpl.max_runtime_minutes,
        # Scheduled templates are pre-approved, so nothing stalls at 2am (§2.4).
        needs_approval=False, schedule_name=s.name, occurrence_date=day, course=instance.course,
        reason=f"Occurrence of Schedule {s.name} ({why}); pre-approved",
    )
    templates.install_inputs(cfg, instance, new.folder, new.spec.id)
    _log(cfg, new.folder, f"Occurrence of Schedule {s.name}: {why}")
    return new.spec.id, new.folder


def fire(cfg: Config, db: Database, s: Schedule, fire_at: datetime, *, now: datetime,
         trigger: str, fires: int = 1, daemon_up: bool = False) -> Decision:
    """Apply the Overlap rule and create (or skip) one Occurrence."""
    d = Decision(schedule=s.name, trigger=trigger, fire_at=iso(fire_at), fires=fires,
                 outcome=CREATED)
    unfinished = unfinished_occurrences(db, s.name)
    if unfinished and s.overlap == OVERLAP_SKIP:
        d.outcome = SKIPPED_OVERLAP
        d.detail = "previous Occurrence " + ", ".join(
            f"{r['id']} is still {r['state']}" for r in unfinished)
        for r in unfinished:
            _log(cfg, r["folder"], f"Schedule {s.name} fired again "
                 f"({'run-now' if trigger == TRIGGER_RUN_NOW else iso(fire_at)}) while this "
                 f"Occurrence is {r['state']}; the new fire was skipped (Overlap rule: skip)")
        return _record(db, d, now)
    for r in unfinished:  # OVERLAP_REPLACE
        try:
            res = failures.cancel(
                cfg, db, r["id"], at=now, daemon_up=daemon_up,
                why=f"replaced by a new Occurrence of Schedule {s.name} (Overlap rule: replace)")
        except failures.NotAllowed:
            continue  # finished meanwhile
        d.replaced.append(r["id"])
        if res.status == "requested":
            d.detail = (d.detail + "; " if d.detail else "") + f"cancel of running {r['id']} requested"
    try:
        d.task_id, d.folder = _occurrence(cfg, db, s, fire_at, trigger=trigger, fires=fires,
                                          now=now)
    except (templates.TemplateError, ConfigError, OSError, ValueError) as exc:
        d.outcome, d.detail = ERROR, f"{type(exc).__name__}: {exc}"
        return _record(db, d, now)
    if d.replaced:
        d.outcome = REPLACED
        d.detail = f"replaced {', '.join(d.replaced)}" + (f"; {d.detail}" if d.detail else "")
    return _record(db, d, now)


def apply_catch_up(cfg: Config, db: Database, now: datetime, *, trigger: str = TRIGGER_POLL,
                   daemon_up: bool = False) -> list[Decision]:
    """Compare ``now`` with every enabled Schedule's last handled fire and
    apply its Catch-up rule, then its Overlap rule. Several missed fires make
    one decision. Called by the daemon (the only process that runs this) on
    every start, wake and poll.

    ``daemon_up``: whether a *separate* daemon may be running Tasks (only
    matters for replacing a running Occurrence); inside the daemon nothing
    runs while this is called.
    """
    decisions: list[Decision] = []
    for row in db.schedules(enabled_only=True):
        s = Schedule.from_row(row)
        count, latest = due(cfg, s, now)
        if not count or latest is None:
            db.touch_schedule(s.name, iso(now))
            continue
        # Claim the fires before acting: a crash after this loses this
        # decision at worst; it can never create a second Occurrence for it.
        if not db.claim_schedule_fires(s.name, expect_last=s.last_fire_at, through=iso(latest),
                                       checked_at=iso(now)):
            continue
        if not s.catch_up.allows(latest, now):
            late = (now - latest).total_seconds() / 3600
            d = Decision(schedule=s.name, trigger=trigger, fire_at=iso(latest), fires=count,
                         outcome=SKIPPED_CATCH_UP,
                         detail=f"missed by {late:.1f}h; Catch-up rule is {s.catch_up}")
            decisions.append(_record(db, d, now))
            continue
        decisions.append(fire(cfg, db, s, latest, now=now, trigger=trigger, fires=count,
                              daemon_up=daemon_up))
    return decisions


def run_now(cfg: Config, db: Database, name: str, *, now: datetime,
            daemon_up: bool) -> Decision:
    """`orch schedule run-now`: one fire now, through the Overlap rule. It
    doesn't move the Schedule's cron state (the next fire stays due)."""
    s = get(db, name)
    return fire(cfg, db, s, now, now=now, trigger=TRIGGER_RUN_NOW, daemon_up=daemon_up)


def last_run(db: Database, s: Schedule) -> Any | None:
    """The latest decision of this Schedule (not of an earlier one of the
    same name that was removed)."""
    since = parse_iso(s.created_at)
    rows = [r for r in db.schedule_runs(s.name) if parse_iso(r["decided_at"]) >= since]
    return rows[-1] if rows else None

