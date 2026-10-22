"""Read-only queries behind `orch list|show|usage` and `orch schedule list`.

Each returns plain data; ``views`` renders it as text for the CLI, and the
dashboard can render the same data its own way.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from . import priority, schedules, usage
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


def usage_agents(cfg: Config, db: Database) -> list[str]:
    """Configured agents first, then any other agent with recorded usage."""
    names = list((cfg.data.get("agents") or {}).keys())
    return names + sorted(a for a in db.usage_agents() if a not in names)


def _window(cfg: Config, db: Database, agent: str, w: str, status: sqlite3.Row | None,
            observations: int, now: datetime) -> WindowView:
    start, reset = usage.current_window(cfg, db, agent, w, now)
    return WindowView(
        window=w, start=start, resets_at=reset, hours=cfg.window_hours(agent, w),
        used=usage.usage_between(db, agent, start, now),
        utilization=status["utilization"] if status is not None else None,
        out_of_usage=(status is not None and status["status"] == "rejected"
                      and reset is not None),
        baseline=usage.baseline(db, agent, w),
        observation_count=observations,
    )


def usage_report(cfg: Config, db: Database, now: datetime) -> list[AgentUsage]:
    """Usage per agent and Window, with Baselines, recent Limit observations,
    the last probe and Tasks paused on a limit."""
    paused_all = db.tasks_in_state(TaskState.PAUSED_USAGE)
    out = []
    for agent in usage_agents(cfg, db):
        statuses = db.window_status(agent)
        observations = db.limit_observations(agent)
        windows = list(cfg.windows(agent))
        windows += sorted((set(statuses) | {o["window"] for o in observations}) - set(windows))
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
        ))
    return out


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
