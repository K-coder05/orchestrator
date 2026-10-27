"""Creating Tasks and running Attempts (one Attempt per call)."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from . import (failover, failures, needsyou, pauses, report, school, spend, stuck, templates,
               usage)
from .adapters import get_adapter, load_api_key
from .adapters.base import AgentAdapter, EventKind, RateLimit, TaskContext
from .config import Config, ConfigError
from .db import Database
from .failures import PostHook
from .ids import new_task_id, slugify, title_from_prompt
from .permissions import build_policy
from .protocol import agent_protocol
from .states import TaskState
from .timeutil import iso, now
from .workspace import PLAN_PENDING, PLAN_STUB, TaskSpec, create_task_folder, occurrence_folder

AdapterProvider = Callable[[str, dict], AgentAdapter]


@dataclass
class NewTask:
    spec: TaskSpec
    folder: Path


def create_task(
    cfg: Config,
    db: Database,
    prompt: str,
    *,
    task_type: str | None = None,
    agent: str | None = None,
    priority: str | None = None,
    deadline: str | None = None,
    run_at: str | None = None,
    long: bool | None = None,
    internet: str | None = None,
    estimated_minutes: int | None = None,
    template: str | None = None,
    max_runtime_minutes: int | None = None,
    needs_approval: bool = False,
    simulate_rate_limit_after: int | None = None,
    reason: str = "created by orch add",
    title: str | None = None,
    agent_override: bool | None = None,
    schedule_name: str | None = None,
    occurrence_date: date | None = None,
    course: str | None = None,
) -> NewTask:
    """Create a Task: DB row, Task folder, ``task.yaml``.

    It starts ``queued`` and approved, or ``awaiting_approval`` (not approved)
    when ``needs_approval`` is set because its estimate is over the plan
    threshold (§2.4; see ``nightshift.planning``). Planning happens before
    this is called.

    ``deadline`` and ``run_at`` are ISO 8601 timestamps (see
    ``nightshift.priority.parse_deadline``/``parse_at``). ``long`` defaults to
    the Task type's ``long`` setting. Only the assigned ``priority`` is
    stored; the Effective priority is computed by the queue (§2.3).
    ``max_runtime_minutes`` (a template's cap) defaults to the Task type's
    cap, else ``defaults.max_runtime_minutes`` (§2.7).
    ``simulate_rate_limit_after`` (fault injection, §3.1 item 15) makes the
    first Attempt that reaches that many Steps hit a simulated rate limit.
    ``title`` (default: the prompt) names the Task and its folder, for
    template Tasks whose prompt is the rendered template. ``agent_override``
    defaults to whether ``agent`` was given. ``schedule_name`` makes the
    Task an Occurrence of that Schedule: its folder is
    ``recurring/<schedule-name>/<occurrence_date>/`` (§1.3). ``course`` (a
    ``school.courses`` id) ties the Task to that course's AI policy (§1.6).
    """
    created = now(cfg.timezone)
    ttype = task_type or cfg.default("task_type")
    agent_name = agent or cfg.default("agent")
    cfg.agent(agent_name)  # validates the agent is configured
    if simulate_rate_limit_after is not None and (
        isinstance(simulate_rate_limit_after, bool)
        or not isinstance(simulate_rate_limit_after, int)
        or simulate_rate_limit_after < 1
    ):
        raise ValueError("simulate_rate_limit_after must be a positive number of Steps")
    task_id = new_task_id(
        lambda i: db.task_id_exists(i) or any(cfg.tasks_dir.glob(f"*_{i}"))
    )
    folder = None
    if schedule_name is not None:
        folder = occurrence_folder(cfg.recurring_dir, schedule_name,
                                   occurrence_date or created.date(), task_id)
    spec = TaskSpec(
        id=task_id,
        title=title_from_prompt(title or prompt),
        slug=slugify(title or prompt),
        prompt=prompt,
        task_type=ttype,
        agent=agent_name,
        agent_override=(agent is not None) if agent_override is None else bool(agent_override),
        priority=priority or cfg.default("priority"),
        deadline=deadline,
        run_at=run_at,
        long=cfg.long_default(ttype) if long is None else bool(long),
        internet=internet or cfg.internet_default(ttype),
        template=template,
        estimated_minutes=estimated_minutes,
        max_runtime_minutes=_runtime_cap(cfg, ttype, max_runtime_minutes),
        approved=not needs_approval,
        approved_at=None if needs_approval else iso(created),
        schedule_name=schedule_name,
        created_at=iso(created),
    )
    folder = create_task_folder(
        cfg.tasks_dir, spec, created, plan=PLAN_PENDING if needs_approval else PLAN_STUB,
        folder=folder,
    )
    fields = spec.to_dict() | {
        "folder": str(folder), "simulate_rate_limit_after": simulate_rate_limit_after,
        "course": course,
    }
    state = TaskState.AWAITING_APPROVAL if needs_approval else TaskState.QUEUED
    db.insert_task(fields, state, reason)
    return NewTask(spec=spec, folder=folder)


def _runtime_cap(cfg: Config, task_type: str, minutes: int | None) -> int:
    if minutes is None:
        return cfg.max_runtime_default(task_type)
    if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
        raise ValueError("max_runtime_minutes must be a positive integer")
    return minutes


#: Why an Attempt was stopped from outside the agent.
STOP_DAEMON = "daemon_stop"  # launchd/SIGTERM: the Task is requeued
STOP_CANCEL = "cancel"  # orch cancel: the Task is cancelled
STOP_RUNTIME_CAP = "runtime_cap"  # max runtime hit: a Failed Attempt
STOP_USAGE_LIMIT = "usage_limit"  # a (possibly simulated) rate limit: the Task pauses
STOP_PAUSE = "pause"  # orch pause: the Task pauses until orch resume
STOP_SPEND_CAP = "spend_cap"  # the Task reached its per-Task spend cap: needs you
STOP_SPEND_PAUSE = "spend_pause"  # the daily/monthly spend cap: the Task pauses
STOP_STUCK = "stuck"  # stuck detection (§2.7): retried once, then needs you

#: Seconds between SIGTERM and SIGKILL when an agent ignores a stop.
KILL_GRACE_SECONDS = 15.0


def signal_agent(proc: subprocess.Popen, sig: int) -> None:
    """Send ``sig`` to the agent's whole process group (it is started in a
    session of its own). gemini relaunches itself as a child node process and
    does not pass SIGTERM on; killing only the parent left that child running
    the Task on its own (seen on gemini-cli 0.65.0)."""
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        if proc.poll() is None:
            proc.send_signal(sig)


class AttemptControl:
    """Lets another thread or a signal handler stop a running Attempt.

    ``request_stop(reason)`` terminates the agent process; ``run_attempt``
    then ends the Attempt according to the first reason given: a daemon stop
    (the default; SIGTERM from launchd) puts the Task back in ``queued``, a
    cancel ends it ``cancelled``, and a runtime cap makes it a Failed Attempt.

    ``stop_after_step(...)`` is the gentler stop of a usage limit or
    ``orch pause``: the agent is stopped once no Step (tool call) is in
    flight, or after ``grace_seconds`` at most, and the Task pauses.
    """

    def __init__(self) -> None:
        self.stop_requested = False
        self.stop_reason: str | None = None
        self.stop_requested_at: float | None = None
        self.proc: subprocess.Popen | None = None
        #: Tool calls seen whose result hasn't arrived yet (the Step in progress).
        self.inflight: set[str] = set()
        #: (stop reason, pause kind, limit) of a pending stop-after-step.
        self.pending: tuple[str, str, RateLimit | None] | None = None
        self.pending_since: float | None = None
        self.grace_seconds: float = 120.0
        #: On a real limit, how long the agent may take to end the session
        #: itself (and report its usage) before it is stopped.
        self.limit_wait_seconds: float = 0.0
        #: When the agent last wrote a line (``time.monotonic()``).
        self.last_output: float = time.monotonic()
        #: The agent's own processes (its CLI and any relaunch of it), taken
        #: at its first line; any other process under it is a running command.
        self.own_pids: set[int] | None = None  # None: not taken yet
        #: Why the Attempt got stuck (with a ``STOP_STUCK`` stop).
        self.stuck_why: str | None = None
        #: The last tool call seen, as ``stuck.call_text`` quotes it.
        self.last_call: str | None = None

    def stop_after_step(self, reason: str, kind: str, limit: RateLimit | None = None) -> None:
        if self.pending is None:
            self.pending = (reason, kind, limit)
            self.pending_since = time.monotonic()
        elif (self.pending[1] == kind and limit is not None and limit.reset_at
              and (self.pending[2] is None or not self.pending[2].reset_at)):
            # A later report of the same hit that carries the reset time.
            self.pending = (self.pending[0], kind, limit)
        self.maybe_stop()

    def maybe_stop(self) -> None:
        """Carry out a pending stop-after-step once the Step is done."""
        if self.pending is None or self.stop_requested:
            return
        elapsed = time.monotonic() - self.pending_since if self.pending_since is not None else 0.0
        overdue = elapsed >= self.grace_seconds
        if self.pending[1] == pauses.KIND_LIMIT and elapsed < self.limit_wait_seconds and not overdue:
            return  # the CLI usually ends a session that hit a limit by itself
        if not self.inflight or overdue:
            self.request_stop(self.pending[0])

    def stuck(self, why: str) -> None:
        """Stop a stuck Attempt at once (§2.7), unless another stop came first."""
        if not self.stop_requested:
            self.stuck_why = why
        self.request_stop(STOP_STUCK)

    def request_stop(self, reason: str = STOP_DAEMON) -> None:
        if not self.stop_requested:
            self.stop_reason = reason
            self.stop_requested_at = time.monotonic()
        self.stop_requested = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            signal_agent(proc, signal.SIGTERM)


def descendant_pids(pid: int) -> set[int]:
    """Every process below ``pid`` (children, their children, ...), from one
    ``ps`` listing; empty if ``ps`` fails."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: set[int] = set()
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


