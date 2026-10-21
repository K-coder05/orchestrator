"""Free-form planning, the y/n confirmation and plan approvals (§2.4)."""

from __future__ import annotations

import json
import sys

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli, planning
from nightshift.adapters.claude import ClaudeAdapter
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.workspace import read_task_yaml

from conftest import FIXTURES


def plan_provider(name, settings):
    assert name == "claude"
    a = ClaudeAdapter(settings)
    orig = a.planning_command

    def planning_command(prompt, *, system, schema):
        cmd = orig(prompt, system=system, schema=schema)
        cmd.argv = [sys.executable, str(FIXTURES / "fake_claude_plan.py")] + cmd.argv[1:]
        return cmd

    a.planning_command = planning_command
    return a


@pytest.fixture
def planner(monkeypatch, tmp_path):
    log = tmp_path / "plan_calls.jsonl"
    monkeypatch.setattr(cli, "planning_provider", plan_provider)
    monkeypatch.setenv("FAKE_PLAN_LOG", str(log))
    for k in ("FAKE_MINUTES", "FAKE_TYPE", "FAKE_PLAN_FAIL"):
        monkeypatch.delenv(k, raising=False)

    def calls():
        return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []

    monkeypatch.calls = calls
    return monkeypatch


def _orch(config_dir, *args, input=None):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args], input=input)


# -- the planning call ------------------------------------------------------

def test_planning_command_has_no_tools_and_a_schema():
    a = ClaudeAdapter({"model": "opus", "planning_model": "haiku", "extra_args": ["--foo"]})
    cmd = a.planning_command("Task: x", system="SYS", schema={"type": "object"})
    argv = cmd.argv
    assert argv[:4] == ["claude", "-p", "--output-format", "json"]
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--tools") + 1] == ""
    assert "--restricted" in argv and "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYS"
    assert json.loads(argv[argv.index("--json-schema") + 1]) == {"type": "object"}
    assert "--foo" not in argv and "--dangerously-skip-permissions" not in argv
    assert cmd.stdin == "Task: x"
    # planning_model falls back to model.
    assert "opus" in ClaudeAdapter({"model": "opus"}).planning_command("x", system="", schema={}).argv


def test_parse_planning_output():
    a = ClaudeAdapter()
    good = json.dumps({"type": "result", "is_error": False, "structured_output": {"a": 1},
                       "modelUsage": {"m": {"outputTokens": 3, "costUSD": 0.1}}})
    r = a.parse_planning_output(good, 0)
    assert r.ok and r.structured == {"a": 1} and r.usage[0].output_tokens == 3
    r = a.parse_planning_output(json.dumps({"type": "result", "is_error": True, "result": "boom"}), 1)
    assert not r.ok and r.error == "boom"
    assert not a.parse_planning_output("garbage", 2).ok
    r = a.parse_planning_output(json.dumps({"type": "result", "result": "not json"}), 0)
    assert not r.ok and "JSON" in r.error


def test_estimate_returns_minutes_and_type(cfg, planner):
    planner.setenv("FAKE_MINUTES", "45")
    planner.setenv("FAKE_TYPE", "research")
    p = planning.estimate(cfg, "Compare three vector DBs", adapter_provider=plan_provider)
    assert (p.estimated_minutes, p.task_type, p.source) == (45, "research", "planning call")
    assert p.usage and p.agent == "claude"
    [c] = planner.calls()
    assert c["kind"] == "estimate" and "Compare three vector DBs" in c["stdin"]
    schema = json.loads(c["argv"][c["argv"].index("--json-schema") + 1])
    assert "research" in schema["properties"]["task_type"]["enum"]
    assert c["cwd"].endswith("data/planning")  # never a Task folder or the repo


def test_estimate_unknown_type_falls_back_and_failure_raises(cfg, planner):
    planner.setenv("FAKE_TYPE", "astrology")
    p = planning.estimate(cfg, "x", adapter_provider=plan_provider)
    assert p.task_type == "general" and "astrology" in p.reason
    planner.setenv("FAKE_PLAN_FAIL", "estimate")
    with pytest.raises(planning.PlanningError, match="overloaded"):
        planning.estimate(cfg, "x", adapter_provider=plan_provider)


