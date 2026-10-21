"""The Codex adapter (§2.9) and coding failover (§2.7, §2.8, Acceptance Test 3).

The adapter was verified against codex-cli 0.163.0 (docs/adapters.md); these
tests run it against the fake `codex exec` in tests/fixtures/fake_codex.py,
which replays a recorded real stream.
"""

from __future__ import annotations

import json
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from nightshift import failover, pauses, usage
from nightshift.adapters import get_adapter
from nightshift.adapters.base import Event, EventKind, Permissions, TaskContext
from nightshift.adapters.codex import (
    CodexAdapter,
    config_args,
    parse_reset_text,
    strict_schema,
    toml_value,
)
from nightshift.daemon import QueueRunner
from nightshift.host import FakeHost
from nightshift.permissions import build_policy
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import iso, now
from nightshift.workspace import read_task_yaml

from conftest import FIXTURES
from test_runner import env, fake_provider as fake_claude_provider  # noqa: F401  (fixture)

LA = ZoneInfo("America/Los_Angeles")
CODEX_SID = "01a16084-0da3-70f0-8ade-cab04834c0ac"
FAKE_CODEX = [sys.executable, str(FIXTURES / "fake_codex.py")]


class Clock:
    def __init__(self):
        self.t = now("America/Los_Angeles")

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def fake_codex(settings) -> CodexAdapter:
    a = CodexAdapter(settings)
    orig = a._flags

    def flags(**kw):
        return FAKE_CODEX + orig(**kw)[1:]

    a._flags = flags
    return a


def provider(name, settings):
    """Both agents through their real adapters, with fake CLIs."""
    if name == "codex":
        return fake_codex(settings)
    return fake_claude_provider(name, settings)


@pytest.fixture
def cenv(env, monkeypatch):  # noqa: F811
    for k in ("FAKE_CODEX_LIMIT", "FAKE_CODEX_LIMIT_ONCE", "FAKE_CODEX_LIMIT_TEXT",
              "FAKE_CODEX_EXIT", "FAKE_CODEX_RESUME_FAIL", "FAKE_CODEX_STEP_DELAY",
              "FAKE_CODEX_PROBE", "FAKE_CODEX_PLAN", "FAKE_CODEX_RESULT", "FAKE_CODEX_PROBE_LOG"):
        monkeypatch.delenv(k, raising=False)
    env.setenv("FAKE_STEP_DELAY", "0.05")  # so a stop lands mid-stream
    return env


def _codex_runs(folder: Path) -> list[dict]:
    path = folder / "logs" / "fake_codex_invocations.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _claude_runs(folder: Path) -> list[dict]:
    path = folder / "logs" / "fake_invocations.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _coding(cfg, db, prompt="Write output/fizzbuzz.py", **kw):
    kw.setdefault("long", False)  # not gated to the overnight window in tests
    return create_task(cfg, db, prompt, task_type="coding", **kw)


def _overrides(argv: list[str]) -> dict:
    """The `-c key=value` overrides of an argv, parsed as TOML values."""
    out = {}
    for i, a in enumerate(argv):
        if a == "-c":
            key, value = argv[i + 1].split("=", 1)
            out[key] = tomllib.loads(f"v = {value}")["v"]
    return out


# --------------------------------------------------------------------------
# The adapter: invocation and permissions
# --------------------------------------------------------------------------


def _perms(cfg, tmp_path, internet="off"):
    ws = tmp_path / "agents" / "tasks" / "t1"
    ws.mkdir(parents=True)
    policy = build_policy(cfg, task_id="t1", workspace=ws, internet=internet)
    return ws, policy, CodexAdapter({}).write_permissions(ws, policy)


def _task(ws):
    return TaskContext(id="t1", prompt="-rm everything", task_type="coding", internet="off",
                       protocol="# Nightshift agent protocol\nline 2 \"quoted\"")


