"""The Gemini CLI adapter (§2.9, Phase 2).

The adapter was written from the gemini-cli 0.65.0 source (docs/adapters.md);
these tests run it against the fake `gemini` in tests/fixtures/fake_gemini.py,
which replays tests/fixtures/gemini_stream.jsonl.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from click.testing import CliRunner

from nightshift import cli, pauses, usage
from nightshift.adapters.base import EventKind, Permissions, TaskContext
from nightshift.adapters.gemini import GeminiAdapter
from nightshift.daemon import QueueRunner
from nightshift.host import FakeHost
from nightshift.permissions import build_policy
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import now

from conftest import FIXTURES
from test_planning import planner  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
SID = "3f2b9c1e-7a4d-4e8b-9c2f-1d5e6a7b8c9d"
FAKE_GEMINI = [sys.executable, str(FIXTURES / "fake_gemini.py")]


def _parse(lines, adapter=None):
    a = adapter or GeminiAdapter({})
    events = [ev for line in lines for ev in a.parse_stream(line)]
    return a, events


# --------------------------------------------------------------------------
# The stream
# --------------------------------------------------------------------------


def test_parse_stream_fixture():
    a, events = _parse((FIXTURES / "gemini_stream.jsonl").read_text().splitlines())
    assert a.session_id() == SID
    kinds = [e.kind for e in events]
    assert kinds.count(EventKind.TOOL_CALL) == 3  # one Step per tool call
    calls = [e for e in events if e.kind == EventKind.TOOL_CALL]
    assert [c.data["name"] for c in calls] == ["read_file", "write_file", "run_shell_command"]
    results = [e for e in events if e.kind == EventKind.TOOL_RESULT]
    assert [(r.data["tool_use_id"], r.data["is_error"]) for r in results] == [
        (calls[0].data["id"], True), (calls[1].data["id"], False), (calls[2].data["id"], False)]
    # A failed tool is a tool error, never an agent error.
    [err] = [e for e in events if e.kind == EventKind.ERROR]
    assert err.data["tool_error"] and "progress.md" in err.data["message"]
    [change] = [e for e in events if e.kind == EventKind.FILE_CHANGE]
    assert change.data["path"] == "output/notes.md"
    [result] = [e for e in events if e.kind == EventKind.RESULT]
    assert result.data == {"is_error": False, "text": "Wrote output/notes.md and report.md."}
    assert a.result_ok() is True and a.result_error() is None


def test_usage_from_result_stats():
    a, _ = _parse((FIXTURES / "gemini_stream.jsonl").read_text().splitlines())
    [u] = a.usage_from_run()
    # `input_tokens` counts the cached prompt tokens; `input` is the rest.
    assert (u.model, u.input_tokens, u.output_tokens, u.cache_read_tokens) == (
        "gemini-3.1-pro", 6100, 410, 18000)
    assert u.cost_usd is None  # Gemini reports no cost
    assert u.raw["total_tokens"] == 24810


def test_no_result_line_means_no_usage_and_no_verdict():
    lines = (FIXTURES / "gemini_stream.jsonl").read_text().splitlines()[:6]
    a, _ = _parse(lines)
    assert a.usage_from_run() == [] and a.result_ok() is None


def test_error_result_fails_the_attempt():
    a, events = _parse([
        json.dumps({"type": "init", "session_id": SID, "model": "m"}),
        json.dumps({"type": "result", "status": "error",
                    "error": {"type": "FatalToolExecutionError", "message": "disk full"},
                    "stats": {"models": {}}}),
    ])
    assert a.result_ok() is False and a.result_error() == "disk full"
    assert any(e.kind == EventKind.ERROR and not e.data.get("tool_error") for e in events)


def test_warnings_and_non_json_lines_are_not_errors():
    a, events = _parse([
        "Loaded cached credentials.",
        json.dumps({"type": "error", "severity": "warning", "message": "Loop detected"}),
    ])
    assert not any(e.kind == EventKind.ERROR for e in events)


# --------------------------------------------------------------------------
# The adapter: invocation and permissions
# --------------------------------------------------------------------------


def _adapter(tmp_path, settings=None) -> GeminiAdapter:
    a = GeminiAdapter(settings or {})
    a.home = tmp_path / "home"  # the user's Gemini config lives here in tests
    return a


def _perms(cfg, tmp_path, internet="off", adapter=None):
    ws = tmp_path / "agents" / "tasks" / "t1"
    ws.mkdir(parents=True, exist_ok=True)
    ro = tmp_path / "career"
    ro.mkdir(exist_ok=True)
    cfg.data["permissions"]["read_only_dirs"] = [str(ro), str(tmp_path / "missing")]
    policy = build_policy(cfg, task_id="t1", workspace=ws, internet=internet)
    a = adapter or _adapter(tmp_path)
    return ws, policy, a.write_permissions(ws, policy)


def _task(ws):
    return TaskContext(id="t1", prompt="-rm everything", task_type="research", internet="off",
                       protocol="# Nightshift agent protocol\nline 2")


def _flag_values(argv, flag):
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag]


def test_build_command_is_headless_stream_json_with_the_per_task_policy(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    cmd = _adapter(tmp_path, {"model": "gemini-3.1-pro"}).build_command(_task(ws), ws, perms)
    argv = cmd.argv
    assert argv[0] == "gemini"
    assert _flag_values(argv, "--output-format") == ["stream-json"]
    assert _flag_values(argv, "--approval-mode") == ["default"]
    assert _flag_values(argv, "-m") == ["gemini-3.1-pro"]
    assert _flag_values(argv, "-e") == ["none"]  # no extensions
    assert _flag_values(argv, "--allowed-mcp-server-names") == ["nightshift-none"]  # no MCP servers
    assert _flag_values(argv, "--admin-policy") == [str(policy.settings_dir / "gemini-policy.toml")]
    # Only read-only dirs that exist are added.
    assert _flag_values(argv, "--include-directories") == [str(policy.read_only_dirs[0])]
    for bad in ("--yolo", "-y", "--sandbox", "--skip-trust", "-p", "--prompt", "--resume"):
        assert bad not in argv
    # The prompt never goes in argv; the protocol rides in front of it on stdin.
    assert "-rm everything" not in " ".join(argv)
    assert cmd.stdin.startswith("# Nightshift agent protocol\nline 2")
    assert cmd.stdin.endswith("-rm everything")
    env = cmd.env
    assert env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] == str(perms.settings_file)
    assert env["GEMINI_CLI_TRUST_WORKSPACE"] == "false"  # workspace settings never load
    assert env["GEMINI_SANDBOX"] == "false"
    assert env["TMPDIR"] == str(ws / ".tmp") and (ws / ".tmp").is_dir()


def test_settings_file_pins_tools_sandbox_and_switches_off_extras(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    assert perms.settings_file.parent == policy.settings_dir  # agent-inaccessible data dir
    s = json.loads(perms.settings_file.read_text())
    assert s["security"]["toolSandboxing"] is True  # every shell command in Seatbelt
    assert s["security"]["folderTrust"]["enabled"] is True
    assert s["security"]["disableYoloMode"] is True
    assert s["tools"]["sandbox"] is False
    assert s["tools"]["sandboxNetworkAccess"] is False
    assert s["tools"]["core"] == ["read_file", "read_many_files", "write_file", "replace",
                                  "glob", "grep_search", "list_directory", "run_shell_command"]
    assert s["tools"]["discoveryCommand"] == "" and s["tools"]["callCommand"] == ""
    assert s["mcp"]["serverCommand"] == ""
    assert s["hooksConfig"]["enabled"] is False and s["skills"]["enabled"] is False
    assert s["experimental"]["enableAgents"] is False
    assert s["general"]["checkpointing"]["enabled"] is False
    assert s["general"]["enableAutoUpdate"] is False
    assert perms.tools == s["tools"]["core"]


def test_internet_on_gets_web_fetch_and_search(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path, internet="on")
    core = json.loads(perms.settings_file.read_text())["tools"]["core"]
    assert core[-2:] == ["web_fetch", "google_web_search"]
    # Shell commands still get no network.
    assert json.loads(perms.settings_file.read_text())["tools"]["sandboxNetworkAccess"] is False


def _rules(perms):
    import tomllib
    return tomllib.loads(Path(perms.sandbox["policy_file"]).read_text())["rule"]


def test_policy_denies_everything_but_the_task_tools(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path, internet="on")
    rules = _rules(perms)
    [base] = [r for r in rules if r["toolName"] == "*"]
    assert base["decision"] == "deny" and base["priority"] == 0
    [allow] = [r for r in rules if r["decision"] == "allow"]
    assert allow["toolName"] == perms.tools and allow["priority"] > base["priority"]
    assert all(r["priority"] > allow["priority"] for r in rules
               if r["decision"] == "deny" and r is not base)


def _js_regex(pattern):
    """Gemini compiles policy patterns as JS regexes; these ones are plain
    enough for Python's re to mean the same."""
    import re
    return re.compile(pattern)


