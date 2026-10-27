"""Task control (§2.4, §2.6, §2.7, §5 items 1-2): the one layer behind every
surface that changes a Task by hand.

``orch approve|deny|cancel|pause|resume|retry|priority|add``, Telegram and
the dashboard all call these functions. Each looks the Task up, checks its
state, makes the change and returns a :class:`Result`: what happened, the
from/to state and a message for a person, or a refusal (``refusal`` set)
with its reason. Nothing here prints or asks a question.

Every control takes ``via``, the surface it came from: ``orch <command>``,
``telegram`` or ``dashboard``. It goes into the transition reason, the
approval record and the intervention record, so the clean-week check
(``nightshift.cleanweek``) sees every change by hand, wherever it was made.
Intake is the exception: ``orch add`` and ``telegram add`` (Telegram's
``add`` and the buttons it sends) create the Task and decide its plan as
part of asking for the work, which the clean-week check doesn't count.

Cancelling or pausing a ``running`` Task under a live daemon is a request
the daemon carries out within a Step or so: :func:`cancel` and :func:`pause`
return at once (``CANCEL_REQUESTED``/``PAUSE_REQUESTED``), and
:func:`wait_for_cancel`/:func:`wait_for_pause` wait for it within a bound
(``timeout=0`` only looks).

Intake is split so a caller without a terminal can drive it step by step:
:func:`propose` (the estimate and Task type; a planning call for a free-form
Task), then :func:`create` or :func:`decline`, then for a Task over the plan
threshold :func:`plan` (``plan.md``) and :func:`approve` or :func:`deny`.

Every function that needs the time takes it from the caller.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import daemon, failures, pauses, planning, templates
from . import priority as prio
from .adapters.base import Usage
from .config import Config, ConfigError
from .db import Database, StateChanged
from .runner import create_task
from .states import TERMINAL, TaskState
from .timeutil import iso
from .workspace import TaskSpec, read_task_yaml, write_task_yaml

#: Surfaces other than ``orch <command>``.
VIA_TELEGRAM = "telegram"
VIA_DASHBOARD = "dashboard"
#: Telegram's ``add`` flow, like ``orch add``: its plan decision is part of
#: asking for the work, not an intervention.
VIA_TELEGRAM_ADD = "telegram add"

# What happened (``Result.action``).
APPROVED = "approved"
DENIED = "denied"
CANCELLED = "cancelled"
CANCEL_REQUESTED = "cancel_requested"  # the daemon stops the running Attempt
PAUSED = "paused"
PAUSE_REQUESTED = "pause_requested"  # the daemon pauses it after the current Step
RESUMED = "resumed"
PAUSE_WITHDRAWN = "pause_withdrawn"  # a pending pause called off; it keeps running
RETRIED = "retried"
PRIORITY_SET = "priority_set"
ENDED = "ended"  # waited on: the Task ended otherwise before the cancel or pause
PROPOSED = "proposed"
DECLINED = "declined"
CREATED = "created"
PLANNED = "planned"
REFUSED = "refused"

# Why a control was refused (``Result.refusal``).
UNKNOWN_TASK = "unknown_task"
WRONG_STATE = "wrong_state"
STATE_CHANGED = "state_changed"  # the daemon moved the Task meanwhile
INVALID = "invalid"  # bad input or config: nothing was changed
NO_PLAN = "no_plan"  # the plan.md planning call failed; the Task waits unplanned

# How long a bounded wait for the daemon lasts by default.
CANCEL_WAIT_SECONDS = 30.0
PAUSE_WAIT_SECONDS = 30.0
POLL_SECONDS = 0.2


@dataclass(frozen=True)
class Draft:
    """A proposed Task, not created yet: what :func:`create` makes."""

    request: str  # what the person asked for
    prompt: str  # what the agent gets (a template's is rendered)
    proposal: planning.Proposal
    task_type: str
    type_given: bool  # the caller chose the Task type
    agent: str | None
    priority: str | None
    deadline: str | None  # ISO 8601
    run_at: str | None
    internet: str | None
    simulate_after: int | None
    needs_plan: bool  # over the plan threshold: plan.md and approval
    template: templates.Template | None = None
    instance: templates.Instance | None = None

    @property
    def estimated_minutes(self) -> int:
        return self.proposal.estimated_minutes

    def to_json(self) -> str:
        """A free-form draft as JSON, to :func:`create` it later (a caller
        that waits for an answer across requests). A template draft is
        made again from its template instead."""
        if self.template is not None:
            raise ValueError("only a free-form draft is stored")
        data = asdict(replace(self, template=None, instance=None))
        del data["template"], data["instance"]
        return json.dumps(data)

    @classmethod
    def from_json(cls, text: str) -> Draft:
        data = json.loads(text)
        p = data.pop("proposal")
        p["usage"] = [Usage(**u) for u in p.get("usage") or []]
        return cls(proposal=planning.Proposal(**p), **data)


@dataclass(frozen=True)
class Result:
    action: str
    message: str
    task_id: str | None = None
    from_state: TaskState | None = None
    to_state: TaskState | None = None
    #: Why it was refused (UNKNOWN_TASK, WRONG_STATE, ...); None: it happened.
    refusal: str | None = None
    #: The daemon isn't running, so nothing will act on this yet.
    note: str | None = None
    #: Lines to show before ``message`` (a new Task's timing, fault injection).
    details: tuple[str, ...] = ()
    draft: Draft | None = None
    plan: str | None = None
    folder: Path | None = None
    spec: TaskSpec | None = None

    @property
    def ok(self) -> bool:
        return self.refusal is None


def refused(refusal: str, message: str, task_id: str | None = None,
            from_state: TaskState | None = None) -> Result:
    return Result(REFUSED, message, task_id=task_id, from_state=from_state, refusal=refusal)


def daemon_note(cfg: Config, what: str = "nothing will pick this Task up yet") -> str | None:
    """The note for a change only the daemon acts on, when it isn't running."""
    if daemon.daemon_running(cfg):
        return None
    return (f"Note: the daemon isn't running, so {what}. "
            "Start it with `orch daemon install` (or `orch daemon run` in a terminal).")