def test_threshold(cfg):
    assert not planning.needs_plan(cfg, 30)
    assert planning.needs_plan(cfg, 31)
    assert not planning.needs_plan(cfg, None)


# -- orch add ---------------------------------------------------------------

def test_short_task_prints_estimate_asks_and_queues_without_plan(config_dir, cfg, db, planner):
    planner.setenv("FAKE_MINUTES", "30")
    planner.setenv("FAKE_TYPE", "comedy")
    r = _orch(config_dir, "add", "Write five puns", input="y\n")
    assert r.exit_code == 0, r.output
    assert "Estimate:  30 minutes" in r.output and "Task type: comedy (proposed)" in r.output
    assert "Create this Task? [y/N]" in r.output
    [row] = db.list_tasks()
    assert row["state"] == "queued" and row["task_type"] == "comedy"
    assert row["estimated_minutes"] == 30 and row["approved"] == 1
    assert [c["kind"] for c in planner.calls()] == ["estimate"]  # no plan call
    assert "No plan was needed" in (cfg.tasks_dir.glob(f"*_{row['id']}").__next__() / "plan.md").read_text()
    assert [t["to_state"] for t in db.transitions(row["id"])] == ["queued"]  # never awaiting_approval
    assert db.approvals(row["id"]) == []
    assert len(db.planning_usage(row["id"])) == 1


def test_no_at_confirmation_creates_nothing(config_dir, db, planner):
    r = _orch(config_dir, "add", "Write five puns", input="n\n")
    assert r.exit_code == 0 and "Not queued." in r.output
    assert db.list_tasks() == []
    [u] = db.planning_usage()  # the call's usage is still recorded
    assert u["task_id"] is None and u["kind"] == "estimate"


def test_type_override_and_yes_skips_prompt(config_dir, db, planner):
    planner.setenv("FAKE_TYPE", "general")
    r = _orch(config_dir, "add", "Fix the parser bug", "--type", "coding", "--yes")
    assert r.exit_code == 0, r.output
    assert "[y/N]" not in r.output
    assert "Task type: coding (from --type; proposed general)" in r.output
    [row] = db.list_tasks()
    assert row["task_type"] == "coding" and row["state"] == "queued"


def test_planning_failure_queues_nothing(config_dir, db, planner):
    planner.setenv("FAKE_PLAN_FAIL", "estimate")
    r = _orch(config_dir, "add", "x", "--yes")
    assert r.exit_code == 1 and "Nothing was queued" in r.output
    assert db.list_tasks() == []


def test_long_task_writes_plan_and_approval_queues(config_dir, cfg, db, planner):
    planner.setenv("FAKE_MINUTES", "90")
    r = _orch(config_dir, "add", "Write a research report on X", input="y\ny\n")
    assert r.exit_code == 0, r.output
    assert "Over 30 minutes" in r.output
    assert "## Goal" in r.output and "Approve this plan? [y/N]" in r.output
    [row] = db.list_tasks()
    tid = row["id"]
    assert row["state"] == "queued" and row["approved"] == 1 and row["approved_at"]
    folder = cfg.tasks_dir.glob(f"*_{tid}").__next__()
    assert (folder / "plan.md").read_text().startswith("# Plan\n\n## Goal")
    assert [t["to_state"] for t in db.transitions(tid)] == ["awaiting_approval", "queued"]
    [a] = db.approvals(tid)
    assert a["decision"] == "approved" and a["via"] == "orch add"
    assert a["estimated_minutes"] == 90 and len(a["plan_sha256"]) == 64
    spec = read_task_yaml(folder)
    assert spec.approved is True and spec.approved_at == row["approved_at"]
    assert [c["kind"] for c in planner.calls()] == ["estimate", "plan"]
    plan_call = planner.calls()[1]
    assert "Write a research report on X" in plan_call["stdin"]
    assert [u["kind"] for u in db.planning_usage(tid)] == ["estimate", "plan"]


def test_long_task_declined_stays_awaiting_approval(config_dir, cfg, db, planner):
    planner.setenv("FAKE_MINUTES", "31")
    r = _orch(config_dir, "add", "Big job", "--yes", input="n\n")
    assert r.exit_code == 0, r.output
    [row] = db.list_tasks()
    assert row["state"] == "awaiting_approval" and row["approved"] == 0
    assert "orch approve" in r.output
    assert db.approvals(row["id"]) == []
    # --yes skips only the confirmation; no answer to the plan prompt is a no.
    r = _orch(config_dir, "add", "Another big job", "--yes")
    assert r.exit_code == 0
    assert {x["state"] for x in db.list_tasks()} == {"awaiting_approval"}


