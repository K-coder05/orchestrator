"""Text rendering for the CLI: ``orch list|show|logs|usage`` and ``orch schedule list``.

The data comes from ``queries``; ``task_logs`` reads the log files itself."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from zoneinfo import ZoneInfo

from . import queries, spend
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


def _priority_cell(assigned: str, effective: str | None) -> str:
    """The assigned Priority, plus the Effective one when a deadline raised it."""
    return assigned if effective in (None, assigned) else f"{assigned}>{effective}"


def format_list(tasks: list[queries.TaskSummary]) -> str:
    if not tasks:
        return "No Tasks."
    return _table(
        ["ID", "STATE", "PRIORITY", "TYPE", "AGENT", "CREATED", "TITLE"],
        [
            [
                t.id,
                str(t.state),
                _priority_cell(t.priority, t.effective_priority),
                t.task_type,
                t.agent,
                _short_time(t.created_at),
                t.title[:60],
            ]
            for t in tasks
        ],
    )


SHOW_FIELDS = (
    "title state priority task_type agent agent_override internet template deadline run_at "
    "long estimated_minutes max_runtime_minutes retries_used retry_at simulate_rate_limit_after "
    "simulated_limit_at pause_kind pause_limit_type pause_reset_at resume_at approved approved_at "
    "schedule_name created_at state_changed_at folder"
).split()


def format_show(d: queries.TaskDetail) -> str:
    row = d.task
    out = [f"Task {d.id}"]
    width = max(len(f) for f in [*SHOW_FIELDS, "effective_priority"])
    open_ = d.state not in TERMINAL
    for f in SHOW_FIELDS:
        value = row[f]
        if f in ("agent_override", "long", "approved"):
            value = "yes" if value else "no"
        if f == "retries_used":
            if not value:
                continue
            value = f"{value} of {d.max_retries}"
        if value is None:
            continue
        out.append(f"  {f.ljust(width)}  {value}")
        if f == "priority" and d.effective_priority is not None:
            out.append(f"  {'effective_priority'.ljust(width)}  {d.effective_priority}")
    if d.blocked:
        out += ["", f"Waiting: {d.blocked}"]
    if d.state == TaskState.RETRY_WAIT and row["retry_at"]:
        out += ["", f"Waiting: retry {row['retries_used']} at {row['retry_at']} "
                f"(`orch retry {d.id}` starts it now)"]
    if d.state == TaskState.PAUSED_USAGE:
        if row["resume_at"]:
            out += ["", f"Waiting: paused ({row['pause_kind']}), queued again at "
                    f"{row['resume_at']} (`orch resume {d.id}` queues it now)"]
        else:
            out += ["", f"Waiting: paused by hand until `orch resume {d.id}`"]
    if row["resume_pending"] and open_:
        out += ["", "Next Attempt continues paused work (same session if under "
                f"{d.resume_session_max_hours:g}h old)"]
    if row["pause_requested_at"] and d.state == TaskState.RUNNING:
        out += ["", f"Pause requested at {row['pause_requested_at']}"]
    if row["cancel_requested_at"] and open_:
        out += ["", f"Cancel requested at {row['cancel_requested_at']}"]
    out += ["", "Prompt:", *(f"  {line}" for line in row["prompt"].splitlines() or [""])]

    out += ["", "Attempts:"]
    if d.attempts:
        table = _table(
            ["#", "AGENT", "STARTED", "ENDED", "EXIT", "OUTCOME", "REASON"],
            [
                [
                    str(a.attempt_no),
                    a.agent,
                    _short_time(a.started_at),
                    _short_time(a.ended_at),
                    "-" if a.exit_code is None else str(a.exit_code),
                    a.outcome or ("running" if a.running else "-"),
                    (a.end_reason or "")[:80],
                ]
                for a in d.attempts
            ],
        )
        out += [f"  {line}" for line in table.splitlines()]
    else:
        out.append("  none yet")

    if d.approvals:
        out += ["", "Approvals:"]
        for a in d.approvals:
            out.append(f"  {_short_time(a.at)}  {a.decision} via {a.via}")

    if d.failovers:
        out += ["", "Failovers:"]
        for fo in d.failovers:
            out.append(f"  {_short_time(fo.at)}  {fo.from_agent} -> {fo.to_agent} "
                       f"({fo.trigger}" + (", deadline-driven" if fo.deadline_driven else "")
                       + ")")

    out += ["", "History:"]
    for t in d.transitions:
        frm = t.from_state or "(new)"
        out.append(f"  {_short_time(t.at)}  {frm} -> {t.to_state}: {t.reason}")
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


def format_credits(c: spend.CreditView, per_task_cap: float) -> list[str]:
    """AI Studio credits: spend so far, what is left, expiry (§1.5)."""
    as_of = f" (balance as of {c.balance_as_of})" if c.balance_as_of else ""
    left = f"${c.remaining:.2f} left of ${c.balance:.2f}{as_of}"
    if c.expiry:
        left += f"; {'expired' if c.expired else 'expires'} {c.expiry}"
    return ["Gemini API credits (paid, agent gemini-api)",
            f"  spent ${c.spent_total:.2f} so far; {left}",
            f"  per-Task cap ${per_task_cap:.2f}", ""]


def format_usage(report: list[queries.AgentUsage], tz: str,
                 credits: tuple[spend.CreditView, float] | None = None) -> str:
    """Usage per agent and Window, and whether each Window has a Baseline;
    with ``credits`` (credit view, per-Task cap), the paid credits too."""
    out: list[str] = []
    for agent in report:
        rows = []
        for w in agent.windows:
            used = w.used
            period = (f"{_local(iso(w.start), tz)} to {_local(iso(w.resets_at), tz)}"
                      if w.resets_at else f"last {w.hours:g}h (reset not known)")
            said = "-" if w.utilization is None else f"{w.utilization * 100:.0f}% used"
            if w.out_of_usage:
                said = "out of usage" + (f", {said}" if said != "-" else "")
            n = w.observation_count
            base = w.baseline
            base_cell = (f"{_cost(base.cost_usd)} (lowest of last {base.observations})"
                         if base else f"none yet ({n} observation{'s' if n != 1 else ''})")
            cost = ("$0.0000" if used.events == 0 else _cost(used.cost_usd)) + (f" +{used.events_without_cost} w/o cost"
                                           if used.events_without_cost else "")
            rows.append([w.window, period, cost, f"{used.input_tokens:,}", f"{used.output_tokens:,}",
                         f"{used.cache_creation_tokens:,}", f"{used.cache_read_tokens:,}",
                         f"{used.requests:,}", said, base_cell])
        out.append(f"{agent.agent}")
        out.append(_table(["  WINDOW", "CURRENT PERIOD", "COST-EQUIVALENT", "INPUT", "OUTPUT",
                           "CACHE WRITE", "CACHE READ", "REQUESTS", "AGENT REPORTS", "BASELINE"],
                          [["  " + r[0], *r[1:]] for r in rows]))
        if agent.recent_observations:
            out.append("  Limit observations (latest last):")
            for o in agent.recent_observations:
                extra = (f", {o.events_without_cost} usage rows without cost"
                         if o.events_without_cost else "")
                where = f" Task {o.task_id}" if o.task_id else ""
                out.append(f"    {_local(o.hit_at, tz)}  {o.window}  {_cost(o.cost_usd)}"
                           f"{extra} since {_local(o.window_start, tz)}; resets "
                           f"{_local(o.resets_at, tz)}; from {o.source}{where}")
        p = agent.last_probe
        if p is not None:
            out.append(f"  Last probe: {_local(p.at, tz)} {p.outcome}"
                       + (f" ({p.detail})" if p.detail else ""))
        for t in agent.paused:
            when = (f"resets {_local(t.resume_at, tz)}" if t.reset_known
                    else f"next probe {_local(t.resume_at, tz)}")
            out.append(f"  Paused on its limit: Task {t.task_id} ({when})")
        out.append("")
    if credits is not None:
        out += format_credits(*credits)
    out.append(USAGE_NOTE)
    return "\n".join(out)


def format_schedules(items: list[queries.ScheduleView], tz: str) -> str:
    """`orch schedule list`: each Schedule, its rules, next fire and last decision."""
    if not items:
        return "No Schedules. Add one with `orch schedule add`."
    blocks = []
    for v in items:
        s = v.schedule
        lines = [
            f"{s.name}  {'enabled' if s.enabled else 'disabled'}",
            f"  rule       {s.cron.expr}  ({tz})",
            f"  template   {s.template}" + (f"  request: {s.request}" if s.request else ""),
            f"  catch-up   {s.catch_up}",
            f"  overlap    {s.overlap}",
        ]
        if s.priority or s.agent:
            lines.append("  settings   " + ", ".join(
                x for x in (s.priority and f"priority {s.priority}", s.agent and f"agent {s.agent}")
                if x))
        lines.append(f"  next fire  {_local(iso(v.next_fire), tz) if v.next_fire else '-'}")
        if v.last is not None:
            state = f" ({v.last.task_state})" if v.last.task_state is not None else ""
            what = v.last.decision.describe().split(": ", 1)[1]
            lines.append(f"  last       {_local(v.last.decided_at, tz)}  {what}{state}")
        else:
            lines.append("  last       never fired")
        lines.append("  open       " + (", ".join(f"{o.task_id} ({o.state})"
                                                  for o in v.open_occurrences)
                                        if v.open_occurrences else "none"))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
