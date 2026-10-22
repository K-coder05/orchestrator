"""Read-only queries behind `orch list|show|usage` and `orch schedule list`,
and the dashboard's own views (inbox, status groups, spend tracker).

Each returns plain data; ``views`` renders it as text for the CLI, and the
dashboard can render the same data its own way.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import priority, schedules, spend, usage
from .adapters import is_paid
from .config import Config
from .db import Database
from .states import TERMINAL, TaskState


# --------------------------------------------------------------------------
# Task list
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSummary:
    """One Task as `orch list` shows it."""

    id: str
    state: TaskState
    priority: str
    #: The Effective priority now; None for a finished Task.
    effective_priority: str | None
    task_type: str
    agent: str
    created_at: str
    title: str


def _effective(cfg: Config, row: sqlite3.Row, now: datetime) -> priority.Evaluation | None:
    if TaskState(row["state"]) in TERMINAL:
        return None
    return priority.evaluate(cfg, row, now)


def task_list(
    cfg: Config, db: Database, now: datetime, states: list[TaskState] | None = None
) -> list[TaskSummary]:
    """Tasks, newest first, optionally only those in ``states``."""
    out = []
    for r in db.list_tasks(states):
        ev = _effective(cfg, r, now)
        out.append(TaskSummary(
            id=r["id"], state=TaskState(r["state"]), priority=r["priority"],
            effective_priority=ev.effective if ev else None,
            task_type=r["task_type"], agent=r["agent"], created_at=r["created_at"],
            title=r["title"],
        ))
    return out


#: The dashboard's status groups (§4 item 8), in display order. Every state
#: is in exactly one; each Task still shows its own state.
STATUS_GROUPS: list[tuple[str, frozenset[TaskState]]] = [
    ("running", frozenset({TaskState.RUNNING})),
    ("queued", frozenset({TaskState.QUEUED, TaskState.RETRY_WAIT, TaskState.SCHEDULED})),
    ("paused", frozenset({TaskState.PAUSED_USAGE, TaskState.PAUSED_SPEND})),
    ("needs you", frozenset({TaskState.NEEDS_YOU, TaskState.AWAITING_APPROVAL})),
    ("done", frozenset({TaskState.DONE, TaskState.PARTIAL, TaskState.CANCELLED})),
    ("failed", frozenset({TaskState.FAILED})),
]

#: How many of the newest Tasks a finished group shows by default.
FINISHED_LIMIT = 20


@dataclass(frozen=True)
class TaskGroup:
    name: str
    #: Newest first; for a finished group (done, failed) only the newest few.
    tasks: list[TaskSummary]
    #: How many Tasks are in the group, shown or not.
    total: int


def task_groups(
    cfg: Config, db: Database, now: datetime, finished_limit: int = FINISHED_LIMIT
) -> list[TaskGroup]:
    """Tasks grouped by status, as the dashboard shows them."""
    tasks = task_list(cfg, db, now)
    out = []
    for name, states in STATUS_GROUPS:
        members = [t for t in tasks if t.state in states]
        finished = states <= TERMINAL
        out.append(TaskGroup(name=name, tasks=members[:finished_limit] if finished else members,
                             total=len(members)))
    return out


# --------------------------------------------------------------------------
# "Needs you" inbox
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class NeedsYouRequest:
    """One open "needs you" request: something waiting on you before its
    Task can go on. View only until Phase 3's approve/deny buttons."""

    task_id: str
    title: str
    task_type: str
    state: TaskState
    #: What is asked: the agent's own ``kind`` from ``needs_you.json``, or
    #: Nightshift's ("plan approval", "re-approval", "spend cap", ...).
    kind: str
    summary: str
    details: str | None
    proposed_action: str | None
    #: When the Task started waiting (its last state change).
    since: str


def _needs_you_file(folder: Path) -> dict[str, Any] | None:
    """The agent's ``needs_you.json``; a dict with only a summary when it is
    not a JSON object; None when there is none."""
    path = folder / "needs_you.json"
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        d = None
    if not isinstance(d, dict):
        return {"summary": "The agent wrote needs_you.json, but it is not a JSON object; "
                           "check the Task folder."}
    return d


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return value if isinstance(value, str) else json.dumps(value)


def _last_reason(db: Database, row: sqlite3.Row) -> tuple[str | None, str]:
    """The from-state and reason of the transition into the Task's current state."""
    ts = db.transitions(row["id"])
    if not ts:
        return None, ""
    return ts[-1]["from_state"], ts[-1]["reason"]