class _Watchdog(threading.Thread):
    """Watches a running Attempt: stops it at its runtime cap or when
    ``orch cancel`` asked for it (read through its own DB connection, since
    SQLite connections stay in their thread), and kills an agent that
    ignores SIGTERM for ``KILL_GRACE_SECONDS``.

    With ``silence`` it is also the silence trigger of stuck detection
    (``nightshift.stuck``, §2.7): no output and no file change for too long
    stops the Attempt as stuck. A Step in flight with a process under the
    agent other than its own is a command running, which is never silence;
    with no command running the agent's shorter stall threshold applies
    (gemini-cli 0.65.0's headless "sandbox expansion" question)."""

    def __init__(self, control: AttemptControl, proc: subprocess.Popen, *, cap_seconds: float,
                 db_path: Path, task_id: str, silence: stuck.SilenceWatch | None = None,
                 descendants: Callable[[int], set[int]] = descendant_pids):
        super().__init__(daemon=True)
        self.control = control
        self.proc = proc
        self.cap_seconds = cap_seconds
        self.db_path = db_path
        self.task_id = task_id
        self.silence = silence
        self.descendants = descendants
        self.started = time.monotonic()
        self.done = threading.Event()
        limits = [cap_seconds]
        if silence is not None:
            limits += [t for t in (silence.threshold(False), silence.threshold(True)) if t]
        self.interval = min(1.0, max(0.05, min(limits) / 20))

    def _busy(self) -> bool:
        """A command runs under the agent: a process that isn't its own."""
        own = self.control.own_pids
        return own is not None and bool(self.descendants(self.proc.pid) - own)

    def _stuck(self) -> str | None:
        if self.silence is None:
            return None
        c = self.control
        return self.silence.check(last_output=c.last_output, inflight=bool(c.inflight),
                                  busy=self._busy)

    def _requests(self, conn: sqlite3.Connection | None) -> tuple[bool, bool]:
        """(cancel requested, pause requested) by ``orch cancel``/``orch pause``."""
        if conn is None:
            return False, False
        try:
            row = conn.execute(
                "SELECT cancel_requested_at, pause_requested_at FROM tasks WHERE id = ?",
                (self.task_id,),
            ).fetchone()
        except sqlite3.Error:
            return False, False
        if row is None:
            return False, False
        return row[0] is not None, row[1] is not None

    def run(self) -> None:
        conn = None
        if str(self.db_path) != ":memory:":
            try:
                conn = sqlite3.connect(str(self.db_path), timeout=5)
            except sqlite3.Error:
                conn = None
        try:
            while not self.done.wait(self.interval):
                if self.proc.poll() is not None:
                    return
                c = self.control
                if c.stop_requested:
                    if c.stop_requested_at and time.monotonic() - c.stop_requested_at > KILL_GRACE_SECONDS:
                        signal_agent(self.proc, signal.SIGKILL)
                    continue
                if time.monotonic() - self.started >= self.cap_seconds:
                    c.request_stop(STOP_RUNTIME_CAP)
                    continue
                why = self._stuck()
                if why is not None:
                    c.stuck(why)
                    continue
                cancel, pause = self._requests(conn)
                if cancel:
                    c.request_stop(STOP_CANCEL)
                    continue
                if pause:
                    c.stop_after_step(STOP_PAUSE, pauses.KIND_BY_HAND)
                c.maybe_stop()
        finally:
            if conn is not None:
                conn.close()


