"""Pausing and resuming (§2.6): usage-limit hard pauses, ``orch pause`` /
``orch resume``, resume of the same session, and expiry.

When an agent hits a rate limit mid-Attempt (or a simulated one, from
``orch add --simulate-rate-limit-after N``), ``run_attempt`` lets the Step in
progress finish, stops the agent and calls :func:`pause_after_attempt`: the
Task moves ``running -> paused_usage`` with the reset time if the agent gave
one. That Attempt is not a Failed Attempt and never touches the retry budget.

The daemon calls :func:`promote_due` before every pick. In resume order
(Effective priority, nearest deadline, oldest) it cancels paused Tasks whose
deadline has passed, sends Tasks paused over 72 hours back for approval, and
requeues Tasks whose reset time has come. A requeued Task keeps
``resume_pending``, so its next Attempt resumes the same agent session when
that session is under 24 hours old (:func:`session_to_resume`), else starts a
fresh session from ``plan.md`` + ``progress.md``.

Every function that needs the time takes it from the caller (fake clocks in
tests).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import failures
from .adapters.base import ProbeReply, RateLimit
from .config import Config
from .db import Database, StateChanged
from .priority import resume_order
from .states import TaskState
from .timeutil import iso, parse_iso
from .workspace import read_task_yaml, write_task_yaml

#: Why a ``paused_usage`` Task is paused (``tasks.pause_kind``).
KIND_LIMIT = "usage_limit"  # the agent reported a rate-limit hit
KIND_SIMULATED = "simulated"  # --simulate-rate-limit-after
KIND_BY_HAND = "by_hand"  # orch pause; only orch resume ends it

#: The prompt of an Attempt that resumes the same agent session.
RESUME_PROMPT = """\
Nightshift: this session was paused (usage limit or a pause by hand) and is \
now resumed. Continue the Task where you left off: check `progress.md` first \
and don't redo finished work. Keep `progress.md` current as before."""

FRESH_NOTE = """

---
Nightshift: this Task was paused and is resuming in a new session (the earlier \
session could not be resumed). Earlier work is already in the Task folder. \
Read `plan.md` and `progress.md` first, continue from there, and don't redo \
finished work."""


def fresh_prompt(prompt: str) -> str:
    """The prompt of a fresh session that continues paused work."""
    return prompt + FRESH_NOTE


def _log(cfg: Config, row: Mapping[str, Any], at: datetime, text: str) -> None:
    failures.log_line(Path(row["folder"]), f"{iso(at)} {text}", cfg.secrets.redact)


# --------------------------------------------------------------------------
# Pausing
# --------------------------------------------------------------------------


def resume_time(cfg: Config, kind: str, limit: RateLimit | None, at: datetime) -> str | None:
    """When the daemon next looks at a Task paused at ``at``: the reported
    reset time (it is then requeued); with none, after
    ``usage.probe_interval_minutes``, when the daemon probes the agent
    (:func:`promote_due`). A pause by hand only ends by hand (None)."""
    if kind == KIND_BY_HAND:
        return None
    if limit is not None and limit.reset_at:
        return iso(parse_iso(limit.reset_at).astimezone(at.tzinfo))
    return iso(at + timedelta(minutes=cfg.usage("probe_interval_minutes")))


def simulated_limit(cfg: Config, at: datetime) -> RateLimit:
    """The limit ``--simulate-rate-limit-after`` raises: it reports a reset
    ``usage.simulated_reset_minutes`` from now, like a real five-hour hit."""
    reset = at + timedelta(minutes=cfg.usage("simulated_reset_minutes"))
    return RateLimit(limit_type="simulated", reset_at=iso(reset))