def _lookup(db: Database, task_id: str) -> tuple[Any, Result | None]:
    row = db.get_task(task_id)
    if row is None:
        return None, refused(UNKNOWN_TASK, f"no Task {task_id!r}", task_id)
    return row, None


# --------------------------------------------------------------------------
# Approve / deny (§2.4)
# --------------------------------------------------------------------------


def approve(cfg: Config, db: Database, task_id: str, *, via: str) -> Result:
    """Approve a Task awaiting approval (its plan.md) and queue it."""
    return _decide(cfg, db, task_id, True, via)


def deny(cfg: Config, db: Database, task_id: str, *, via: str) -> Result:
    """Deny a Task awaiting approval; it is cancelled."""
    return _decide(cfg, db, task_id, False, via)


def _decide(cfg: Config, db: Database, task_id: str, approve_it: bool, via: str) -> Result:
    row, bad = _lookup(db, task_id)
    if bad:
        return bad
    state = TaskState(row["state"])
    if state != TaskState.AWAITING_APPROVAL:
        return refused(WRONG_STATE, f"Task {task_id} is {state}, not awaiting approval",
                       task_id, state)
    try:
        if approve_it:
            planning.approve(cfg, db, task_id, via=via)
        else:
            planning.deny(cfg, db, task_id, via=via)
    except (ValueError, StateChanged) as exc:  # decided or moved meanwhile
        return refused(STATE_CHANGED, str(exc), task_id, state)
    if approve_it:
        return Result(APPROVED, f"Task {task_id} approved and queued.", task_id, state,
                      TaskState.QUEUED, note=daemon_note(cfg))
    return Result(DENIED, f"Task {task_id} denied and cancelled.", task_id, state,
                  TaskState.CANCELLED)


# --------------------------------------------------------------------------
# Retry (§2.7)
# --------------------------------------------------------------------------


