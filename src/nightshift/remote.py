"""Telegram commands (§5 item 1, §2.12): Nightshift as a remote control.

The daemon's long poll (``alerts.Notifier.poll_updates``) hands every text
message from ``telegram.user_id`` to :func:`run`, which answers through
``reply``. Messages from anyone else never get here.

| Command                 | Does                                              |
|-------------------------|---------------------------------------------------|
| ``status``              | daemon up or down, running Task, queue, needs you |
| ``list [state ...]``    | Tasks grouped by status, optionally some states   |
| ``approve <id>``        | approve a Task awaiting approval                  |
| ``deny <id>``           | deny it (cancelled)                               |
| ``cancel <id>``         | cancel an unfinished Task                         |
| ``pause <id>``          | pause it until resumed                            |
| ``resume <id>``         | queue a paused Task now                           |
| ``retry <id>``          | retry a failed or partial Task                    |
| ``priority <id> <P>``   | set its Priority (urgent, normal, background)     |
| ``help``                | these commands                                    |

A leading ``/`` (and a ``@botname`` suffix) is optional; case doesn't
matter. Every change goes through ``nightshift.control`` with ``via:
telegram``, so it is checked, recorded and counted for the clean-week check
exactly as the same ``orch`` command would be.

Comedy Tasks show their id and state only, never their title (§1.6). The
control layer's messages are built from ids, states and fixed reasons, so
they carry no Task content either. The caller redacts secrets from every
reply.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from . import alerts, control, daemon, queries, views
from . import priority as prio
from .config import Config
from .db import Database
from .states import TERMINAL, TaskState

#: Telegram refuses messages longer than this.
MAX_MESSAGE = 4096

#: How many of the newest finished Tasks a ``list`` group shows.
FINISHED_LIMIT = 10

#: Longest title a ``list`` or ``status`` line shows.
TITLE_CHARS = 50

HELP = """Nightshift commands:
status - daemon, running Task, queue, needs you
list [state ...] - Tasks by status (e.g. list queued failed)
approve <id> - approve a Task awaiting approval
deny <id> - deny it (it is cancelled)
cancel <id> - cancel an unfinished Task
pause <id> - pause a Task until you resume it
resume <id> - queue a paused Task now
retry <id> - retry a failed or partial Task
priority <id> <urgent|normal|background> - set its Priority
help - this list
A leading / is optional."""

#: Commands that take a Task id, and what else they take.
USAGE = {
    "approve": "approve <id>",
    "deny": "deny <id>",
    "cancel": "cancel <id>",
    "pause": "pause <id>",
    "resume": "resume <id>",
    "retry": "retry <id>",
    "priority": "priority <id> <urgent|normal|background>",
}

Reply = Callable[[str], None]


def parse(text: str) -> tuple[str, list[str]]:
    """The command word (lower case, no ``/`` or ``@botname``) and its arguments."""
    words = text.split()
    if not words:
        return "", []
    verb = words[0].lower().removeprefix("/").split("@", 1)[0]
    return verb, words[1:]


def run(cfg: Config, db: Database, text: str, *, at: datetime, reply: Reply,
        cancel_wait: float | None = None, pause_wait: float | None = None) -> str:
    """Carry out one command and answer it through ``reply`` (once, or twice
    for a cancel or pause the daemon carries out). Returns the command word
    for the log, or ``unknown``; never the message itself.

    ``cancel_wait`` and ``pause_wait`` bound the wait for the daemon (default
    ``control``'s own bounds)."""
    verb, args = parse(text)
    if verb in ("help", "start"):
        reply(HELP)
    elif verb == "status":
        reply(status_text(cfg, db))
    elif verb == "list":
        reply(list_text(cfg, db, at, args))
    elif verb in USAGE:
        _control(cfg, db, verb, args, at=at, reply=reply, cancel_wait=cancel_wait,
                 pause_wait=pause_wait)
    else:
        reply("Unknown command. Send help for the list.")
        return "unknown"
    return verb


def _control(cfg: Config, db: Database, verb: str, args: list[str], *, at: datetime,
             reply: Reply, cancel_wait: float | None, pause_wait: float | None) -> None:
    want = 2 if verb == "priority" else 1
    if len(args) != want:
        reply(f"Usage: {USAGE[verb]}")
        return
    tid = args[0]
    via = control.VIA_TELEGRAM
    if verb == "approve":
        res = control.approve(cfg, db, tid, via=via)
    elif verb == "deny":
        res = control.deny(cfg, db, tid, via=via)
    elif verb == "retry":
        res = control.retry(cfg, db, tid, via=via, at=at)
    elif verb == "resume":
        res = control.resume(cfg, db, tid, via=via, at=at)
    elif verb == "priority":
        level = args[1].lower()
        if level not in prio.PRIORITIES:
            reply(f"Unknown priority {args[1]!r}. Usage: {USAGE[verb]}")
            return
        res = control.set_priority(cfg, db, tid, level, via=via, at=at)
    elif verb == "cancel":
        res = control.cancel(cfg, db, tid, via=via, at=at)
        reply(result_text(res))
        if res.action == control.CANCEL_REQUESTED:
            reply(result_text(control.wait_for_cancel(cfg, db, tid, timeout=cancel_wait)))
        return
    else:  # pause
        res = control.pause(cfg, db, tid, via=via, at=at)
        reply(result_text(res))
        if res.action == control.PAUSE_REQUESTED:
            reply(result_text(control.wait_for_pause(cfg, db, tid, timeout=pause_wait)))
        return
    reply(result_text(res))


# `orch resume k3f9` in a control message is `/resume k3f9` here.
_ORCH_COMMAND = re.compile(r"`orch ((?:%s)\b[^`]*)`" % "|".join(USAGE))


def result_text(res: control.Result) -> str:
    """A control's outcome, or its refusal, worded for Telegram."""
    text = res.message if res.ok else f"Not done: {res.message}"
    if res.note:
        text += f"\n{res.note}"
    return _ORCH_COMMAND.sub(r"/\1", text)


def _status_only(task_type: str) -> bool:
    return task_type in alerts.STATUS_ONLY_TYPES


def _task_line(t: queries.TaskSummary) -> str:
    if _status_only(t.task_type):
        return f"{t.id} {t.state}"
    title = t.title if len(t.title) <= TITLE_CHARS else t.title[: TITLE_CHARS - 1] + "…"
    pri = views._priority_cell(t.priority, t.effective_priority)
    return f"{t.id} {t.state} {pri} {t.task_type}: {title}"


def status_text(cfg: Config, db: Database) -> str:
    up = daemon.daemon_running(cfg)
    lines = [f"Nightshift: daemon {'up' if up else 'down'}"]
    running = db.tasks_in_state(TaskState.RUNNING)
    if not running:
        lines.append("Running: nothing")
    for r in running:
        if _status_only(r["task_type"]):
            lines.append(f"Running: {r['id']} {r['state']}")
        else:
            lines.append(f"Running: {r['id']} {r['task_type']}: {r['title'][:TITLE_CHARS]}")
    counts, groups = views.queue_summary(db), dict(queries.STATUS_GROUPS)
    waiting = sum(counts.get(str(s), 0) for s in groups["queued"])
    paused = sum(counts.get(str(s), 0) for s in groups["paused"])
    lines.append(f"Queue: {waiting} waiting to run ({counts.get('queued', 0)} queued, "
                 f"{counts.get('retry_wait', 0)} waiting to retry, "
                 f"{counts.get('scheduled', 0)} scheduled), {paused} paused")
    lines.append(f"Needs you: {len(queries.needs_you_inbox(cfg, db))} open")
    return "\n".join(lines)


def list_text(cfg: Config, db: Database, at: datetime, args: list[str]) -> str:
    """Tasks in the dashboard's status groups, newest first; ``args`` are
    states to show only (as ``orch list --status``)."""
    names = {str(s) for s in TaskState}
    bad = [a for a in args if a.lower() not in names]
    if bad:
        return (f"Unknown state {bad[0]!r}. Usage: list [state ...], states: "
                + ", ".join(str(s) for s in TaskState))
    wanted = {TaskState(a.lower()) for a in args}
    tasks = queries.task_list(cfg, db, at, sorted(wanted) or None)
    lines: list[str] = []
    for name, states in queries.STATUS_GROUPS:
        members = [t for t in tasks if t.state in states]
        if not members:
            continue
        shown = members[:FINISHED_LIMIT] if states <= TERMINAL else members
        more = f", newest {len(shown)} shown" if len(shown) < len(members) else ""
        lines.append(f"{name.capitalize()} ({len(members)}{more})")
        lines += [f"  {_task_line(t)}" for t in shown]
    if not lines:
        return "No Tasks." if not wanted else "No Tasks in " + ", ".join(sorted(wanted)) + "."
    return clip("\n".join(lines))


def clip(text: str) -> str:
    """``text`` cut at a line to fit one Telegram message."""
    if len(text) <= MAX_MESSAGE:
        return text
    tail = "\n… more not shown; orch list has them all."
    cut = text[: MAX_MESSAGE - len(tail)].rsplit("\n", 1)[0]
    return cut + tail
