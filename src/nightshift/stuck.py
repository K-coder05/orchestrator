"""Stuck detection (§2.7, Phase 3).

An Attempt is **stuck** when either trigger fires:

- **Silence**: no new output from the agent and no file change in the Task
  folder for ``stuck.silence_minutes`` (15). A Step in flight with a command
  running under the agent is a long command, not silence: the command keeps
  the quiet clock from running out, and the runtime cap bounds it. With a
  Step in flight and **no** command running, an agent may set a shorter
  threshold (``AgentAdapter.stall_seconds``, Gemini's
  ``agents.gemini.stall_minutes``, 5): the stall guard of gemini-cli 0.65.0's
  headless sandbox-expansion hang, now one case of this trigger.
- **Repeats**: the same tool call (same tool, same input) ``stuck.repeat_calls``
  (3) times in a row.

A stuck Attempt is stopped at once (the Step in flight is the silent one or
the repeat, so there is nothing to wait for), its session and ``progress.md``
kept, and recorded in ``runs`` with outcome ``stuck`` and why. It is not a
Failed Attempt and never touches the retry budget. The first time the Task
is requeued to continue (same session when §2.6 allows, else fresh from
``plan.md`` + ``progress.md``) with a note saying what it was stuck on; the
second time it moves to ``needs_you`` (alert, inbox, report), where
``orch retry`` runs it again or ``orch cancel`` drops it.

The detectors take their clock from the caller, so they are tested with a
fake one.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from . import failures
from .config import Config
from .db import Database
from .states import TaskState
from .timeutil import iso

#: The Attempt outcome (``runs.outcome``) of a stuck Attempt.
OUTCOME = "stuck"
#: Stuck Attempts a Task may have before it needs you: retried once (§2.7).
LIMIT = 2
#: Once the quiet clock runs out, silence must hold this long more (at most)
#: before the Attempt counts as stuck: a command that just ended has its
#: result line on the way, and must not be read as silence.
CONFIRM_SECONDS = 10.0
#: Longest tool input quoted in a reason or note.
MAX_INPUT = 200


def call_text(name: Any, tool_input: Any) -> str:
    """A tool call as reasons and notes quote it: ``Bash {"command": "ls"}``."""
    text = json.dumps(tool_input, sort_keys=True, default=str, ensure_ascii=False)
    if len(text) > MAX_INPUT:
        text = text[: MAX_INPUT - 1] + "…"
    return f"`{name}` {text}"


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g} min"


# --------------------------------------------------------------------------
# The two triggers
# --------------------------------------------------------------------------


class RepeatWatch:
    """The repeat trigger: fed every tool call (Step) in order, it reports
    the same call ``limit`` times in a row."""

    def __init__(self, limit: int | None):
        self.limit = limit
        self.key: str | None = None
        self.count = 0

    def see(self, name: Any, tool_input: Any) -> str | None:
        """Count one tool call; why the Attempt is stuck, or None."""
        key = json.dumps([name, tool_input], sort_keys=True, default=str)
        self.count = self.count + 1 if key == self.key else 1
        self.key = key
        if self.limit and self.count >= self.limit:
            return (f"it made the same tool call {self.count} times in a row: "
                    f"{call_text(name, tool_input)}")
        return None


def folder_state(folder: Path) -> tuple[int, int]:
    """What changes when anything in the Task folder changes: the number of
    entries and the newest modification time (directories included, so a
    delete or rename counts). ``logs/`` is Nightshift's own and left out;
    symlinks are not followed."""
    count, newest = 0, 0
    for root, dirs, files in os.walk(folder):
        if Path(root) == folder:
            dirs[:] = [d for d in dirs if d != "logs"]
        for name in [*dirs, *files]:
            try:
                st = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            count += 1
            newest = max(newest, st.st_mtime_ns)
    return count, newest


class SilenceWatch:
    """The silence trigger, checked by the Attempt's watchdog about once a
    second with the time the agent last wrote a line.

    The folder is only scanned, and the processes under the agent only
    listed, once the quiet clock has run out: a change, or a command
    running for a Step in flight, restarts the clock (so a long command costs
    one scan per threshold). Silence must then hold for a short confirmation
    (:data:`CONFIRM_SECONDS`, at most the threshold) before it counts."""

    def __init__(self, folder: Path, *, silence_seconds: float | None,
                 stall_seconds: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 scan: Callable[[Path], Any] = folder_state):
        self.folder = folder
        self.silence_seconds = silence_seconds
        self.stall_seconds = stall_seconds
        self.clock = clock
        self.scan = scan
        self.active_at = clock()  # last file change or command seen
        self.state = scan(folder)
        self.suspect_at: float | None = None

    def threshold(self, inflight: bool) -> float | None:
        limits = [s for s in (self.silence_seconds,
                              self.stall_seconds if inflight else None) if s]
        return min(limits) if limits else None

    def check(self, *, last_output: float, inflight: bool,
              busy: Callable[[], bool]) -> str | None:
        """Why the Attempt is stuck, or None. ``busy()`` says whether a
        command runs under the agent (only asked with a Step in flight)."""
        limit = self.threshold(inflight)
        now = self.clock()
        if limit is None or now - max(last_output, self.active_at) < limit:
            self.suspect_at = None
            return None
        state = self.scan(self.folder)
        if state != self.state or (inflight and busy()):
            self.state, self.active_at, self.suspect_at = state, now, None
            return None
        if self.suspect_at is None:
            self.suspect_at = now
        if now - self.suspect_at < min(CONFIRM_SECONDS, limit):
            return None
        if inflight and self.stall_seconds and limit == self.stall_seconds:
            return (f"it went silent with a Step in flight and no command running: no output "
                    f"and no file changes for {_minutes(limit)} (stall guard)")
        return f"it went silent: no output and no file changes for {_minutes(limit)}"


# --------------------------------------------------------------------------
# After a stuck Attempt
# --------------------------------------------------------------------------

NOTE = """\
Nightshift: your previous session was stopped because it got stuck: {why}. \
Don't repeat what got you stuck. If a command hangs or a tool keeps \
returning the same result, try a different approach, or write what blocks \
you to `needs_you.json` and stop."""