def test_policy_blocks_writes_to_read_only_dirs_and_linkedin(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path, internet="on")
    rules = _rules(perms)
    ro = str(policy.read_only_dirs[0])

    def denied(tool, args):
        s = "{" + ",".join(f"\0{json.dumps(k)}:{json.dumps(v)}\0" for k, v in sorted(args.items())) + "}"
        for r in rules:
            if r["decision"] != "deny" or r["toolName"] == "*":
                continue
            names = r["toolName"] if isinstance(r["toolName"], list) else [r["toolName"]]
            if tool not in names:
                continue
            pat = r.get("argsPattern") or ('"command":"' + r["commandRegex"] if "commandRegex" in r else None)
            if pat and _js_regex(pat).search(s):
                return True
        return False

    assert denied("write_file", {"file_path": ro + "/cv.tex", "content": "x"})
    assert denied("replace", {"file_path": ro + "/cv.tex", "old_string": "a", "new_string": "b"})
    assert denied("write_file", {"file_path": "../../career/cv.tex", "content": "x"})
    assert not denied("write_file", {"file_path": str(ws / "output" / "a.md"), "content": ro})
    assert not denied("write_file", {"file_path": "output/a.md", "content": "x"})
    assert denied("run_shell_command", {"command": f"echo x >> {ro}/cv.tex"})
    assert not denied("run_shell_command", {"command": "ls output"})
    assert denied("web_fetch", {"prompt": "summarize https://www.linkedin.com/jobs/1"})
    assert denied("web_fetch", {"prompt": "read https://LNKD.IN/abc"})
    assert not denied("web_fetch", {"prompt": "summarize https://example.com"})


