"""Creating Tasks and running Attempts (one Attempt per call)."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import report
from .adapters import get_adapter
from .adapters.base import AgentAdapter, EventKind, TaskContext
from .config import Config
from .db import Database
from .ids import new_task_id, slugify, title_from_prompt
from .permissions import build_policy
from .protocol import agent_protocol
from .states import TaskState
from .timeutil import iso, now
from .workspace import PLAN_PENDING, PLAN_STUB, TaskSpec, create_task_folder

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
    internet: str | None = None,
    estimated_minutes: int | None = None,
    template: str | None = None,
    needs_approval: bool = False,
    reason: str = "created by orch add",
) -> NewTask:
    """Create a Task: DB row, Task folder, ``task.yaml``.

    It starts ``queued`` and approved, or ``awaiting_approval`` (not approved)
    when ``needs_approval`` is set because its estimate is over the plan
    threshold (§2.4; see ``nightshift.planning``). Planning happens before
    this is called.
    """
    created = now(cfg.timezone)
    ttype = task_type or cfg.default("task_type")
    agent_name = agent or cfg.default("agent")
    cfg.agent(agent_name)  # validates the agent is configured
    task_id = new_task_id(
        lambda i: db.task_id_exists(i) or any(cfg.tasks_dir.glob(f"*_{i}"))
    )
    spec = TaskSpec(
        id=task_id,
        title=title_from_prompt(prompt),
        slug=slugify(prompt),
        prompt=prompt,
        task_type=ttype,
        agent=agent_name,
        agent_override=agent is not None,
        priority=priority or cfg.default("priority"),
        internet=internet or cfg.internet_default(ttype),
        template=template,
        estimated_minutes=estimated_minutes,
        max_runtime_minutes=int(cfg.default("max_runtime_minutes")),
        approved=not needs_approval,
        approved_at=None if needs_approval else iso(created),
        created_at=iso(created),
    )
    folder = create_task_folder(
        cfg.tasks_dir, spec, created, plan=PLAN_PENDING if needs_approval else PLAN_STUB
    )
    fields = spec.to_dict() | {"folder": str(folder)}
    state = TaskState.AWAITING_APPROVAL if needs_approval else TaskState.QUEUED
    db.insert_task(fields, state, reason)
    return NewTask(spec=spec, folder=folder)


class AttemptControl:
    """Lets another thread or a signal handler stop a running Attempt.

    ``request_stop()`` terminates the agent process; ``run_attempt`` then ends
    the Attempt as interrupted and puts the Task back in ``queued`` instead
    of judging it failed. The daemon uses this on SIGTERM (launchd stop).
    """

    def __init__(self) -> None:
        self.stop_requested = False
        self.proc: subprocess.Popen | None = None

    def request_stop(self) -> None:
        self.stop_requested = True
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()


@dataclass
class AttemptResult:
    run_id: int
    state: TaskState
    session_id: str | None
    exit_code: int | None


def _log(folder: Path, line: str, redact: Callable[[str], str]) -> None:
    with open(folder / "logs" / "nightshift.log", "a") as f:
        f.write(redact(line) + "\n")


def run_attempt(
    cfg: Config,
    db: Database,
    task_id: str,
    *,
    adapter_provider: AdapterProvider = get_adapter,
    on_event: Callable | None = None,
    control: AttemptControl | None = None,
) -> AttemptResult:
    """Run one Attempt of a queued Task, then finalize its report.

    Blocks until the agent exits. If ``control`` requests a stop, the Attempt
    ends as interrupted and the Task goes back to ``queued``.
    """
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    folder = Path(row["folder"])
    redact = cfg.secrets.redact
    agent_name = row["agent"]
    adapter = adapter_provider(agent_name, cfg.agent(agent_name))

    # A fresh policy every Attempt, so config changes apply and nothing the
    # agent did to an earlier one carries over.
    policy = build_policy(cfg, task_id=task_id, workspace=folder, internet=row["internet"])
    permissions = adapter.write_permissions(folder, policy)
    task = TaskContext(
        id=task_id,
        prompt=row["prompt"],
        task_type=row["task_type"],
        internet=row["internet"],
        protocol=agent_protocol(
            folder, permissions.read_only_dirs, internet=policy.internet,
            denied_domains=policy.denied_domains,
        ),
    )
    command = adapter.build_command(task, folder, permissions)

    attempt_no = len(db.attempts(task_id)) + 1
    stream_log = folder / "logs" / f"attempt-{attempt_no}.{agent_name}.jsonl"
    stderr_log = folder / "logs" / f"attempt-{attempt_no}.{agent_name}.stderr.log"
    # The protocol is long and identical every time; keep the stored command readable.
    stored_argv = [redact(a) if a != task.protocol else "<agent protocol>" for a in command.argv]
    run_id = db.start_attempt(task_id, agent_name, stored_argv, str(stream_log))
    db.transition(task_id, TaskState.RUNNING, f"Attempt {attempt_no} started on {agent_name}", run_id=run_id)
    _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} started: {json.dumps(stored_argv)}", redact)

    env = os.environ.copy() | command.env
    exit_code: int | None = None
    end_reason = ""
    errors: list[str] = []
    interrupted = False
    session_recorded = False
    try:
        proc = subprocess.Popen(
            command.argv,
            cwd=folder,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except OSError as exc:
        proc = None
        errors.append(f"could not start {command.argv[0]}: {exc}")

    if proc is not None and control is not None:
        control.proc = proc
        if control.stop_requested:
            proc.terminate()

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
            if command.stdin is not None:
                proc.stdin.write(command.stdin)
            proc.stdin.close()
            with open(stream_log, "w") as out:
                for line in proc.stdout:
                    out.write(redact(line))
                    out.flush()
                    for ev in adapter.parse_stream(line):
                        if ev.kind == EventKind.SESSION and not session_recorded and adapter.session_id():
                            db.set_attempt_session(run_id, adapter.session_id())
                            session_recorded = True
                        elif ev.kind == EventKind.ERROR and not ev.data.get("tool_error"):
                            errors.append(str(ev.data.get("message")))
                        if on_event:
                            on_event(ev)
            exit_code = proc.wait()
        except KeyboardInterrupt:
            interrupted = True
            proc.terminate()
            try:
                exit_code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                exit_code = proc.wait()
        t.join(timeout=5)
        stderr_log.write_text(redact("".join(stderr_chunks)))
        if stderr_chunks and exit_code:
            errors.append(redact("".join(stderr_chunks)).strip()[-500:])

    # -- decide the outcome --------------------------------------------------
    usage = adapter.usage_from_run()
    db.record_usage(run_id, task_id, agent_name, [u.as_dict() for u in usage])
    report_text = (folder / "report.md").read_text() if (folder / "report.md").exists() else ""
    ok = adapter.result_ok()
    detail: str | None = None
    if control is not None:
        control.proc = None
    if control is not None and control.stop_requested and proc is not None:
        end_reason = redact(f"daemon stopped during Attempt {attempt_no}; Task requeued")
        db.finish_attempt(
            run_id, exit_code=exit_code, outcome="interrupted", end_reason=end_reason,
            session_id=adapter.session_id(),
        )
        _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} ended: {end_reason}", redact)
        db.transition(task_id, TaskState.QUEUED, end_reason, run_id=run_id)
        return AttemptResult(
            run_id=run_id, state=TaskState.QUEUED, session_id=adapter.session_id(), exit_code=exit_code
        )
    if interrupted:
        state, outcome = TaskState.CANCELLED, "cancelled"
        end_reason = "interrupted from the terminal (Ctrl-C)"
    elif exit_code != 0 or ok is False or proc is None:
        state, outcome = TaskState.FAILED, "failed"
        err = adapter.result_error() or (errors[-1] if errors else None)
        end_reason = f"agent exited with code {exit_code}" + (f": {err}" if err else "")
        detail = redact(end_reason)
    elif (folder / "needs_you.json").exists():
        state, outcome = TaskState.NEEDS_YOU, "needs_you"
        end_reason = "agent wrote needs_you.json and stopped"
    else:
        word = report.agent_result(report_text)
        state = {"partial": TaskState.PARTIAL, "failed": TaskState.FAILED}.get(word or "", TaskState.DONE)
        outcome = "completed"
        end_reason = f"agent finished; report says {word or 'nothing (assumed done)'}"
    end_reason = redact(end_reason)

    db.finish_attempt(
        run_id, exit_code=exit_code, outcome=outcome, end_reason=end_reason, session_id=adapter.session_id()
    )
    _log(folder, f"{iso(now(cfg.timezone))} Attempt {attempt_no} ended: {end_reason}", redact)

    report.finalize(
        folder,
        title=row["title"],
        result=str(state),
        result_detail=detail,
        attempts=db.attempts(task_id),
        usage_rows=db.usage_for_task(task_id),
        redact=redact,
    )
    db.transition(task_id, state, end_reason, run_id=run_id)
    return AttemptResult(run_id=run_id, state=state, session_id=adapter.session_id(), exit_code=exit_code)


def _finalize(cfg: Config, db: Database, row, state: TaskState, reason: str) -> None:
    folder = Path(row["folder"])
    if folder.exists():
        report.finalize(
            folder,
            title=row["title"],
            result=str(state),
            result_detail=reason,
            attempts=db.attempts(row["id"]),
            usage_rows=db.usage_for_task(row["id"]),
            redact=cfg.secrets.redact,
        )
        _log(folder, f"{iso(now(cfg.timezone))} {reason}", cfg.secrets.redact)


def fail_orphaned(cfg: Config, db: Database) -> list[str]:
    """Fail Tasks left ``running`` by a daemon that died mid-Attempt.

    Only the daemon (holding the daemon lock) may call this: then nothing
    else can be running a Task. The cut-off Attempt is a Failed Attempt (a
    crash, per CONTEXT.md). Returns the ids it failed.
    """
    failed: list[str] = []
    for row in db.tasks_in_state(TaskState.RUNNING):
        reason = "the daemon stopped unexpectedly while an Attempt was running"
        last = None
        for a in db.attempts(row["id"]):
            if a["ended_at"] is None:
                db.finish_attempt(
                    a["id"], exit_code=None, outcome="failed", end_reason=reason, session_id=None
                )
                last = a["id"]
        _finalize(cfg, db, row, TaskState.FAILED, reason)
        db.transition(row["id"], TaskState.FAILED, reason, run_id=last)
        failed.append(row["id"])
    return failed


def fail_unstartable(cfg: Config, db: Database, task_id: str, error: str) -> None:
    """Fail a Task whose Attempt raised before or while running."""
    row = db.get_task(task_id)
    if row is None:
        return
    state = TaskState(row["state"])
    reason = cfg.secrets.redact(f"Nightshift error: {error}")[:500]
    for a in db.attempts(task_id):
        if a["ended_at"] is None:
            db.finish_attempt(a["id"], exit_code=None, outcome="failed", end_reason=reason, session_id=None)
    if state in (TaskState.QUEUED, TaskState.RUNNING):
        _finalize(cfg, db, row, TaskState.FAILED, reason)
        db.transition(task_id, TaskState.FAILED, reason)
