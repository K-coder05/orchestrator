"""Paid-per-use spend (§1.5): pricing paid calls, the per-Task cap, credits.

Only the Gemini API path (agent ``gemini-api``, AI Studio credits) spends
money. Its adapter reports each model call it made (``paid_calls()``);
Nightshift prices it from ``spend.gemini_api_prices`` and writes one
``spend_events`` row per call. A Task whose spend reaches
``spend.per_task_usd`` is stopped and handed to you (``needs_you``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .adapters.base import PaidCall
from .config import Config
from .db import Database

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
    return CreditView(
        spent_total=sum(r["cost_usd"] for r in rows), spent_since_balance=spent_since,
        balance=balance, balance_as_of=None if as_of is None else str(as_of),
        remaining=0.0 if expired else max(balance - spent_since, 0.0),
        expiry=None if expiry is None else str(expiry), expired=expired,
    )


def _date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))