def test_resume_command(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    a = _adapter(tmp_path)
    cmd = a.resume_command(SID, "continue", task=_task(ws), permissions=perms)
    assert _flag_values(cmd.argv, "--resume") == [SID]
    assert cmd.stdin.startswith("# Nightshift agent protocol") and cmd.stdin.endswith("continue")
    assert cmd.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] == str(perms.settings_file)
    with pytest.raises(ValueError):
        a.resume_command(SID, "continue", task=_task(ws))


def test_refuses_unsafe_commands(cfg, tmp_path):
    ws, policy, perms = _perms(cfg, tmp_path)
    with pytest.raises(ValueError, match="permission policy"):
        _adapter(tmp_path).build_command(_task(ws), ws, Permissions())
    for bad in (["--yolo"], ["-y"], ["--approval-mode=yolo"], ["--sandbox"], ["--policy", "x"],
                ["--include-directories", "/"], ["--skip-trust"], ["-e", "evil"]):
        with pytest.raises(ValueError, match="extra_args"):
            _adapter(tmp_path, {"extra_args": bad}).build_command(_task(ws), ws, perms)


def test_refuses_user_config_that_would_widen_a_task(cfg, tmp_path):
    a = _adapter(tmp_path)
    gem = a.home / ".gemini"
    gem.mkdir(parents=True)
    (gem / "settings.json").write_text('{\n  // a comment\n  "context": {"includeDirectories": ["~/"]}\n}')
    with pytest.raises(ValueError, match="includeDirectories"):
        _perms(cfg, tmp_path, adapter=a)
    (gem / "settings.json").write_text('{"ui": {"theme": "x"}}')
    (gem / "policies").mkdir()
    (gem / "policies" / "sandbox.toml").write_text("[modes.default]\nnetwork = true\n")
    with pytest.raises(ValueError, match="sandbox.toml"):
        _perms(cfg, tmp_path, adapter=a)