class _SpendWatcher(threading.Thread):
    """Records the paid calls of a running Attempt as spend (one
    ``spend_events`` row each) and stops the Attempt as soon as its Task
    reaches the per-Task cap, or paid calls reach the daily or monthly cap
    (§1.5). Has its own DB connection (SQLite
    connections stay in their thread). Once ``done`` is set it collects the
    calls written at the agent's exit, then ends."""

    def __init__(self, cfg: Config, adapter: AgentAdapter, control: AttemptControl, *,
                 db_path: Path, task_id: str, run_id: int, clock: Callable[[], datetime],
                 interval: float = 0.5):
        super().__init__(daemon=True)
        self.cfg, self.adapter, self.control = cfg, adapter, control
        self.db_path, self.task_id, self.run_id, self.clock = db_path, task_id, run_id, clock
        self.interval = interval
        self.done = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        db = Database(self.db_path, tz=self.cfg.timezone)
        try:
            while True:
                finished = self.done.is_set()
                self._collect(db, may_stop=not finished)
                if finished:
                    return
                self.done.wait(self.interval)
        except Exception as exc:  # spend must never be lost silently
            self.error = f"{type(exc).__name__}: {exc}"
            self.control.request_stop(STOP_SPEND_CAP)
        finally:
            db.close()

    def _collect(self, db: Database, *, may_stop: bool) -> None:
        for call in self.adapter.paid_calls():
            spend.record(self.cfg, db, self.task_id, call, run_id=self.run_id,
                         agent=self.adapter.name, at=iso(self.clock()))
        # After the agent exited the calls are only recorded: an Attempt
        # that finished on its own is not turned into a stop.
        if may_stop and spend.cap_reached(self.cfg, db, self.task_id):
            self.control.request_stop(STOP_SPEND_CAP)
        elif may_stop and spend.spend_pause(self.cfg, db, self.clock()) is not None:
            self.control.request_stop(STOP_SPEND_PAUSE)


@dataclass
class AttemptResult:
    run_id: int
    state: TaskState
    session_id: str | None
    exit_code: int | None


_log = failures.log_line