def test_build_command_is_codex_exec_with_the_per_task_policy(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    cmd = CodexAdapter({"model": "gpt-5-codex"}).build_command(_task(ws), ws, perms)
    argv = cmd.argv
    assert argv[:2] == ["codex", "exec"] and argv[-1] == "-"
    for flag in ("--json", "--skip-git-repo-check", "--ignore-user-config", "--ignore-rules",
                 "--strict-config"):
        assert flag in argv
    assert argv[argv.index("-m") + 1] == "gpt-5-codex"
    assert cmd.stdin == "-rm everything"  # the prompt is never an argument
    assert "--ephemeral" not in argv  # Attempts must be resumable
    for bad in ("--dangerously-bypass-approvals-and-sandbox", "--yolo", "--sandbox",
                "--full-auto", "--add-dir"):
        assert bad not in argv
    o = _overrides(argv)
    assert o["approval_policy"] == "never"
    assert o["default_permissions"] == "nightshift"
    assert o["developer_instructions"] == _task(ws).protocol
    assert o["web_search"] == "disabled"
    assert o["permissions.nightshift.network"] == {"enabled": False}
    # Default-on features that reach past the shell sandbox are off (0.163.0).
    for f in ("hooks", "apps", "plugins", "browser_use", "computer_use", "in_app_browser"):
        assert o[f"features.{f}"] is False
    assert "model_reasoning_effort" not in o  # the model's default unless configured
    fs = o["permissions.nightshift.filesystem"]
    assert fs[str(ws)] == "write" and fs[":workspace_roots"] == {".": "write"}
    assert fs[str(Path(cfg.data_dir).resolve())] == "deny"
    assert fs[str(Path.home().resolve())] == "deny"  # a private root
    for d in policy.no_access_dirs:
        assert fs[str(d)] == "deny"
    for d in policy.read_only_dirs:
        assert fs[str(d)] == "read"
    for d in policy.tool_dirs:
        assert fs[str(d)] == "read"
    env = o["shell_environment_policy.set"]
    assert env["TMPDIR"] == str(ws / ".tmp") and (ws / ".tmp").is_dir()
    assert o["shell_environment_policy.inherit"] == "core"


def test_reasoning_effort_setting(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    a = CodexAdapter({"model": "gpt-6-luna", "reasoning_effort": "low"})
    for argv in (a.build_command(_task(ws), ws, perms).argv, a.probe_command().argv):
        assert _overrides(argv)["model_reasoning_effort"] == "low"


def test_internet_on_gets_web_search_only(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path, internet="on")
    o = _overrides(CodexAdapter({}).build_command(_task(ws), ws, perms).argv)
    assert o["web_search"] == "live"
    assert o["permissions.nightshift.network"] == {"enabled": False}  # never shell network


def test_settings_audit_copy_is_valid_toml(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    assert perms.settings_file == policy.settings_dir / "codex-config.toml"
    doc = tomllib.loads(perms.settings_file.read_text())
    assert doc["approval_policy"] == "never"
    assert doc["permissions"]["nightshift"]["network"] == {"enabled": False}


def test_resume_command(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    a = CodexAdapter({})
    cmd = a.resume_command("sid-1", "go on", task=_task(ws), permissions=perms)
    assert cmd.argv[-3:] == ["resume", "sid-1", "-"] and cmd.stdin == "go on"
    assert _overrides(cmd.argv)["approval_policy"] == "never"
    with pytest.raises(ValueError):
        a.resume_command("sid-1", "go on")


def test_refuses_unsafe_commands(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    with pytest.raises(ValueError):
        CodexAdapter({}).build_command(_task(ws), ws, Permissions())
    for bad in (["--yolo"], ["-c", "sandbox_mode=danger-full-access"], ["--sandbox=read-only"],
                ["--add-dir", "/"], ["-capproval_policy=on-request"], ["--profile", "x"],
                ["--enable", "computer_use"], ["--search"], ["--remote", "ws://x"]):
        with pytest.raises(ValueError):
            CodexAdapter({"extra_args": bad}).build_command(_task(ws), ws, perms)
    CodexAdapter({"extra_args": ["--color", "never"]}).build_command(_task(ws), ws, perms)


def test_toml_values_parse_back():
    for v in ["plain", 'a "q" \\ back\nnew line ’', True, 3, ["x", "y"],
              {":root": "read", "/a b/c": "deny", ":workspace_roots": {".": "write"}}]:
        assert tomllib.loads(f"v = {toml_value(v)}")["v"] == v
    assert config_args({"a.b": 1}) == ["-c", "a.b=1"]


def test_registered():
    assert isinstance(get_adapter("codex"), CodexAdapter)


# --------------------------------------------------------------------------
# The adapter: stream, usage, limits
# --------------------------------------------------------------------------


def _parse(lines, adapter=None):
    a = adapter or CodexAdapter({"model": "gpt-5-codex"})
    events = [e for ln in lines for e in a.parse_stream(ln)]
    return a, events


def test_parse_stream_fixture():
    a, events = _parse((FIXTURES / "codex_stream.jsonl").read_text().splitlines())
    assert a.session_id() == CODEX_SID
    kinds = [e.kind for e in events]
    assert kinds[0] == EventKind.SESSION
    calls = [e.data["id"] for e in events if e.kind == EventKind.TOOL_CALL]
    results = [e.data["tool_use_id"] for e in events if e.kind == EventKind.TOOL_RESULT]
    # A recorded real stream (codex-cli 0.163.0, AT3): one command, six file
    # changes; agent_message items are not Steps.
    assert calls == ["item_1", "item_2", "item_3", "item_5", "item_6", "item_7", "item_8"]
    assert results == calls
    files = [e.data["path"] for e in events if e.kind == EventKind.FILE_CHANGE]
    assert files == ["output/test_fizzbuzz.py", "progress.md", "output/README.md", "progress.md",
                     "logs/decisions.jsonl", "report.md"]
    assert kinds[-1] == EventKind.RESULT and a.result_ok() is True and a.result_error() is None
    [u] = a.usage_from_run()
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (23542, 96768, 2184)
    assert u.cost_usd is None and u.model == "gpt-5-codex" and u.requests == 1
    assert u.raw["reasoning_output_tokens"] == 231


def test_resumed_session_counts_only_its_own_tokens():
    """turn.completed reports the thread's running total (seen on a real
    resume: 80,730 then 110,414 input tokens), so a resume subtracts the
    total the session ended at before."""
    a = CodexAdapter({})
    a.prior_usage = {"input_tokens": 80730, "cached_input_tokens": 62208,
                     "cache_write_input_tokens": 0, "output_tokens": 355}
    a.parse_stream('{"type":"turn.completed","usage":{"input_tokens":110414,'
                   '"cached_input_tokens":90112,"cache_write_input_tokens":0,'
                   '"output_tokens":463,"reasoning_output_tokens":0}}')
    [u] = a.usage_from_run()
    assert (u.input_tokens, u.cache_read_tokens, u.output_tokens) == (29684 - 27904, 27904, 108)
    assert u.raw["input_tokens"] == 110414  # the reported total, for the next resume


def test_failed_step_and_failed_turn():
    lines = [
        '{"type":"thread.started","thread_id":"t"}',
        '{"type":"item.completed","item":{"id":"i1","type":"command_execution","command":"curl x",'
        '"aggregated_output":"You have hit your usage limit","exit_code":1,"status":"failed"}}',
        '{"type":"turn.failed","error":{"message":"stream disconnected before completion"}}',
    ]
    a, events = _parse(lines)
    tool_err = [e for e in events if e.kind == EventKind.ERROR and e.data.get("tool_error")]
    assert len(tool_err) == 1
    # A tool's output that mentions a limit is never a hit.
    assert all(a.detect_rate_limit(e) is None for e in events)
    assert a.result_ok() is False and "disconnected" in a.result_error()
    assert a.usage_from_run() == []


def test_usage_limit_hit_is_detected_with_reset_time():
    a = CodexAdapter({})
    a.clock = lambda: datetime(2026, 10, 21, 10, 0, tzinfo=LA)
    text = "You’ve hit your usage limit. Try again at 3:20 PM."
    _, events = _parse([json.dumps({"type": "error", "message": text}),
                        json.dumps({"type": "turn.failed", "error": {"message": text}})], a)
    hits = [h for h in (a.detect_rate_limit(e) for e in events) if h]
    assert len(hits) == 2
    reset = datetime.fromisoformat(hits[0].reset_at)
    local = reset.astimezone()
    assert (local.hour, local.minute) == (15, 20) and reset > a.clock()
    assert hits[0].limit_type is None  # Codex doesn't name the Window
    assert a.detect_rate_limit(1) is None


@pytest.mark.parametrize("text,expect", [
    ("You’ve hit your usage limit. Upgrade to Plus to continue using Codex "
     "(https://chatgpt.com/explore/plus), or try again at Apr 30th, 2026 11:21 AM.",
     (2026, 4, 30, 11, 21)),
    ("You've hit your usage limit. Try again in 1 day 23 hours 37 minutes.", "delta"),
    ("You’ve hit your usage limit. Try again later.", None),
    ("Quota exceeded. Check your plan and billing details.", None),
    ("exceeded retry limit, last status: 429 Too Many Requests", None),
])
def test_limit_text_formats(text, expect):
    a = CodexAdapter({})
    base = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
    a.clock = lambda: base
    hit = a.detect_rate_limit(Event(EventKind.ERROR, {"message": text}))
    assert hit is not None
    if expect is None:
        assert hit.reset_at is None
    elif expect == "delta":
        assert datetime.fromisoformat(hit.reset_at) - base == timedelta(days=1, hours=23,
                                                                         minutes=37)
    else:
        local = datetime.fromisoformat(hit.reset_at).astimezone()
        assert (local.year, local.month, local.day, local.hour, local.minute) == expect


def test_transient_errors_are_not_limits():
    a = CodexAdapter({})
    [e] = a.parse_stream(json.dumps({"type": "error",
                                     "message": "Reconnecting... 2/5 (429 Too Many Requests)"}))
    assert e.kind == EventKind.OTHER and a.detect_rate_limit(e) is None
    [e] = a.parse_stream(json.dumps({"type": "error", "message": "model not found"}))
    assert e.kind == EventKind.ERROR and a.detect_rate_limit(e) is None


def test_same_day_reset_already_passed_is_tomorrow():
    t = datetime(2026, 10, 21, 16, 0).astimezone()
    out = datetime.fromisoformat(parse_reset_text("Try again at 3:20 PM.", t))
    assert out - t == timedelta(hours=23, minutes=20)


# --------------------------------------------------------------------------
# Planning calls and probes
# --------------------------------------------------------------------------


def test_planning_command_is_toolless_with_a_strict_schema():
    schema = {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "string"}},
              "required": ["a"], "additionalProperties": False}
    cmd = CodexAdapter({"planning_model": "gpt-5-mini"}).planning_command(
        "Task: x", system="You estimate.", schema=schema)
    argv = cmd.argv
    assert "--ephemeral" in argv and argv[-1] == "-" and cmd.stdin == "Task: x"
    assert argv[argv.index("-m") + 1] == "gpt-5-mini"
    o = _overrides(argv)
    assert o["features.shell_tool"] is False and o["default_permissions"] == ":read-only"
    assert o["web_search"] == "disabled" and o["developer_instructions"] == "You estimate."
    written = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
    assert written["required"] == ["a", "b"] and written == strict_schema(schema)


def test_parse_planning_output():
    a = CodexAdapter({})
    ok = "\n".join([
        '{"type":"thread.started","thread_id":"e"}',
        '{"type":"item.completed","item":{"id":"i","type":"agent_message",'
        '"text":"{\\"estimated_minutes\\": 12, \\"task_type\\": \\"coding\\", \\"reason\\": \\"r\\"}"}}',
        '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,'
        '"output_tokens":3,"reasoning_output_tokens":0}}'])
    r = a.parse_planning_output(ok, 0)
    assert r.ok and r.structured["estimated_minutes"] == 12 and r.usage[0].input_tokens == 10
    bad = a.parse_planning_output(ok.replace('"text":"{', '"text":"not json {'), 0)
    assert not bad.ok and "JSON" in bad.error
    assert not a.parse_planning_output("", 1).ok


def test_probe_command_and_replies(cfg, db, cenv, tmp_path):
    cmd = CodexAdapter({}).probe_command()
    assert "--ephemeral" in cmd.argv and cmd.stdin == "OK?"
    assert _overrides(cmd.argv)["features.shell_tool"] is False
    clock = Clock()
    found = usage.probe(cfg, db, "codex", adapter_provider=provider, clock=clock)
    assert found.available
    cenv.setenv("FAKE_CODEX_PROBE", "limit")
    found = usage.probe(cfg, db, "codex", adapter_provider=provider, clock=clock)
    assert not found.available and found.limit is not None and found.limit.reset_at
    assert usage.agent_out_until(cfg, db, "codex", clock()) is not None
    [obs] = db.limit_observations("codex")
    assert obs["window"] == "five_hour"  # the shortest Window: conservative
    cenv.setenv("FAKE_CODEX_PROBE", "error")
    found = usage.probe(cfg, db, "codex", adapter_provider=provider, clock=clock)
    assert not found.available and found.limit is None and found.error
    cenv.setenv("FAKE_CODEX_PROBE", "ok")
    usage.probe(cfg, db, "codex", adapter_provider=provider, clock=clock)
    assert usage.agent_out_until(cfg, db, "codex", clock()) is None  # cleared


# --------------------------------------------------------------------------
# Acceptance Test 3 with fakes: Claude -> Codex on a (simulated) limit
# --------------------------------------------------------------------------


def test_at3_simulated_limit_fails_over_to_codex_and_finishes(cfg, db, cenv):
    clock = Clock()
    cfg.data["retries"]["max_retries"] = 0  # a failover must not need retry budget
    new = _coding(cfg, db, simulate_rate_limit_after=2)
    tid = new.spec.id
    (new.folder / "plan.md").write_text("# Plan\n1. fizzbuzz\n")
    cenv.setenv("FAKE_PROGRESS", "claude: started fizzbuzz")
    host = FakeHost()
    qr = QueueRunner(cfg, db, host, adapter_provider=provider, clock=clock)

    assert qr.run_next() == tid
    row = db.get_task(tid)
    # No waiting for Claude's reset: straight back in the queue, on Codex.
    assert row["state"] == "queued" and row["agent"] == "codex" and row["resume_pending"] == 1
    assert row["simulated_limit_at"] and row["retries_used"] == 0
    assert read_task_yaml(new.folder).agent == "codex"
    [f] = db.failovers(tid)
    assert (f["from_agent"], f["to_agent"], f["trigger"], f["deadline_driven"]) == (
        "claude", "codex", "simulated", 0)

    assert qr.run_next() == tid  # same moment: no wait
    assert db.task_state(tid) == TaskState.DONE
    [run] = _codex_runs(new.folder)
    assert "resume" not in run["argv"]  # can't resume Claude's session: fresh
    assert run["stdin"].startswith("Write output/fizzbuzz.py")
    assert "plan.md" in run["stdin"] and "progress.md" in run["stdin"]
    assert "claude" in run["stdin"] and "codex" in run["stdin"]
    saw = json.loads((new.folder / "logs" / "fake_codex_saw.json").read_text())
    assert saw["plan.md"].startswith("# Plan") and "claude: started" in saw["progress.md"]
    attempts = db.attempts(tid)
    assert [(a["agent"], a["outcome"]) for a in attempts] == [
        ("claude", "paused"), ("codex", "completed")]
    assert attempts[1]["session_id"] == CODEX_SID
    assert "<agent protocol>" in attempts[1]["command"]  # stored argv stays readable
    assert [t["to_state"] for t in db.transitions(tid)] == [
        "queued", "running", "queued", "running", "done"]
    assert "failing over from claude to codex" in db.transitions(tid)[2]["reason"]
    assert "failover from claude" in db.transitions(tid)[3]["reason"]
    text = (new.folder / "report.md").read_text()
    assert "**done**" in text and "| claude | 1 |" in text and "| codex | 1 |" in text
    assert "## 2. Needs you\n\nNothing." in text  # not deadline-driven
    assert (new.folder / "output" / "fizzbuzz.py").exists()
    assert host.awake_log == ["hold", "release"] * 2
    log = (new.folder / "logs" / "nightshift.log").read_text()
    assert "failing over from claude to codex" in log


def test_real_claude_limit_fails_over_and_holds_claude(cfg, db, cenv):
    clock = Clock()
    reset = clock() + timedelta(hours=3)
    cenv.setenv("FAKE_LIMIT_AT", "4")
    cenv.setenv("FAKE_RESETS_AT", str(int(reset.timestamp())))
    new = _coding(cfg, db)
    tid = new.spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "queued" and row["agent"] == "codex"
    assert db.failovers(tid)[0]["trigger"] == "usage_limit"
    assert len(db.limit_observations("claude")) == 1  # the hit still teaches a Baseline
    assert usage.agent_out_until(cfg, db, "claude", clock()) == reset.replace(microsecond=0)

    # Claude is out: another coding Task moves to Codex before it starts; a
    # Task type without failover waits for Claude.
    other = _coding(cfg, db, "Second coding Task")
    plain = create_task(cfg, db, "A general Task")
    assert qr.run_next() == tid and db.task_state(tid) == TaskState.DONE
    assert db.get_task(other.spec.id)["agent"] == "codex"
    assert db.failovers(other.spec.id)[0]["run_id"] is None
    assert qr.run_next() == other.spec.id and db.task_state(other.spec.id) == TaskState.DONE
    assert qr.run_next() is None  # the general Task is held: Claude is out
    assert db.get_task(plain.spec.id)["agent"] == "claude"
    clock.advance(hours=3, minutes=1)
    assert qr.run_next() == plain.spec.id


def test_agent_override_never_fails_over(cfg, db, cenv):
    clock = Clock()
    new = _coding(cfg, db, agent="claude", simulate_rate_limit_after=2)
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=provider, clock=clock)
    assert res.state == TaskState.PAUSED_USAGE
    assert db.get_task(new.spec.id)["agent"] == "claude" and not db.failovers(new.spec.id)


def test_pause_by_hand_never_fails_over(cfg, db, cenv):
    new = _coding(cfg, db)
    tid = new.spec.id
    db.request_pause(tid)
    res = run_attempt(cfg, db, tid, adapter_provider=provider, clock=Clock())
    assert res.state == TaskState.PAUSED_USAGE and db.get_task(tid)["agent"] == "claude"


# --------------------------------------------------------------------------
# Both out: wait, resume on whichever resets first
# --------------------------------------------------------------------------


def _limit_both(cfg, db, cenv, clock, *, claude_h, codex_h):
    cenv.setenv("FAKE_LIMIT_AT", "4")
    cenv.setenv("FAKE_RESETS_AT", str(int((clock() + timedelta(hours=claude_h)).timestamp())))
    cenv.setenv("FAKE_CODEX_LIMIT", "1")
    cenv.setenv("FAKE_CODEX_LIMIT_ONCE", "1")
    reset = (clock() + timedelta(hours=codex_h)).astimezone()
    cenv.setenv("FAKE_CODEX_LIMIT_TEXT",
                f"You’ve hit your usage limit. Try again at "
                f"{reset.strftime('%b')} {reset.day}th, {reset.year} "
                f"{reset.strftime('%I:%M %p').lstrip('0')}.")
    new = _coding(cfg, db)
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    assert qr.run_next() == new.spec.id  # Claude hits its limit -> Codex
    assert db.get_task(new.spec.id)["agent"] == "codex"
    assert qr.run_next() == new.spec.id  # Codex hits its limit too
    return new, qr


def test_both_out_resumes_on_codex_when_it_resets_first(cfg, db, cenv):
    clock = Clock()
    new, qr = _limit_both(cfg, db, cenv, clock, claude_h=5, codex_h=1)
    tid = new.spec.id
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["agent"] == "codex"
    assert parse(row["resume_at"]) - clock() <= timedelta(hours=1)
    assert len(db.failovers(tid)) == 1  # nowhere to go
    clock.advance(minutes=30)
    assert qr.run_next() is None
    clock.advance(minutes=31)
    # Pretend the session had reached this total before (Codex reports the
    # thread's running total): the resumed Attempt counts only the rest.
    codex_run = db.attempts(tid)[-1]["id"]
    db.record_usage(codex_run, tid, "codex", [{"model": "m", "raw": {
        "input_tokens": 100000, "cached_input_tokens": 90000, "output_tokens": 2000}}])
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE and db.get_task(tid)["agent"] == "codex"
    runs = _codex_runs(new.folder)
    assert len(runs) == 2 and runs[1]["argv"][-3:-1] == ["resume", CODEX_SID]  # same agent: resume
    last = db.usage_for_task(tid)[-1]
    assert (last["input_tokens"], last["cache_read_tokens"], last["output_tokens"]) == (
        (120310 - 100000) - (96768 - 90000), 96768 - 90000, 184)


def test_both_out_resumes_on_claude_when_it_resets_first(cfg, db, cenv):
    clock = Clock()
    new, qr = _limit_both(cfg, db, cenv, clock, claude_h=2, codex_h=8)
    tid = new.spec.id
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["agent"] == "codex"
    assert abs(parse(row["resume_at"]) - (clock() + timedelta(hours=2))) < timedelta(seconds=2)
    assert "whichever resets first (claude" in (new.folder / "logs" / "nightshift.log").read_text()
    clock.advance(hours=2, minutes=1)
    cenv.delenv("FAKE_LIMIT_AT")
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE and db.get_task(tid)["agent"] == "claude"
    triggers = [f["trigger"] for f in db.failovers(tid)]
    assert triggers == ["usage_limit", "reset"]
    last = _claude_runs(new.folder)[-1]
    assert "--resume" not in last["argv"] and "codex" in last["stdin"]  # fresh, from progress.md


def test_both_out_with_no_reset_times_probes_and_switches(cfg, db, cenv, monkeypatch):
    clock = Clock()
    new = _coding(cfg, db)
    tid = new.spec.id
    # Codex already known out without a reset time; Claude hits a limit with none.
    usage.record_limit_hit(cfg, db, "codex", None, at=clock(), source="probe")
    cenv.setenv("FAKE_LIMIT_AT", "4")
    cenv.setenv("FAKE_LIMIT_FORMAT", "none")
    probed = []

    def prober(agent):
        probed.append(agent)
        from nightshift.adapters.base import ProbeReply, RateLimit
        return ProbeReply(available=False, limit=RateLimit(None, None))

    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock, prober=prober)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["agent"] == "claude"
    # Codex's no-reset hold ends at its probe interval, before Claude's.
    clock.advance(minutes=31)
    cenv.delenv("FAKE_LIMIT_AT")
    assert qr.run_next() == tid
    assert db.get_task(tid)["agent"] == "codex" and db.task_state(tid) == TaskState.DONE


def parse(s):
    return datetime.fromisoformat(s)


# --------------------------------------------------------------------------
# Retry with switch (§2.7)
# --------------------------------------------------------------------------


def test_second_retry_switches_to_codex(cfg, db, cenv):
    clock = Clock()
    cenv.setenv("FAKE_EXIT", "3")  # claude crashes every time
    new = _coding(cfg, db)
    tid = new.spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "retry_wait" and row["agent"] == "claude"  # retry 1: same agent
    clock.advance(minutes=10)
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "retry_wait" and row["agent"] == "codex"  # retry 2: switched
    reason = db.transitions(tid)[-1]["reason"]
    assert "retry 2 of 2 on codex" in reason and "switching from claude to codex" in reason
    [f] = db.failovers(tid)
    assert f["trigger"] == "retry"
    clock.advance(minutes=30)
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE
    assert [a["agent"] for a in db.attempts(tid)] == ["claude", "claude", "codex"]
    assert "claude" in _codex_runs(new.folder)[0]["stdin"]  # the failover note


def test_general_tasks_never_switch_on_retry(cfg, db, cenv):
    clock = Clock()
    cenv.setenv("FAKE_EXIT", "3")
    new = create_task(cfg, db, "general")
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    for wait in (10, 30, 0):
        qr.run_next()
        clock.advance(minutes=wait)
    assert db.task_state(new.spec.id) == TaskState.FAILED
    assert {a["agent"] for a in db.attempts(new.spec.id)} == {"claude"}


def test_retry_does_not_switch_to_an_agent_that_is_out(cfg, db, cenv):
    clock = Clock()
    cenv.setenv("FAKE_EXIT", "3")
    usage.record_limit_hit(cfg, db, "codex", None, at=clock(), source="probe")
    cfg.data["usage"]["probe_interval_minutes"] = 600
    new = _coding(cfg, db)
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    qr.run_next()
    clock.advance(minutes=10)
    qr.run_next()
    assert db.get_task(new.spec.id)["agent"] == "claude" and not db.failovers(new.spec.id)


# --------------------------------------------------------------------------
# Deadline-driven failovers (§2.8)
# --------------------------------------------------------------------------


@pytest.fixture
def deadline_rule(cfg):
    cfg.data["task_types"]["resume"]["failover"] = {"agents": ["claude", "codex"],
                                                    "when": "deadline"}
    return cfg


def test_deadline_driven_failover_is_flagged_in_needs_you(deadline_rule, db, cenv):
    cfg = deadline_rule
    clock = Clock()
    new = create_task(cfg, db, "Tailor", task_type="resume", simulate_rate_limit_after=2,
                      deadline=iso(clock() + timedelta(hours=10)))
    tid = new.spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    assert qr.run_next() == tid and db.get_task(tid)["agent"] == "codex"
    [f] = db.failovers(tid)
    assert f["deadline_driven"] == 1
    assert "deadline-driven" in db.transitions(tid)[-1]["reason"]
    assert qr.run_next() == tid and db.task_state(tid) == TaskState.DONE
    text = (new.folder / "report.md").read_text()
    needs = text.split("## 2. Needs you")[1].split("## 3.")[0]
    assert "Deadline-driven failover" in needs and "claude to codex" in needs
    assert "Nothing." not in needs


def test_far_deadline_waits_for_the_reset(deadline_rule, db, cenv):
    cfg = deadline_rule
    clock = Clock()
    new = create_task(cfg, db, "Tailor", task_type="resume", simulate_rate_limit_after=2,
                      deadline=iso(clock() + timedelta(days=5)))
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=provider, clock=clock)
    assert res.state == TaskState.PAUSED_USAGE and db.get_task(new.spec.id)["agent"] == "claude"