def note(why: str, last_call: str | None = None) -> str:
    """What the next Attempt is told about the stuck one."""
    text = NOTE.format(why=why)
    if last_call and "same tool call" not in why:
        text += f" Your last tool call was {last_call}."
    return text


def resume_prompt(stuck_note: str, rest: str) -> str:
    """The prompt of an Attempt that resumes the stuck session."""
    return f"{stuck_note}\n\n{rest}"


def waiting(row: Mapping[str, Any]) -> bool:
    """Whether the Task in ``row`` is in ``needs_you`` for getting stuck again."""
    return row["state"] == TaskState.NEEDS_YOU and int(row["stuck_count"] or 0) >= LIMIT


def _needs_you_entry(task_id: str, attempt_no: int) -> str:
    return (f"- **Stuck:** Attempt {attempt_no} got stuck again after one retry "
            "(see section 8 for what it was stuck on). The work so far is in the Task folder. "
            f"`orch retry {task_id}` runs it again, `orch cancel {task_id}` drops it.")


def needs_you_lines(db: Database, task_id: str) -> list[str]:
    """The report's Needs you entry of a Task stopped for getting stuck again."""
    row = db.get_task(task_id)
    attempts = db.attempts(task_id)
    if row is None or int(row["stuck_count"] or 0) < LIMIT or not attempts:
        return []
    if attempts[-1]["outcome"] != OUTCOME:
        return []
    return [_needs_you_entry(task_id, attempts[-1]["attempt_no"])]


def report_lines(db: Database, task_id: str) -> list[str]:
    """Every stuck Attempt of the Task and why, for the report's section 8."""
    return [f"- Attempt {a['attempt_no']} ({a['agent']}): {a['end_reason']}"
            for a in db.attempts(task_id) if a["outcome"] == OUTCOME]


def after_stuck(cfg: Config, db: Database, task_id: str, *, why: str, note_text: str,
                attempt_no: int, at: datetime, run_id: int) -> TaskState:
    """Move the running Task on after its Attempt got stuck (already
    recorded in ``runs``): requeued to continue with ``note_text`` the first
    time, ``needs_you`` (final report written) the second."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    folder = Path(row["folder"])
    n = int(row["stuck_count"] or 0) + 1
    fields = {"stuck_count": n, "stuck_note": note_text}
    if n < LIMIT:
        reason = (f"Attempt {attempt_no} stuck: {why}; queued again to continue once, "
                  "told what it was stuck on (not charged to the retry budget)")
        reason = cfg.secrets.redact(reason)
        db.transition(task_id, TaskState.QUEUED, reason, run_id=run_id,
                      set_fields=fields | {"resume_pending": 1})
        failures.log_line(folder, f"{iso(at)} {reason}", cfg.secrets.redact)
        return TaskState.QUEUED
    reason = cfg.secrets.redact(f"Attempt {attempt_no} stuck again: {why}; it needs you")
    extras = failures.report_extras(cfg, db, task_id)
    extras["needs_you"] = [_needs_you_entry(task_id, attempt_no), *extras["needs_you"]]
    from . import report

    report.finalize(
        folder, title=row["title"], result=str(TaskState.NEEDS_YOU),
        result_detail=f"Attempt {attempt_no} got stuck a second time: {why}.",
        attempts=db.attempts(task_id), usage_rows=db.usage_for_task(task_id),
        redact=cfg.secrets.redact,
        appendix=failures._appendix(cfg, row), **extras,
    )
    failures.log_line(folder, f"{iso(at)} {reason}", cfg.secrets.redact)
    db.transition(task_id, TaskState.NEEDS_YOU, reason, run_id=run_id,
                  set_fields=fields | {"resume_pending": 1})
    return TaskState.NEEDS_YOU
