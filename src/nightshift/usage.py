"""Usage Windows, Limit observations, Baselines and probing (§1.5).

Every usage field an adapter reports is stored per Attempt (``usage_events``)
and per call outside Attempts (planning calls and probes, ``planning_usage``).
This module reads them per **Window**: one usage-limit period of an agent's
plan, such as Claude's rolling 5 hours or its weekly cap.

- **Where a Window starts.** Agents report the reset time of their Windows
  as they run (Claude: every ``rate_limit_event``); the latest is kept in
  ``window_status``. The current Window is ``[reset - length, now]`` while
  that reset is ahead; otherwise (unknown, or passed) the trailing
  ``length`` hours. Lengths come from ``usage.windows.<agent>``.
- **Limit observations.** Every actual limit hit (not a simulated one) is
  stored with the usage Nightshift recorded in that Window up to the hit,
  in reported cost-equivalent (plus tokens and requests). A Window that is
  still out (a probe hitting the same limit, a second hit before the reset)
  is observed once.
- **Baselines.** The lowest cost-equivalent of a Window's last 3 Limit
  observations. Nightshift sees only its own usage, never interactive use,
  so recorded usage at a hit undercounts the Window and the Baseline errs
  low (conservative). Thresholds that act on Baselines are Phase 2.
- **Probing.** A Task paused on a limit with no parseable reset time is not
  simply retried: at the probe interval the daemon sends the agent a
  minimal probe request (:func:`probe`) and requeues its paused Tasks only
  once the probe goes through (``nightshift.pauses.promote_due``).

Every function that needs the time takes it from the caller.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .adapters import get_adapter
from .adapters.base import AgentAdapter, ProbeReply, RateLimit, WindowInfo
from .config import Config
from .db import Database
from .timeutil import iso, parse_iso

AdapterProvider = Callable[[str, dict], AgentAdapter]

#: How many of a Window's latest Limit observations make its Baseline.
BASELINE_OBSERVATIONS = 3


# --------------------------------------------------------------------------
# Windows
# --------------------------------------------------------------------------


@dataclass
class WindowUsage:
    """Usage recorded in one span of time, summed over every model."""

    cost_usd: float | None = None  # reported cost-equivalent; None if nothing reported one
    events: int = 0
    events_without_cost: int = 0  # e.g. an Attempt stopped before its result line
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    attempts_events: int = 0  # rows from Attempts (the rest: planning calls, probes)


def window_name(cfg: Config, agent: str, limit_type: str | None) -> str:
    """The Window a limit hit belongs to. A hit that names none counts as the
    agent's shortest Window (conservative, see ``Config.window_hours``)."""
    if limit_type:
        return limit_type
    table = cfg.windows(agent)
    return min(table, key=table.get) if table else "unknown"


def current_window(cfg: Config, db: Database, agent: str, window: str,
                   at: datetime) -> tuple[datetime, datetime | None]:
    """(start, reset) of ``window``'s current period at ``at``. The reset is
    None when no future reset is known (a trailing window is used)."""
    length = timedelta(hours=cfg.window_hours(agent, window))
    st = db.window_status(agent).get(window)
    if st is not None and st["resets_at"]:
        reset = parse_iso(st["resets_at"])
        if reset > at:
            return min(reset - length, at), reset
    return at - length, None


def usage_between(db: Database, agent: str, start: datetime, end: datetime) -> WindowUsage:
    """Usage ``agent`` recorded in ``(start, end]``. A row is dated when it was
    recorded (an Attempt's usage at its end)."""
    out = WindowUsage()
    for r in db.agent_usage(agent):
        at = parse_iso(r["at"])
        if not (start < at <= end):
            continue
        out.events += 1
        if r["kind"] == "attempt":
            out.attempts_events += 1
        if r["cost_usd"] is None:
            out.events_without_cost += 1
        else:
            out.cost_usd = (out.cost_usd or 0.0) + float(r["cost_usd"])
        out.input_tokens += r["input_tokens"] or 0
        out.output_tokens += r["output_tokens"] or 0
        out.cache_creation_tokens += r["cache_creation_tokens"] or 0
        out.cache_read_tokens += r["cache_read_tokens"] or 0
        out.requests += r["requests"] or 0
    return out


def record_windows(db: Database, agent: str, infos: Iterable[WindowInfo], at: datetime) -> None:
    """Keep the latest Window status an agent reported."""
    for w in infos:
        db.set_window_status(agent, w.window, resets_at=w.resets_at, utilization=w.utilization,
                             status=w.status, seen_at=iso(at))


# --------------------------------------------------------------------------
# Limit observations and Baselines
# --------------------------------------------------------------------------


def _same_window_still_out(cfg: Config, agent: str, last, window: str,
                           reset: datetime | None, at: datetime) -> bool:
    """Whether ``last`` (an earlier observation) was of the Window that is
    still out at ``at``, so this hit is not a new observation."""
    if last is None:
        return False
    last_reset = parse_iso(last["resets_at"]) if last["resets_at"] else None
    if last_reset is not None:
        if reset is not None:
            return abs((reset - last_reset).total_seconds()) < 120
        return last_reset > at
    # The last hit gave no reset time: its Window ended within one length of
    # the hit. A new reset time whose Window started after that hit is new.
    length = timedelta(hours=cfg.window_hours(agent, window))
    last_hit = parse_iso(last["hit_at"])
    if at - last_hit >= length:
        return False
    return reset is None or reset - length <= last_hit


