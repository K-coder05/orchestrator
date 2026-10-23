"""The real Gemini CLI with scripted model responses (opt-in).

Runs the installed ``gemini`` with Nightshift's per-Task settings, policy and
environment, but with ``--fake-responses-non-strict``: the CLI replays model
responses from a file instead of calling Google, so no sign-in or quota is
needed. Everything else is the real CLI: stream-json, the settings layers,
folder trust, the policy engine, the per-command shell sandbox and sessions.
This is how the adapter was checked on 0.65.0 (docs/adapters.md); re-run it
after every Gemini CLI upgrade:

    NIGHTSHIFT_GEMINI_CLI=1 .venv/bin/python -m pytest tests/test_gemini_cli.py

Shell commands that the sandbox stops and that then exit non-zero make
0.65.0 wait forever for a "sandbox expansion" answer, so the probes below end
in ``; true`` and the one that shows the hang has a short timeout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from nightshift.adapters.base import EventKind, TaskContext
from nightshift.adapters.gemini import GeminiAdapter
from nightshift.config import load_config
from nightshift.daemon import QueueRunner
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.permissions import build_policy
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import now

from conftest import write_test_env

pytestmark = pytest.mark.skipif(
    os.environ.get("NIGHTSHIFT_GEMINI_CLI") != "1" or shutil.which("gemini") is None,
    reason="real-CLI test; set NIGHTSHIFT_GEMINI_CLI=1 (needs the gemini CLI, no sign-in)",
)

MODEL = "gemini-2.5-flash"  # a fixed model: no "auto" router call to script


def _usage(p: int, c: int, cached: int = 0) -> dict:
    return {"promptTokenCount": p, "candidatesTokenCount": c, "cachedContentTokenCount": cached,
            "totalTokenCount": p + c}


def calls(*fcs: tuple[str, dict], p: int = 100, c: int = 10) -> dict:
    """One model turn asking for these tool calls."""
    parts = [{"functionCall": {"name": n, "args": a}} for n, a in fcs]
    return {"method": "generateContentStream", "response": [{"candidates": [{
        "content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": _usage(p, c)}]}


def text(t: str, p: int = 100, c: int = 10) -> dict:
    return {"method": "generateContentStream", "response": [{"candidates": [{
        "content": {"role": "model", "parts": [{"text": t}]}, "finishReason": "STOP",
        "index": 0}], "usageMetadata": _usage(p, c)}]}


def sh(command: str) -> tuple[str, dict]:
    return ("run_shell_command", {"command": command, "description": "check"})


class Scripted(GeminiAdapter):
    """The adapter, with each command given the next script of responses."""

    def __init__(self, settings, scripts: list[list[dict]], folder: Path, home: Path):
        super().__init__({"model": MODEL, **settings})
        self.home = home  # sign-in files would be linked from here: none needed
        self.api_key = "scripted-responses-need-no-real-key"
        self.scripts, self.folder = scripts, folder

    def _scripted(self, cmd):
        f = self.folder / f"script-{uuid.uuid4().hex[:8]}.jsonl"
        f.write_text("\n".join(json.dumps(r) for r in self.scripts.pop(0)) + "\n")
        cmd.argv = cmd.argv + ["--fake-responses-non-strict", str(f)]
        return cmd

    def build_command(self, task, workspace, permissions):
        return self._scripted(super().build_command(task, workspace, permissions))

    def resume_command(self, session_id, prompt, **kw):
        return self._scripted(super().resume_command(session_id, prompt, **kw))


@pytest.fixture
def setup(tmp_path):
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    ro = tmp_path / "career"
    ro.mkdir()
    (ro / "master.tex").write_text("read-only original\n")
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "agents": {"gemini": {"model": MODEL}},
        "permissions": {"read_only_dirs": [str(ro)]}}))
    write_test_env(config_dir)
    cfg = load_config(config_dir, host=FakeHost())
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "applications.csv").write_text("DATA-SECRET\n")
    return cfg, home, ro


def _run(cfg, home, script, *, internet="off", resume=None, timeout=60):
    """One Attempt's command run by hand; returns (adapter, events, stdout)."""
    ws = cfg.agents_root / "tasks" / "t1"
    ws.mkdir(parents=True, exist_ok=True)
    a = Scripted({}, [script], ws.parent, home)
    perms = a.write_permissions(ws, build_policy(cfg, task_id="t1", workspace=ws,
                                                 internet=internet))
    task = TaskContext(id="t1", prompt="do it", task_type="coding", internet=internet,
                       protocol="# protocol")
    cmd = (a.resume_command(resume, "continue", task=task, permissions=perms) if resume
           else a.build_command(task, ws, perms))
    try:
        p = subprocess.run(cmd.argv, input=cmd.stdin, cwd=ws, env=os.environ | cmd.env,
                           capture_output=True, text=True, timeout=timeout)
        out = p.stdout
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
    events = [ev for line in out.splitlines() for ev in a.parse_stream(line)]
    return a, events, out