def _request(cfg: Config, db: Database, row: sqlite3.Row) -> NeedsYouRequest:
    state = TaskState(row["state"])
    from_state, reason = _last_reason(db, row)
    kind, summary, details, action = "needs you", reason, None, None
    if state == TaskState.NEEDS_YOU:
        asked = _needs_you_file(Path(row["folder"]))
        if any(a["outcome"] == spend.OUTCOME_CAP for a in db.attempts(row["id"])) and not asked:
            kind, summary = "spend cap", spend.cap_reason(cfg, db, row["id"])
        elif asked is not None:
            kind = _text(asked.get("kind")) or "request"
            summary = _text(asked.get("summary")) or reason
            details = _text(asked.get("details"))
            action = _text(asked.get("proposed_action"))
    elif state == TaskState.AWAITING_APPROVAL:
        if from_state == TaskState.PAUSED_USAGE:
            kind = "re-approval"
        else:
            kind = "plan approval"
            mins = row["estimated_minutes"]
            summary = (f"Approve the plan (estimated {mins} min) before it runs" if mins
                       else "Approve the plan before it runs")
        action = f"orch approve {row['id']} (or orch deny {row['id']})"
    elif state == TaskState.PAUSED_SPEND:
        kind = "spend cap"
        action = "orch raise-cap USD, or wait for the month to end"
    return NeedsYouRequest(
        task_id=row["id"], title=row["title"], task_type=row["task_type"], state=state,
        kind=kind, summary=summary or "(no summary)", details=details,
        proposed_action=action, since=row["state_changed_at"],
    )


def needs_you_inbox(cfg: Config, db: Database) -> list[NeedsYouRequest]:
    """Every open "needs you" request, longest waiting first: Tasks in
    ``needs_you`` or ``awaiting_approval``, and Tasks paused on the monthly
    spend cap (which only you can raise)."""
    rows = [*db.tasks_in_state(TaskState.NEEDS_YOU),
            *db.tasks_in_state(TaskState.AWAITING_APPROVAL),
            *(r for r in db.tasks_in_state(TaskState.PAUSED_SPEND)
              if r["pause_kind"] == spend.KIND_MONTH)]
    rows.sort(key=lambda r: (r["state_changed_at"], r["created_at"]))
    return [_request(cfg, db, r) for r in rows]


# --------------------------------------------------------------------------
# Task detail
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptView:
    attempt_no: int
    agent: str
    started_at: str
    ended_at: str | None
    exit_code: int | None
    outcome: str | None
    end_reason: str | None

    @property
    def running(self) -> bool:
        return self.ended_at is None


@dataclass(frozen=True)
class ApprovalView:
    at: str
    decision: str
    via: str


@dataclass(frozen=True)
class FailoverView:
    at: str
    from_agent: str
    to_agent: str
    trigger: str
    deadline_driven: bool


@dataclass(frozen=True)
class TransitionView:
    at: str
    from_state: str | None  # None when the Task was created
    to_state: str
    reason: str


@dataclass(frozen=True)
class TaskDetail:
    """One Task as `orch show` shows it."""

    #: Every stored column of the Task (settings, pause and cancel requests, prompt, folder).
    task: dict[str, Any]
    #: The Effective priority now; None for a finished Task.
    effective_priority: str | None
    #: Why a queued Task may not start now; None when it may or isn't queued.
    blocked: str | None
    max_retries: int
    resume_session_max_hours: float
    attempts: list[AttemptView]
    approvals: list[ApprovalView]
    failovers: list[FailoverView]
    transitions: list[TransitionView]

    @property
    def id(self) -> str:
        return self.task["id"]

    @property
    def state(self) -> TaskState:
        return TaskState(self.task["state"])