def run_attempt(
    cfg: Config,
    db: Database,
    task_id: str,
    *,
    adapter_provider: AdapterProvider = get_adapter,
    on_event: Callable | None = None,
    control: AttemptControl | None = None,
    clock: Callable[[], datetime] | None = None,
    post_hooks: Sequence[PostHook] = (),
    max_runtime_seconds: float | None = None,
    continue_fresh: bool = False,
    request_decision: int | None = None,
    stuck_note: str | None = None,
) -> AttemptResult:
    """Run one Attempt of a queued Task, then finalize its report.

    Blocks until the agent exits. If ``control`` requests a daemon stop, the
    Attempt ends as interrupted and the Task goes back to ``queued``.

    A Failed Attempt (crash, runtime cap, failed post-hook) is charged to the
    retry budget: the Task goes to ``retry_wait``, or ``failed`` once the
    budget is spent (see ``nightshift.failures``). ``clock`` dates the retry;
    ``max_runtime_seconds`` overrides the Task's cap (tests).

    A rate limit (real, or simulated by ``--simulate-rate-limit-after``) or a
    pause request lets the Step in progress finish, stops the agent and
    pauses the Task (``nightshift.pauses``); that is not a Failed Attempt.
    The next Attempt of a paused Task resumes its agent session when it is
    young enough, else starts fresh from ``plan.md`` + ``progress.md``; a
    resume that fails before any Step falls back to a fresh session at once
    (``continue_fresh``, carrying ``request_decision`` and ``stuck_note`` along).

    After a resolved "needs you" request (``nightshift.needsyou``) the next
    Attempt continues the same way, and its prompt states the decision and
    the original request.

    An Attempt that gets stuck (``nightshift.stuck``, §2.7) is stopped at
    once and is not a Failed Attempt: the Task continues once with a note
    about what it was stuck on, and needs you if it gets stuck again.
    """
    clock = clock or (lambda: now(cfg.timezone))
    control = control or AttemptControl()
    control.grace_seconds = cfg.usage("step_grace_seconds")
    control.limit_wait_seconds = cfg.usage("limit_exit_wait_seconds")
    control.inflight.clear()
    control.pending = control.pending_since = None
    control.own_pids = None
    control.stuck_why = control.last_call = None
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    folder = Path(row["folder"])
    redact = cfg.secrets.redact
    agent_name = row["agent"]
    adapter = adapter_provider(agent_name, cfg.agent(agent_name))
    if hasattr(adapter, "clock"):
        adapter.clock = clock  # reset times given as a clock time ("resets 3pm")
    paid = adapter.paid
    # The API key an agent signs in with (a paid path's, §1.5, or Gemini's
    # free-tier key); loading it registers it for redaction.
    load_api_key(adapter, cfg.secrets, required=True)
    # A course Task's AI policy, from config as it is now (§1.6): without one
    # the Attempt never starts and the Task fails with the reason.
    course_policy = None
    if row["course"]:
        try:
            course_policy = school.protocol_section(school.course(cfg, row["course"]))
        except school.CourseError as exc:
            raise ConfigError(f"{exc}. The Task did not start") from None
    elif row["template"]:
        # A Task about all my courses (the weekly roundup): every policy.
        course_policy = templates.all_courses_policy(cfg, row)

    # A fresh policy every Attempt, so config changes apply and nothing the
    # agent did to an earlier one carries over.
    policy = build_policy(cfg, task_id=task_id, workspace=folder, internet=row["internet"])
    permissions = adapter.write_permissions(folder, policy)
    resuming = bool(row["resume_pending"])
    choice = None
    if resuming and not continue_fresh:
        choice = pauses.session_to_resume(cfg, db, row, clock())
    continuing = continue_fresh or resuming
    earlier = db.attempts(task_id)
    # The Task moved here from another agent (failover, §2.8): a fresh session
    # that picks up the other agent's work from plan.md + progress.md.
    switched_from = earlier[-1]["agent"] if earlier and earlier[-1]["agent"] != agent_name else None
    # A decided "needs you" request: the next Attempt is told (once).
    decision_id = row["request_decision_id"] if request_decision is None else request_decision
    decision = needsyou.decision_note(db, decision_id, folder)
    if switched_from:
        prompt = failover.failover_prompt(row["prompt"], switched_from, agent_name)
        if decision:
            prompt += f"\n\n{decision}"
    elif decision:
        prompt = needsyou.fresh_prompt(row["prompt"], decision,
                                       choice.why if choice is not None else "the resume failed")
    else:
        prompt = pauses.fresh_prompt(row["prompt"]) if continuing else row["prompt"]
    # The Attempt after a stuck one is told what it was stuck on (once, §2.7).
    stuck_note = row["stuck_note"] if stuck_note is None else stuck_note
    if stuck_note:
        prompt += f"\n\n{stuck_note}"
    task = TaskContext(
        id=task_id,
        prompt=prompt,
        task_type=row["task_type"],
        internet=row["internet"],
        protocol=agent_protocol(
            folder, permissions.read_only_dirs, internet=policy.internet,
            denied_domains=policy.denied_domains, course_policy=course_policy,
        ),
    )
    resume_session = choice.session_id if choice is not None else None
    if resume_session and hasattr(adapter, "prior_usage"):
        # Codex reports the session's running total: hand it the total the
        # earlier Attempts of this session ended at, so only this one's part counts.
        runs = {a["id"] for a in earlier if a["session_id"] == resume_session}
        raws = [json.loads(u["raw"]) for u in db.usage_for_task(task_id)
                if u["run_id"] in runs and u["raw"]]
        adapter.prior_usage = raws[-1] if raws else None
    if resume_session:
        resume_text = needsyou.resume_prompt(decision) if decision else pauses.RESUME_PROMPT
        if stuck_note:
            resume_text = stuck.resume_prompt(
                stuck_note, needsyou.resume_prompt(decision) if decision else needsyou.RESUME_TAIL)
        command = adapter.resume_command(resume_session, resume_text, task=task,
                                         permissions=permissions)
        how = f"resuming {choice.why}"
    else:
        command = adapter.build_command(task, folder, permissions)
        how = None
        if switched_from:
            how = f"fresh session from plan.md + progress.md (failover from {switched_from})"
        elif continuing:
            how = "fresh session from plan.md + progress.md" + (
                f" ({choice.why})" if choice is not None else " (the resume failed)")

    attempt_no = len(db.attempts(task_id)) + 1
    stream_log = folder / "logs" / f"attempt-{attempt_no}.{agent_name}.jsonl"
    stderr_log = folder / "logs" / f"attempt-{attempt_no}.{agent_name}.stderr.log"
    # The protocol is long and identical every time; keep the stored command readable.
    stored_argv = [_stored_arg(a, task.protocol, redact) for a in command.argv]
    progress_before = _mtime(folder / "progress.md")
    run_id = db.start_attempt(task_id, agent_name, stored_argv, str(stream_log))
    started = f"Attempt {attempt_no} started on {agent_name}" + (f", {how}" if how else "")
    if decision:
        verdict = db.approval(decision_id)["decision"]
        started += f", told its needs-you request was {verdict}"
    if stuck_note:
        started += ", told what the last Attempt got stuck on"
    db.transition(task_id, TaskState.RUNNING, started, run_id=run_id,
                  set_fields={"resume_pending": 0, "request_decision_id": None,
                              "stuck_note": None})
    _log(folder, f"{iso(now(cfg.timezone))} {started}: {json.dumps(stored_argv)}", redact)

    # Fault injection: one simulated limit per Task, after N Steps of an Attempt.
    simulate_after = row["simulate_rate_limit_after"] if not row["simulated_limit_at"] else None
    steps = 0
    repeats = stuck.RepeatWatch(cfg.stuck_repeat_calls)

    env = os.environ.copy() | command.env
    exit_code: int | None = None
    end_reason = ""
    errors: list[str] = []
    interrupted = False
    session_recorded = False
    # A Task already at its spend cap makes no paid call at all, nor does
    # one while the daily or monthly cap holds.
    at_cap = paid and spend.cap_reached(cfg, db, task_id)
    spend_held = paid and not at_cap and spend.spend_pause(cfg, db, clock()) is not None
    # A template's pre-hooks (the school calendar fetch): Nightshift's own
    # steps before the agent starts; one that fails is a Failed Attempt.
    pre_error = None
    if row["template"] and not at_cap and not spend_held:
        pre_error = templates.run_pre_hooks(cfg, row, folder, clock())
    proc: subprocess.Popen | None = None
    if not at_cap and not spend_held and pre_error is None:
        try:
            proc = subprocess.Popen(
                command.argv,
                cwd=folder,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                # Its own process group: a stop reaches every process of the
                # agent (signal_agent), and nothing of it outlives the Attempt.
                start_new_session=True,
            )
        except OSError as exc:
            errors.append(f"could not start {command.argv[0]}: {exc}")

    watchdog: _Watchdog | None = None
    if proc is not None:
        control.proc = proc
        if control.stop_requested:
            signal_agent(proc, signal.SIGTERM)
        cap = max_runtime_seconds if max_runtime_seconds is not None else (
            failures.runtime_cap_seconds(cfg, row))
        control.last_output = time.monotonic()
        silence = None
        if cfg.stuck_silence_seconds or adapter.stall_seconds:
            silence = stuck.SilenceWatch(folder, silence_seconds=cfg.stuck_silence_seconds,
                                         stall_seconds=adapter.stall_seconds)
        watchdog = _Watchdog(control, proc, cap_seconds=cap, db_path=db.path, task_id=task_id,
                             silence=silence)
        watchdog.start()
    spend_watcher: _SpendWatcher | None = None
    if paid and proc is not None:
        spend_watcher = _SpendWatcher(cfg, adapter, control, db_path=db.path, task_id=task_id,
                                      run_id=run_id, clock=clock)
        spend_watcher.start()

    if proc is not None:
        stderr_chunks: list[str] = []

        def pump_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_chunks.append(line)

        t = threading.Thread(target=pump_stderr, daemon=True)
        t.start()
        try:
            assert proc.stdin is not None and proc.stdout is not None
            try:
                if command.stdin is not None:
                    proc.stdin.write(command.stdin)
                proc.stdin.close()
            except BrokenPipeError:  # the agent was already stopped
                pass
            with open(stream_log, "w") as out:
                for line in proc.stdout:
                    control.last_output = time.monotonic()
                    if control.own_pids is None:
                        # The agent is up (its first line) and no Step runs
                        # yet: what runs under it now is its own (gemini
                        # relaunches itself as a child node process).
                        control.own_pids = descendant_pids(proc.pid)
                    out.write(redact(line))
                    out.flush()
                    for ev in adapter.parse_stream(line):
                        if ev.kind == EventKind.SESSION and not session_recorded and adapter.session_id():
                            db.set_attempt_session(run_id, adapter.session_id())
                            session_recorded = True
                        elif ev.kind == EventKind.ERROR and not ev.data.get("tool_error"):
                            errors.append(str(ev.data.get("message")))
                        elif ev.kind == EventKind.RATE_LIMIT_INFO:
                            usage.record_windows(db, agent_name, adapter.window_info(ev), clock())
                        if ev.kind == EventKind.TOOL_CALL:
                            steps += 1
                            control.inflight.add(str(ev.data.get("id") or f"step-{steps}"))
                            name, tool_input = ev.data.get("name"), ev.data.get("input")
                            control.last_call = stuck.call_text(name, tool_input)
                            why = repeats.see(name, tool_input)
                            if why is not None:
                                control.stuck(why)
                            if simulate_after and steps == simulate_after:
                                control.stop_after_step(
                                    STOP_USAGE_LIMIT, pauses.KIND_SIMULATED,
                                    pauses.simulated_limit(cfg, clock()))
                        elif ev.kind == EventKind.TOOL_RESULT:
                            control.inflight.discard(str(ev.data.get("tool_use_id")))
                        limit = adapter.detect_rate_limit(ev)
                        if limit is not None:
                            control.stop_after_step(STOP_USAGE_LIMIT, pauses.KIND_LIMIT, limit)
                        if on_event:
                            on_event(ev)
                    control.maybe_stop()
            exit_code = proc.wait()
        except KeyboardInterrupt:
            interrupted = True
            signal_agent(proc, signal.SIGTERM)
            try:
                exit_code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                signal_agent(proc, signal.SIGKILL)
                exit_code = proc.wait()
        # Whatever the agent left running in its group (an orphaned child,
        # a background command) ends with the Attempt.
        signal_agent(proc, signal.SIGKILL)
        t.join(timeout=5)
        if watchdog is not None:
            watchdog.done.set()
            watchdog.join(timeout=5)
        if spend_watcher is not None:
            spend_watcher.done.set()
            spend_watcher.join(timeout=30)
        stderr_log.write_text(redact("".join(stderr_chunks)))
        if stderr_chunks and exit_code:
            errors.append(redact("".join(stderr_chunks)).strip()[-500:])
        if exit_code is not None and control.pending is None:
            limit = adapter.detect_rate_limit(exit_code)
            if limit is not None:
                control.pending = (STOP_USAGE_LIMIT, pauses.KIND_LIMIT, limit)

    # -- decide the outcome --------------------------------------------------
    db.record_usage(run_id, task_id, agent_name, [u.as_dict() for u in adapter.usage_from_run()],
                    at=iso(clock()))
    if row["template"]:
        # A template's deadline source (a posting's close date, §2.3): after
        # every Attempt, paused ones included, so the queue and pause expiry see it.
        try:
            templates.apply_output_deadline(cfg, db, task_id)
        except Exception as exc:  # bad agent output must never break the Attempt
            _log(folder, f"{iso(now(cfg.timezone))} could not read the deadline source: {exc}",
                 redact)
    report_text = (folder / "report.md").read_text() if (folder / "report.md").exists() else ""
    ok = adapter.result_ok()
    detail: str | None = None
    failed_attempt: str | None = None  # why, if this is a Failed Attempt
    control.proc = None
    stop = control.stop_reason if control.stop_requested and proc is not None else None
    if stop == STOP_DAEMON and db.cancel_requested(task_id):
        stop = STOP_CANCEL  # `orch cancel` asked first; don't requeue it
    # A limit or pause that came up ends the Attempt as a pause, unless the
    # agent finished cleanly on its own before it could be stopped.
    finished_alone = stop is None and exit_code == 0 and ok is not False
    # A pending limit or `orch pause` wins over a spend pause: the paused Task
    # is paused on spend again before its next pick if the cap still holds.
    if (control.pending is not None and stop not in (STOP_CANCEL, STOP_SPEND_CAP)
            and not interrupted and not finished_alone):
        _reason, kind, limit = control.pending
        step_note = f"after {steps} Step{'s' if steps != 1 else ''}"
        if kind == pauses.KIND_SIMULATED:
            end_reason = (f"Attempt {attempt_no} hit a simulated rate limit {step_note} "
                          f"(--simulate-rate-limit-after {simulate_after})")
        elif kind == pauses.KIND_BY_HAND:
            end_reason = (f"Attempt {attempt_no} paused by hand "
                          f"({db.request_via(task_id, 'pause')}) {step_note}")
        else:
            ltype = f" ({limit.limit_type})" if limit is not None and limit.limit_type else ""
            end_reason = f"Attempt {attempt_no} hit {agent_name}'s usage limit{ltype} {step_note}"
        end_reason = redact(end_reason)
        db.finish_attempt(run_id, exit_code=exit_code, outcome="paused", end_reason=end_reason,
                          session_id=adapter.session_id())
        _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} ended: {end_reason}", redact)
        # What the next Attempt picks up from (Gate 1: progress.md is current).
        progress_now = _mtime(folder / "progress.md")
        _log(folder, f"{iso(now(cfg.timezone))} progress.md " + (
            "is missing" if progress_now is None else
            "was updated during this Attempt" if progress_now != progress_before else
            "was NOT updated during this Attempt"), redact)
        if kind == pauses.KIND_LIMIT:
            # A real hit (never a simulated one): a Limit observation.
            obs = usage.record_limit_hit(cfg, db, agent_name, limit, at=clock(), source="attempt",
                                         task_id=task_id, run_id=run_id)
            if obs is not None:
                _log(folder, f"{iso(now(cfg.timezone))} recorded Limit observation {obs}", redact)
        # Fail over to another agent of the Task type if one isn't out, else
        # pause (§2.6, §2.8).
        state = failover.after_usage_limit(cfg, db, task_id, kind=kind, limit=limit,
                                           reason=end_reason, at=clock(), run_id=run_id)
        return AttemptResult(run_id=run_id, state=state,
                             session_id=adapter.session_id(), exit_code=exit_code)
    db.clear_pause_request(task_id)
    if stop == STOP_SPEND_PAUSE or spend_held:
        state = _pause_on_spend(cfg, db, task_id, folder, run_id=run_id, attempt_no=attempt_no,
                                at=clock(), exit_code=exit_code, session_id=adapter.session_id(),
                                resume_pending=stop == STOP_SPEND_PAUSE or continuing)
        return AttemptResult(run_id=run_id, state=state, session_id=adapter.session_id(),
                             exit_code=exit_code)
    if stop == STOP_DAEMON:
        end_reason = redact(f"daemon stopped during Attempt {attempt_no}; Task requeued")
        db.finish_attempt(
            run_id, exit_code=exit_code, outcome="interrupted", end_reason=end_reason,
            session_id=adapter.session_id(),
        )
        _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} ended: {end_reason}", redact)
        # Work a pause cut off is still to be continued by the next Attempt.
        db.transition(task_id, TaskState.QUEUED, end_reason, run_id=run_id,
                      set_fields={"resume_pending": int(continuing)})
        return AttemptResult(
            run_id=run_id, state=TaskState.QUEUED, session_id=adapter.session_id(), exit_code=exit_code
        )
    if stop == STOP_CANCEL:
        state, outcome = TaskState.CANCELLED, "cancelled"
        end_reason = (f"cancelled by hand ({db.request_via(task_id, 'cancel')}) "
                      f"during Attempt {attempt_no}")
        detail = end_reason
    elif stop == STOP_SPEND_CAP and spend_watcher is not None and spend_watcher.error:
        failed_attempt = f"Attempt {attempt_no} stopped: could not record spend: {spend_watcher.error}"
    elif stop == STOP_SPEND_CAP or at_cap:
        state, outcome = TaskState.NEEDS_YOU, spend.OUTCOME_CAP
        end_reason = f"Attempt {attempt_no} stopped: {spend.cap_reason(cfg, db, task_id)}"
        detail = end_reason
    elif stop == STOP_RUNTIME_CAP:
        cap_min = (max_runtime_seconds / 60 if max_runtime_seconds is not None
                   else failures.runtime_cap_seconds(cfg, row) / 60)
        failed_attempt = f"Attempt {attempt_no} hit its runtime cap of {cap_min:g} min"
    elif stop == STOP_STUCK and not (exit_code == 0 and ok is not False):
        # Stuck (§2.7): not a Failed Attempt. The session and progress.md
        # are kept; the Task continues once with a note, then needs you.
        why = control.stuck_why or "no reason recorded"
        end_reason = redact(f"Attempt {attempt_no} stuck: {why}")
        db.finish_attempt(run_id, exit_code=exit_code, outcome=stuck.OUTCOME,
                          end_reason=end_reason, session_id=adapter.session_id())
        _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} ended: {end_reason}", redact)
        state = stuck.after_stuck(cfg, db, task_id, why=redact(why),
                                  note_text=redact(stuck.note(why, control.last_call)),
                                  attempt_no=attempt_no, at=clock(), run_id=run_id)
        return AttemptResult(run_id=run_id, state=state, session_id=adapter.session_id(),
                             exit_code=exit_code)
    elif interrupted:
        state, outcome = TaskState.CANCELLED, "cancelled"
        end_reason = "interrupted from the terminal (Ctrl-C)"
    elif pre_error is not None:
        failed_attempt = f"Attempt {attempt_no}: the agent did not start: {pre_error}"
    elif exit_code != 0 or ok is False or proc is None:
        err = adapter.result_error() or (errors[-1] if errors else None)
        if resume_session and steps == 0 and proc is not None:
            # The session could not be resumed (e.g. "No conversation found");
            # not the agent's failure. Start a fresh session right away.
            end_reason = redact(f"Attempt {attempt_no} could not resume session {resume_session}"
                                + (f": {err}" if err else ""))
            db.finish_attempt(run_id, exit_code=exit_code, outcome="resume_failed",
                              end_reason=end_reason, session_id=adapter.session_id())
            _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} ended: {end_reason}",
                 redact)
            db.transition(task_id, TaskState.QUEUED,
                          f"{end_reason}; starting a fresh session from plan.md + progress.md",
                          run_id=run_id)
            return run_attempt(
                cfg, db, task_id, adapter_provider=adapter_provider, on_event=on_event,
                control=control, clock=clock, post_hooks=post_hooks,
                max_runtime_seconds=max_runtime_seconds, continue_fresh=True,
                request_decision=decision_id, stuck_note=stuck_note,
            )
        failed_attempt = (f"Attempt {attempt_no} crashed: agent exited with code {exit_code}"
                          + (f": {err}" if err else ""))
    elif (folder / "needs_you.json").exists():
        state, outcome = TaskState.NEEDS_YOU, "needs_you"
        end_reason = "agent wrote needs_you.json and stopped"
    else:
        word = report.agent_result(report_text)
        state = {"partial": TaskState.PARTIAL, "failed": TaskState.FAILED}.get(word or "", TaskState.DONE)
        outcome = "completed"
        end_reason = f"agent finished; report says {word or 'nothing (assumed done)'}"
        if state != TaskState.FAILED:
            hook_error = failures.run_post_hooks(post_hooks, folder, row)
            if hook_error:
                failed_attempt = f"Attempt {attempt_no}: {hook_error}"
    if failed_attempt is not None:
        outcome = "failed"
        end_reason = failed_attempt
    end_reason = redact(end_reason)

    db.finish_attempt(
        run_id, exit_code=exit_code, outcome=outcome, end_reason=end_reason, session_id=adapter.session_id()
    )
    _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} ended: {end_reason}", redact)

    if failed_attempt is not None:
        result = failures.after_failed_attempt(
            cfg, db, task_id, end_reason, at=clock(), run_id=run_id
        )
        return AttemptResult(
            run_id=run_id, state=result.state, session_id=adapter.session_id(), exit_code=exit_code
        )

    report.finalize(
        folder,
        title=row["title"],
        result=str(state),
        result_detail=detail,
        attempts=db.attempts(task_id),
        usage_rows=db.usage_for_task(task_id),
        redact=redact,
        appendix=templates.report_appendix(cfg, row) if row["template"] else None,
        **failures.report_extras(cfg, db, task_id),
    )
    db.transition(task_id, state, end_reason, run_id=run_id)
    return AttemptResult(run_id=run_id, state=state, session_id=adapter.session_id(), exit_code=exit_code)