def retry(cfg: Config, db: Database, task_id: str, *, via: str, at: datetime) -> Result:
    """Retry a failed or partial Task (fresh retry budget), or start a Task
    in retry_wait now."""
    row, bad = _lookup(db, task_id)
    if bad:
        return bad
    state = TaskState(row["state"])
    try:
        reason = failures.retry_by_hand(cfg, db, task_id, at=at, via=via)
    except StateChanged as exc:
        return refused(STATE_CHANGED, str(exc), task_id, state)
    except failures.NotAllowed as exc:
        return refused(WRONG_STATE, str(exc), task_id, state)
    return Result(RETRIED, f"Task {task_id} queued: {reason}", task_id, state, TaskState.QUEUED,
                  note=daemon_note(cfg))


# --------------------------------------------------------------------------
# Cancel (§2.7)
# --------------------------------------------------------------------------


def cancel(cfg: Config, db: Database, task_id: str, *, via: str, at: datetime) -> Result:
    """Cancel a Task in any unfinished state. A running Task under a live
    daemon only gets a request (``CANCEL_REQUESTED``); see :func:`wait_for_cancel`."""
    row, bad = _lookup(db, task_id)
    if bad:
        return bad
    state = TaskState(row["state"])
    try:
        res = failures.cancel(cfg, db, task_id, at=at, daemon_up=daemon.daemon_running(cfg),
                              via=via)
    except StateChanged as exc:
        return refused(STATE_CHANGED, str(exc), task_id, state)
    except failures.NotAllowed as exc:
        return refused(WRONG_STATE, str(exc), task_id, state)
    except KeyError:
        return refused(UNKNOWN_TASK, f"no Task {task_id!r}", task_id)
    if res.status == "cancelled":
        return Result(CANCELLED, f"Task {task_id} cancelled (was {res.from_state}).", task_id,
                      res.from_state, TaskState.CANCELLED)
    return Result(CANCEL_REQUESTED, f"Task {task_id} is running; asked the daemon to stop it.",
                  task_id, res.from_state, TaskState.RUNNING)


def wait_for_cancel(cfg: Config, db: Database, task_id: str, *,
                    timeout: float | None = None) -> Result:
    """After a cancel request: wait up to ``timeout`` seconds (default
    ``CANCEL_WAIT_SECONDS``; 0 only looks) for the daemon to stop the Task."""
    state = _wait_while_running(db, task_id,
                                CANCEL_WAIT_SECONDS if timeout is None else timeout)
    if state is None:
        return refused(UNKNOWN_TASK, f"no Task {task_id!r}", task_id)
    if state == TaskState.CANCELLED:
        return Result(CANCELLED, f"Task {task_id} cancelled (was running).", task_id,
                      TaskState.RUNNING, state)
    if state == TaskState.RUNNING:
        return Result(CANCEL_REQUESTED,
                      f"Cancel requested for Task {task_id}; the daemon hasn't stopped it yet.",
                      task_id, state, state)
    return Result(ENDED, f"Task {task_id} ended {state} before it could be cancelled.", task_id,
                  TaskState.RUNNING, state)


# --------------------------------------------------------------------------
# Pause / resume (§2.6)
# --------------------------------------------------------------------------


def pause(cfg: Config, db: Database, task_id: str, *, via: str, at: datetime) -> Result:
    """Pause a Task until resumed by hand. A running Task under a live daemon
    only gets a request (``PAUSE_REQUESTED``); see :func:`wait_for_pause`."""
    row, bad = _lookup(db, task_id)
    if bad:
        return bad
    state = TaskState(row["state"])
    try:
        res = pauses.pause_by_hand(cfg, db, task_id, at=at,
                                   daemon_up=daemon.daemon_running(cfg), via=via)
    except StateChanged as exc:
        return refused(STATE_CHANGED, str(exc), task_id, state)
    except pauses.NotAllowed as exc:
        return refused(WRONG_STATE, str(exc), task_id, state)
    except KeyError:
        return refused(UNKNOWN_TASK, f"no Task {task_id!r}", task_id)
    if res.status == "paused":
        return Result(PAUSED, f"Task {task_id} paused (was {res.from_state}). "
                              f"`orch resume {task_id}` queues it again.",
                      task_id, res.from_state, TaskState.PAUSED_USAGE)
    return Result(PAUSE_REQUESTED, f"Task {task_id} is running; asked the daemon to pause it "
                                   "after the current Step.",
                  task_id, res.from_state, TaskState.RUNNING)


