"""Paid-per-use spend (§1.5): pricing paid calls, the per-Task cap, credits.

Only the Gemini API path (agent ``gemini-api``, AI Studio credits) spends
money. Its adapter reports each model call it made (``paid_calls()``);
Nightshift prices it from ``spend.gemini_api_prices`` and writes one
``spend_events`` row per call. A Task whose spend reaches
``spend.per_task_usd`` is stopped and handed to you (``needs_you``).

The daily and monthly caps (``spend.per_day_usd``, ``spend.per_month_usd``)
hold every paid call: paid Tasks move to ``paused_spend`` (a running one is
stopped), subscription work goes on. A day's pause ends at midnight
(``timezone``, America/Los_Angeles by default); a month's when you raise the cap (``orch raise-cap``)
or the month ends. The daemon then queues them again (:func:`promote_paused`).
Every function that needs the time takes it from the caller (fake clocks).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from datetime import date, datetime, timedelta
from typing import Any

from . import failures
from .adapters import is_paid
from .adapters.base import PaidCall
from .config import Config
from .db import Database, StateChanged
from .priority import resume_order
from .states import TaskState
from .timeutil import iso, parse_iso

#: ``runs.outcome`` of an Attempt stopped by the per-Task cap.
OUTCOME_CAP = "spend_cap"


def price(cfg: Config, call: PaidCall) -> float:
    """Dollars a call cost: uncached prompt (and tool-use prompt) tokens at
    the input price, cached ones at the cached price, output and thinking
    tokens at the output price."""
    p = cfg.gemini_api_price(call.model)
    return (call.uncached_tokens * p["input"] + call.cached_tokens * p["cached_input"]
            + (call.output_tokens + call.thoughts_tokens) * p["output"]) / 1_000_000


def record(cfg: Config, db: Database, task_id: str, call: PaidCall, *, run_id: int | None,
           agent: str, at: str) -> float:
    """Write ``call`` as a ``spend_events`` row; returns its cost."""
    cost = price(cfg, call)
    db.record_spend(task_id, run_id=run_id, agent=agent, model=call.model, at=at,
                    input_tokens=call.uncached_tokens,
                    cached_tokens=call.cached_tokens, output_tokens=call.output_tokens,
                    thoughts_tokens=call.thoughts_tokens, cost_usd=cost, raw=call.raw)
    return cost


def cap_reached(cfg: Config, db: Database, task_id: str) -> bool:
    return db.task_spend(task_id) >= cfg.per_task_spend_cap


def cap_reason(cfg: Config, db: Database, task_id: str) -> str:
    return (f"the Task reached its ${cfg.per_task_spend_cap:.2f} per-Task spend cap "
            f"(${db.task_spend(task_id):.2f} spent on paid Gemini API calls)")


def needs_you_lines(cfg: Config, db: Database, task_id: str) -> list[str]:
    """The report's Needs you entry of a Task the cap stopped."""
    if not any(a["outcome"] == OUTCOME_CAP for a in db.attempts(task_id)):
        return []
    return [f"- **Spend cap:** Nightshift stopped this Task: {cap_reason(cfg, db, task_id)}. "
            "It spends nothing more; the work so far is in the Task folder."]


#: Which cap a ``paused_spend`` pause is for (``tasks.pause_kind``).
KIND_DAY = "spend_day"
KIND_MONTH = "spend_month"

#: In the reason of every monthly-cap pause: alerts make it a "needs you".
MONTHLY_CAP = "monthly spend cap"


@dataclass(frozen=True)
class SpendPause:
    """A daily or monthly cap that holds every paid call right now."""

    kind: str  # KIND_DAY or KIND_MONTH
    spent: float
    cap: float
    until: datetime  # when the period ends: midnight, or the 1st of next month
    reason: str


def _local(at: str, now: datetime) -> datetime:
    """A ``spend_events.at`` in ``now``'s time zone (a bare date: its midnight)."""
    t = datetime.fromisoformat(at)
    return t.replace(tzinfo=now.tzinfo) if t.tzinfo is None else t.astimezone(now.tzinfo)


def _spent_since(db: Database, start: datetime, now: datetime) -> float:
    return sum(r["cost_usd"] for r in db.spend_events() if _local(r["at"], now) >= start)


#: ``meta`` key of a month's raised cap (``orch raise-cap``), + ``YYYY-MM``.
MONTH_CAP_KEY = "spend.month_cap."


def month_cap(cfg: Config, db: Database, now: datetime) -> float:
    """This month's cap: ``spend.per_month_usd``, or what ``orch raise-cap``
    raised it to for this month."""
    raised = db.get_meta(MONTH_CAP_KEY + now.strftime("%Y-%m"))
    return max(cfg.per_month_spend_cap, float(raised)) if raised else cfg.per_month_spend_cap


def raise_month_cap(cfg: Config, db: Database, now: datetime, usd: float) -> float:
    """Raise this month's cap to ``usd`` (this month only); returns the old cap."""
    old = month_cap(cfg, db, now)
    if usd <= old:
        raise ValueError(f"the new cap must be above this month's cap of ${old:.2f}")
    db.set_meta(MONTH_CAP_KEY + now.strftime("%Y-%m"), repr(float(usd)))
    return old


