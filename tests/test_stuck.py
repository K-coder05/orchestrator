"""Stuck detection (§2.7): the two triggers with a fake clock, then the whole
flow through the runner with the fake Claude's stream."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from nightshift import alerts, cli, control, queries, stuck
from nightshift.config import ConfigError, load_config
from nightshift.host import FakeHost
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import now

from test_runner import env, fake_provider  # noqa: F401  (the env fixture)


# --------------------------------------------------------------------------
# The repeat trigger
# --------------------------------------------------------------------------


def test_the_same_call_three_times_in_a_row_is_stuck():
    w = stuck.RepeatWatch(3)
    assert w.see("Bash", {"command": "ls"}) is None
    assert w.see("Bash", {"command": "ls"}) is None
    why = w.see("Bash", {"command": "ls"})
    assert why == 'it made the same tool call 3 times in a row: `Bash` {"command": "ls"}'


def test_a_different_call_in_between_starts_the_count_again():
    w = stuck.RepeatWatch(3)
    calls = [("Bash", {"command": "ls"}), ("Bash", {"command": "ls"}),
             ("Read", {"file_path": "a"}), ("Bash", {"command": "ls"}),
             ("Bash", {"command": "ls -l"}), ("Bash", {"command": "ls"})]
    assert all(w.see(n, i) is None for n, i in calls)
    # Same tool, other input; other tool, same input: not the same call.
    w = stuck.RepeatWatch(2)
    assert w.see("Read", {"path": "a"}) is None
    assert w.see("Write", {"path": "a"}) is None


def test_key_order_of_the_input_does_not_matter():
    w = stuck.RepeatWatch(2)
    w.see("Edit", {"a": 1, "b": 2})
    assert w.see("Edit", {"b": 2, "a": 1}) is not None


def test_repeat_trigger_off():
    w = stuck.RepeatWatch(None)
    assert all(w.see("Bash", {"command": "ls"}) is None for _ in range(10))


def test_long_inputs_are_cut_in_the_reason():
    text = stuck.call_text("Write", {"content": "x" * 1000})
    assert len(text) < 260 and text.endswith("…")


# --------------------------------------------------------------------------
# The silence trigger, on a fake clock
# --------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _watch(tmp_path, clock, *, silence=900.0, stall=None):
    folder = tmp_path / "task"
    (folder / "logs").mkdir(parents=True, exist_ok=True)
    (folder / "progress.md").write_text("- started\n")
    return folder, stuck.SilenceWatch(folder, silence_seconds=silence, stall_seconds=stall,
                                      clock=clock)


def never_asked():
    raise AssertionError("busy() asked with no Step in flight")


def test_fifteen_quiet_minutes_are_stuck_after_a_short_confirmation(tmp_path):
    clock = Clock()
    _, w = _watch(tmp_path, clock)
    out = clock.t  # the agent's last line
    clock.t += 899
    assert w.check(last_output=out, inflight=False, busy=never_asked) is None
    clock.t += 1  # 15 min: silence starts to count ...
    assert w.check(last_output=out, inflight=False, busy=never_asked) is None
    clock.t += stuck.CONFIRM_SECONDS - 1
    assert w.check(last_output=out, inflight=False, busy=never_asked) is None
    clock.t += 1  # ... and still holds after the confirmation
    why = w.check(last_output=out, inflight=False, busy=never_asked)
    assert why == "it went silent: no output and no file changes for 15 min"


def test_output_restarts_the_quiet_clock(tmp_path):
    clock = Clock()
    _, w = _watch(tmp_path, clock)
    clock.t += 900
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None  # suspect
    out = clock.t  # a line arrives during the confirmation
    clock.t += 30
    assert w.check(last_output=out, inflight=False, busy=never_asked) is None
    clock.t += 899
    assert w.check(last_output=out, inflight=False, busy=never_asked) is None


def test_a_file_change_in_the_task_folder_restarts_the_quiet_clock(tmp_path):
    clock = Clock()
    folder, w = _watch(tmp_path, clock)
    (folder / "output").mkdir()
    (folder / "output" / "notes.md").write_text("working\n")  # silently, by a command
    clock.t += 900
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None
    clock.t += 899  # 15 min from the change, not from the last line
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None
    clock.t += 1 + stuck.CONFIRM_SECONDS
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None  # suspect
    clock.t += stuck.CONFIRM_SECONDS
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is not None


def test_a_deleted_file_counts_as_a_change(tmp_path):
    clock = Clock()
    folder, w = _watch(tmp_path, clock)
    (folder / "progress.md").unlink()
    clock.t += 900
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None
    assert w.suspect_at is None


def test_nightshifts_own_logs_are_not_activity(tmp_path):
    clock = Clock()
    folder, w = _watch(tmp_path, clock)
    (folder / "logs" / "nightshift.log").write_text("Nightshift wrote this\n")
    clock.t += 900
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None  # suspect
    clock.t += stuck.CONFIRM_SECONDS
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is not None


def test_a_long_silent_command_is_not_silence(tmp_path):
    # A Step in flight with a command running under the agent (the CLI
    # reports the call as still running): never stuck, however long.
    clock = Clock()
    _, w = _watch(tmp_path, clock)
    asked = []

    def busy():
        asked.append(clock.t)
        return True

    for _ in range(8):  # two hours of one silent command
        clock.t += 900
        assert w.check(last_output=1000.0, inflight=True, busy=busy) is None
    assert len(asked) == 8  # one process listing per quiet period, not per tick


def test_a_command_that_just_ended_is_not_silence(tmp_path):
    # The command ended and its result line is on its way: the confirmation
    # keeps that moment from counting as silence.
    clock = Clock()
    _, w = _watch(tmp_path, clock)
    clock.t += 900
    assert w.check(last_output=1000.0, inflight=True, busy=lambda: True) is None
    clock.t += 900  # the command has just ended, its result not read yet
    assert w.check(last_output=1000.0, inflight=True, busy=lambda: False) is None
    out = clock.t + 0.5  # the result line
    clock.t += 1
    assert w.check(last_output=out, inflight=False, busy=never_asked) is None


def test_a_step_in_flight_with_no_command_running_is_silence(tmp_path):
    clock = Clock()
    _, w = _watch(tmp_path, clock)
    clock.t += 900
    assert w.check(last_output=1000.0, inflight=True, busy=lambda: False) is None
    clock.t += stuck.CONFIRM_SECONDS
    assert "15 min" in w.check(last_output=1000.0, inflight=True, busy=lambda: False)


def test_the_stall_guard_is_the_shorter_threshold_with_a_step_in_flight(tmp_path):
    clock = Clock()
    _, w = _watch(tmp_path, clock, stall=300)
    clock.t += 300
    # No Step in flight (the CLI retrying the model): only the 15 minutes count.
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None
    clock.t += stuck.CONFIRM_SECONDS
    assert w.check(last_output=1000.0, inflight=False, busy=never_asked) is None
    # A Step in flight and no command running: 5 minutes.
    assert w.check(last_output=1000.0, inflight=True, busy=lambda: False) is None
    clock.t += stuck.CONFIRM_SECONDS
    why = w.check(last_output=1000.0, inflight=True, busy=lambda: False)
    assert why == ("it went silent with a Step in flight and no command running: no output and "
                   "no file changes for 5 min (stall guard)")


def test_silence_trigger_off(tmp_path):
    clock = Clock()
    _, w = _watch(tmp_path, clock, silence=None)
    clock.t += 10 ** 6
    assert w.check(last_output=1000.0, inflight=True, busy=lambda: False) is None


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def test_defaults(cfg):
    assert cfg.stuck_silence_seconds == 900 and cfg.stuck_repeat_calls == 3


def _load(tmp_path, stuck_cfg):
    d = tmp_path / "c"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({"stuck": stuck_cfg}))
    return load_config(d, host=FakeHost())


def test_null_turns_a_trigger_off(tmp_path):
    cfg = _load(tmp_path, {"silence_minutes": None, "repeat_calls": None})
    assert cfg.stuck_silence_seconds is None and cfg.stuck_repeat_calls is None


@pytest.mark.parametrize("bad", [{"silence_minutes": 0}, {"silence_minutes": "15m"},
                                 {"repeat_calls": 1}, {"repeat_calls": 2.5},
                                 {"repeat_calls": True}, {"retries": 1}])
def test_bad_stuck_config_is_rejected(tmp_path, bad):
    with pytest.raises(ConfigError, match="stuck"):
        _load(tmp_path, bad)


# --------------------------------------------------------------------------
# Through the runner, with the fake Claude
# --------------------------------------------------------------------------

SID = "5e1f0c55-0000-4000-8000-000000000043"


def _looping_stream(tmp_path: Path, times: int = 3) -> Path:
    """A stream that calls `ls` ``times`` times in a row (each answered),
    then nothing more: the fake then sleeps (FAKE_SLEEP) until stopped."""
    lines = [{"type": "system", "subtype": "init", "session_id": SID, "model": "claude-haiku-5-5"}]
    for n in range(1, times + 1):
        lines.append({"type": "assistant", "session_id": SID, "message": {
            "id": f"msg_{n}", "model": "claude-haiku-5-5", "content": [
                {"type": "tool_use", "id": f"toolu_{n}", "name": "Bash",
                 "input": {"command": "ls output", "description": "List output"}}]}})
        lines.append({"type": "user", "session_id": SID, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"toolu_{n}", "content": "notes.md"}]}})
    path = tmp_path / "looping.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path


def _invocations(folder: Path) -> list[dict]:
    path = folder / "logs" / "fake_invocations.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def _show(config_dir, task_id) -> str:
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "show", task_id])
    assert r.exit_code == 0, r.output
    return r.output


def test_looping_twice_retries_once_with_a_note_then_needs_you(cfg, db, env, tmp_path,  # noqa: F811
                                                                config_dir):
    env.setenv("FAKE_STREAM", str(_looping_stream(tmp_path)))
    env.setenv("FAKE_SLEEP", "30")  # it would go on; Nightshift stops it
    new = create_task(cfg, db, "Summarize the notes")
    tid = new.spec.id

    res = run_attempt(cfg, db, tid, adapter_provider=fake_provider)
    # Stuck once: stopped (not after 30 s), requeued to continue, not charged.
    assert res.state == TaskState.QUEUED
    [a] = db.attempts(tid)
    assert a["outcome"] == "stuck" and a["session_id"] == SID
    assert a["end_reason"] == ('Attempt 1 stuck: it made the same tool call 3 times in a row: '
                               '`Bash` {"command": "ls output", "description": "List output"}')
    row = db.get_task(tid)
    assert row["retries_used"] == 0 and row["stuck_count"] == 1 and row["resume_pending"] == 1
    assert "not charged to the retry budget" in db.transitions(tid)[-1]["reason"]
    assert "Next Attempt is told what the last Attempt got stuck on" in _show(config_dir, tid)

    res = run_attempt(cfg, db, tid, adapter_provider=fake_provider)
    # The retry resumed the same session, told what it was looping on.
    second = _invocations(new.folder)[-1]
    assert second["argv"][second["argv"].index("--resume") + 1] == SID
    assert "got stuck: it made the same tool call 3 times in a row" in second["stdin"]
    assert "`Bash` {\"command\": \"ls output\"" in second["stdin"]
    assert "check `progress.md` first" in second["stdin"]
    assert "told what the last Attempt got stuck on" in db.transitions(tid)[-2]["reason"]

    # Stuck again: needs you.
    assert res.state == TaskState.NEEDS_YOU
    row = db.get_task(tid)
    assert row["stuck_count"] == 2 and row["retries_used"] == 0
    assert [a["outcome"] for a in db.attempts(tid)] == ["stuck", "stuck"]

    text = (new.folder / "report.md").read_text()
    assert "**needs_you**" in text and "Attempt 2 got stuck a second time" in text
    assert "- **Stuck:** Attempt 2 got stuck again after one retry" in text
    assert "Stuck Attempts (stopped by Nightshift's stuck detection, §2.7):" in text
    assert "- Attempt 1 (claude): Attempt 1 stuck: it made the same tool call" in text
    assert "- Attempt 2 (claude): Attempt 2 stuck: it made the same tool call" in text

    shown = _show(config_dir, tid)
    assert "stuck_count" in shown and "2 of 2" in shown
    assert "Waiting: stuck again after one retry" in shown
    stuck_part = shown.split("Stuck:\n", 1)[1]
    assert "Attempt 1 stuck: it made the same tool call 3 times in a row: `Bash`" in stuck_part

    # The alert and the inbox say it is stuck; retry is the way on.
    last = db.transitions(tid)[-1]
    msg = alerts.alert_text(db.get_task(tid), alerts.alert_kind(
        last["from_state"], last["to_state"], last["reason"]), last["reason"])
    assert "needs you" in msg and "Stuck: Attempt 2 got stuck again" in msg
    [item] = queries.needs_you_inbox(cfg, db)
    assert item.kind == "stuck" and "stuck again" in item.summary
    assert f"orch retry {tid}" in item.proposed_action
    assert control.RETRY in control.available(db.get_task(tid))
    assert not control.decidable(db, db.get_task(tid))  # approve/deny don't apply

    res = control.retry(cfg, db, tid, via="orch retry", at=now(cfg.timezone))
    assert res.ok and res.to_state == TaskState.QUEUED
    row = db.get_task(tid)
    assert row["stuck_count"] == 0 and row["resume_pending"] == 1 and row["stuck_note"]
    assert queries.needs_you_inbox(cfg, db) == []


def test_going_silent_is_stuck(cfg, db, env):  # noqa: F811
    cfg.data["stuck"]["silence_minutes"] = 0.02  # 1.2 s
    env.setenv("FAKE_SLEEP", "30")  # the whole stream, then silence
    new = create_task(cfg, db, "Write a haiku")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    assert res.state == TaskState.QUEUED
    [a] = db.attempts(new.spec.id)
    assert a["outcome"] == "stuck"
    assert a["end_reason"] == ("Attempt 1 stuck: it went silent: no output and no file changes "
                               "for 0.02 min")
    note = db.get_task(new.spec.id)["stuck_note"]
    assert "went silent" in note and "Your last tool call was `" in note


def test_a_normal_run_is_not_stuck(cfg, db, env):  # noqa: F811
    cfg.data["stuck"]["silence_minutes"] = 0.02
    env.setenv("FAKE_STEP_DELAY", "0.05")
    new = create_task(cfg, db, "Write a haiku")
    assert run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider).state == TaskState.DONE


def test_triggers_off_leave_the_runtime_cap(cfg, db, env, tmp_path):  # noqa: F811
    cfg.data["stuck"] = {"silence_minutes": None, "repeat_calls": None}
    env.setenv("FAKE_STREAM", str(_looping_stream(tmp_path, times=5)))
    env.setenv("FAKE_SLEEP", "30")
    new = create_task(cfg, db, "p")
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider,
                      max_runtime_seconds=2)
    assert res.state == TaskState.RETRY_WAIT
    assert "runtime cap" in db.attempts(new.spec.id)[0]["end_reason"]


def test_a_stuck_note_survives_a_failed_resume(cfg, db, env, tmp_path):  # noqa: F811
    env.setenv("FAKE_STREAM", str(_looping_stream(tmp_path)))
    env.setenv("FAKE_SLEEP", "30")
    new = create_task(cfg, db, "Summarize the notes")
    run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    env.setenv("FAKE_RESUME_FAIL", "1")
    env.delenv("FAKE_SLEEP")
    env.setenv("FAKE_STREAM", str(Path(__file__).parent / "fixtures" / "claude_stream.jsonl"))
    res = run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider)
    assert res.state == TaskState.DONE
    fresh = _invocations(new.folder)[-1]
    assert "--resume" not in fresh["argv"]
    assert "got stuck: it made the same tool call 3 times in a row" in fresh["stdin"]