def pause_after_attempt(
    cfg: Config, db: Database, task_id: str, *, kind: str, limit: RateLimit | None,
    reason: str, at: datetime, run_id: int | None = None,
) -> str:
    """Move a running Task to ``paused_usage`` after its Attempt ended on a
    limit (or ``orch pause``). The retry budget is not touched. Returns the
    transition reason."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    resume_at = resume_time(cfg, kind, limit, at)
    if kind == KIND_BY_HAND:
        why = f"{reason}; paused until `orch resume {task_id}`"
    elif limit is not None and limit.reset_at:
        why = f"{reason}; resets at {resume_at}"
    else:
        why = f"{reason}; no reset time given, probing at {resume_at}"
    fields: dict[str, Any] = {
        "pause_kind": kind,
        "pause_limit_type": limit.limit_type if limit else None,
        "pause_reset_at": resume_at if limit is not None and limit.reset_at else None,
        "resume_at": resume_at,
        "resume_pending": 1,
        "pause_requested_at": None,
    }
    if kind == KIND_SIMULATED:
        fields["simulated_limit_at"] = iso(at)
    db.transition(task_id, TaskState.PAUSED_USAGE, why, run_id=run_id, set_fields=fields)
    _log(cfg, row, at, why)
    return why


def held_agents(cfg: Config, db: Database, now: datetime) -> dict[str, str]:
    """Agents whose Window is known to be out: a Task on them is paused on a
    real limit whose reset is still ahead. No new Task starts on them until
    then (a simulated limit holds back only its own Task)."""
    held: dict[str, str] = {}
    for row in db.tasks_in_state(TaskState.PAUSED_USAGE):
        if row["pause_kind"] != KIND_LIMIT or not row["resume_at"]:
            continue
        if parse_iso(row["resume_at"]) > now:
            until = min(row["resume_at"], held.get(row["agent"], "~"))
            held[row["agent"]] = f"{row['agent']} is out of usage until about {until}"
    # A Task that failed over left no paused Task behind; the agent's
    # Window status still says it is out (``usage.agent_out_until``).
    from . import usage  # usage is imported lazily to keep start-up light

    for agent in cfg.data.get("agents") or {}:
        if agent not in held:
            until = usage.agent_out_until(cfg, db, agent, now)
            if until is not None:
                held[agent] = f"{agent} is out of usage until about {iso(until)}"
    return held


# --------------------------------------------------------------------------
# Resuming
# --------------------------------------------------------------------------


@dataclass
class Promoted:
    resumed: list[str] = field(default_factory=list)
    reapproval: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    still_out: list[str] = field(default_factory=list)  # a probe found the limit still there
    probes: dict[str, ProbeReply] = field(default_factory=dict)  # agent -> what its probe found

#: Sends an agent a minimal probe request (``nightshift.usage.probe``).
Prober = Callable[[str], ProbeReply]


_CLEAR_PAUSE = {"pause_kind": None, "pause_limit_type": None, "pause_reset_at": None,
                "resume_at": None}


def promote_due(cfg: Config, db: Database, now: datetime,
                prober: Prober | None = None) -> Promoted:
    """Expire or requeue paused Tasks, in resume order (§2.6).

    A Task paused on a real limit with no known reset time is not requeued
    blindly at its probe time: ``prober`` sends the agent a minimal request
    first (once per agent per call). It went through: the Task is queued.
    Still limited: the Task stays paused until the reset the probe reported,
    else the next probe interval. The probe itself failed: the Task is
    queued and tries for itself (it pauses again if the limit is still
    there). Without a ``prober`` such Tasks are requeued at their probe time.
    """
    out = Promoted()
    max_paused = timedelta(hours=cfg.usage("reapproval_after_hours"))
    for row in resume_order(cfg, db.tasks_in_state(TaskState.PAUSED_USAGE), now):
        tid = row["id"]
        try:
            if row["deadline"] and parse_iso(row["deadline"]) <= now:
                reason = f"deadline {row['deadline']} passed while paused; cancelled"
                db.transition(tid, TaskState.CANCELLED, reason, expect=TaskState.PAUSED_USAGE,
                              set_fields=_CLEAR_PAUSE)
                failures.finalize(cfg, db, db.get_task(tid), TaskState.CANCELLED, reason, at=now)
                out.cancelled.append(tid)
            elif now - parse_iso(row["state_changed_at"]) > max_paused:
                hours = cfg.usage("reapproval_after_hours")
                reason = (f"paused for more than {hours:g} hours; needs re-approval "
                          f"(`orch approve {tid}`)")
                db.transition(tid, TaskState.AWAITING_APPROVAL, reason,
                              expect=TaskState.PAUSED_USAGE,
                              set_fields=_CLEAR_PAUSE | {"approved": 0, "approved_at": None})
                _sync_yaml_unapproved(Path(row["folder"]))
                _log(cfg, row, now, reason)
                out.reapproval.append(tid)
            elif not (row["resume_at"] and parse_iso(row["resume_at"]) <= now) and (
                    near := _deadline_failover(cfg, db, row, now)) is not None:
                # Waiting for a reset, but the deadline came near (§2.8).
                from . import failover  # failover imports this module

                failover.move_paused_for_deadline(cfg, db, row, near[0], near[1], now,
                                                  dict(_CLEAR_PAUSE))
                out.resumed.append(tid)
            elif row["resume_at"] and parse_iso(row["resume_at"]) <= now:
                from . import failover  # failover imports this module

                # Every agent was out: resume on whichever has reset (§2.8).
                other = failover.agent_on_resume(cfg, db, row, now)
                if other is not None:
                    failover.resume_on(cfg, db, row, other[0], other[1], now, dict(_CLEAR_PAUSE))
                    out.resumed.append(tid)
                    continue
                what = ("reset time" if row["pause_reset_at"] else "retry time")
                reason = f"usage pause over ({what} {row['resume_at']}); queued to resume"
                if (prober is not None and row["pause_kind"] == KIND_LIMIT
                        and not row["pause_reset_at"]):
                    agent = row["agent"]
                    if agent not in out.probes:
                        out.probes[agent] = prober(agent)
                    found = out.probes[agent]
                    if found.available:
                        reason = f"probe: {agent} answered, its usage Window has reset; queued to resume"
                    elif found.limit is not None:
                        other = failover.agent_on_resume(cfg, db, row, now, still_out=True)
                        if other is not None:
                            failover.resume_on(cfg, db, row, other[0], other[1], now,
                                               dict(_CLEAR_PAUSE))
                            out.resumed.append(tid)
                            continue
                        _still_out(cfg, db, row, found.limit, now)
                        out.still_out.append(tid)
                        continue
                    else:
                        reason = (f"probe of {agent} failed ({found.error}); queued to resume, "
                                  "the Task tries for itself")
                db.transition(tid, TaskState.QUEUED, reason, expect=TaskState.PAUSED_USAGE,
                              set_fields=_CLEAR_PAUSE)
                _log(cfg, row, now, reason)
                out.resumed.append(tid)
        except StateChanged:
            continue  # resumed, cancelled or paused by hand meanwhile
    return out


def _deadline_failover(cfg: Config, db: Database, row: Mapping[str, Any],
                       now: datetime) -> Any:
    from . import failover  # failover imports this module

    return failover.deadline_failover(cfg, db, row, now)


def _still_out(cfg: Config, db: Database, row: Mapping[str, Any], limit: RateLimit,
               now: datetime) -> None:
    """A probe found the agent still out of usage: keep the Task paused until
    the reset the probe reported, else the next probe interval."""
    resume_at = resume_time(cfg, KIND_LIMIT, limit, now)
    with db.tx() as c:
        c.execute(
            "UPDATE tasks SET resume_at = ?, pause_reset_at = ?, "
            "pause_limit_type = COALESCE(?, pause_limit_type) WHERE id = ? AND state = ?",
            (resume_at, resume_at if limit.reset_at else None, limit.limit_type, row["id"],
             str(TaskState.PAUSED_USAGE)),
        )
    if limit.reset_at:
        why = f"probe: {row['agent']} is still out of usage; resets at {resume_at}"
    else:
        why = f"probe: {row['agent']} is still out of usage, no reset time given; probing at {resume_at}"
    _log(cfg, row, now, why)


def _sync_yaml_unapproved(folder: Path) -> None:
    if (folder / "task.yaml").exists():
        spec = read_task_yaml(folder)
        spec.approved, spec.approved_at = False, None
        write_task_yaml(folder, spec)


@dataclass(frozen=True)
class SessionChoice:
    session_id: str | None  # None: start a fresh session
    why: str


def session_to_resume(cfg: Config, db: Database, row: Mapping[str, Any],
                      now: datetime) -> SessionChoice:
    """The agent session the next Attempt of a paused Task should resume: the
    last Attempt's session, if it ran on the Task's current agent and the
    session is younger than ``usage.resume_session_max_hours`` (its age counts
    from the first Attempt that used it)."""
    attempts = [a for a in db.attempts(row["id"]) if a["session_id"]]
    if not attempts:
        return SessionChoice(None, "no earlier session was recorded")
    last = attempts[-1]
    sid = last["session_id"]
    if last["agent"] != row["agent"]:
        return SessionChoice(None, f"the last session ran on {last['agent']}, not {row['agent']}")
    first = min(parse_iso(a["started_at"]) for a in attempts if a["session_id"] == sid)
    age = now - first
    max_age = timedelta(hours=cfg.usage("resume_session_max_hours"))
    hours = age.total_seconds() / 3600
    if age >= max_age:
        return SessionChoice(None, f"session {sid} is {hours:.1f}h old "
                                   f"(over {max_age.total_seconds() / 3600:g}h)")
    return SessionChoice(sid, f"session {sid} is {hours:.1f}h old")


# --------------------------------------------------------------------------
# By hand: orch pause / orch resume
# --------------------------------------------------------------------------


class NotAllowed(Exception):
    """The Task's state doesn't allow this command."""


PAUSABLE = frozenset({TaskState.QUEUED, TaskState.RETRY_WAIT, TaskState.RUNNING,
                      TaskState.PAUSED_USAGE})


@dataclass(frozen=True)
class PauseResult:
    #: "paused" (done now) or "requested" (the daemon pauses it after the Step).
    status: str
    from_state: TaskState


def pause_by_hand(cfg: Config, db: Database, task_id: str, *, at: datetime, daemon_up: bool,
                  via: str = "orch pause") -> PauseResult:
    """Pause a Task until ``orch resume``.

    A ``running`` Task under a live daemon gets a pause request: the daemon
    lets the current Step finish, stops the agent and pauses the Task, which
    later resumes its session. A running Task with no daemon alive (orphan)
    is paused here, its open Attempt closed. A Task paused on a usage limit
    stays paused until resumed by hand.
    """
    for _ in range(3):  # the daemon may move the Task while we look
        row = db.get_task(task_id)
        if row is None:
            raise KeyError(task_id)
        state = TaskState(row["state"])
        if state not in PAUSABLE:
            raise NotAllowed(f"Task {task_id} is {state}; only queued, retry_wait, running or "
                             "paused Tasks can be paused")
        if state == TaskState.PAUSED_USAGE and row["pause_kind"] == KIND_BY_HAND:
            raise NotAllowed(f"Task {task_id} is already paused by hand")
        if state == TaskState.RUNNING and daemon_up:
            db.request_pause(task_id)
            return PauseResult("requested", state)
        reason = f"paused by hand ({via}) while {state}"
        fields: dict[str, Any] = {
            "pause_kind": KIND_BY_HAND, "resume_at": None, "retry_at": None,
            "pause_requested_at": None,
        }
        if state == TaskState.RUNNING:
            fields["resume_pending"] = 1
        if state == TaskState.PAUSED_USAGE:
            # Nothing changes state; only the daemon's own resume is called off.
            with db.tx() as c:
                if c.execute("UPDATE tasks SET pause_kind = ?, resume_at = NULL "
                             "WHERE id = ? AND state = ?",
                             (KIND_BY_HAND, task_id, str(state))).rowcount != 1:
                    continue
            _log(cfg, row, at, f"{reason}; stays paused until `orch resume {task_id}`")
            return PauseResult("paused", state)
        try:
            db.transition(task_id, TaskState.PAUSED_USAGE, reason, expect=state,
                          set_fields=fields)
        except StateChanged:
            continue
        for a in db.attempts(task_id):
            if a["ended_at"] is None:
                db.finish_attempt(a["id"], exit_code=None, outcome="paused",
                                  end_reason=reason, session_id=None)
        _log(cfg, row, at, reason)
        return PauseResult("paused", state)
    raise NotAllowed(f"Task {task_id} kept changing state; try again")


def resume_by_hand(cfg: Config, db: Database, task_id: str, *, at: datetime,
                   via: str = "orch resume") -> str:
    """Queue a paused Task now (whatever its reset time), or call off a
    pending ``orch pause`` of a running one. Returns what happened."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    state = TaskState(row["state"])
    if state == TaskState.RUNNING and row["pause_requested_at"]:
        db.clear_pause_request(task_id)
        return "pause request withdrawn; the Task keeps running"
    if state == TaskState.AWAITING_APPROVAL and row["resume_pending"]:
        raise NotAllowed(f"Task {task_id} was paused too long and needs re-approval: "
                         f"`orch approve {task_id}`")
    if state == TaskState.PAUSED_SPEND:
        raise NotAllowed(f"Task {task_id} is paused on a spend cap; it resumes on its own once "
                         "the cap lifts (`orch raise-cap USD` raises this month's)")
    if state != TaskState.PAUSED_USAGE:
        raise NotAllowed(f"Task {task_id} is {state}, not paused")
    kind = row["pause_kind"] or "pause"
    reason = f"resumed by hand ({via}) from a {kind.replace('_', ' ')} pause"
    db.transition(task_id, TaskState.QUEUED, reason, expect=state, set_fields=_CLEAR_PAUSE)
    _log(cfg, row, at, reason)
    return reason