def spend_pause(cfg: Config, db: Database, now: datetime) -> SpendPause | None:
    """The cap paid calls are paused on at ``now`` (local time), if any: the
    month's spend at its cap pauses them until you raise it (or the month
    ends), else the day's spend at ``spend.per_day_usd`` until midnight."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month = day.replace(day=1)
    spent = _spent_since(db, month, now)
    cap = month_cap(cfg, db, now)
    if spent >= cap:
        until = (month + timedelta(days=32)).replace(day=1)
        return SpendPause(KIND_MONTH, spent, cap, until, (
            f"paid calls reached the ${cap:.2f} {MONTHLY_CAP} (${spent:.2f} spent this "
            f"month); paused until you raise it (`orch raise-cap USD`) or the month ends"))
    spent = _spent_since(db, day, now)
    if spent >= cfg.per_day_spend_cap:
        until = (day + timedelta(days=1, hours=2)).replace(hour=0)  # DST-safe midnight
        return SpendPause(KIND_DAY, spent, cfg.per_day_spend_cap, until, (
            f"paid calls reached the ${cfg.per_day_spend_cap:.2f} daily spend cap "
            f"(${spent:.2f} spent today); paused until midnight ({until.isoformat()})"))
    return None


def pause_fields(pause: SpendPause) -> dict[str, Any]:
    """The Task columns a ``paused_spend`` transition sets."""
    return {"pause_kind": pause.kind, "pause_limit_type": None, "pause_reset_at": None,
            "resume_at": iso(pause.until)}


_CLEAR_PAUSE = {"pause_kind": None, "pause_limit_type": None, "pause_reset_at": None,
                "resume_at": None}


def pause_paid_queued(cfg: Config, db: Database, now: datetime) -> list[str]:
    """While a cap holds, move queued paid Tasks to ``paused_spend``."""
    pause = spend_pause(cfg, db, now)
    if pause is None:
        return []
    paused = []
    for row in db.tasks_in_state(TaskState.QUEUED):
        if not is_paid(row["agent"]):
            continue
        try:
            db.transition(row["id"], TaskState.PAUSED_SPEND, pause.reason,
                          expect=TaskState.QUEUED, set_fields=pause_fields(pause))
        except StateChanged:
            continue
        failures.log_line(Path(row["folder"]), f"{iso(now)} {pause.reason}", cfg.secrets.redact)
        paused.append(row["id"])
    return paused


def promote_paused(cfg: Config, db: Database, now: datetime) -> list[str]:
    """Once no cap holds, queue ``paused_spend`` Tasks again, in resume order
    (a Task stopped mid-Attempt continues its work). One whose deadline
    passed while paused is cancelled instead."""
    pause = spend_pause(cfg, db, now)
    moved = []
    for row in resume_order(cfg, db.tasks_in_state(TaskState.PAUSED_SPEND), now):
        tid, folder = row["id"], Path(row["folder"])
        try:
            if row["deadline"] and parse_iso(row["deadline"]) <= now:
                reason = f"deadline {row['deadline']} passed while paused on spend; cancelled"
                db.transition(tid, TaskState.CANCELLED, reason, expect=TaskState.PAUSED_SPEND,
                              set_fields=_CLEAR_PAUSE)
                failures.finalize(cfg, db, db.get_task(tid), TaskState.CANCELLED, reason, at=now)
            elif pause is None:
                what = ("monthly spend cap pause over (the cap was raised or a new month began)"
                        if row["pause_kind"] == KIND_MONTH
                        else "daily spend cap pause over (a new day began)")
                reason = f"{what}; queued to resume"
                db.transition(tid, TaskState.QUEUED, reason, expect=TaskState.PAUSED_SPEND,
                              set_fields=_CLEAR_PAUSE)
                failures.log_line(folder, f"{iso(now)} {reason}", cfg.secrets.redact)
            else:
                continue
        except StateChanged:
            continue  # cancelled meanwhile
        moved.append(tid)
    return moved


@dataclass(frozen=True)
class CreditView:
    """AI Studio credits for ``orch usage``."""

    spent_total: float  # every paid call ever recorded
    spent_since_balance: float  # since plans.gemini.api_credit_balance_as_of
    balance: float  # as configured
    balance_as_of: str | None
    remaining: float
    expiry: str | None
    expired: bool
    spent_today: float
    day_cap: float
    spent_this_month: float
    month_cap: float
    pause: SpendPause | None  # the cap holding paid calls now, if any


def credits(cfg: Config, db: Database, now: datetime) -> CreditView:
    """Spend so far and the credit left: the configured balance minus the
    spend recorded after the day the balance was read (that day's spend is
    taken to be in it); nothing once expired."""
    plan = (cfg.data.get("plans") or {}).get("gemini") or {}
    balance = float(plan.get("api_credit_balance_usd") or 0)
    as_of = plan.get("api_credit_balance_as_of")
    expiry = plan.get("api_credit_expiry")
    rows = db.spend_events()
    since = [r for r in rows if as_of is None or r["at"][:10] > _date(as_of).isoformat()]
    spent_since = sum(r["cost_usd"] for r in since)
    expired = expiry is not None and now.date() > _date(expiry)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return CreditView(
        spent_total=sum(r["cost_usd"] for r in rows), spent_since_balance=spent_since,
        balance=balance, balance_as_of=None if as_of is None else str(as_of),
        remaining=0.0 if expired else max(balance - spent_since, 0.0),
        expiry=None if expiry is None else str(expiry), expired=expired,
        spent_today=_spent_since(db, day, now), day_cap=cfg.per_day_spend_cap,
        spent_this_month=_spent_since(db, day.replace(day=1), now),
        month_cap=month_cap(cfg, db, now), pause=spend_pause(cfg, db, now),
    )


def _date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))