def _pause_on_spend(cfg: Config, db: Database, task_id: str, folder: Path, *, run_id: int,
                    attempt_no: int, at: datetime, exit_code: int | None,
                    session_id: str | None, resume_pending: bool) -> TaskState:
    """End an Attempt the daily or monthly spend cap stopped (or kept from
    starting): the Task moves to ``paused_spend`` until the cap lifts
    (``spend.promote_paused``). Not a Failed Attempt."""
    pause = spend.spend_pause(cfg, db, at)
    # The cap lifted meanwhile (raised by hand): back in line at once.
    state = TaskState.QUEUED if pause is None else TaskState.PAUSED_SPEND
    why = "the spend cap that stopped it no longer holds" if pause is None else pause.reason
    end_reason = cfg.secrets.redact(f"Attempt {attempt_no} stopped: {why}")
    db.finish_attempt(run_id, exit_code=exit_code, outcome="paused", end_reason=end_reason,
                      session_id=session_id)
    _log(folder, f"{iso(at)} Attempt {attempt_no} ended: {end_reason}", cfg.secrets.redact)
    fields = {} if pause is None else spend.pause_fields(pause)
    db.transition(task_id, state, end_reason, run_id=run_id,
                  set_fields=fields | {"resume_pending": int(resume_pending)})
    return state