def pause_wait_seconds(cfg: Config) -> float:
    """The default bound of :func:`wait_for_pause`: a Step may run for a while."""
    return max(PAUSE_WAIT_SECONDS, cfg.usage("step_grace_seconds") + 5)


def wait_for_pause(cfg: Config, db: Database, task_id: str, *,
                   timeout: float | None = None) -> Result:
    """After a pause request: wait up to ``timeout`` seconds (default
    :func:`pause_wait_seconds`; 0 only looks) for the daemon to pause the Task."""
    state = _wait_while_running(db, task_id,
                                pause_wait_seconds(cfg) if timeout is None else timeout)
    if state is None:
        return refused(UNKNOWN_TASK, f"no Task {task_id!r}", task_id)
    if state == TaskState.PAUSED_USAGE:
        return Result(PAUSED, f"Task {task_id} paused (was running). "
                              f"`orch resume {task_id}` continues it.",
                      task_id, TaskState.RUNNING, state)
    if state == TaskState.RUNNING:
        return Result(PAUSE_REQUESTED, f"Pause requested for Task {task_id}; the current Step "
                                       "hasn't finished yet.", task_id, state, state)
    return Result(ENDED, f"Task {task_id} ended {state} before it could be paused.", task_id,
                  TaskState.RUNNING, state)


def _wait_while_running(db: Database, task_id: str, timeout: float) -> TaskState | None:
    """The Task's state once it stops running or ``timeout`` passes (None: no such Task)."""
    if db.get_task(task_id) is None:
        return None
    limit = time.monotonic() + timeout
    while time.monotonic() < limit and db.task_state(task_id) == TaskState.RUNNING:
        time.sleep(POLL_SECONDS)
    return db.task_state(task_id)


def resume(cfg: Config, db: Database, task_id: str, *, via: str, at: datetime) -> Result:
    """Queue a paused Task now, whatever its reset time, or withdraw a pending
    pause of a running one."""
    row, bad = _lookup(db, task_id)
    if bad:
        return bad
    state = TaskState(row["state"])
    try:
        reason = pauses.resume_by_hand(cfg, db, task_id, at=at, via=via)
    except StateChanged as exc:
        return refused(STATE_CHANGED, str(exc), task_id, state)
    except pauses.NotAllowed as exc:
        return refused(WRONG_STATE, str(exc), task_id, state)
    now_state = db.task_state(task_id)
    return Result(RESUMED if state != TaskState.RUNNING else PAUSE_WITHDRAWN,
                  f"Task {task_id}: {reason}", task_id, state, now_state,
                  note=daemon_note(cfg) if now_state == TaskState.QUEUED else None)


# --------------------------------------------------------------------------
# Priority (§2.3)
# --------------------------------------------------------------------------


def set_priority(cfg: Config, db: Database, task_id: str, level: str, *, via: str,
                 at: datetime) -> Result:
    """Change a Task's assigned Priority (the Effective priority follows)."""
    row, bad = _lookup(db, task_id)
    if bad:
        return bad
    state = TaskState(row["state"])
    if level not in prio.PRIORITIES:
        return refused(INVALID, f"unknown priority {level!r}", task_id, state)
    if state in TERMINAL:
        return refused(WRONG_STATE, f"Task {task_id} is {state}; its priority is final",
                       task_id, state)
    old = row["priority"]
    db.set_priority(task_id, level)
    db.record_intervention(at=iso(at), command=via, task_id=task_id,
                           detail=f"priority {old} -> {level}")
    folder = Path(row["folder"])
    if (folder / "task.yaml").exists():
        spec = read_task_yaml(folder)
        spec.priority = level  # the assigned Priority only, never the effective one
        write_task_yaml(folder, spec)
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    with open(folder / "logs" / "nightshift.log", "a") as f:
        f.write(f"{iso(at)} priority {old} -> {level} via {via}\n")
    eff = prio.evaluate(cfg, db.get_task(task_id), at).effective
    note = "" if eff == level else f" (effective {eff}: deadline {row['deadline']})"
    return Result(PRIORITY_SET, f"Task {task_id} priority {old} -> {level}{note}", task_id,
                  state, state)


