"""Task folders (§1.3) and ``task.yaml`` (§2.2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from . import report


@dataclass
class TaskSpec:
    """The request and settings of a Task, as stored in ``task.yaml``."""

    id: str
    title: str
    slug: str
    prompt: str
    task_type: str
    agent: str
    priority: str
    internet: str
    created_at: str
    template: str | None = None
    agent_override: bool = False
    deadline: str | None = None
    run_at: str | None = None
    long: bool = False
    estimated_minutes: int | None = None
    max_runtime_minutes: int | None = None
    approved: bool = False
    approved_at: str | None = None
    schedule_name: str | None = None

    # Field order in task.yaml follows §2.2.
    YAML_ORDER = (
        "id title slug prompt template task_type agent agent_override priority deadline "
        "run_at internet long estimated_minutes max_runtime_minutes approved approved_at "
        "schedule_name created_at"
    ).split()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: d[k] for k in self.YAML_ORDER}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskSpec":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def folder_name(created: datetime, slug: str, task_id: str) -> str:
    return f"{created:%Y-%m-%d}_{slug}_{task_id}"


PLAN_STUB = """\
# Plan

No plan was needed: this Task was estimated at 30 minutes or less, so it
started without plan approval (§2.4). The agent may write its own plan here.
"""

PLAN_PENDING = """\
# Plan

This Task was estimated over the plan threshold, so it needs a plan and your
approval before it runs (§2.4). The plan has not been written yet: the
planning call failed or hasn't finished. `orch approve <id>` runs it anyway;
`orch deny <id>` cancels it.
"""

PROGRESS_STUB = """\
# Progress

Checkpoint notes. The agent updates this after every meaningful step.

## Done

## Next

## Open decisions
"""


def create_task_folder(
    tasks_dir: Path, spec: TaskSpec, created: datetime, *, plan: str = PLAN_STUB
) -> Path:
    """Create ``<tasks_dir>/YYYY-MM-DD_<slug>_<id>/`` with its standard contents."""
    folder = tasks_dir / folder_name(created, spec.slug, spec.id)
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "output").mkdir()
    (folder / "logs").mkdir()
    write_task_yaml(folder, spec)
    (folder / "plan.md").write_text(plan)
    (folder / "progress.md").write_text(PROGRESS_STUB)
    (folder / "report.md").write_text(report.skeleton(spec.title))
    return folder


def write_task_yaml(folder: Path, spec: TaskSpec) -> None:
    (folder / "task.yaml").write_text(
        yaml.safe_dump(spec.to_dict(), sort_keys=False, allow_unicode=True, width=100)
    )


def read_task_yaml(folder: Path) -> TaskSpec:
    return TaskSpec.from_dict(yaml.safe_load((folder / "task.yaml").read_text()))