def test_failover_rule_from_config(cfg):
    row = {"agent_override": 0, "task_type": "coding", "agent": "claude", "deadline": None}
    r = failover.rule(cfg, row, now(cfg.timezone))
    assert r.agents == ["claude", "codex"] and not r.deadline_driven
    assert failover.rule(cfg, row | {"task_type": "general"}, now(cfg.timezone)) is None
    assert failover.rule(cfg, row | {"agent_override": 1}, now(cfg.timezone)) is None


def test_failover_config_is_validated(tmp_path):
    import yaml

    from nightshift.config import ConfigError, load_config
    d = tmp_path / "c"
    d.mkdir()
    for bad in ({"agents": ["claude", "gemini"]}, {"agents": "codex"},
                {"agents": ["claude", "codex"], "when": "sometimes"}):
        (d / "config.yaml").write_text(yaml.safe_dump(
            {"paths": {"agents_root": str(tmp_path / "a")},
             "task_types": {"coding": {"failover": bad}}}))
        with pytest.raises(ConfigError):
            load_config(d, host=FakeHost())


def test_show_lists_failovers(cfg, db, cenv, config_dir):
    from click.testing import CliRunner

    from nightshift import cli
    new = _coding(cfg, db, simulate_rate_limit_after=2)
    run_attempt(cfg, db, new.spec.id, adapter_provider=provider, clock=Clock())
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "show", new.spec.id])
    assert r.exit_code == 0 and "Failovers:" in r.output and "claude -> codex (simulated)" in r.output


def test_pauses_import_has_no_cycle():
    assert pauses.KIND_LIMIT and failover.TRIGGER_RETRY


def test_free_form_estimate_on_codex(cfg, cenv):
    from nightshift import planning
    cfg.data["planning"]["agent"] = "codex"
    prop = planning.estimate(cfg, "Write fizzbuzz", adapter_provider=provider)
    assert prop.estimated_minutes == 10 and prop.task_type == "coding"
