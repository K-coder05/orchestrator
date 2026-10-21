"""Read-only views of Tasks for the CLI: ``orch list|show|logs|report``."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path

from .adapters import get_adapter
from .adapters.base import Event, EventKind
from .config import Config
from .db import Database
from .states import TaskState


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


def format_list(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "No Tasks."
    return _table(
        ["ID", "STATE", "PRIORITY", "TYPE", "AGENT", "CREATED", "TITLE"],
        [
            [
                r["id"],
                r["state"],
                r["priority"],
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
    "long estimated_minutes max_runtime_minutes approved approved_at schedule_name created_at "
    "state_changed_at folder"
).split()


def format_show(db: Database, row: sqlite3.Row) -> str:
    out = [f"Task {row['id']}"]
    width = max(len(f) for f in SHOW_FIELDS)
    for f in SHOW_FIELDS:
        value = row[f]
        if f in ("agent_override", "long", "approved"):
            value = "yes" if value else "no"
        if value is None:
            continue
        out.append(f"  {f.ljust(width)}  {value}")
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

