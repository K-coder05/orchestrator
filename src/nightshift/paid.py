"""The paid-credit rule (§1.5): always use the subscription first.

A Task whose own agent is a subscription with a paid path (Gemini, paid
path ``gemini-api``) and is out of usage moves to the paid path only when
both are true:

- the subscription quota is gone (a real limit hit: ``usage.agent_out_until``
  or a Task paused on the agent's limit; a simulated limit never spends), and
- the Task is urgent (Effective priority), **or** the subscription's next
  reset falls after the end of the Task's Catch-up window (an Occurrence of
  a Schedule with ``within N hours``: its fire time + N). A Task with no
  such window can wait, so only urgency counts for it.

Otherwise it waits like any Task whose agent is out. A Task that failed
over (§2.8, §2.7) to another agent never uses paid credits: when the
failover target's subscription is also out, it waits (§7, resolved). A Task
with ``--agent`` never moves. Nor does one while a spend cap holds, at its
per-Task cap, or with no API key: spend caps apply to every paid call.
Once the subscription is no longer out, a Task the rule moved goes back to
it if it is queued or ``paused_spend``; a running one finishes on paid
credits.

The daemon calls :func:`apply` before every pick, after subscription
failover (``failover.reassign_held``), so another subscription is always
tried first. Every decision is a ``paid_credit_decisions`` row (a waiting
Task's is recorded again only when its verdict or rule changes, or the
Task changed state since), a line in ``logs/nightshift.log`` and a line in
the report's section 8. Every function takes the time from the caller
(fake clocks in tests).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import failover, failures, pauses, spend, usage
from .adapters import REGISTRY, get_adapter, paid_path
from .config import Config
from .db import Database, StateChanged
from .priority import deadline_urgent_hours, effective_priority
from .states import TaskState
from .timeutil import iso, parse_iso

#: ``failovers.trigger`` of a move onto the paid path, and of the move back.
TRIGGER_PAID = "paid_credit"
TRIGGER_BACK = "subscription_reset"

#: ``paid_credit_decisions.rule``: why the rule decided as it did.
RULE_URGENT = "urgent"
RULE_AFTER_WINDOW = "after_window"
RULE_BEFORE_WINDOW = "before_window"
RULE_NO_WINDOW = "no_window"
RULE_FAILED_OVER = "failed_over"
RULE_SPEND_CAP = "spend_cap"
RULE_TASK_CAP = "task_cap"
RULE_NO_KEY = "no_key"
RULE_BACK = "subscription_reset"

_CLEAR_PAUSE = {"pause_kind": None, "pause_limit_type": None, "pause_reset_at": None,
                "resume_at": None}


@dataclass(frozen=True)
class Verdict:
    use_paid: bool
    rule: str
    reason: str


@dataclass(frozen=True)
class Decision:
    task_id: str
    agent: str  # the subscription agent
    to_agent: str | None  # the paid path, if the Task moved there
    verdict: Verdict


def catch_up_end(db: Database, row: Mapping[str, Any]) -> datetime | None:
    """The end of an Occurrence's Catch-up window (fire time + N hours), or
    None: not an Occurrence, a ``run on next wake`` rule, or a Schedule that
    no longer exists."""
    if not row["schedule_name"]:
        return None
    s = db.get_schedule(row["schedule_name"])
    fire = db.occurrence_fire_at(row["id"])
    if s is None or fire is None or s["catch_up"] != "within":
        return None
    return parse_iso(fire) + timedelta(hours=float(s["catch_up_hours"] or 0))


def subscription_back_at(cfg: Config, db: Database, agent: str,
                         now: datetime) -> datetime | None:
    """When subscription ``agent``, out on a real limit, is back as far as is
    known (None: it isn't out): its Window status and Tasks paused on its
    limit. With no reset time given this is the next probe, the earliest it
    can be back."""
    times = [usage.agent_out_until(cfg, db, agent, now)]
    times += [parse_iso(r["resume_at"]) for r in db.tasks_in_state(TaskState.PAUSED_USAGE)
              if r["agent"] == agent and r["pause_kind"] == pauses.KIND_LIMIT
              and r["resume_at"]]
    ahead = [t for t in times if t is not None and t > now]
    return max(ahead) if ahead else None


def failed_over(db: Database, row: Mapping[str, Any]) -> bool:
    """Whether the Task is on another agent than its own (the one it was
    created on) because it failed over or a retry switched it."""
    moves = [f for f in db.failovers(row["id"])
             if f["trigger"] not in (TRIGGER_PAID, TRIGGER_BACK)]
    return bool(moves) and moves[0]["from_agent"] != row["agent"]


def verdict(cfg: Config, db: Database, row: Mapping[str, Any], back_at: datetime,
            now: datetime) -> Verdict:
    """The rule for a Task whose subscription is out until about ``back_at``."""
    agent, to = row["agent"], paid_path(row["agent"])
    if failed_over(db, row):
        return Verdict(False, RULE_FAILED_OVER, (
            f"{agent} is out of usage, but the Task failed over to {agent}: paid credits are "
            "never used after a failover; it waits for a reset"))
    urgent = effective_priority(row["priority"], row["deadline"], now,
                                urgent_within_hours=deadline_urgent_hours(cfg)) == "urgent"
    end = catch_up_end(db, row)
    if urgent:
        v = Verdict(True, RULE_URGENT, f"{agent} is out of usage until about {iso(back_at)} "
                                       "and the Task is urgent")
    elif end is not None and back_at > end:
        v = Verdict(True, RULE_AFTER_WINDOW, (
            f"{agent} is out of usage until about {iso(back_at)}, after the Task's Catch-up "
            f"window ends ({iso(end)})"))
    elif end is not None:
        return Verdict(False, RULE_BEFORE_WINDOW, (
            f"{agent} is out of usage until about {iso(back_at)}, before the Task's Catch-up "
            f"window ends ({iso(end)}), and the Task is not urgent: it waits"))
    else:
        return Verdict(False, RULE_NO_WINDOW, (
            f"{agent} is out of usage until about {iso(back_at)}; the Task is not urgent and "
            "has no Catch-up window: it waits"))
    # Spend caps apply to every paid call (§1.5).
    if spend.cap_reached(cfg, db, row["id"]):
        return Verdict(False, RULE_TASK_CAP,
                       f"{v.reason}, but {spend.cap_reason(cfg, db, row['id'])}: it waits")
    held = spend.spend_pause(cfg, db, now)
    if held is not None:
        return Verdict(False, RULE_SPEND_CAP, f"{v.reason}, but {held.reason}: it waits")
    configured = bool(to) and to in (cfg.data.get("agents") or {})
    secret = get_adapter(to, cfg.agent(to)).api_key_secret if configured and to else None
    if not configured or (secret and not cfg.secrets.get(secret)):
        return Verdict(False, RULE_NO_KEY, f"{v.reason}, but the paid path {to} is not set up "
                                           "(no agent config or API key): it waits")
    return Verdict(True, v.rule, f"{v.reason}: using paid credits ({to})")


def _repeats(db: Database, row: Mapping[str, Any], v: Verdict) -> bool:
    """Whether a waiting verdict repeats the Task's last decision, with the
    Task unchanged since (a new outage after it ran is recorded again)."""
    last = db.paid_credit_decisions(row["id"])
    return bool(last) and (last[-1]["use_paid"], last[-1]["rule"], last[-1]["task_changed_at"]) \
        == (int(v.use_paid), v.rule, row["state_changed_at"])


def _move(cfg: Config, db: Database, row: Mapping[str, Any], to: str, v: Verdict,
          at: datetime) -> bool:
    """Move a waiting Task to the paid path; a Task paused on usage is queued."""
    reason = f"paid-credit rule: {v.reason}"
    try:
        if row["state"] == TaskState.QUEUED:
            return failover.move_queued(cfg, db, row, to, trigger=TRIGGER_PAID,
                                        deadline_driven=False, reason=reason, at=at)
        failover.switch(cfg, db, row, to, to_state=TaskState.QUEUED, trigger=TRIGGER_PAID,
                        deadline_driven=False, reason=reason, at=at, run_id=None,
                        set_fields=dict(_CLEAR_PAUSE), expect=TaskState.PAUSED_USAGE)
        return True
    except StateChanged:
        return False


def _candidates(db: Database, now: datetime) -> list[Any]:
    """Tasks on a subscription with a paid path that are waiting: queued, or
    paused on a real usage limit whose resume time is ahead."""
    rows = list(db.tasks_in_state(TaskState.QUEUED))
    rows += [r for r in db.tasks_in_state(TaskState.PAUSED_USAGE)
             if r["pause_kind"] == pauses.KIND_LIMIT
             and r["resume_at"] and parse_iso(r["resume_at"]) > now]
    return [r for r in rows if not r["agent_override"] and paid_path(r["agent"])]


def _moved_by_rule(db: Database, row: Mapping[str, Any]) -> str | None:
    """The subscription a Task on a paid path came from, if the rule moved
    it there (its last move); None otherwise."""
    moves = db.failovers(row["id"])
    if not moves or moves[-1]["trigger"] != TRIGGER_PAID or row["agent_override"]:
        return None
    sub = getattr(REGISTRY.get(row["agent"]), "subscription", None)
    return sub if sub == moves[-1]["from_agent"] else None


def _move_back(cfg: Config, db: Database, now: datetime) -> list[Decision]:
    """Tasks the rule moved go back to their subscription once it is no
    longer out, if they are queued or ``paused_spend``."""
    out = []
    rows = list(db.tasks_in_state(TaskState.QUEUED))
    rows += db.tasks_in_state(TaskState.PAUSED_SPEND)
    for row in rows:
        sub = _moved_by_rule(db, row)
        if sub is None or subscription_back_at(cfg, db, sub, now) is not None:
            continue
        v = Verdict(False, RULE_BACK, f"{sub} is no longer out of usage: back on the "
                                      f"subscription ({row['agent']} -> {sub})")
        reason = f"paid-credit rule: {v.reason}; fresh session from plan.md + progress.md"
        try:
            if row["state"] == TaskState.QUEUED:
                if not failover.move_queued(cfg, db, row, sub, trigger=TRIGGER_BACK,
                                            deadline_driven=False, reason=reason, at=now):
                    continue
            else:
                failover.switch(cfg, db, row, sub, to_state=TaskState.QUEUED,
                                trigger=TRIGGER_BACK, deadline_driven=False, reason=reason,
                                at=now, run_id=None, set_fields=dict(_CLEAR_PAUSE),
                                expect=TaskState.PAUSED_SPEND)
        except StateChanged:
            continue
        db.record_paid_credit_decision(row["id"], at=iso(now), agent=sub, to_agent=sub,
                                       use_paid=False, rule=v.rule, reason=v.reason,
                                       task_changed_at=row["state_changed_at"])
        out.append(Decision(row["id"], sub, sub, v))
    return out


def apply(cfg: Config, db: Database, now: datetime) -> list[Decision]:
    """Apply the paid-credit rule: Tasks it moved whose subscription reset
    go back to it, then every waiting Task whose subscription is out is
    decided; returns the decisions recorded (moves and new waits)."""
    out = _move_back(cfg, db, now)
    for row in _candidates(db, now):
        agent = row["agent"]
        back_at = subscription_back_at(cfg, db, agent, now)
        if back_at is None:
            continue  # the subscription is not out: nothing to decide
        v = verdict(cfg, db, row, back_at, now)
        to = paid_path(agent) if v.use_paid else None
        if to:
            if not _move(cfg, db, row, to, v, now):
                continue
        elif _repeats(db, row, v):
            continue
        else:
            failures.log_line(Path(row["folder"]), f"{iso(now)} paid-credit rule: {v.reason}",
                              cfg.secrets.redact)
        db.record_paid_credit_decision(row["id"], at=iso(now), agent=agent, to_agent=to,
                                       use_paid=v.use_paid, rule=v.rule, reason=v.reason,
                                       task_changed_at=row["state_changed_at"])
        out.append(Decision(row["id"], agent, to, v))
    return out


def report_lines(db: Database, task_id: str) -> list[str]:
    """The report's lines for a Task's paid-credit decisions (section 8)."""
    out = []
    for d in db.paid_credit_decisions(task_id):
        what = ("**used paid credits**" if d["use_paid"]
                else "went back on the subscription" if d["rule"] == RULE_BACK else "waited")
        out.append(f"- {d['at']}: {what} ({d['reason']})")
    return out