# --------------------------------------------------------------------------
# Intake (§2.4): propose -> create | decline -> plan -> approve | deny
# --------------------------------------------------------------------------


def propose(cfg: Config, request: str, *, at: datetime, template: str | None = None,
            task_type: str | None = None, agent: str | None = None,
            priority: str | None = None, deadline: str | None = None,
            run_at: str | None = None, internet: str | None = None,
            simulate_after: int | None = None,
            adapter_provider: planning.AdapterProvider | None = None,
            progress: Callable[[str], None] | None = None) -> Result:
    """The estimate and Task type of a new Task, before it exists.

    Free-form: a planning call estimates the minutes and proposes a Task
    type (``task_type`` overrides it); ``progress`` hears when that call
    starts. With ``template`` both come from the template, with no call.
    ``deadline`` and ``run_at`` are as typed (``nightshift.priority``). Times
    are checked before a planning call is spent on a typo.
    """
    try:
        tpl = instance = None
        if template:
            tpl = templates.load(cfg, template)
            instance = templates.instantiate(cfg, tpl, request)
            proposal = planning.from_template(tpl.as_dict())
            cfg.agent(agent or tpl.agent)
        else:
            cfg.agent(agent or cfg.default("agent"))
        deadline_at = prio.parse_deadline(deadline, at, cfg.timezone) if deadline else None
        start_at = prio.parse_at(run_at, at, cfg.timezone) if run_at else None
        if tpl is None:
            if progress is not None:
                progress("Planning: asking for an estimate and a Task type...")
            proposal = planning.estimate(cfg, request,
                                         adapter_provider=adapter_provider or planning.get_adapter)
    except (ConfigError, planning.PlanningError, templates.TemplateError) as exc:
        return refused(INVALID, f"{exc}. Nothing was queued.")
    draft = Draft(
        request=request, prompt=instance.prompt if instance else request, proposal=proposal,
        task_type=task_type or (tpl.task_type if tpl else proposal.task_type),
        type_given=task_type is not None, agent=agent, priority=priority, deadline=deadline_at,
        run_at=start_at, internet=internet, simulate_after=simulate_after,
        needs_plan=planning.needs_plan(cfg, proposal.estimated_minutes), template=tpl,
        instance=instance,
    )
    return Result(PROPOSED, "\n".join(describe(cfg, draft)), draft=draft)


def describe(cfg: Config, draft: Draft) -> list[str]:
    """The lines a person confirms a :class:`Draft` by."""
    p = draft.proposal
    if draft.template is None:
        note = "proposed" if not draft.type_given or draft.task_type == p.task_type else (
            f"from --type; proposed {p.task_type}"
        )
        lines = [f"Estimate:  {p.estimated_minutes} minutes",
                 f"Task type: {draft.task_type} ({note})"]
        if p.reason:
            lines.append(f"Reason:    {p.reason}")
        if draft.needs_plan:
            lines.append(f"Over {planning.threshold(cfg):g} minutes: a plan.md will be written "
                         "for your approval.")
        return lines
    tpl, inst = draft.template, draft.instance
    lines = [f"Template:  {tpl.name}" + (f" ({tpl.description})" if tpl.description else ""),
             f"Estimate:  {p.estimated_minutes} minutes (from the template)",
             f"Task type: {draft.task_type}" + (" (from --type)" if draft.type_given else "")]
    lines += [f"Input:     {var} = {inst.values[var]}" for var in tpl.inputs]
    lines += [f"Input:     {f.source} -> {f.to}" for f in inst.files]
    if tpl.deadline and not draft.deadline:
        lines.append(f"Deadline:  from the agent's {tpl.deadline['from_output']} "
                     f"({tpl.deadline['field']}), if the posting states one")
    return lines


def decline(db: Database, draft: Draft) -> Result:
    """The person said no to a :class:`Draft`: nothing is queued (a planning
    call's usage is still recorded)."""
    if draft.template is None:
        planning.record_usage(db, None, "estimate", draft.proposal)
    return Result(DECLINED, "Not queued.", draft=draft)