def record_limit_hit(
    cfg: Config, db: Database, agent: str, limit: RateLimit | None, *, at: datetime,
    source: str, task_id: str | None = None, run_id: int | None = None,
) -> int | None:
    """Store an actual limit hit as a Limit observation, with the usage
    recorded in its Window up to ``at``. Returns the observation id, or None
    when it is the same Window as the last observation (still out)."""
    limit = limit or RateLimit(limit_type=None, reset_at=None)
    window = window_name(cfg, agent, limit.limit_type)
    reset = parse_iso(limit.reset_at) if limit.reset_at else None
    observations = db.limit_observations(agent, window)
    if _same_window_still_out(cfg, agent, observations[-1] if observations else None,
                              window, reset, at):
        return None
    length = timedelta(hours=cfg.window_hours(agent, window))
    if reset is not None and reset > at:
        start = min(reset - length, at)
    else:
        start, known = current_window(cfg, db, agent, window, at)
        reset = reset or known
    used = usage_between(db, agent, start, at)
    st = db.window_status(agent).get(window)  # the plan-wide utilization last reported
    db.set_window_status(agent, window, resets_at=iso(reset) if reset else None,
                         utilization=None, status="rejected", seen_at=iso(at))
    return db.insert_limit_observation({
        "agent": agent,
        "window": window,
        "limit_type": limit.limit_type,
        "hit_at": iso(at),
        "source": source,
        "task_id": task_id,
        "run_id": run_id,
        "resets_at": iso(reset) if reset else None,
        "window_start": iso(start),
        "cost_usd": used.cost_usd,
        "events_without_cost": used.events_without_cost,
        "input_tokens": used.input_tokens,
        "output_tokens": used.output_tokens,
        "cache_creation_tokens": used.cache_creation_tokens,
        "cache_read_tokens": used.cache_read_tokens,
        "requests": used.requests,
        "utilization": st["utilization"] if st is not None else None,
    })


@dataclass
class Baseline:
    cost_usd: float
    observations: int  # how many of the last 3 had a reported cost


def baseline(db: Database, agent: str, window: str) -> Baseline | None:
    """The learned capacity of a Window: the lowest reported cost-equivalent
    of its last 3 Limit observations. None until one exists.

    Observations with no own usage recorded (cost missing or 0: the Window
    was used up interactively) say nothing about capacity and are skipped.
    """
    usable = [o for o in db.limit_observations(agent, window) if o["cost_usd"]]
    costs = [o["cost_usd"] for o in usable[-BASELINE_OBSERVATIONS:]]
    if not costs:
        return None
    return Baseline(cost_usd=min(costs), observations=len(costs))


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


def probe(
    cfg: Config, db: Database, agent: str, *, adapter_provider: AdapterProvider = get_adapter,
    clock: Callable[[], datetime],
) -> ProbeReply:
    """Send ``agent`` its minimal probe request and record what it found:
    usage (``planning_usage``, kind ``probe``), Window status, the probe
    itself, and a Limit observation if it hit a limit in a new Window."""
    adapter = adapter_provider(agent, cfg.agent(agent))
    if hasattr(adapter, "clock"):
        adapter.clock = clock
    command = adapter.probe_command()
    workdir = cfg.data_dir / "probe"  # empty and Nightshift-owned, like planning calls
    workdir.mkdir(parents=True, exist_ok=True)
    timeout = cfg.usage("probe_timeout_seconds")
    try:
        proc = subprocess.run(
            command.argv, input=command.stdin, capture_output=True, text=True, cwd=workdir,
            env=os.environ.copy() | command.env, timeout=timeout,
        )
        reply = adapter.parse_probe_output(proc.stdout, proc.returncode)
        if reply.error and proc.stderr.strip():
            reply.error += ": " + proc.stderr.strip()[-300:]
    except OSError as exc:
        reply = ProbeReply(available=False, error=f"could not start {command.argv[0]}: {exc}")
    except subprocess.TimeoutExpired:
        reply = ProbeReply(available=False, error=f"no answer within {timeout:g} s")
    if reply.error:
        reply.error = cfg.secrets.redact(reply.error)[:500]
    at = clock()
    if reply.usage:
        db.record_planning_usage(None, "probe", agent, [u.as_dict() for u in reply.usage],
                                 at=iso(at))
    record_windows(db, agent, reply.windows, at)
    if reply.available:
        outcome, detail = "available", None
    elif reply.limit is not None:
        outcome, detail = "limited", reply.limit.limit_type
        record_limit_hit(cfg, db, agent, reply.limit, at=at, source="probe")
    else:
        outcome, detail = "error", reply.error
    db.record_probe(agent, at=iso(at), outcome=outcome,
                    resets_at=reply.limit.reset_at if reply.limit else None, detail=detail)
    return reply