def test_plan_call_failure_leaves_task_awaiting_approval(config_dir, cfg, db, planner):
    planner.setenv("FAKE_MINUTES", "120")
    planner.setenv("FAKE_PLAN_FAIL", "plan")
    r = _orch(config_dir, "add", "Big job", "--yes")
    assert r.exit_code == 0, r.output
    [row] = db.list_tasks()
    assert row["state"] == "awaiting_approval"
    folder = cfg.tasks_dir.glob(f"*_{row['id']}").__next__()
    assert "has not been written yet" in (folder / "plan.md").read_text()
    assert "plan call on claude failed" in (folder / "logs" / "nightshift.log").read_text()


# -- orch approve / deny ------------------------------------------------------

def _awaiting(cfg, db, prompt="big"):
    return create_task(cfg, db, prompt, estimated_minutes=60, needs_approval=True)


def test_approve_and_deny(config_dir, cfg, db):
    a, b = _awaiting(cfg, db, "alpha"), _awaiting(cfg, db, "beta")
    assert a.spec.approved is False
    r = _orch(config_dir, "approve", a.spec.id)
    assert r.exit_code == 0 and "approved and queued" in r.output
    assert db.task_state(a.spec.id) == TaskState.QUEUED
    assert db.approvals(a.spec.id)[0]["via"] == "orch approve"
    assert read_task_yaml(a.folder).approved is True

    r = _orch(config_dir, "deny", b.spec.id)
    assert r.exit_code == 0 and "denied" in r.output
    assert db.task_state(b.spec.id) == TaskState.CANCELLED
    [d] = db.approvals(b.spec.id)
    assert d["decision"] == "denied" and d["via"] == "orch deny"
    assert "**cancelled**" in (b.folder / "report.md").read_text()
    assert db.transitions(b.spec.id)[-1]["reason"] == "plan denied (orch deny)"

    # Only Tasks awaiting approval.
    for cmd, tid in (("approve", a.spec.id), ("deny", b.spec.id)):
        r = _orch(config_dir, cmd, tid)
        assert r.exit_code == 1 and "not awaiting approval" in r.output
    assert _orch(config_dir, "approve", "zzzz").exit_code == 1
    assert len(db.approvals(a.spec.id)) == 1


def test_show_lists_approvals(config_dir, cfg, db):
    t = _awaiting(cfg, db)
    _orch(config_dir, "approve", t.spec.id)
    r = _orch(config_dir, "show", t.spec.id)
    assert "Approvals:" in r.output and "approved via orch approve" in r.output
    assert "estimated_minutes" in r.output


# -- templates ----------------------------------------------------------------

def test_template_tasks_use_template_estimate_and_type(cfg, db):
    tpl = {"name": "resume-tailor", "task_type": "resume", "estimated_minutes": 20}

    p = planning.from_template(tpl)
    assert (p.estimated_minutes, p.task_type, p.source) == (20, "resume", "template resume-tailor")
    new = create_task(
        cfg, db, "Tailor for https://example.org/job", task_type=p.task_type,
        estimated_minutes=p.estimated_minutes, template="resume-tailor",
        needs_approval=planning.needs_plan(cfg, p.estimated_minutes),
    )
    row = db.get_task(new.spec.id)
    assert row["task_type"] == "resume" and row["estimated_minutes"] == 20
    assert row["template"] == "resume-tailor" and row["state"] == "queued"
    with pytest.raises(planning.PlanningError):
        planning.from_template({"name": "bad", "task_type": "x"})


def test_threshold_is_configurable(tmp_path):
    from nightshift.config import ConfigError, load_config
    from nightshift.host import FakeHost

    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump(
        {"paths": {"agents_root": str(tmp_path / "a")}, "planning": {"approval_threshold_minutes": 60}}))
    c = load_config(d, host=FakeHost())
    assert not planning.needs_plan(c, 45)
    (d / "config.yaml").write_text(yaml.safe_dump({"planning": {"agent": "nobody"}}))
    with pytest.raises(ConfigError):
        load_config(d, host=FakeHost())
