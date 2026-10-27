"""Failures (§2.7): runtime caps, the retry budget, retries and cancel.

A **Failed Attempt** is one that crashed (non-zero exit, agent error result,
binary would not start, daemon died mid-Attempt), hit its runtime cap, or
failed a post-hook. Only Failed Attempts are charged to the retry budget
(``retries.max_retries``, default 2). Each retry waits in ``retry_wait``
(10 minutes before the first, 30 before the second) and then goes back in the
queue on the same agent. Once the budget is spent the Task is ``failed``.

Every function that needs the time takes it from the caller, so the whole
retry schedule is testable with a fake clock.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import report
from .config import Config
from .db import Database, StateChanged
from .states import TERMINAL, TaskState
from .timeutil import iso, parse_iso

#: A post-hook checks or post-processes a finished Attempt's output (e.g. the
#: Tracker merge). It gets the Task folder and the Task row and raises to fail.
PostHook = Callable[[Path, Mapping[str, Any]], None]


class PostHookFailed(Exception):
    """Raised by a post-hook whose check failed."""


def log_line(folder: Path, line: str, redact: Callable[[str], str]) -> None:
    """Append one line to the Task's ``logs/nightshift.log``."""
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    with open(folder / "logs" / "nightshift.log", "a") as f:
        f.write(redact(line) + "\n")


def finalize(cfg: Config, db: Database, row: Mapping[str, Any], state: TaskState, reason: str,
             *, at: datetime) -> None:
    """Write the final ``report.md`` for a Task that ends without the agent's
    say (failed by Nightshift, cancelled) and log the reason."""
    folder = Path(row["folder"])
    if folder.exists():
        report.finalize(
            folder,
            title=row["title"],
            result=str(state),
            result_detail=reason,
            attempts=db.attempts(row["id"]),
            usage_rows=db.usage_for_task(row["id"]),
            redact=cfg.secrets.redact,
            appendix=_appendix(cfg, row),
            **report_extras(cfg, db, row["id"]),
        )
        log_line(folder, f"{iso(at)} {reason}", cfg.secrets.redact)


def report_extras(cfg: Config, db: Database, task_id: str) -> dict[str, Any]:
    """What Nightshift adds to a Task's report from its own records: Needs
    you entries (resolved requests too), and for section 8 the dollars spent per paid agent and the
    paid-credit decisions (§1.5)."""
    from . import github, needsyou, paid, stuck  # all import this module

    row = db.get_task(task_id)
    return {"needs_you": needs_you_extra(cfg, db, task_id),
            "resolved": needsyou.report_lines(db, task_id, Path(row["folder"])) if row else [],
            "spent": db.task_spend_by_agent(task_id),
            "paid_credits": paid.report_lines(db, task_id),
            "stuck": stuck.report_lines(db, task_id),
            "github": github.report_lines(db, task_id)}


def needs_you_extra(cfg: Config, db: Database, task_id: str) -> list[str]:
    """Needs you entries Nightshift adds to the report itself (deadline-driven
    failovers, §2.8; the per-Task spend cap, §1.5; stuck again, §2.7)."""
    from . import failover, spend, stuck  # failover and stuck import this module

    return (spend.needs_you_lines(cfg, db, task_id) + failover.needs_you_lines(db, task_id)
            + stuck.needs_you_lines(db, task_id))


def _appendix(cfg: Config, row: Mapping[str, Any]) -> str | None:
    if not row["template"]:
        return None
    from . import templates  # templates imports this module

    return templates.report_appendix(cfg, row)


# --------------------------------------------------------------------------
# Runtime caps and post-hooks
# --------------------------------------------------------------------------


def runtime_cap_seconds(cfg: Config, row: Mapping[str, Any]) -> float:
    """One Attempt's runtime cap: the Task's ``max_runtime_minutes`` (set from
    its template or Task type when it was created), else the config default."""
    minutes = row["max_runtime_minutes"] or cfg.max_runtime_default(row["task_type"])
    return float(minutes) * 60