def _stored_arg(arg: str, protocol: str, redact: Callable[[str], str]) -> str:
    """One argv entry as stored: the long, identical-every-time protocol is
    shown as ``<agent protocol>`` (also inside a ``key=<quoted protocol>``
    config override)."""
    if arg == protocol:
        return "<agent protocol>"
    quoted = json.dumps(protocol)
    if quoted in arg:
        arg = arg.replace(quoted, '"<agent protocol>"')
    return redact(arg)


def _mtime(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _finalize(cfg: Config, db: Database, row, state: TaskState, reason: str) -> None:
    failures.finalize(cfg, db, row, state, reason, at=now(cfg.timezone))


def fail_orphaned(
    cfg: Config, db: Database, *, clock: Callable[[], datetime] | None = None
) -> list[str]:
    """Settle Tasks left ``running`` by a daemon that died mid-Attempt.

    Only the daemon (holding the daemon lock) may call this: then nothing
    else can be running a Task. The cut-off Attempt is a Failed Attempt (a
    crash, per CONTEXT.md), so the Task goes to ``retry_wait`` or, with its
    budget spent, ``failed``. A Task ``orch cancel`` was waiting on is
    cancelled instead. Returns the ids it settled.
    """
    clock = clock or (lambda: now(cfg.timezone))
    settled: list[str] = []
    for row in db.tasks_in_state(TaskState.RUNNING):
        cancel = row["cancel_requested_at"] is not None
        reason = (
            f"cancelled by hand ({db.request_via(row['id'], 'cancel')}); the daemon stopped "
            "before it could stop the Attempt"
            if cancel else "crashed: the daemon stopped unexpectedly while an Attempt was running"
        )
        last = None
        for a in db.attempts(row["id"]):
            if a["ended_at"] is None:
                db.finish_attempt(
                    a["id"], exit_code=None, outcome="cancelled" if cancel else "failed",
                    end_reason=f"Attempt {a['attempt_no']} {reason}", session_id=None,
                )
                last = a["id"]
        if cancel:
            failures.finalize(cfg, db, row, TaskState.CANCELLED, reason, at=clock())
            db.transition(row["id"], TaskState.CANCELLED, reason, run_id=last)
        else:
            failures.after_failed_attempt(cfg, db, row["id"], reason, at=clock(), run_id=last)
        settled.append(row["id"])
    return settled


def fail_unstartable(cfg: Config, db: Database, task_id: str, error: str) -> None:
    """Fail a Task whose Attempt raised before or while running."""
    row = db.get_task(task_id)
    if row is None:
        return
    state = TaskState(row["state"])
    reason = cfg.secrets.redact(f"Nightshift error: {error}")[:500]
    for a in db.attempts(task_id):
        if a["ended_at"] is None:
            # `orch cancel` may have cancelled it between pick and start.
            outcome = "cancelled" if state == TaskState.CANCELLED else "failed"
            db.finish_attempt(a["id"], exit_code=None, outcome=outcome, end_reason=reason, session_id=None)
    if state in (TaskState.QUEUED, TaskState.RUNNING):
        _finalize(cfg, db, row, TaskState.FAILED, reason)
        db.transition(task_id, TaskState.FAILED, reason)
