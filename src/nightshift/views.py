"""Read-only views of Tasks for the CLI: ``orch list|show|logs|report``."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import priority, usage
from .adapters import get_adapter
from .adapters.base import Event, EventKind
from .config import Config
from .db import Database
from .states import TERMINAL, TaskState
from .timeutil import iso, parse_iso


def _short_time(value: str | None) -> str:
    # 2026-10-20T01:02:03-07:00 -> 2026-10-20 01:02
    return value[:16].replace("T", " ") if value else "-"


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for r in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, r)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths)).rstrip()]
    for r in rows:
        lines.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    return "\n".join(lines)


def _priority_cell(r: sqlite3.Row, cfg: Config | None, now: datetime | None) -> str:
    """The assigned Priority, plus the Effective one when a deadline raised it."""
    if cfg is None or now is None or TaskState(r["state"]) in TERMINAL:
        return r["priority"]
    eff = priority.evaluate(cfg, r, now).effective
    return r["priority"] if eff == r["priority"] else f"{r['priority']}>{eff}"


def format_list(
    rows: list[sqlite3.Row], *, cfg: Config | None = None, now: datetime | None = None
) -> str:
    if not rows:
        return "No Tasks."
    return _table(
        ["ID", "STATE", "PRIORITY", "TYPE", "AGENT", "CREATED", "TITLE"],
        [
            [
                r["id"],
                r["state"],
                _priority_cell(r, cfg, now),
                r["task_type"],
                r["agent"],
                _short_time(r["created_at"]),
                r["title"][:60],
            ]
            for r in rows
        ],
    )


SHOW_FIELDS = (
    "title state priority task_type agent agent_override internet template deadline run_at "
    "long estimated_minutes max_runtime_minutes retries_used retry_at simulate_rate_limit_after "
    "simulated_limit_at pause_kind pause_limit_type pause_reset_at resume_at approved approved_at "
    "schedule_name created_at state_changed_at folder"
).split()


def format_show(
    db: Database, row: sqlite3.Row, *, cfg: Config | None = None, now: datetime | None = None
) -> str:
    out = [f"Task {row['id']}"]
    width = max(len(f) for f in [*SHOW_FIELDS, "effective_priority"])
    ev = None
    if cfg is not None and now is not None and TaskState(row["state"]) not in TERMINAL:
        ev = priority.evaluate(cfg, row, now)
    for f in SHOW_FIELDS:
        value = row[f]
        if f in ("agent_override", "long", "approved"):
            value = "yes" if value else "no"
        if f == "retries_used":
            if not value:
                continue
            value = f"{value} of {cfg.max_retries}" if cfg is not None else value
        if value is None:
            continue
        out.append(f"  {f.ljust(width)}  {value}")
        if f == "priority" and ev is not None:
            out.append(f"  {'effective_priority'.ljust(width)}  {ev.effective}")
    if ev is not None and ev.blocked and row["state"] == str(TaskState.QUEUED):
        out += ["", f"Waiting: {ev.blocked}"]
    if row["state"] == str(TaskState.RETRY_WAIT) and row["retry_at"]:
        out += ["", f"Waiting: retry {row['retries_used']} at {row['retry_at']} "
                f"(`orch retry {row['id']}` starts it now)"]
    if row["state"] == str(TaskState.PAUSED_USAGE):
        if row["resume_at"]:
            out += ["", f"Waiting: paused ({row['pause_kind']}), queued again at "
                    f"{row['resume_at']} (`orch resume {row['id']}` queues it now)"]
        else:
            out += ["", f"Waiting: paused by hand until `orch resume {row['id']}`"]
    if row["resume_pending"] and TaskState(row["state"]) not in TERMINAL:
        out += ["", "Next Attempt continues paused work (same session if under "
                f"{cfg.usage('resume_session_max_hours') if cfg else 24:g}h old)"]
    if row["pause_requested_at"] and row["state"] == str(TaskState.RUNNING):
        out += ["", f"Pause requested at {row['pause_requested_at']}"]
    if row["cancel_requested_at"] and TaskState(row["state"]) not in TERMINAL:
        out += ["", f"Cancel requested at {row['cancel_requested_at']}"]
    out += ["", "Prompt:", *(f"  {line}" for line in row["prompt"].splitlines() or [""])]

    attempts = db.attempts(row["id"])
    out += ["", "Attempts:"]
    if attempts:
        table = _table(
            ["#", "AGENT", "STARTED", "ENDED", "EXIT", "OUTCOME", "REASON"],
            [
                [
                    str(a["attempt_no"]),
                    a["agent"],
                    _short_time(a["started_at"]),
                    _short_time(a["ended_at"]),
                    "-" if a["exit_code"] is None else str(a["exit_code"]),
                    a["outcome"] or ("running" if a["ended_at"] is None else "-"),
                    (a["end_reason"] or "")[:80],
                ]
                for a in attempts
            ],
        )
        out += [f"  {line}" for line in table.splitlines()]
    else:
        out.append("  none yet")

    approvals = db.approvals(row["id"])
    if approvals:
        out += ["", "Approvals:"]
        for a in approvals:
            out.append(f"  {_short_time(a['at'])}  {a['decision']} via {a['via']}")

    switches = db.failovers(row["id"])
    if switches:
        out += ["", "Failovers:"]
        for f in switches:
            out.append(f"  {_short_time(f['at'])}  {f['from_agent']} -> {f['to_agent']} "
                       f"({f['trigger']}" + (", deadline-driven" if f["deadline_driven"] else "")
                       + ")")

    out += ["", "History:"]
    for t in db.transitions(row["id"]):
        frm = t["from_state"] or "(new)"
        out.append(f"  {_short_time(t['at'])}  {frm} -> {t['to_state']}: {t['reason']}")
    return "\n".join(out)


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def format_event(ev: Event) -> str | None:
    """One readable line for an agent event, or None to skip it."""
    if ev.kind == EventKind.TOOL_CALL:
        return f"[step] {ev.data.get('name')}"
    if ev.kind == EventKind.TEXT and not ev.data.get("raw"):
        lines = ev.data.get("text", "").strip().splitlines()
        return f"[agent] {lines[0][:200]}" if lines else None
    if ev.kind == EventKind.TEXT and ev.data.get("raw"):
        text = str(ev.data.get("text", "")).strip()
        return f"[output] {text[:200]}" if text else None
    if ev.kind == EventKind.FILE_CHANGE:
        return f"[file] {ev.data.get('path')}"
    if ev.kind == EventKind.ERROR:
        return f"[error] {ev.data.get('message')}"
    if ev.kind == EventKind.RESULT:
        return "[result] " + ("error" if ev.data.get("is_error") else "ok")
    return None


def _stream_path(folder: Path, attempt: sqlite3.Row) -> Path | None:
    if not attempt["log_path"]:
        return None
    path = Path(attempt["log_path"])
    return path if path.is_absolute() else folder / path


def task_logs(
    cfg: Config,
    db: Database,
    task_id: str,
    *,
    follow: bool = False,
    raw: bool = False,
    interval: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> Iterator[str]:
    """Nightshift's log lines for the Task plus every Attempt's agent stream,
    rendered readably (``raw`` gives the stream's JSON lines as written).

    With ``follow`` it keeps printing new output, including new Attempts,
    until the Task is no longer queued or running (Ctrl-C stops it earlier).
    Only whole lines are printed; a partial last line waits for the next pass.
    """
    read_upto: dict[str, int] = {}  # file -> bytes already printed
    adapters: dict[int, object | None] = {}

    def new_lines(path: Path) -> list[str]:
        if not path.exists():
            return []
        with open(path, "rb") as f:
            f.seek(read_upto.get(str(path), 0))
            chunk = f.read()
        cut = chunk.rfind(b"\n") + 1
        read_upto[str(path)] = read_upto.get(str(path), 0) + cut
        return chunk[:cut].decode(errors="replace").splitlines()

    while True:
        row = db.get_task(task_id)
        folder = Path(row["folder"])
        for line in new_lines(folder / "logs" / "nightshift.log"):
            yield f"[nightshift] {line}"
        for a in db.attempts(task_id):
            path = _stream_path(folder, a)
            if path is None:
                continue
            if a["id"] not in adapters:
                yield f"== Attempt {a['attempt_no']} ({a['agent']}) =="
                adapter = None
                if not raw:
                    try:
                        adapter = get_adapter(a["agent"], cfg.agent(a["agent"]))
                    except Exception:  # unknown agent: fall back to raw lines
                        adapter = None
                adapters[a["id"]] = adapter
            adapter = adapters[a["id"]]
            for line in new_lines(path):
                if adapter is None:
                    yield line
                    continue
                for ev in adapter.parse_stream(line):  # type: ignore[attr-defined]
                    text = format_event(ev)
                    if text:
                        yield text
        state = TaskState(row["state"])
        if not follow:
            return
        if state not in (TaskState.QUEUED, TaskState.RUNNING):
            yield f"Task {task_id} is {state}."
            return
        sleep(interval)


def queue_summary(db: Database) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in db.conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state"):
        counts[r["state"]] = r["n"]
    return counts



# --------------------------------------------------------------------------
# orch usage
# --------------------------------------------------------------------------

USAGE_NOTE = (
    "Nightshift counts only its own usage (Attempts, planning calls, probes). Interactive use "
    "of the same plan is invisible to it, so recorded usage undercounts each Window and "
    "Baselines err low.")


def _local(value: str | None, tz: str) -> str:
    if not value:
        return "-"
    return parse_iso(value).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def _cost(value: float | None) -> str:
    return "not reported" if value is None else f"${value:.4f}"


def usage_agents(cfg: Config, db: Database) -> list[str]:
    """Configured agents first, then any other agent with recorded usage."""
    names = list((cfg.data.get("agents") or {}).keys())
    return names + sorted(a for a in db.usage_agents() if a not in names)


def format_usage(cfg: Config, db: Database, now: datetime) -> str:
    """Usage per agent and Window, and whether each Window has a Baseline."""
    tz = cfg.timezone
    out: list[str] = []
    for agent in usage_agents(cfg, db):
        statuses = db.window_status(agent)
        observed = {o["window"] for o in db.limit_observations(agent)}
        windows = list(cfg.windows(agent))
        windows += sorted((set(statuses) | observed) - set(windows))
        rows = []
        for w in windows:
            start, reset = usage.current_window(cfg, db, agent, w, now)
            used = usage.usage_between(db, agent, start, now)
            period = (f"{_local(iso(start), tz)} to {_local(iso(reset), tz)}"
                      if reset else f"last {cfg.window_hours(agent, w):g}h (reset not known)")
            st = statuses.get(w)
            said = "-"
            if st is not None and st["utilization"] is not None:
                said = f"{st['utilization'] * 100:.0f}% used"
            if st is not None and st["status"] == "rejected" and reset:
                said = "out of usage" + (f", {said}" if said != "-" else "")
            n = len(db.limit_observations(agent, w))
            base = usage.baseline(db, agent, w)
            base_cell = (f"{_cost(base.cost_usd)} (lowest of last {base.observations})"
                         if base else f"none yet ({n} observation{'s' if n != 1 else ''})")
            cost = ("$0.0000" if used.events == 0 else _cost(used.cost_usd)) + (f" +{used.events_without_cost} w/o cost"
                                           if used.events_without_cost else "")
            rows.append([w, period, cost, f"{used.input_tokens:,}", f"{used.output_tokens:,}",
                         f"{used.cache_creation_tokens:,}", f"{used.cache_read_tokens:,}",
                         f"{used.requests:,}", said, base_cell])
        out.append(f"{agent}")
        out.append(_table(["  WINDOW", "CURRENT PERIOD", "COST-EQUIVALENT", "INPUT", "OUTPUT",
                           "CACHE WRITE", "CACHE READ", "REQUESTS", "AGENT REPORTS", "BASELINE"],
                          [["  " + r[0], *r[1:]] for r in rows]))
        obs = db.limit_observations(agent)[-5:]
        if obs:
            out.append("  Limit observations (latest last):")
            for o in obs:
                extra = (f", {o['events_without_cost']} usage rows without cost"
                         if o["events_without_cost"] else "")
                where = f" Task {o['task_id']}" if o["task_id"] else ""
                out.append(f"    {_local(o['hit_at'], tz)}  {o['window']}  {_cost(o['cost_usd'])}"
                           f"{extra} since {_local(o['window_start'], tz)}; resets "
                           f"{_local(o['resets_at'], tz)}; from {o['source']}{where}")
        probes = db.probes(agent)
        if probes:
            p = probes[-1]
            out.append(f"  Last probe: {_local(p['at'], tz)} {p['outcome']}"
                       + (f" ({p['detail']})" if p["detail"] else ""))
        paused = [r for r in db.tasks_in_state(TaskState.PAUSED_USAGE)
                  if r["agent"] == agent and r["pause_kind"] == "usage_limit"]
        for r in paused:
            when = (f"resets {_local(r['resume_at'], tz)}" if r["pause_reset_at"]
                    else f"next probe {_local(r['resume_at'], tz)}")
            out.append(f"  Paused on its limit: Task {r['id']} ({when})")
        out.append("")
    out.append(USAGE_NOTE)
    return "\n".join(out)


def format_schedules(cfg: Config, db: Database, now: datetime) -> str:
    """`orch schedule list`: each Schedule, its rules, next fire and last decision."""
    from . import schedules  # late: schedules imports the runner

    items = schedules.all_schedules(db)
    if not items:
        return "No Schedules. Add one with `orch schedule add`."
    blocks = []
    for s in items:
        nxt = schedules.next_fire(cfg, s, now) if s.enabled else None
        last = schedules.last_run(db, s)
        lines = [
            f"{s.name}  {'enabled' if s.enabled else 'disabled'}",
            f"  rule       {s.cron.expr}  ({cfg.timezone})",
            f"  template   {s.template}" + (f"  request: {s.request}" if s.request else ""),
            f"  catch-up   {s.catch_up}",
            f"  overlap    {s.overlap}",
        ]
        if s.priority or s.agent:
            lines.append("  settings   " + ", ".join(
                x for x in (s.priority and f"priority {s.priority}", s.agent and f"agent {s.agent}")
                if x))
        lines.append(f"  next fire  {_local(iso(nxt), cfg.timezone) if nxt else '-'}")
        if last is not None:
            state = ""
            if last["task_id"]:
                row = db.get_task(last["task_id"])
                state = f" ({row['state']})" if row is not None else ""
            what = schedules.Decision(
                schedule=s.name, trigger=last["trigger"], fire_at=last["fire_at"],
                fires=last["fires"], outcome=last["outcome"], task_id=last["task_id"],
            ).describe().split(": ", 1)[1]
            lines.append(f"  last       {_local(last['decided_at'], cfg.timezone)}  {what}{state}")
        else:
            lines.append("  last       never fired")
        open_ = schedules.unfinished_occurrences(db, s.name)
        lines.append("  open       " + (", ".join(f"{r['id']} ({r['state']})" for r in open_)
                                        if open_ else "none"))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