def post_hooks_for(cfg: Config, row: Mapping[str, Any], host: Any = None) -> Sequence[PostHook]:
    """The post-hooks of a Task. Free-form Tasks have none; template Tasks
    get their template's (``nightshift.templates``). ``host`` runs confined
    tools such as the LaTeX compile (default: the real Mac)."""
    if not row["template"]:
        return ()
    from . import templates  # templates imports this module

    return templates.post_hooks(cfg, row, host)


def run_post_hooks(
    hooks: Sequence[PostHook], folder: Path, row: Mapping[str, Any]
) -> str | None:
    """Run post-hooks in order; the first failure's message, or None."""
    for hook in hooks:
        name = getattr(hook, "__name__", type(hook).__name__)
        try:
            hook(folder, row)
        except Exception as exc:  # any exception from a hook fails it
            return f"post-hook {name} failed: {exc or type(exc).__name__}"
    return None


# --------------------------------------------------------------------------
# Retry budget
# --------------------------------------------------------------------------


def retry_wait(cfg: Config, retry_no: int) -> timedelta:
    """How long the Task waits in ``retry_wait`` before retry ``retry_no``
    (1-based): 10 minutes, then 30; the last configured value repeats."""
    waits = cfg.retry_wait_minutes
    return timedelta(minutes=waits[min(retry_no, len(waits)) - 1])


@dataclass(frozen=True)
class FailureOutcome:
    state: TaskState  # retry_wait or failed
    reason: str
    retries_used: int
    retry_at: str | None


def after_failed_attempt(
    cfg: Config, db: Database, task_id: str, reason: str, *, at: datetime,
    run_id: int | None = None,
) -> FailureOutcome:
    """Charge a Failed Attempt to the retry budget and move the running Task
    to ``retry_wait`` (budget left) or ``failed`` (budget spent, final report
    written). ``at`` is when the Attempt ended, from the caller's clock."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    used = int(row["retries_used"] or 0)
    budget = cfg.max_retries
    if used < budget:
        n = used + 1
        wait = retry_wait(cfg, n)
        retry_at = iso(at + wait)
        from . import failover  # failover imports this module

        # The second retry may switch to another agent of the Task type (§2.7).
        switch = failover.retry_agent(cfg, db, row, n, at)
        agent = switch[0] if switch else row["agent"]
        why = (f"{reason}; retry {n} of {budget} on {agent} "
               f"after {wait.total_seconds() / 60:g} min, at {retry_at}")
        fields = {"retries_used": n, "retry_at": retry_at}
        if switch:
            why += (f"; switching from {row['agent']} to {agent} for this retry "
                    "(fresh session from plan.md + progress.md)")
            failover.record_retry_switch(cfg, db, row, agent, switch[1], at=at, run_id=run_id,
                                         reason=why, set_fields=fields)
        else:
            db.transition(task_id, TaskState.RETRY_WAIT, why, run_id=run_id, set_fields=fields)
            log_line(Path(row["folder"]), f"{iso(at)} {why}", cfg.secrets.redact)
        return FailureOutcome(TaskState.RETRY_WAIT, why, n, retry_at)
    why = (f"{reason}; no retries left ({used} of {budget} used)" if budget
           else f"{reason}; retries are off")
    finalize(cfg, db, row, TaskState.FAILED, why, at=at)
    db.transition(task_id, TaskState.FAILED, why, run_id=run_id, set_fields={"retry_at": None})
    return FailureOutcome(TaskState.FAILED, why, used, None)


def due_retries(db: Database, now: datetime) -> list[Mapping[str, Any]]:
    return [
        r for r in db.tasks_in_state(TaskState.RETRY_WAIT)
        if r["retry_at"] is None or parse_iso(r["retry_at"]) <= now
    ]


def promote_due(cfg: Config, db: Database, now: datetime) -> list[str]:
    """Move every ``retry_wait`` Task whose wait is over back to ``queued``."""
    moved: list[str] = []
    for row in due_retries(db, now):
        reason = (f"retry {row['retries_used']} of {cfg.max_retries}: "
                  f"waited until {row['retry_at']}")
        try:
            db.transition(row["id"], TaskState.QUEUED, reason, expect=TaskState.RETRY_WAIT,
                          set_fields={"retry_at": None})
        except StateChanged:
            continue  # cancelled or retried by hand meanwhile
        moved.append(row["id"])
    return moved


# --------------------------------------------------------------------------
# By hand: orch retry / orch cancel
# --------------------------------------------------------------------------

RETRYABLE = frozenset({TaskState.FAILED, TaskState.PARTIAL, TaskState.RETRY_WAIT})


def retryable(row: Mapping[str, Any]) -> bool:
    """Whether ``orch retry`` applies to the Task in ``row``: failed, partial,
    waiting to retry, or in ``needs_you`` for getting stuck again (§2.7)."""
    from . import stuck  # stuck imports this module

    return TaskState(row["state"]) in RETRYABLE or stuck.waiting(row)


class NotAllowed(Exception):
    """The Task's state doesn't allow this command."""


class StillChanging(NotAllowed, StateChanged):
    """The daemon kept moving the Task while this command looked at it."""


def retry_by_hand(cfg: Config, db: Database, task_id: str, *, at: datetime,
                  via: str = "orch retry") -> str:
    """Requeue a ``failed`` or ``partial`` Task with a fresh retry budget, or
    end a ``retry_wait`` early (its budget is kept). A Task in ``needs_you``
    for getting stuck again is requeued to continue (its next Attempt is told
    what it was stuck on), with fresh stuck and retry allowances. Returns the
    reason."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    state = TaskState(row["state"])
    if not retryable(row):
        raise NotAllowed(f"Task {task_id} is {state}; only failed, partial or retry_wait "
                         "Tasks, or ones that got stuck, can be retried")
    if state == TaskState.NEEDS_YOU:
        reason = (f"retried by hand after getting stuck ({via}); stuck count and retry "
                  "budget reset")
        fields: dict[str, Any] = {"retry_at": None, "retries_used": 0, "stuck_count": 0,
                                  "resume_pending": 1}
    elif state == TaskState.RETRY_WAIT:
        reason = f"retry {row['retries_used']} of {cfg.max_retries} started early ({via})"
        fields = {"retry_at": None}
    else:
        reason = f"retried by hand from {state} ({via}); retry budget reset"
        fields = {"retry_at": None, "retries_used": 0, "stuck_count": 0}
    db.transition(task_id, TaskState.QUEUED, reason, expect=state, set_fields=fields)
    log_line(Path(row["folder"]), f"{iso(at)} {reason}", cfg.secrets.redact)
    return reason


