"""Creating Tasks and running Attempts in the foreground."""

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
from .protocol import agent_protocol
from .states import TaskState
from .timeutil import iso, now
from .workspace import TaskSpec, create_task_folder

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
) -> NewTask:
    """Create a free-form Task: DB row, Task folder, ``task.yaml``; state queued."""
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
        max_runtime_minutes=int(cfg.default("max_runtime_minutes")),
        # Free-form planning and plan approval arrive with issue #5; until
        # then a Task is treated as <= 30 minutes and starts right away.
        approved=True,
        approved_at=iso(created),
        created_at=iso(created),
    )
    folder = create_task_folder(cfg.tasks_dir, spec, created)
    fields = spec.to_dict() | {"folder": str(folder)}
    db.insert_task(fields, TaskState.QUEUED, "created by orch add")
    return NewTask(spec=spec, folder=folder)


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
) -> AttemptResult:
    """Run one Attempt of a queued Task in the foreground, then finalize its report."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    folder = Path(row["folder"])
    redact = cfg.secrets.redact
    agent_name = row["agent"]
    adapter = adapter_provider(agent_name, cfg.agent(agent_name))

    task = TaskContext(
        id=task_id,
        prompt=row["prompt"],
        task_type=row["task_type"],
        internet=row["internet"],
        protocol=agent_protocol(folder),
    )
    permissions = adapter.write_permissions(folder, {"internet": row["internet"]})
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