def _results(events) -> list[tuple[bool, str]]:
    """(failed?, output) of each tool result, in order."""
    out = []
    for ev in events:
        if ev.kind == EventKind.TOOL_RESULT:
            out.append([ev.data["is_error"], ""])
        elif ev.kind == EventKind.ERROR and ev.data.get("tool_error") and out:
            out[-1][1] = ev.data["message"]
    return [tuple(x) for x in out]


def test_stream_json_events_match_the_fixture(setup):
    cfg, home, _ = setup
    a, events, out = _run(cfg, home, [
        calls(("read_file", {"file_path": "progress.md"}),
              ("write_file", {"file_path": "output/notes.md", "content": "# Notes\n"}),
              sh("ls output")),
        text("Wrote output/notes.md.", p=300, c=20)])
    kinds = {json.loads(line)["type"] for line in out.splitlines() if line.startswith("{")}
    assert kinds == {"init", "message", "tool_use", "tool_result", "result"}
    assert [e.data["name"] for e in events if e.kind == EventKind.TOOL_CALL] == [
        "read_file", "write_file", "run_shell_command"]
    assert [f for f, _ in _results(events)] == [True, False, False]  # no progress.md yet
    assert [e.data["path"] for e in events if e.kind == EventKind.FILE_CHANGE] == [
        "output/notes.md"]
    assert a.result_ok() is True and a.session_id()
    [u] = a.usage_from_run()
    assert (u.model, u.input_tokens, u.output_tokens) == (MODEL, 400, 30)


def test_file_tools_and_policy_confine_the_task(setup):
    cfg, home, ro = setup
    outside = cfg.agents_root.parent / "outside"
    outside.mkdir()
    _, events, _ = _run(cfg, home, [calls(
        ("write_file", {"file_path": f"{outside}/x.txt", "content": "x"}),
        ("replace", {"file_path": f"{ro}/master.tex", "old_string": "original",
                     "new_string": "edited", "instruction": "x"}),
        ("read_file", {"file_path": f"{ro}/master.tex"}),
        ("read_file", {"file_path": f"{cfg.data_dir}/applications.csv"}),
        sh(f"cat {cfg.data_dir}/applications.csv"),
        ("web_fetch", {"prompt": "https://example.com"}),
        sh("git status; true"),
        ("write_file", {"file_path": "output/inside.txt", "content": "inside"})), text("Done.")])
    res = _results(events)
    assert [f for f, _ in res] == [True, True, False, True, True, True, True, False]
    assert "outside the allowed workspace" in res[0][1]  # file tools: workspace only
    assert "read-only or off limits" in res[1][1]  # Nightshift's admin rule
    assert "Shell commands may not touch" in res[4][1]  # Nightshift's admin rule
    assert "not found" in res[5][1]  # internet off: no web tools at all
    assert res[6][1] == "Tool execution denied by policy."  # git in an untrusted folder
    assert (ro / "master.tex").read_text() == "read-only original\n"
    assert not (outside / "x.txt").exists()
    assert (cfg.agents_root / "tasks" / "t1" / "output" / "inside.txt").exists()


def test_shell_sandbox_has_no_home_data_or_network(setup):
    cfg, home, _ = setup
    canary = Path.home() / f".nightshift-gemini-canary-{uuid.uuid4().hex[:8]}"
    canary.write_text("HOME-CANARY\n")
    try:
        cli_home = cfg.data_dir / "permissions" / "t1" / "gemini-home"
        _, events, out = _run(cfg, home, [calls(
            sh(f"cat {canary}; true"),
            sh("cat ../../data/applications.csv; true"),  # relative: past the policy rule
            sh("cat $GEMINI_CLI_HOME/.gemini/settings.json; true"),
            sh(f"ls {Path.home()}/.gemini 2>&1; true"),
            sh("curl -sS https://example.com 2>&1 | head -1; true"),
            sh("echo ok > output.txt && cat output.txt"),
            sh("echo key=${GEMINI_API_KEY:-unset}; true")), text("Done.")])
        res = _results(events)
        assert not any(f for f, _ in res), res
        assert "HOME-CANARY" not in out and "DATA-SECRET" not in out
        assert '"toolSandboxing"' not in out  # the CLI home is unreadable
        assert out.count("Operation not permitted") >= 4
        assert "Could not resolve host" in out
        assert '"output":"ok"' in out  # PATH reaches bash, redirection allowed
        assert "key=unset" in out  # the CLI's API key never reaches a command
        assert cli_home.is_dir()
    finally:
        canary.unlink()


def test_a_denied_command_that_fails_hangs_the_cli(setup):
    """The known 0.65.0 gap (docs/safety.md): after a sandbox denial with a
    non-zero exit the CLI waits for a "sandbox expansion" answer that never
    comes in headless mode. If this starts passing quickly, the gap is fixed."""
    cfg, home, _ = setup
    _, events, _ = _run(cfg, home, [calls(sh(f"cat {Path.home()}/.zshrc")), text("Done.")],
                        timeout=20)
    assert not any(e.kind == EventKind.RESULT for e in events)