@dataclass(frozen=True)
class CancelResult:
    #: "cancelled" (done now) or "requested" (the daemon stops the Attempt).
    status: str
    from_state: TaskState


def cancel(cfg: Config, db: Database, task_id: str, *, at: datetime, daemon_up: bool,
           via: str = "orch cancel", why: str | None = None) -> CancelResult:
    """Cancel a Task in any non-terminal state.

    A ``running`` Task under a live daemon only gets a cancel request: the
    daemon's Attempt watchdog sees it within a second or so, stops the agent
    and ends the Task ``cancelled`` itself. A ``running`` Task with no daemon
    alive is an orphan; it is cancelled here, its open Attempt closed.
    ``why`` replaces "cancelled by hand (via)" in the reason (e.g. a
    Schedule's Overlap rule replacing an Occurrence).
    """
    for _ in range(3):  # the daemon may move the Task while we look
        row = db.get_task(task_id)
        if row is None:
            raise KeyError(task_id)
        state = TaskState(row["state"])
        if state in TERMINAL:
            raise NotAllowed(f"Task {task_id} is already {state}")
        if state == TaskState.RUNNING and daemon_up:
            db.request_cancel(task_id, via)
            return CancelResult("requested", state)
        reason = f"{why or f'cancelled by hand ({via})'} while {state}"
        try:
            db.transition(task_id, TaskState.CANCELLED, reason, expect=state,
                          set_fields={"retry_at": None})
        except StateChanged:
            continue
        for a in db.attempts(task_id):
            if a["ended_at"] is None:
                db.finish_attempt(a["id"], exit_code=None, outcome="cancelled",
                                  end_reason=reason, session_id=None)
        finalize(cfg, db, db.get_task(task_id), TaskState.CANCELLED, reason, at=at)
        return CancelResult("cancelled", state)
    raise StillChanging(f"Task {task_id} kept changing state; try again")
