from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from nightshift import report
from nightshift.ids import is_valid_id, new_task_id, random_id, slugify
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.workspace import TaskSpec, create_task_folder, folder_name, read_task_yaml

TASK_YAML_FIELDS = (
    "id title slug prompt template task_type agent agent_override priority deadline run_at "
    "internet long estimated_minutes max_runtime_minutes approved approved_at schedule_name "
    "created_at"
).split()


def test_random_id_is_4_lowercase_base36():
    for _ in range(500):
        i = random_id()
        assert re.fullmatch(r"[0-9a-z]{4}", i)
        assert is_valid_id(i)


def test_new_task_id_skips_taken_ids():
    seq = iter(["aaaa", "bbbb", "cccc"])
    assert new_task_id({"aaaa", "bbbb"}, generator=lambda: next(seq)) == "cccc"


def test_new_task_id_rejects_bad_generator():
    with pytest.raises(ValueError):
        new_task_id(set(), generator=lambda: "ABCD")


def test_new_task_id_gives_up_when_exhausted():
    with pytest.raises(RuntimeError):
        new_task_id(lambda i: True, max_tries=5)


def test_slugify():
    assert slugify("Tailor my resume for Acme SWE intern!") == "tailor-my-resume-for-acme-swe"
    assert slugify("!!!") == "task"
    assert len(slugify("x" * 200)) <= 40


def test_folder_name_format():
    created = datetime(2026, 10, 20, 23, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert folder_name(created, "resume-acme-swe-intern", "k3f9") == "2026-10-20_resume-acme-swe-intern_k3f9"


def _spec(**kw):
    base = dict(
        id="k3f9", title="T", slug="resume-acme", prompt="p", task_type="general", agent="claude",
        priority="normal", internet="off", created_at="2026-10-20T23:00:00-07:00",
    )
    return TaskSpec(**(base | kw))


def test_create_task_folder_layout(tmp_path):
    created = datetime(2026, 10, 20, 23, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    folder = create_task_folder(tmp_path, _spec(), created)
    assert folder.name == "2026-10-20_resume-acme_k3f9"
    assert {p.name for p in folder.iterdir()} == {
        "task.yaml", "plan.md", "progress.md", "output", "logs", "report.md"
    }
    assert (folder / "output").is_dir() and (folder / "logs").is_dir()
    data = yaml.safe_load((folder / "task.yaml").read_text())
    assert list(data) == TASK_YAML_FIELDS
    assert read_task_yaml(folder) == _spec()
    # The report skeleton has every §2.11 section.
    _, sections = report.parse_sections((folder / "report.md").read_text())
    assert list(sections) == list(range(1, 10))


def test_create_task_folder_never_reuses_a_folder(tmp_path):
    created = datetime(2026, 10, 20, tzinfo=ZoneInfo("America/Los_Angeles"))
    create_task_folder(tmp_path, _spec(), created)
    with pytest.raises(FileExistsError):
        create_task_folder(tmp_path, _spec(), created)


def test_create_task_end_to_end_folder_and_db(cfg, db):
    new = create_task(cfg, db, "Tailor my resume for Acme SWE intern", task_type="coding")
    folder = new.folder
    assert folder.parent == cfg.tasks_dir
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_tailor-my-resume-for-acme-swe_[0-9a-z]{4}", folder.name)
    assert folder.name.endswith("_" + new.spec.id)
    for name in ("task.yaml", "plan.md", "progress.md", "report.md"):
        assert (folder / name).is_file()
    for name in ("output", "logs"):
        assert (folder / name).is_dir()
    spec = read_task_yaml(folder)
    assert spec.internet == "off"  # coding default
    assert spec.agent == "claude" and spec.agent_override is False
    assert db.task_state(new.spec.id) == TaskState.QUEUED
    [t] = db.transitions(new.spec.id)
    assert t["from_state"] is None and t["to_state"] == "queued" and t["reason"] and t["at"]


def test_task_ids_unique_across_many_tasks(cfg, db):
    ids = {create_task(cfg, db, f"task {i}").spec.id for i in range(30)}
    assert len(ids) == 30