def test_the_stall_guard_ends_the_hang(setup):
    """Through Nightshift the hang above is stopped by the stall guard (a
    Failed Attempt), not the runtime cap; a long silent command is not."""
    cfg, home, _ = setup
    db = Database(cfg.db_path, tz=cfg.timezone)
    cfg.data["agents"]["gemini"]["stall_minutes"] = 0.1  # 6 s
    try:
        hang = create_task(cfg, db, "hang", agent="gemini", long=False)
        busy = create_task(cfg, db, "busy", agent="gemini", long=False)
        scripts = [[calls(sh(f"cat {Path.home()}/.zshrc")), text("Done.")],
                   [calls(sh("sleep 12; echo slept")), text("Done.")]]

        def provider(name, settings):
            return Scripted(settings, scripts, hang.folder.parent, home)

        start = time.monotonic()
        res = run_attempt(cfg, db, hang.spec.id, adapter_provider=provider,
                          max_runtime_seconds=120)
        assert time.monotonic() - start < 60
        [a] = db.attempts(hang.spec.id)
        assert res.state == TaskState.RETRY_WAIT and "stalled" in a["end_reason"], a["end_reason"]
        res = run_attempt(cfg, db, busy.spec.id, adapter_provider=provider,
                          max_runtime_seconds=120)
        [b] = db.attempts(busy.spec.id)
        assert "stalled" not in (b["end_reason"] or ""), b["end_reason"]
        assert '"output":"slept"' in Path(b["log_path"]).read_text()
    finally:
        db.close()


def test_workspace_settings_never_load_and_resume_keeps_the_session(setup):
    cfg, home, _ = setup
    a, _, _ = _run(cfg, home, [calls(("write_file", {"file_path": "a.txt", "content": "a"})),
                               text("One.")])
    sid = a.session_id()
    ws = cfg.agents_root / "tasks" / "t1"
    # An agent-written workspace settings file trying to switch the sandbox off.
    (ws / ".gemini").mkdir()
    (ws / ".gemini" / "settings.json").write_text(json.dumps(
        {"security": {"toolSandboxing": False}}))
    b, events, out = _run(cfg, home, [calls(sh("ls $HOME; true")), text("Two.", p=50, c=5)],
                          resume=sid)
    assert b.session_id() == sid
    assert "Operation not permitted" in out  # still sandboxed
    [u] = b.usage_from_run()
    assert (u.input_tokens, u.output_tokens) == (150, 15)  # this Attempt's own tokens only
    sessions = list((cfg.data_dir / "permissions" / "t1" / "gemini-home").rglob("chats/session-*"))
    assert len(sessions) == 1


def test_pause_and_resume_through_nightshift(setup):
    cfg, home, _ = setup
    db = Database(cfg.db_path, tz=cfg.timezone)
    cfg.data["retries"]["max_retries"] = 0
    try:
        new = create_task(cfg, db, "Write a.txt, b.txt and c.txt, then report.md.",
                          agent="gemini", long=False, simulate_rate_limit_after=1)
        report = ("# Files\n\n## 1. Result\ndone: wrote three files\n\n"
                  "## 3. Summary\nWrote them.\n")
        scripts = [
            [calls(("write_file", {"file_path": "output/a.txt", "content": "a"})),
             calls(sh("sleep 3"), p=200),  # the stop lands while this runs
             calls(("write_file", {"file_path": "output/b.txt", "content": "b"}), p=300),
             text("not reached", p=1, c=1)],
            [calls(("write_file", {"file_path": "output/b.txt", "content": "b"}), p=400),
             calls(("write_file", {"file_path": "output/c.txt", "content": "c"}), p=500),
             calls(("write_file", {"file_path": "report.md", "content": report}), p=600),
             text("Done.", p=700)],
        ]

        def provider(name, settings):
            assert name == "gemini"
            return Scripted(settings, scripts, new.folder.parent, home)

        t = [now(cfg.timezone)]
        qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=lambda: t[0])
        qr.run_next()
        assert db.task_state(new.spec.id) == TaskState.PAUSED_USAGE
        t[0] += timedelta(minutes=10)
        qr.run_next()
        assert db.task_state(new.spec.id) == TaskState.DONE
        first, second = db.attempts(new.spec.id)
        assert second["session_id"] == first["session_id"] and "--resume" in second["command"]
        assert {u["agent"] for u in db.usage_for_task(new.spec.id)} == {"gemini"}
        assert "| gemini | 2 |" in (new.folder / "report.md").read_text()
    finally:
        db.close()


def test_internet_on_task_never_fetches_a_denied_domain(setup):
    cfg, home, _ = setup
    _, events, out = _run(cfg, home, [calls(
        ("web_fetch", {"prompt": "Summarize https://www.LinkedIn.com/jobs/view/1"}),
        ("google_web_search", {"query": "site:linkedin.com internships"}),
        sh("curl -sS https://example.com 2>&1 | head -1; true")), text("Done.")],
        internet="on")
    res = _results(events)
    assert [f for f, _ in res] == [True, True, False]
    assert all("never fetched" in m for _, m in res[:2])  # Nightshift's admin rule
    assert "Could not resolve host" in out  # the shell still has no network
