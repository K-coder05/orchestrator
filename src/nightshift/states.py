"""Task lifecycle states (§2.1) and the transitions allowed between them."""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    SCHEDULED = "scheduled"
    AWAITING_APPROVAL = "awaiting_approval"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED_USAGE = "paused_usage"
    PAUSED_SPEND = "paused_spend"
    NEEDS_YOU = "needs_you"
    RETRY_WAIT = "retry_wait"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


S = TaskState

TERMINAL: frozenset[TaskState] = frozenset({S.DONE, S.PARTIAL, S.FAILED, S.CANCELLED})

#: States a brand-new Task may start in.
INITIAL: frozenset[TaskState] = frozenset({S.SCHEDULED, S.AWAITING_APPROVAL, S.QUEUED})

ALLOWED: dict[TaskState, frozenset[TaskState]] = {
    S.SCHEDULED: frozenset({S.QUEUED, S.AWAITING_APPROVAL, S.CANCELLED}),
    S.AWAITING_APPROVAL: frozenset({S.QUEUED, S.SCHEDULED, S.CANCELLED}),
    # queued -> failed: the daemon could not even start an Attempt (e.g. bad agent config).
    S.QUEUED: frozenset({S.RUNNING, S.AWAITING_APPROVAL, S.SCHEDULED, S.FAILED, S.CANCELLED}),
    # running -> queued: the daemon was stopped mid-Attempt; the Task goes back in line.
    S.RUNNING: frozenset(
        {
            S.QUEUED,
            S.DONE,
            S.PARTIAL,
            S.FAILED,
            S.PAUSED_USAGE,
            S.PAUSED_SPEND,
            S.NEEDS_YOU,
            S.RETRY_WAIT,
            S.CANCELLED,
        }
    ),
    S.PAUSED_USAGE: frozenset({S.QUEUED, S.RUNNING, S.AWAITING_APPROVAL, S.CANCELLED}),
    S.PAUSED_SPEND: frozenset({S.QUEUED, S.RUNNING, S.CANCELLED}),
    S.NEEDS_YOU: frozenset({S.QUEUED, S.DONE, S.PARTIAL, S.FAILED, S.CANCELLED}),
    S.RETRY_WAIT: frozenset({S.QUEUED, S.RUNNING, S.FAILED, S.CANCELLED}),
    # `orch retry` may requeue a finished-but-unsuccessful Task (§2.7).
    S.PARTIAL: frozenset({S.QUEUED}),
    S.FAILED: frozenset({S.QUEUED}),
    S.DONE: frozenset(),
    S.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    pass


def check_transition(from_state: TaskState | None, to_state: TaskState) -> None:
    if from_state is None:
        if to_state not in INITIAL:
            raise InvalidTransition(f"a new Task cannot start in {to_state}")
        return
    if to_state not in ALLOWED[from_state]:
        raise InvalidTransition(f"{from_state} -> {to_state} is not allowed")