def test_path_keeps_tool_dirs_and_drops_entries_that_expose_home(cfg, tmp_path, monkeypatch):
    home = Path.home()
    monkeypatch.setenv("PATH", ":".join([
        str(home / "bin"), str(home / ".nvm" / "versions" / "node" / "v24" / "bin"),
        "/opt/homebrew/bin", "/usr/bin", str(home / "Documents" / "scripts")]))
    ws, policy, perms = _perms(cfg, tmp_path)
    path = _adapter(tmp_path).build_command(_task(ws), ws, perms).env["PATH"].split(":")
    # ~/bin would make the sandbox allow reading all of ~ (it opens the parent of a bin dir).
    assert path == [str(home / ".nvm" / "versions" / "node" / "v24" / "bin"),
                    "/opt/homebrew/bin", "/usr/bin"]


def test_registered():
    from nightshift.adapters import get_adapter
    assert isinstance(get_adapter("gemini", {}), GeminiAdapter)


# --------------------------------------------------------------------------
# Quota hits
# --------------------------------------------------------------------------

NOW = datetime(2026, 10, 22, 1, 0, tzinfo=LA)


def _limit_result(etype, message):
    return json.dumps({"type": "result", "status": "error",
                       "error": {"type": etype, "message": message}, "stats": {"models": {}}})


def _hits(lines):
    a = GeminiAdapter({})
    a.clock = lambda: NOW
    return [h for line in lines for ev in a.parse_stream(line)
            if (h := a.detect_rate_limit(ev)) is not None]


def test_terminal_quota_error_is_a_limit_hit():
    [hit] = _hits([_limit_result(
        "TerminalQuotaError", "[API Error: You have exhausted your daily quota on this model.]")])
    assert hit.limit_type == "daily" and hit.reset_at is None  # probed


@pytest.mark.parametrize("message,expect", [
    ("[API Error: You have exhausted your capacity on this model. Your quota will reset after "
     "2h31m10s.]", NOW + timedelta(hours=2, minutes=31, seconds=10)),
    ("[API Error: Quota exceeded for metric generate_content. Please retry in 3812.5s.]",
     NOW + timedelta(seconds=3812)),
    ("[API Error: QUOTA_EXHAUSTED\nSuggested retry after 3600s.]", NOW + timedelta(hours=1)),
])
def test_reset_time_is_read_from_the_text(message, expect):
    [hit] = _hits([_limit_result("TerminalQuotaError", message)])
    assert datetime.fromisoformat(hit.reset_at) == expect.replace(microsecond=0)


def test_throttling_and_tool_output_are_not_limit_hits():
    assert _hits([_limit_result("RetryableQuotaError",
                                "[API Error: Quota exceeded for metric generate_requests_per_minute. "
                                "Please retry in 2s.]")]) == []
    assert _hits([json.dumps({"type": "tool_use", "tool_id": "t", "tool_name": "web_fetch",
                              "parameters": {}}),
                  json.dumps({"type": "tool_result", "tool_id": "t", "status": "error",
                              "error": {"type": "x", "message": "quota exceeded on site"}})]) == []
    assert _hits([_limit_result("ModelNotFoundError", "[API Error: Model not found]")]) == []
    assert GeminiAdapter({}).detect_rate_limit(173) is None  # exit code alone says nothing


# --------------------------------------------------------------------------
# Planning calls and probes
# --------------------------------------------------------------------------