def create(cfg: Config, db: Database, draft: Draft, *, via: str) -> Result:
    """Create the Task of a :class:`Draft`: ``queued``, or ``awaiting_approval``
    over the plan threshold (then :func:`plan` writes its plan.md)."""
    tpl = draft.template
    minutes = draft.estimated_minutes
    source = via if tpl is None else (
        f"{via} --template {tpl.name}" if via.startswith("orch ") else f"{via}, template {tpl.name}"
    )
    reason = (f"created by {source} (estimated {minutes} min"
              + (", needs plan approval)" if draft.needs_plan else ", no plan needed)"))
    try:
        if tpl is None:
            new = create_task(
                cfg, db, draft.prompt, task_type=draft.task_type, agent=draft.agent,
                priority=draft.priority, deadline=draft.deadline, run_at=draft.run_at,
                internet=draft.internet, estimated_minutes=minutes,
                needs_approval=draft.needs_plan, simulate_rate_limit_after=draft.simulate_after,
                reason=reason,
            )
        else:
            new = create_task(
                cfg, db, draft.prompt, title=draft.request, task_type=draft.task_type,
                agent=draft.agent or tpl.agent, agent_override=draft.agent is not None,
                priority=draft.priority, deadline=draft.deadline, run_at=draft.run_at,
                long=tpl.long, internet=draft.internet or tpl.internet,
                estimated_minutes=minutes, template=tpl.name,
                max_runtime_minutes=tpl.max_runtime_minutes, needs_approval=draft.needs_plan,
                simulate_rate_limit_after=draft.simulate_after, reason=reason,
            )
    except ConfigError as exc:
        return refused(INVALID, str(exc))
    tid = new.spec.id
    if tpl is None:
        planning.record_usage(db, tid, "estimate", draft.proposal)
    else:
        templates.install_inputs(cfg, draft.instance, new.folder, tid)
    details = timing_notes(cfg, new.spec)
    if draft.simulate_after:
        n = draft.simulate_after
        details.append(f"Fault injection: a simulated rate limit after {n} "
                       f"Step{'s' if n != 1 else ''}")
    if draft.needs_plan:
        return Result(CREATED, f"Task {tid} is awaiting approval: {new.folder}", tid, None,
                      TaskState.AWAITING_APPROVAL, details=tuple(details), draft=draft,
                      folder=new.folder, spec=new.spec)
    return Result(CREATED, f"Task {tid} queued: {new.folder}", tid, None, TaskState.QUEUED,
                  note=daemon_note(cfg), details=tuple(details), draft=draft, folder=new.folder,
                  spec=new.spec)


def timing_notes(cfg: Config, spec: TaskSpec) -> list[str]:
    """When a new Task can start, so a held-back Task is never a surprise."""
    lines = []
    if spec.deadline:
        lines.append(f"Deadline:  {spec.deadline} (urgent from "
                     f"{prio.deadline_urgent_hours(cfg):g}h before)")
    if spec.run_at:
        lines.append(f"Starts:    no earlier than {spec.run_at}")
    if spec.long:
        start, end = prio.long_window(cfg)
        lines.append(f"Long Task: starts only between {start:%H:%M} and {end:%H:%M} "
                     f"({cfg.timezone}) unless urgent")
    return lines


def plan(cfg: Config, db: Database, task_id: str, *,
         adapter_provider: planning.AdapterProvider | None = None) -> Result:
    """Write the plan.md of a Task awaiting approval (a planning call) and
    return it for :func:`approve` or :func:`deny`. A failed call is refused
    with ``NO_PLAN``; the Task still waits and can be approved without one."""
    row, bad = _lookup(db, task_id)
    if bad:
        return bad
    state = TaskState(row["state"])
    if state != TaskState.AWAITING_APPROVAL:
        return refused(WRONG_STATE, f"Task {task_id} is {state}, not awaiting approval",
                       task_id, state)
    try:
        text = planning.write_plan(cfg, db, task_id,
                                   adapter_provider=adapter_provider or planning.get_adapter)
    except planning.PlanningError as exc:
        return refused(NO_PLAN, f"Could not write a plan: {exc}", task_id, state)
    return Result(PLANNED, text.rstrip(), task_id, state, state, plan=text)
