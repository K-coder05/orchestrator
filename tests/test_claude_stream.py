from __future__ import annotations

import json
from pathlib import Path

from nightshift.adapters import get_adapter
from nightshift.adapters.base import Event, EventKind, PermissionPolicy, TaskContext
from nightshift.adapters.claude import ClaudeAdapter
from nightshift.protocol import agent_protocol

from conftest import FIXTURES


def _feed(adapter, name):
    events = []
    for line in (FIXTURES / name).read_text().splitlines():
        events += adapter.parse_stream(line)
    return events


def test_registry_returns_claude_adapter():
    assert isinstance(get_adapter("claude", {}), ClaudeAdapter)


def test_parse_real_stream_session_steps_and_file_changes():
    a = ClaudeAdapter()
    events = _feed(a, "claude_stream.jsonl")
    kinds = [e.kind for e in events]
    assert kinds[0] == EventKind.SESSION
    assert a.session_id() == "f5e745a1-aea4-451e-bef1-64b1544eb8de"

    steps = [e for e in events if e.kind == EventKind.TOOL_CALL]
    assert [s.data["name"] for s in steps] == [
        "Bash", "Bash", "Write", "Bash", "Write", "Read", "Read", "Write", "Write"
    ]
    changes = [e.data["path"] for e in events if e.kind == EventKind.FILE_CHANGE]
    assert "/tmp/nightshift-fixture/task/output/haiku.txt" in changes
    assert len(changes) == 4

    texts = [e.data["text"] for e in events if e.kind == EventKind.TEXT]
    assert any("output/haiku.txt" in t for t in texts)

    denied = [e for e in events if e.kind == EventKind.ERROR and e.data.get("permission_denied")]
    assert denied and denied[0].data["name"] == "Bash"

    assert EventKind.RATE_LIMIT_INFO in kinds
    [result] = [e for e in events if e.kind == EventKind.RESULT]
    assert result.data["subtype"] == "success" and result.data["is_error"] is False
    assert a.result_ok() is True and a.result_error() is None


def test_usage_from_run_reports_every_field():
    a = ClaudeAdapter()
    _feed(a, "claude_stream.jsonl")
    [u] = a.usage_from_run()
    assert u.model == "claude-haiku-5-5"
    assert (u.input_tokens, u.output_tokens) == (12, 4059)
    assert (u.cache_creation_tokens, u.cache_read_tokens) == (18841, 133805)
    assert u.requests == 6  # distinct assistant message ids
    assert abs(u.cost_usd - 0.00713695) < 1e-9
    assert u.raw["costUSD"] == u.cost_usd


def test_usage_empty_before_result():
    assert ClaudeAdapter().usage_from_run() == []


def test_error_stream_is_not_ok():
    a = ClaudeAdapter()
    events = _feed(a, "claude_stream_error.jsonl")
    assert a.result_ok() is False
    assert "issue with the selected model" in a.result_error()
    errors = [e for e in events if e.kind == EventKind.ERROR]
    assert any(e.data.get("api_error_status") == 404 for e in errors)
    assert any(e.data.get("message") == "model_not_found" for e in errors)


def test_non_json_and_blank_lines():
    a = ClaudeAdapter()
    assert a.parse_stream("") == []
    [e] = a.parse_stream("not json at all")
    assert e.kind == EventKind.TEXT and e.data["raw"] is True


def test_detect_rate_limit():
    a = ClaudeAdapter()
    allowed = Event(EventKind.RATE_LIMIT_INFO, {"status": "allowed", "resetsAt": 1792578000})
    assert a.detect_rate_limit(allowed) is None
    hit = Event(EventKind.RATE_LIMIT_INFO, {"status": "rejected", "rateLimitType": "five_hour", "resetsAt": 1792578000})
    rl = a.detect_rate_limit(hit)
    assert rl.limit_type == "five_hour" and rl.reset_at.startswith("2026-10-")
    assert a.detect_rate_limit(1) is None


def _task(folder: Path) -> TaskContext:
    return TaskContext(id="k3f9", prompt="-do the thing", task_type="general", internet="off",
                       protocol=agent_protocol(folder))


def _policy(tmp_path: Path, internet: bool = False) -> PermissionPolicy:
    return PermissionPolicy(workspace=tmp_path / "ws", internet=internet,
                            settings_dir=tmp_path / "data" / "permissions" / "k3f9")


def test_build_command_headless_with_protocol(tmp_path):
    a = ClaudeAdapter({"binary": "claude", "model": "haiku"})
    perms = a.write_permissions(tmp_path, _policy(tmp_path))
    cmd = a.build_command(_task(tmp_path), tmp_path, perms)
    argv = cmd.argv
    assert argv[:2] == ["claude", "-p"]
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv  # required by stream-json with -p
    assert argv[argv.index("--model") + 1] == "haiku"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert "--dangerously-skip-permissions" not in argv
    assert "WebFetch" not in argv[argv.index("--tools") + 1]
    protocol = argv[argv.index("--append-system-prompt") + 1]
    assert str(tmp_path) in protocol and "progress.md" in protocol and "needs_you.json" in protocol
    # The prompt goes on stdin, never as an argument.
    assert cmd.stdin == "-do the thing" and "-do the thing" not in argv


def test_internet_on_allows_web_tools(tmp_path):
    perms = ClaudeAdapter().write_permissions(tmp_path, _policy(tmp_path, internet=True))
    assert "WebFetch" in perms.tools and "WebFetch" in perms.allow


def test_protocol_says_to_write_progress_with_the_file_tool(tmp_path):
    # A real haiku run lost its only progress.md update to an auto-denied
    # heredoc (`cat > progress.md <<EOF`) and then paused with it unchanged.
    text = " ".join(agent_protocol(tmp_path).split())
    assert "not shell redirection or heredocs" in text
    assert "If an update of `progress.md` is denied anyway, redo it" in text


def test_resume_command_reinjects_protocol(tmp_path):
    a = ClaudeAdapter()
    perms = a.write_permissions(tmp_path, _policy(tmp_path))
    cmd = a.resume_command("sess-1", "continue", task=_task(tmp_path), permissions=perms)
    assert cmd.argv[cmd.argv.index("--resume") + 1] == "sess-1"
    assert "--append-system-prompt" in cmd.argv
    assert cmd.argv[cmd.argv.index("--settings") + 1] == str(perms.settings_file)
    assert cmd.stdin == "continue"
