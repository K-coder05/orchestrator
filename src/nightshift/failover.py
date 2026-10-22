"""Failover: moving a Task to another agent (§2.7, §2.8).

Phase 1 fails over coding Tasks between Claude and Codex
(``task_types.coding.failover: {agents: [claude, codex], when: always}``);
the mechanism is per Task type and names no agent itself.

- **Out of usage.** When an Attempt ends on a usage limit (real, or
  simulated by ``--simulate-rate-limit-after``), the Task moves to the first
  other agent of its type's list that isn't known to be out
  (``usage.agent_out_until``) and goes straight back in the queue. That
  Attempt can't resume the other agent's session: it starts a fresh session
  from ``plan.md`` + ``progress.md``.
- **All out.** The Task pauses as usual (``paused_usage``), but comes back
  at the earliest reset of any agent on its list, and resumes on whichever
  agent has reset (:func:`agent_on_resume`, called by
  ``pauses.promote_due``).
- **Second retry.** The second retry of a Failed Attempt may switch to
  another agent on the list (:func:`retry_agent`); the first never does.
- **Deadline-driven.** A type with ``when: deadline`` fails over only while
  the Task's deadline is within ``queue.deadline_urgent_hours``; every such
  failover is flagged in the report's Needs you list.

A Task created with ``--agent`` (``agent_override``) never moves. Every
switch is a row in ``failovers`` and a line in ``logs/nightshift.log``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import failures, pauses, usage
from .adapters import REGISTRY
from .adapters.base import RateLimit
from .config import Config
from .db import Database
from .priority import deadline_urgent_hours
from .states import TaskState
from .timeutil import iso, parse_iso
from .workspace import read_task_yaml, write_task_yaml

TRIGGER_LIMIT = "usage_limit"
TRIGGER_SIMULATED = "simulated"
TRIGGER_RESET = "reset"
TRIGGER_RETRY = "retry"

#: The prompt note of an Attempt that starts on another agent.
FAILOVER_NOTE = """

---
Nightshift: this Task was started by another agent ({from_agent}), which ran out \
of usage or failed; you ({to_agent}) are taking over in a new session. Earlier work \
is already in the Task folder. Read `plan.md` and `progress.md` first, continue \
from there, and don't redo finished work. Keep `progress.md` current as before."""


def failover_prompt(prompt: str, from_agent: str, to_agent: str) -> str:
    return prompt + FAILOVER_NOTE.format(from_agent=from_agent, to_agent=to_agent)


@dataclass(frozen=True)
class Rule:
    agents: list[str]  # the Task type's agents, in order of preference
    deadline_driven: bool  # allowed only because the deadline is near


def rule(cfg: Config, row: Mapping[str, Any], now: datetime) -> Rule | None:
    """Whether (and between which agents) this Task may fail over now."""
    if row["agent_override"]:
        return None
    agents, when = cfg.failover(row["task_type"])
    agents = [a for a in agents if a in REGISTRY]
    if row["agent"] not in agents or len(agents) < 2:
        return None
    if when == "deadline":
        if not row["deadline"]:
            return None
        if parse_iso(row["deadline"]) - now > timedelta(hours=deadline_urgent_hours(cfg)):
            return None
        return Rule(agents, True)
    return Rule(agents, False)


def available(cfg: Config, db: Database, agents: list[str], now: datetime,
              exclude: str) -> list[str]:
    """``agents`` (in order) other than ``exclude`` not known to be out."""
    return [a for a in agents if a != exclude and usage.agent_out_until(cfg, db, a, now) is None]


def switch(cfg: Config, db: Database, row: Mapping[str, Any], to_agent: str, *,
           to_state: TaskState, trigger: str, deadline_driven: bool, reason: str,
           at: datetime, run_id: int | None, set_fields: dict[str, Any] | None = None,
           expect: TaskState | None = None) -> None:
    fields = {"agent": to_agent} | (set_fields or {})
    db.transition(row["id"], to_state, reason, run_id=run_id, expect=expect, set_fields=fields)
    db.record_failover(row["id"], run_id=run_id, at=iso(at), from_agent=row["agent"],
                       to_agent=to_agent, trigger=trigger, deadline_driven=deadline_driven,
                       reason=reason)
    _sync_yaml_agent(Path(row["folder"]), to_agent)
    failures.log_line(Path(row["folder"]), f"{iso(at)} {reason}", cfg.secrets.redact)


