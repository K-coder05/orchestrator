from __future__ import annotations

import json
import os
import sys

import pytest

from nightshift.adapters.claude import ClaudeAdapter
from nightshift.config import Secrets
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState

from conftest import FIXTURES

SECRET = "sk-test-secret-0042"


def fake_provider(name, settings):
    assert name == "claude"
    a = ClaudeAdapter(settings)
    a.binary = sys.executable
    a.extra_args = []
    orig = a._base_argv

    def base_argv(perms):
        argv = orig(perms)
        return [sys.executable, str(FIXTURES / "fake_claude.py")] + argv[1:]

    a._base_argv = base_argv
    return a


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("FAKE_STREAM", str(FIXTURES / "claude_stream.jsonl"))
    for k in ("FAKE_LEAK", "FAKE_RESULT", "FAKE_EXIT", "FAKE_NEEDS_YOU", "FAKE_SLEEP",
              "FAKE_IGNORE_TERM", "FAKE_STEP_DELAY", "FAKE_RESUME_FAIL", "FAKE_LIMIT_AT",
              "FAKE_RESETS_AT"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def test_foreground_attempt_end_to_end(cfg, db, env):
    new = create_task(cfg, db, "Write a haiku to output/haiku.txt")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    folder = new.folder

    assert res.state == TaskState.DONE and res.exit_code == 0
    # One Attempt per session, with the session id recorded.
    [attempt] = db.attempts(new.spec.id)
    assert attempt["session_id"] == "f5e745a1-aea4-451e-bef1-64b1544eb8de"
    assert attempt["outcome"] == "completed" and attempt["ended_at"]
    assert "<agent protocol>" in json.loads(attempt["command"])

    # Every transition, with reasons.
    rows = db.transitions(new.spec.id)
    assert [r["to_state"] for r in rows] == ["queued", "running", "done"]
    assert all(r["reason"] and r["at"] for r in rows)

    # The agent protocol was injected and the prompt went in on stdin.
    inv = json.loads((folder / "logs" / "fake_invocation.json").read_text())
    protocol = inv["argv"][inv["argv"].index("--append-system-prompt") + 1]
    assert "Nightshift agent protocol" in protocol and str(folder) in protocol
    assert inv["stdin"] == "Write a haiku to output/haiku.txt"

    # Usage recorded per Attempt and filled into the report.
    [u] = db.usage_for_task(new.spec.id)
    assert u["output_tokens"] == 4059 and u["requests"] == 6
    text = (folder / "report.md").read_text()
    assert "## 8. Time, usage and cost per agent" in text
    assert "| claude | 1 |" in text and "$0.0071" in text
    assert "Wrote it." in text
    assert (folder / "logs" / "attempt-1.claude.jsonl").stat().st_size > 0


def test_secrets_never_logged_or_reported(cfg, db, env):
    cfg.secrets = Secrets({"API_KEY": SECRET})
    env.setenv("FAKE_LEAK", SECRET)
    new = create_task(cfg, db, "leaky task")
    run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    for path in new.folder.rglob("*"):
        if path.is_file() and path.name != "fake_invocation.json":
            assert SECRET not in path.read_text(errors="replace"), path
    assert "[REDACTED]" in (new.folder / "report.md").read_text()
    assert SECRET not in (new.folder / "logs" / "attempt-1.claude.stderr.log").read_text()


def test_partial_result_from_agent_report(cfg, db, env):
    env.setenv("FAKE_RESULT", "partial")
    new = create_task(cfg, db, "p")
    assert run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider).state == TaskState.PARTIAL


def test_crash_is_failed(cfg, db, env):
    cfg.data["retries"]["max_retries"] = 0  # budget spent: straight to failed
    env.setenv("FAKE_EXIT", "3")
    env.setenv("FAKE_RESULT", "none")
    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    assert res.state == TaskState.FAILED
    text = (new.folder / "report.md").read_text()
    assert "**failed**" in text and "code 3" in text
    assert db.attempts(new.spec.id)[0]["exit_code"] == 3


def test_error_result_is_failed(cfg, db, env):
    cfg.data["retries"]["max_retries"] = 0  # budget spent: straight to failed
    env.setenv("FAKE_STREAM", str(FIXTURES / "claude_stream_error.jsonl"))
    env.setenv("FAKE_RESULT", "none")
    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    assert res.state == TaskState.FAILED
    assert "selected model" in db.transitions(new.spec.id)[-1]["reason"]


def test_needs_you(cfg, db, env):
    env.setenv("FAKE_NEEDS_YOU", "1")
    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    assert res.state == TaskState.NEEDS_YOU
    assert "May I push?" in (new.folder / "report.md").read_text()


def test_missing_binary_is_failed(cfg, db, env):
    cfg.data["retries"]["max_retries"] = 0  # budget spent: straight to failed
    def provider(name, settings):
        return ClaudeAdapter({"binary": "/nonexistent/claude"})

    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=provider)
    assert res.state == TaskState.FAILED
    assert "could not start" in db.transitions(new.spec.id)[-1]["reason"]
