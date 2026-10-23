"""Free-form planning and plan approval (§2.4).

A free-form ``orch add`` first gets a quick planning call: the agent estimates
``estimated_minutes`` and proposes a Task type. Tasks estimated over
``planning.approval_threshold_minutes`` (30) get a second planning call that
expands the one-line prompt into ``plan.md``; they wait in
``awaiting_approval`` until approved. Template Tasks take the estimate and
type from their template and make no planning call.

Planning calls are not Attempts: they run with no tools in a Nightshift-owned
directory, keep no session and never change a Task's state. Their usage goes
to the ``planning_usage`` table.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import report
from .adapters import get_adapter, load_api_key
from .adapters.base import AgentAdapter, PlanningReply, Usage
from .config import Config
from .db import Database
from .states import TaskState
from .timeutil import iso, now
from .workspace import read_task_yaml, write_task_yaml

AdapterProvider = Callable[[str, dict], AgentAdapter]


class PlanningError(Exception):
    """A planning call failed or gave an unusable answer."""


@dataclass
class Proposal:
    """An estimate and Task type, from a planning call or a template."""

    estimated_minutes: int
    task_type: str
    source: str  # "planning call" or "template <name>"
    reason: str | None = None
    agent: str | None = None  # the agent that made the planning call
    usage: list[Usage] = field(default_factory=list)


ESTIMATE_SYSTEM = """\
You are the planner for Nightshift, which runs Tasks with a headless AI coding
agent overnight. The agent works alone in an empty Task folder with file tools
and a sandboxed shell (web fetch and search only on internet Tasks). Nobody
answers questions while it runs.

For the Task you are given, do not do the Task. Estimate the wall-clock
minutes the agent will need from start to a finished report, and pick the
Task type that fits best. Task types: {types}. Use "{default}" when nothing
else fits. Give a one-sentence reason. Answer only with the requested JSON.
"""

PLAN_SYSTEM = """\
You are the planner for Nightshift, which runs Tasks with a headless AI coding
agent overnight. The agent works alone in its Task folder, puts deliverables
in output/, keeps progress.md current and writes report.md. It may read but
never change these folders: {read_only}. It must stop and ask before
spending money, pushing, deploying, sending messages, submitting anything,
posting, installing system packages, deleting files outside its folder or
logging in.

Do not do the Task. Expand the one-line Task you are given into a short
plan.md in Markdown, at most about 40 lines, with exactly these headings:
# Plan, ## Goal, ## Steps (numbered; each one a checkable piece of progress),
## Deliverables (paths under output/), ## Risks and open questions.
Answer only with the requested JSON; plan_markdown holds the whole file.
"""


def planning_agent(cfg: Config) -> str:
    return str(cfg.planning("agent") or cfg.default("agent"))


def threshold(cfg: Config) -> float:
    return float(cfg.planning("approval_threshold_minutes") or 30)


def needs_plan(cfg: Config, estimated_minutes: int | None) -> bool:
    """Over the threshold: a plan.md and approval. At or under: neither."""
    return estimated_minutes is not None and estimated_minutes > threshold(cfg)


def _planning_dir(cfg: Config) -> Path:
    # An empty, Nightshift-owned working directory, so no project files or
    # CLAUDE.md are picked up by the planning call.
    d = cfg.data_dir / "planning"
    d.mkdir(parents=True, exist_ok=True)
    return d


def call(
    cfg: Config,
    prompt: str,
    *,
    system: str,
    schema: dict[str, Any],
    adapter_provider: AdapterProvider = get_adapter,
) -> tuple[str, PlanningReply]:
    """Run one planning call on the planning agent. Never raises for agent
    failures: a failed call is a ``PlanningReply`` with ``ok=False``."""
    agent = planning_agent(cfg)
    adapter = adapter_provider(agent, cfg.agent(agent))
    load_api_key(adapter, cfg.secrets)
    try:
        command = adapter.planning_command(prompt, system=system, schema=schema)
    except ValueError as exc:  # e.g. no API key to sign in with
        return agent, PlanningReply(ok=False, error=cfg.secrets.redact(str(exc)))
    timeout = float(cfg.planning("timeout_seconds") or 180)
    try:
        proc = subprocess.run(
            command.argv,
            input=command.stdin,
            capture_output=True,
            text=True,
            cwd=_planning_dir(cfg),
            env=os.environ.copy() | command.env,
            timeout=timeout,
        )
    except OSError as exc:
        return agent, PlanningReply(ok=False, error=f"could not start {command.argv[0]}: {exc}")
    except subprocess.TimeoutExpired:
        return agent, PlanningReply(ok=False, error=f"no answer within {timeout:g} s")
    reply = adapter.parse_planning_output(proc.stdout, proc.returncode)
    if not reply.ok and proc.stderr.strip() and reply.error and "exit code" in reply.error:
        reply.error += ": " + proc.stderr.strip()[-300:]
    if reply.error:
        reply.error = cfg.secrets.redact(reply.error)
    return agent, reply


def estimate(
    cfg: Config, prompt: str, *, adapter_provider: AdapterProvider = get_adapter
) -> Proposal:
    """The quick planning call for a free-form Task: minutes and Task type."""
    types = cfg.task_types
    default = str(cfg.default("task_type"))
    ttype_schema: dict[str, Any] = {"type": "string"}
    if types:
        ttype_schema["enum"] = sorted(set(types) | {default})
    schema = {
        "type": "object",
        "properties": {
            "estimated_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "task_type": ttype_schema,
            "reason": {"type": "string"},
        },
        "required": ["estimated_minutes", "task_type"],
        "additionalProperties": False,
    }
    system = ESTIMATE_SYSTEM.format(types=", ".join(sorted(set(types) | {default})), default=default)
    agent, reply = call(
        cfg, f"Task: {prompt}", system=system, schema=schema, adapter_provider=adapter_provider
    )
    if not reply.ok or reply.structured is None:
        raise PlanningError(f"planning call on {agent} failed: {reply.error}")
    ans = reply.structured
    try:
        minutes = int(ans["estimated_minutes"])
    except (KeyError, TypeError, ValueError):
        raise PlanningError(f"planning call on {agent} gave no estimated_minutes") from None
    if minutes < 1:
        raise PlanningError(f"planning call on {agent} estimated {minutes} minutes")
    ttype = str(ans.get("task_type") or default)
    reason = str(ans["reason"]).strip() if ans.get("reason") else None
    if types and ttype not in types:
        reason = f"proposed unknown type {ttype!r}, using {default!r}" + (f"; {reason}" if reason else "")
        ttype = default
    return Proposal(
        estimated_minutes=minutes, task_type=ttype, source="planning call",
        reason=reason, agent=agent, usage=reply.usage,
    )


def from_template(template: Mapping[str, Any]) -> Proposal:
    """A template Task's estimate and type come from its template; no call."""
    name = template.get("name") or "template"
    try:
        minutes = int(template["estimated_minutes"])
        ttype = str(template["task_type"])
    except (KeyError, TypeError, ValueError):
        raise PlanningError(
            f"template {name!r} must set estimated_minutes and task_type"
        ) from None
    return Proposal(estimated_minutes=minutes, task_type=ttype, source=f"template {name}")