def test_planning_command_is_toolless_with_the_schema_in_the_prompt():
    cmd = GeminiAdapter({"model": "pro", "planning_model": "flash"}).planning_command(
        "Summarize lecture 3", system="You plan.", schema={"type": "object"})
    assert _flag_values(cmd.argv, "-m") == ["flash"]
    s = json.loads(Path(cmd.env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text())
    assert s["tools"]["core"] == []
    import tomllib
    [rule] = tomllib.loads(Path(_flag_values(cmd.argv, "--admin-policy")[0]).read_text())["rule"]
    assert (rule["toolName"], rule["decision"]) == ("*", "deny")
    assert cmd.stdin.startswith("You plan.") and '{"type": "object"}' in cmd.stdin
    assert cmd.stdin.endswith("Summarize lecture 3")


def _reply(text, status="success"):
    return "\n".join([
        json.dumps({"type": "init", "session_id": "s", "model": "flash"}),
        json.dumps({"type": "message", "role": "assistant", "content": text, "delta": True}),
        json.dumps({"type": "result", "status": status, "stats": {"models": {"flash": {
            "input_tokens": 900, "output_tokens": 12, "cached": 0, "input": 900}}}}),
    ])


def test_parse_planning_output():
    a = GeminiAdapter({})
    r = a.parse_planning_output(_reply('```json\n{"estimated_minutes": 5}\n```'), 0)
    assert r.ok and r.structured == {"estimated_minutes": 5} and r.usage[0].input_tokens == 900
    r = a.parse_planning_output(_reply("I think about 5 minutes"), 0)
    assert not r.ok and "JSON" in r.error
    r = a.parse_planning_output(_reply("{}", status="error"), 1)
    assert not r.ok


def test_parse_probe_output():
    a = GeminiAdapter({})
    assert a.parse_probe_output(_reply("OK"), 0).available
    out = a.parse_probe_output(_limit_result(
        "TerminalQuotaError", "[API Error: You have exhausted your daily quota on this model.]"), 173)
    assert not out.available and out.limit is not None and out.error is None
    out = a.parse_probe_output("", 41)
    assert not out.available and out.limit is None and "41" in out.error


# --------------------------------------------------------------------------
# End to end with the fake CLI
# --------------------------------------------------------------------------


def fake_gemini(settings) -> GeminiAdapter:
    a = GeminiAdapter(settings)
    a.home = Path(os.environ["FAKE_GEMINI_HOME"])
    orig = a._base_argv

    def base_argv(model, policy_file):
        return FAKE_GEMINI + orig(model, policy_file)[1:]

    a._base_argv = base_argv
    return a


def provider(name, settings):
    assert name == "gemini"
    return fake_gemini(settings)


@pytest.fixture
def genv(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("FAKE_GEMINI_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("FAKE_GEMINI_HOME", str(tmp_path / "home"))
    return monkeypatch


def _gemini_runs(folder: Path) -> list[dict]:
    path = folder / "logs" / "fake_gemini_invocations.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


class Clock:
    def __init__(self):
        self.t = now("America/Los_Angeles")

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def test_orch_add_agent_gemini_runs_to_done_with_a_report(config_dir, cfg, db, genv, planner):
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "add",
                                      "Summarize lecture 3", "--agent", "gemini", "--yes"])
    assert r.exit_code == 0, r.output
    [row] = db.list_tasks()
    tid = row["id"]
    assert row["agent"] == "gemini"
    res = run_attempt(cfg, db, tid, adapter_provider=provider)
    assert res.state == TaskState.DONE and res.exit_code == 0
    [attempt] = db.attempts(tid)
    assert attempt["session_id"] == SID and attempt["outcome"] == "completed"
    folder = Path(row["folder"])
    [run] = _gemini_runs(folder)
    assert run["env"]["GEMINI_CLI_TRUST_WORKSPACE"] == "false"
    assert "Nightshift" in run["stdin"] and run["stdin"].endswith("Summarize lecture 3")
    [u] = db.usage_for_task(tid)
    assert (u["agent"], u["model"], u["input_tokens"], u["cache_read_tokens"]) == (
        "gemini", "gemini-3.1-pro", 6100, 18000)
    text = (folder / "report.md").read_text()
    assert "**done**" in text and "| gemini | 1 |" in text
    assert (folder / "output" / "notes.md").exists()


def test_pause_then_resume_same_gemini_session(cfg, db, genv):
    clock = Clock()
    cfg.data["retries"]["max_retries"] = 0
    new = create_task(cfg, db, "Summarize lecture 3", agent="gemini", long=False,
                      simulate_rate_limit_after=2)
    tid = new.spec.id
    genv.setenv("FAKE_GEMINI_STEP_DELAY", "0.05")  # so the stop lands mid-stream
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.PAUSED_USAGE
    clock.advance(minutes=5)
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE
    first, second = _gemini_runs(new.folder)
    assert "--resume" not in first["argv"]
    assert second["argv"][second["argv"].index("--resume") + 1] == SID
    assert second["stdin"].endswith(pauses.RESUME_PROMPT)
    assert "Nightshift agent protocol" in second["stdin"] or "Nightshift" in second["stdin"]
    assert [a["outcome"] for a in db.attempts(tid)] == ["paused", "completed"]


def test_failed_resume_falls_back_to_a_fresh_gemini_session(cfg, db, genv):
    clock = Clock()
    new = create_task(cfg, db, "x", agent="gemini", long=False, simulate_rate_limit_after=2)
    tid = new.spec.id
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    genv.setenv("FAKE_GEMINI_STEP_DELAY", "0.05")
    qr.run_next()
    genv.setenv("FAKE_GEMINI_RESUME_FAIL", "1")
    clock.advance(minutes=5)
    qr.run_next()  # the resume fails before any Step
    qr.run_next()  # a fresh session
    assert db.task_state(tid) == TaskState.DONE
    runs = _gemini_runs(new.folder)
    assert "--resume" in runs[1]["argv"] and "--resume" not in runs[-1]["argv"]


def test_real_quota_hit_pauses_and_records_a_limit_observation(config_dir, cfg, db, genv):
    clock = Clock()
    create_task(cfg, db, "earlier", agent="gemini", long=False)
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=provider, clock=clock)
    qr.run_next()  # some recorded usage in the Window
    genv.setenv("FAKE_GEMINI_LIMIT", "1")
    new = create_task(cfg, db, "x", agent="gemini", long=False)
    assert qr.run_next() == new.spec.id
    assert db.task_state(new.spec.id) == TaskState.PAUSED_USAGE
    [obs] = db.limit_observations("gemini")
    assert obs["window"] == "daily"
    out_until = usage.agent_out_until(cfg, db, "gemini", clock())
    assert out_until is not None and abs(
        (out_until - clock()).total_seconds() - 2 * 3600) < 120  # "reset after 2h0m0s"
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage"])
    assert r.exit_code == 0, r.output
    assert "gemini" in r.output and "daily" in r.output and "out of usage" in r.output


def test_gemini_probe(cfg, db, genv):
    clock = Clock()
    assert usage.probe(cfg, db, "gemini", adapter_provider=provider, clock=clock).available
    genv.setenv("FAKE_GEMINI_PROBE", "limit")
    found = usage.probe(cfg, db, "gemini", adapter_provider=provider, clock=clock)
    assert not found.available and found.limit is not None
    assert usage.agent_out_until(cfg, db, "gemini", clock()) is not None
    genv.setenv("FAKE_GEMINI_PROBE", "ok")
    usage.probe(cfg, db, "gemini", adapter_provider=provider, clock=clock)
    assert usage.agent_out_until(cfg, db, "gemini", clock()) is None


def test_free_form_estimate_on_gemini(cfg, genv):
    from nightshift import planning
    cfg.data["planning"]["agent"] = "gemini"
    prop = planning.estimate(cfg, "Summarize lecture 3", adapter_provider=provider)
    assert prop.estimated_minutes == 10 and prop.task_type == "research"