def task_detail(cfg: Config, db: Database, task_id: str, now: datetime) -> TaskDetail:
    """Raises KeyError for an unknown Task."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    ev = _effective(cfg, row, now)
    return TaskDetail(
        task=dict(row),
        effective_priority=ev.effective if ev else None,
        blocked=ev.blocked if ev and row["state"] == str(TaskState.QUEUED) else None,
        max_retries=cfg.max_retries,
        resume_session_max_hours=cfg.usage("resume_session_max_hours"),
        attempts=[
            AttemptView(attempt_no=a["attempt_no"], agent=a["agent"], started_at=a["started_at"],
                        ended_at=a["ended_at"], exit_code=a["exit_code"], outcome=a["outcome"],
                        end_reason=a["end_reason"])
            for a in db.attempts(task_id)
        ],
        approvals=[ApprovalView(at=a["at"], decision=a["decision"], via=a["via"])
                   for a in db.approvals(task_id)],
        failovers=[
            FailoverView(at=f["at"], from_agent=f["from_agent"], to_agent=f["to_agent"],
                         trigger=f["trigger"], deadline_driven=bool(f["deadline_driven"]))
            for f in db.failovers(task_id)
        ],
        transitions=[
            TransitionView(at=t["at"], from_state=t["from_state"], to_state=t["to_state"],
                           reason=t["reason"])
            for t in db.transitions(task_id)
        ],
    )


# --------------------------------------------------------------------------
# Usage
# --------------------------------------------------------------------------

RECENT_OBSERVATIONS = 5


@dataclass(frozen=True)
class WindowView:
    """One usage Window's current period and what Nightshift recorded in it."""

    window: str
    start: datetime
    #: The next reset; None when not known (``start`` is then a trailing ``hours``).
    resets_at: datetime | None
    hours: float
    used: usage.WindowUsage
    #: The plan-wide share used, as the agent last reported it (0..1+).
    utilization: float | None
    #: The agent rejected requests in this period and its reset is still ahead.
    out_of_usage: bool
    baseline: usage.Baseline | None
    observation_count: int
    #: Cost-equivalent used in the current period, as a percent of the
    #: Baseline; None with no Baseline (thresholds don't apply yet).
    percent_of_baseline: float | None
    #: Time left until ``resets_at``; None when the reset is not known.
    resets_in: timedelta | None


@dataclass(frozen=True)
class LimitObservationView:
    window: str
    hit_at: str
    window_start: str
    resets_at: str | None
    cost_usd: float | None
    events_without_cost: int
    source: str
    task_id: str | None


@dataclass(frozen=True)
class ProbeView:
    at: str
    outcome: str
    detail: str | None


@dataclass(frozen=True)
class PausedOnLimit:
    """A Task paused because its agent hit a usage limit."""

    task_id: str
    resume_at: str | None
    #: True when ``resume_at`` is the Window's reset; else it is the next probe.
    reset_known: bool


@dataclass(frozen=True)
class AgentUsage:
    agent: str
    windows: list[WindowView]
    #: The latest Limit observations, oldest first.
    recent_observations: list[LimitObservationView]
    last_probe: ProbeView | None
    paused: list[PausedOnLimit]
    #: Why new Tasks (or background ones, in the daytime reserve) can't start
    #: on it now under the proactive thresholds (§1.5).
    held: str | None = None
    held_background: str | None = None


def usage_agents(cfg: Config, db: Database) -> list[str]:
    """Configured agents first, then any other agent with recorded usage.
    Paid paths have no usage Windows: their spend is shown as credits."""
    names = [a for a in (cfg.data.get("agents") or {}) if not is_paid(a)]
    return names + sorted(a for a in db.usage_agents() if a not in names and not is_paid(a))


def _window(cfg: Config, db: Database, agent: str, w: str, status: sqlite3.Row | None,
            observations: int, now: datetime) -> WindowView:
    start, reset = usage.current_window(cfg, db, agent, w, now)
    load = usage.window_load(cfg, db, agent, w, now)
    return WindowView(
        window=w, start=start, resets_at=reset, hours=cfg.window_hours(agent, w),
        used=usage.usage_between(db, agent, start, now),
        utilization=status["utilization"] if status is not None else None,
        out_of_usage=(status is not None and status["status"] == "rejected"
                      and reset is not None),
        baseline=usage.baseline(db, agent, w),
        observation_count=observations,
        percent_of_baseline=load.percent if load is not None else None,
        resets_in=reset - now if reset is not None else None,
    )


def usage_report(cfg: Config, db: Database, now: datetime) -> list[AgentUsage]:
    """Usage per agent and Window, with Baselines, recent Limit observations,
    the last probe and Tasks paused on a limit."""
    paused_all = db.tasks_in_state(TaskState.PAUSED_USAGE)
    holds = usage.threshold_holds(cfg, db, now)
    out = []
    for agent in usage_agents(cfg, db):
        statuses = db.window_status(agent)
        observations = db.limit_observations(agent)
        windows = usage.windows_of(cfg, db, agent)
        probes = db.probes(agent)
        out.append(AgentUsage(
            agent=agent,
            windows=[_window(cfg, db, agent, w, statuses.get(w),
                             sum(o["window"] == w for o in observations), now)
                     for w in windows],
            recent_observations=[
                LimitObservationView(
                    window=o["window"], hit_at=o["hit_at"], window_start=o["window_start"],
                    resets_at=o["resets_at"], cost_usd=o["cost_usd"],
                    events_without_cost=o["events_without_cost"], source=o["source"],
                    task_id=o["task_id"])
                for o in observations[-RECENT_OBSERVATIONS:]
            ],
            last_probe=(ProbeView(at=probes[-1]["at"], outcome=probes[-1]["outcome"],
                                  detail=probes[-1]["detail"]) if probes else None),
            paused=[
                PausedOnLimit(task_id=r["id"], resume_at=r["resume_at"],
                              reset_known=bool(r["pause_reset_at"]))
                for r in paused_all
                if r["agent"] == agent and r["pause_kind"] == "usage_limit"
            ],
            held=holds.all.get(agent),
            held_background=holds.background.get(agent),
        ))
    return out