def _why(row: Mapping[str, Any], to_agent: str, r: Rule) -> str:
    what = f"failing over from {row['agent']} to {to_agent} ({row['task_type']} Tasks fail over"
    if r.deadline_driven:
        what += f"; deadline-driven: the deadline {row['deadline']} is near"
    return what + ")"


def after_usage_limit(
    cfg: Config, db: Database, task_id: str, *, kind: str, limit: RateLimit | None,
    reason: str, at: datetime, run_id: int | None = None,
) -> TaskState:
    """End a running Task's Attempt that hit a usage limit: fail over to the
    next agent that isn't out, else pause (coming back at the earliest reset
    of any of its agents). A pause by hand never fails over."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    r = rule(cfg, row, at) if kind in (pauses.KIND_LIMIT, pauses.KIND_SIMULATED) else None
    if r is not None:
        free = available(cfg, db, r.agents, at, exclude=row["agent"])
        if free:
            to = free[0]
            fields: dict[str, Any] = {"resume_pending": 1, "pause_requested_at": None}
            if kind == pauses.KIND_SIMULATED:
                fields["simulated_limit_at"] = iso(at)  # fires once per Task
            switch(cfg, db, row, to, to_state=TaskState.QUEUED,
                   trigger=TRIGGER_SIMULATED if kind == pauses.KIND_SIMULATED else TRIGGER_LIMIT,
                   deadline_driven=r.deadline_driven, at=at, run_id=run_id, set_fields=fields,
                   reason=f"{reason}; {_why(row, to, r)}: fresh session on {to} from "
                           "plan.md + progress.md")
            return TaskState.QUEUED
    pauses.pause_after_attempt(cfg, db, task_id, kind=kind, limit=limit, reason=reason, at=at,
                               run_id=run_id)
    if r is not None:
        _resume_at_first_reset(cfg, db, db.get_task(task_id), r, at)
    return TaskState.PAUSED_USAGE


def _resume_at_first_reset(cfg: Config, db: Database, row: Mapping[str, Any], r: Rule,
                           at: datetime) -> None:
    """Every agent of the Task is out: come back at whichever reset is first."""
    others = {a: usage.agent_out_until(cfg, db, a, at) for a in r.agents if a != row["agent"]}
    known = {a: t for a, t in others.items() if t is not None}
    if not known or not row["resume_at"]:
        return
    agent, first = min(known.items(), key=lambda kv: kv[1])
    if first >= parse_iso(row["resume_at"]):
        return
    with db.tx() as c:
        c.execute("UPDATE tasks SET resume_at = ? WHERE id = ? AND state = ?",
                  (iso(first), row["id"], str(TaskState.PAUSED_USAGE)))
    failures.log_line(Path(row["folder"]),
                      f"{iso(at)} every agent of this Task is out of usage; it resumes on "
                      f"whichever resets first ({agent} at {iso(first)})", cfg.secrets.redact)


def agent_on_resume(cfg: Config, db: Database, row: Mapping[str, Any], now: datetime, *,
                    still_out: bool = False) -> tuple[str, Rule] | None:
    """For a paused Task whose resume time has come: another agent to resume
    on, when the Task's own agent is still out (``still_out``: a probe just
    said so) but that one has reset."""
    if row["pause_kind"] not in (pauses.KIND_LIMIT, pauses.KIND_SIMULATED):
        return None
    out = still_out or usage.agent_out_until(cfg, db, row["agent"], now) is not None or bool(
        row["pause_reset_at"] and parse_iso(row["pause_reset_at"]) > now)
    if not out:
        return None
    r = rule(cfg, row, now)
    if r is None:
        return None
    free = available(cfg, db, r.agents, now, exclude=row["agent"])
    return (free[0], r) if free else None


def resume_on(cfg: Config, db: Database, row: Mapping[str, Any], to_agent: str, r: Rule,
              now: datetime, set_fields: dict[str, Any]) -> None:
    """Requeue a paused Task on ``to_agent``, which reset first."""
    switch(cfg, db, row, to_agent, to_state=TaskState.QUEUED, trigger=TRIGGER_RESET,
           deadline_driven=r.deadline_driven, at=now, run_id=None, set_fields=set_fields,
           expect=TaskState.PAUSED_USAGE,
           reason=f"usage pause over: {to_agent} has reset while {row['agent']} is still out; "
                   f"{_why(row, to_agent, r)}: fresh session on {to_agent} from plan.md + "
                   "progress.md")


def reassign_held(cfg: Config, db: Database, held: Mapping[str, str],
                  now: datetime) -> list[tuple[str, str, str]]:
    """Queued Tasks on an agent that is out of usage move to the next agent of
    their type that isn't, instead of waiting (§2.8). Returns (task, from, to)."""
    moved = []
    for row in db.tasks_in_state(TaskState.QUEUED):
        if row["agent"] not in held:
            continue
        r = rule(cfg, row, now)
        if r is None:
            continue
        free = [a for a in available(cfg, db, r.agents, now, exclude=row["agent"])
                if a not in held]
        if not free:
            continue
        to = free[0]
        reason = f"{held[row['agent']]}; {_why(row, to, r)} before the Task started"
        if move_queued(cfg, db, row, to, trigger=TRIGGER_LIMIT,
                       deadline_driven=r.deadline_driven, reason=reason, at=now):
            moved.append((row["id"], row["agent"], to))
    return moved