def record_usage(db: Database, task_id: str | None, kind: str, proposal_or_usage) -> None:
    """Store a planning call's usage (no-op for template proposals)."""
    if isinstance(proposal_or_usage, Proposal):
        agent, usage = proposal_or_usage.agent, proposal_or_usage.usage
    else:
        agent, usage = proposal_or_usage
    if agent and usage:
        db.record_planning_usage(task_id, kind, agent, [u.as_dict() for u in usage])


def _log(cfg: Config, folder: Path, line: str) -> None:
    with open(folder / "logs" / "nightshift.log", "a") as f:
        f.write(cfg.secrets.redact(f"{iso(now(cfg.timezone))} {line}") + "\n")


def write_plan(
    cfg: Config, db: Database, task_id: str, *, adapter_provider: AdapterProvider = get_adapter
) -> str:
    """Have the agent expand the Task's prompt into ``plan.md``; return it.

    The agent writes the plan's content; Nightshift saves it to the Task
    folder, so the planning call needs no file access. Raises
    ``PlanningError`` if the call fails (the pending stub stays in place).
    """
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    folder = Path(row["folder"])
    read_only = ", ".join(str(Path(p).expanduser()) for p in
                          (cfg.data.get("permissions") or {}).get("read_only_dirs") or []) or "none"
    schema = {
        "type": "object",
        "properties": {"plan_markdown": {"type": "string"}},
        "required": ["plan_markdown"],
        "additionalProperties": False,
    }
    prompt = (
        f"Task type: {row['task_type']}\nEstimated minutes: {row['estimated_minutes']}\n"
        f"Internet: {row['internet']}\nTask: {row['prompt']}"
    )
    agent, reply = call(
        cfg, prompt, system=PLAN_SYSTEM.format(read_only=read_only), schema=schema,
        adapter_provider=adapter_provider,
    )
    record_usage(db, task_id, "plan", (agent, reply.usage))
    text = (reply.structured or {}).get("plan_markdown") if reply.ok else None
    if not isinstance(text, str) or not text.strip():
        err = reply.error or "the answer had no plan_markdown"
        _log(cfg, folder, f"plan call on {agent} failed: {err}")
        raise PlanningError(f"plan call on {agent} failed: {err}")
    text = cfg.secrets.redact(text.strip()) + "\n"
    (folder / "plan.md").write_text(text)
    _log(cfg, folder, f"plan.md written by a planning call on {agent}")
    return text


def plan_sha256(folder: Path) -> str | None:
    path = folder / "plan.md"
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _sync_yaml(folder: Path, approved: bool, approved_at: str | None) -> None:
    if (folder / "task.yaml").exists():
        spec = read_task_yaml(folder)
        spec.approved, spec.approved_at = approved, approved_at
        write_task_yaml(folder, spec)


def approve(cfg: Config, db: Database, task_id: str, *, via: str) -> None:
    """Approve a Task awaiting approval: record it and queue the Task."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    folder = Path(row["folder"])
    at = db.decide_approval(
        task_id, approved=True, via=via, to_state=TaskState.QUEUED,
        reason=f"plan approved ({via})", plan_sha256=plan_sha256(folder),
    )
    _sync_yaml(folder, True, at)
    _log(cfg, folder, f"plan approved ({via}); Task queued")


def deny(cfg: Config, db: Database, task_id: str, *, via: str) -> None:
    """Deny a Task awaiting approval: record it and cancel the Task."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    folder = Path(row["folder"])
    reason = f"plan denied ({via})"
    db.decide_approval(
        task_id, approved=False, via=via, to_state=TaskState.CANCELLED,
        reason=reason, plan_sha256=plan_sha256(folder),
    )
    _sync_yaml(folder, False, None)
    if folder.exists():
        report.finalize(
            folder, title=row["title"], result=str(TaskState.CANCELLED), result_detail=reason,
            attempts=[], usage_rows=[], redact=cfg.secrets.redact,
        )
        _log(cfg, folder, f"{reason}; Task cancelled")