# --------------------------------------------------------------------------
# Spend
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskSpend:
    """A Task's paid spend against the per-Task cap."""

    task_id: str
    title: str
    state: str
    spent: float
    #: ``spent`` as a percent of the per-Task cap.
    percent: float
    at_cap: bool
    last_spent_at: str


@dataclass(frozen=True)
class SpendTracker:
    """Paid spend against the caps (§1.5): today and this month (in
    ``credits``, with the cap holding paid calls now, if any) and per Task."""

    credits: spend.CreditView
    per_task_cap: float
    #: Tasks that spent this month or are not finished, latest spend first.
    tasks: list[TaskSpend]


def _percent(spent: float, cap: float) -> float:
    return spent / cap * 100 if cap > 0 else 100.0


def spend_tracker(cfg: Config, db: Database, now: datetime) -> SpendTracker:
    cap = cfg.per_task_spend_cap
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    totals: dict[str, float] = {}
    last: dict[str, str] = {}
    for r in db.spend_events():
        tid = r["task_id"]
        totals[tid] = totals.get(tid, 0.0) + r["cost_usd"]
        if tid not in last or spend.local_time(r["at"], now) > spend.local_time(last[tid], now):
            last[tid] = r["at"]
    tasks = []
    for tid, spent in totals.items():
        row = db.get_task(tid)
        if row is None:
            continue
        recent = spend.local_time(last[tid], now) >= month
        if not recent and TaskState(row["state"]) in TERMINAL:
            continue
        tasks.append(TaskSpend(task_id=tid, title=row["title"], state=row["state"],
                               spent=spent, percent=_percent(spent, cap),
                               at_cap=spent >= cap, last_spent_at=last[tid]))
    tasks.sort(key=lambda t: spend.local_time(t.last_spent_at, now), reverse=True)
    return SpendTracker(credits=spend.credits(cfg, db, now), per_task_cap=cap, tasks=tasks)


# --------------------------------------------------------------------------
# Schedules
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LastDecision:
    """A Schedule's latest fire decision."""

    decided_at: str
    decision: schedules.Decision
    #: The state of the Occurrence it created, if any and still on record.
    task_state: TaskState | None


@dataclass(frozen=True)
class OpenOccurrence:
    task_id: str
    state: TaskState


@dataclass(frozen=True)
class ScheduleView:
    """One Schedule as `orch schedule list` shows it."""

    schedule: schedules.Schedule
    #: None when the Schedule is disabled.
    next_fire: datetime | None
    #: None when it never fired.
    last: LastDecision | None
    #: Its unfinished Occurrences, oldest first.
    open_occurrences: list[OpenOccurrence]


def _last_decision(db: Database, s: schedules.Schedule) -> LastDecision | None:
    last = schedules.last_run(db, s)
    if last is None:
        return None
    row = db.get_task(last["task_id"]) if last["task_id"] else None
    return LastDecision(
        decided_at=last["decided_at"],
        decision=schedules.Decision(
            schedule=s.name, trigger=last["trigger"], fire_at=last["fire_at"],
            fires=last["fires"], outcome=last["outcome"], task_id=last["task_id"],
        ),
        task_state=TaskState(row["state"]) if row is not None else None,
    )


def schedule_list(cfg: Config, db: Database, now: datetime) -> list[ScheduleView]:
    """Each Schedule with its next fire, last decision and open Occurrences."""
    return [
        ScheduleView(
            schedule=s,
            next_fire=schedules.next_fire(cfg, s, now) if s.enabled else None,
            last=_last_decision(db, s),
            open_occurrences=[OpenOccurrence(task_id=r["id"], state=TaskState(r["state"]))
                              for r in schedules.unfinished_occurrences(db, s.name)],
        )
        for s in schedules.all_schedules(db)
    ]