def move_queued(cfg: Config, db: Database, row: Mapping[str, Any], to_agent: str, *,
                trigger: str, deadline_driven: bool, reason: str, at: datetime) -> bool:
    """Move a queued Task to ``to_agent`` (it stays queued); False if it left
    ``queued`` meanwhile."""
    if not db.set_agent(row["id"], to_agent, expect=TaskState.QUEUED):
        return False
    db.record_failover(row["id"], run_id=None, at=iso(at), from_agent=row["agent"],
                       to_agent=to_agent, trigger=trigger, deadline_driven=deadline_driven,
                       reason=reason)
    _sync_yaml_agent(Path(row["folder"]), to_agent)
    failures.log_line(Path(row["folder"]), f"{iso(at)} {reason}", cfg.secrets.redact)
    return True


def _sync_yaml_agent(folder: Path, agent: str) -> None:
    if (folder / "task.yaml").exists():
        spec = read_task_yaml(folder)
        spec.agent = agent
        write_task_yaml(folder, spec)


def retry_agent(cfg: Config, db: Database, row: Mapping[str, Any], retry_no: int,
                now: datetime) -> tuple[str, Rule] | None:
    """The agent the ``retry_no``-th retry switches to, if any: only the
    second retry may switch (§2.7), to the next agent not out."""
    if retry_no != 2:
        return None
    r = rule(cfg, row, now)
    if r is None:
        return None
    free = available(cfg, db, r.agents, now, exclude=row["agent"])
    return (free[0], r) if free else None


def record_retry_switch(cfg: Config, db: Database, row: Mapping[str, Any], to_agent: str,
                        r: Rule, *, at: datetime, run_id: int | None, reason: str,
                        set_fields: dict[str, Any]) -> None:
    switch(cfg, db, row, to_agent, to_state=TaskState.RETRY_WAIT, trigger=TRIGGER_RETRY,
           deadline_driven=r.deadline_driven, at=at, run_id=run_id, set_fields=set_fields,
           reason=reason)


def needs_you_lines(db: Database, task_id: str) -> list[str]:
    """The report's Needs you entries for deadline-driven failovers (§2.8)."""
    out = []
    for f in db.failovers(task_id):
        if f["deadline_driven"]:
            out.append(f"- **Deadline-driven failover:** at {f['at']} this Task moved from "
                       f"{f['from_agent']} to {f['to_agent']} because its deadline was near. "
                       "Review the result more carefully.")
    return out


def summary_lines(db: Database, task_id: str) -> list[str]:
    """One line per failover, for ``orch show``."""
    return [f"{f['at']}  {f['from_agent']} -> {f['to_agent']} ({f['trigger']}"
            + (", deadline-driven" if f["deadline_driven"] else "") + ")"
            for f in db.failovers(task_id)]
