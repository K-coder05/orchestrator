"""Reset detection, probing, Limit observations, Baselines and `orch usage` (§1.5)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from click.testing import CliRunner

from nightshift import cli, pauses, usage
from nightshift.adapters.base import Event, EventKind, RateLimit
from nightshift.adapters.claude import ClaudeAdapter, parse_reset_text, window_from_text
from nightshift.daemon import QueueRunner
from nightshift.host import FakeHost
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import iso

from conftest import FIXTURES
from test_pauses import Clock
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

LA = ZoneInfo("America/Los_Angeles")
REAL_RESET = datetime.fromtimestamp(1792578000, tz=timezone.utc).isoformat()


def _events(adapter, path):
    out = []
    for line in path.read_text().splitlines():
        out += adapter.parse_stream(line)
    return out


# --------------------------------------------------------------------------
# Detection of the real format
# --------------------------------------------------------------------------


def test_real_limit_hit_is_detected_with_type_and_reset():
    a = ClaudeAdapter()
    a.clock = lambda: datetime(2026, 10, 21, 0, 52, tzinfo=LA)
    evs = _events(a, FIXTURES / "claude_limit_hit.jsonl")
    hits = [(e.kind, a.detect_rate_limit(e)) for e in evs]
    hits = [(k, h) for k, h in hits if h is not None]
    kinds = [k for k, _ in hits]
    # rate_limit_event, synthetic assistant error, error result: all three say so.
    assert kinds == [EventKind.RATE_LIMIT_INFO, EventKind.ERROR, EventKind.ERROR]
    for _, h in hits:
        assert h.limit_type == "five_hour"
        assert datetime.fromisoformat(h.reset_at) == datetime.fromisoformat(REAL_RESET)
    windows = {w.window: w for e in evs for w in a.window_info(e)}
    assert windows["five_hour"].utilization == 1.04 and windows["five_hour"].status == "rejected"
    assert windows["seven_day"].utilization == 0.18
    assert a.result_ok() is False
    # The synthetic message is not a request.
    assert a.usage_from_run()[0].requests is None


def test_reset_and_window_from_message_text():
    now = datetime(2026, 10, 21, 16, 0, tzinfo=LA)
    assert parse_reset_text("You've hit your session limit · resets 3:20am (America/Los_Angeles)",
                            now) == "2026-10-22T03:20:00-07:00"
    assert parse_reset_text("resets 5pm (America/Los_Angeles)", now) == "2026-10-21T17:00:00-07:00"
    assert parse_reset_text("You've hit your weekly limit · resets Oct 25, 3pm (America/Los_Angeles)",
                            now) == "2026-10-25T15:00:00-07:00"
    assert parse_reset_text("resets Jan 2, 9am (America/Los_Angeles)",
                            datetime(2026, 12, 30, tzinfo=LA)).startswith("2027-01-02T09:00")
    assert datetime.fromisoformat(parse_reset_text("Claude AI usage limit reached|1792578000", now)) \
        == datetime.fromisoformat(REAL_RESET)
    assert parse_reset_text("Claude AI usage limit reached", now) is None
    assert window_from_text("You've hit your session limit") == "five_hour"
    assert window_from_text("You've hit your weekly limit") == "seven_day"
    assert window_from_text("You've hit your Opus weekly limit") == "seven_day_opus"
    assert window_from_text("You've hit your limit") is None


def test_text_only_error_still_gives_reset_time():
    a = ClaudeAdapter()
    a.clock = lambda: datetime(2026, 10, 21, 1, 0, tzinfo=LA)
    ev = Event(EventKind.ERROR, {"message": "rate_limit", "api_error_status": 429,
                                 "text": "You've hit your session limit · resets 3:20am "
                                         "(America/Los_Angeles)"})
    hit = a.detect_rate_limit(ev)
    assert hit.limit_type == "five_hour" and hit.reset_at == "2026-10-21T03:20:00-07:00"
    # A throttle with no limit details: a hit with no reset (probing finds it).
    bare = a.detect_rate_limit(Event(EventKind.ERROR, {"message": "rate_limit"}))
    assert bare.limit_type is None and bare.reset_at is None


def test_usage_of_a_stopped_attempt_comes_from_the_stream():
    a = ClaudeAdapter()
    lines = (FIXTURES / "claude_stream.jsonl").read_text().splitlines()[:-1]  # no result
    for line in lines:
        a.parse_stream(line)
    [u] = a.usage_from_run()
    assert u.model == "claude-haiku-5-5" and u.cost_usd is None
    assert u.requests == 6 and u.input_tokens == 12 and u.cache_read_tokens > 0
    assert u.output_tokens > 0


def test_probe_command_has_no_tools_and_no_session():
    a = ClaudeAdapter({"model": "haiku"})
    argv = a.probe_command().argv
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv and "--restricted" in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--settings" not in argv and "--add-dir" not in argv


# --------------------------------------------------------------------------
# Limit observations and Baselines
# --------------------------------------------------------------------------


def test_limit_hit_records_observation_with_window_usage(cfg, db, env):  # noqa: F811
    clock = Clock()
    reset = (clock() + timedelta(hours=2)).replace(microsecond=0)
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    done = create_task(cfg, db, "earlier").spec.id
    assert qr.run_next() == done and db.task_state(done) == TaskState.DONE
    earlier_cost = sum(r["cost_usd"] for r in db.usage_for_task(done))

    env.setenv("FAKE_LIMIT_AT", "6")
    env.setenv("FAKE_RESETS_AT", str(int(reset.timestamp())))
    clock.advance(minutes=1)
    hit = create_task(cfg, db, "hits the limit").spec.id
    assert qr.run_next() == hit and db.task_state(hit) == TaskState.PAUSED_USAGE

    [obs] = db.limit_observations("claude")
    assert obs["window"] == "five_hour" and obs["source"] == "attempt"
    assert obs["task_id"] == hit and obs["limit_type"] == "five_hour"
    assert datetime.fromisoformat(obs["resets_at"]) == reset
    assert datetime.fromisoformat(obs["window_start"]) == reset - timedelta(hours=5)
    # The earlier Attempt's reported cost, plus the cut-off Attempt's usage.
    assert abs(obs["cost_usd"] - earlier_cost) < 1e-9
    assert obs["requests"] >= 6 + 1 and obs["input_tokens"] > 0
    assert obs["utilization"] == 1.04
    assert usage.baseline(db, "claude", "five_hour").cost_usd == obs["cost_usd"]
    # The hit Attempt's own usage row (result line with cost 0) is recorded too.
    assert db.usage_for_task(hit)


def test_simulated_limit_is_not_an_observation(cfg, db, env):  # noqa: F811
    tid = create_task(cfg, db, "x", simulate_rate_limit_after=1).spec.id
    assert run_attempt(cfg, db, tid, adapter_provider=fake_provider).state == TaskState.PAUSED_USAGE
    assert db.limit_observations() == []


def test_same_window_is_observed_once_and_baseline_is_lowest_of_last_3(cfg, db):
    t0 = datetime(2026, 10, 21, 1, 0, tzinfo=LA)
    def hit(at, reset, cost):
        tid = create_task(cfg, db, f"t{at}").spec.id
        run = db.start_attempt(tid, "claude", ["claude"], "x")
        db.record_usage(run, tid, "claude", [{"model": "m", "cost_usd": cost, "input_tokens": 5}],
                        at=iso(at - timedelta(minutes=1)))
        return usage.record_limit_hit(cfg, db, "claude",
                                      RateLimit("five_hour", iso(reset) if reset else None),
                                      at=at, source="attempt")

    assert hit(t0, t0 + timedelta(hours=2), 4.0) is not None
    # Still out: the same Window, by reset time or (no reset) within its length.
    assert usage.record_limit_hit(cfg, db, "claude", RateLimit("five_hour", iso(t0 + timedelta(hours=2))),
                                  at=t0 + timedelta(minutes=30), source="probe") is None
    assert usage.record_limit_hit(cfg, db, "claude", None, at=t0 + timedelta(minutes=40),
                                  source="probe") is None
    t1 = t0 + timedelta(hours=6)
    assert hit(t1, t1 + timedelta(hours=1), 3.0) is not None
    t2 = t1 + timedelta(hours=6)
    assert hit(t2, None, 5.0) is not None  # no reset given: trailing 5h Window
    obs = db.limit_observations("claude", "five_hour")
    assert [round(o["cost_usd"], 2) for o in obs] == [4.0, 3.0, 5.0]
    assert obs[2]["resets_at"] is None
    assert datetime.fromisoformat(obs[2]["window_start"]) == t2 - timedelta(hours=5)
    b = usage.baseline(db, "claude", "five_hour")
    assert b.cost_usd == 3.0 and b.observations == 3
    t3 = t2 + timedelta(hours=6)
    hit(t3, t3 + timedelta(hours=1), 6.0)
    assert usage.baseline(db, "claude", "five_hour").cost_usd == 3.0
    t4 = t3 + timedelta(hours=6)
    hit(t4, t4 + timedelta(hours=1), 7.0)  # 3.0 drops out of the last 3
    assert usage.baseline(db, "claude", "five_hour").cost_usd == 5.0
    assert usage.baseline(db, "claude", "seven_day") is None
    # A hit in a Window Nightshift didn't use (all interactive) is no Baseline.
    usage.record_limit_hit(cfg, db, "claude", RateLimit("seven_day", None),
                           at=t4 + timedelta(days=30), source="probe")
    assert usage.baseline(db, "claude", "seven_day") is None


# --------------------------------------------------------------------------
# Probing
# --------------------------------------------------------------------------


def _paused_without_reset(cfg, db, env, clock, qr):  # noqa: F811
    env.setenv("FAKE_LIMIT_AT", "6")
    env.setenv("FAKE_LIMIT_FORMAT", "none")  # "Claude AI usage limit reached": no reset
    tid = create_task(cfg, db, "x").spec.id
    assert qr.run_next() == tid
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["pause_reset_at"] is None
    assert row["resume_at"] == iso(clock() + timedelta(minutes=30))
    env.delenv("FAKE_LIMIT_AT")
    return tid


def test_no_reset_time_probes_until_the_window_is_back(cfg, db, env, tmp_path):  # noqa: F811
    clock = Clock()
    log = tmp_path / "probes.jsonl"
    env.setenv("FAKE_PROBE_LOG", str(log))
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    tid = _paused_without_reset(cfg, db, env, clock, qr)
    [obs] = db.limit_observations("claude")
    assert obs["resets_at"] is None and obs["window"] == "five_hour"

    clock.advance(minutes=29)
    assert qr.run_next() is None and not log.exists()  # not due yet

    env.setenv("FAKE_PROBE", "none")  # still limited, still no reset time
    clock.advance(minutes=1)
    assert qr.run_next() is None
    row = db.get_task(tid)
    assert row["state"] == "paused_usage" and row["resume_at"] == iso(clock() + timedelta(minutes=30))
    assert len(log.read_text().splitlines()) == 1
    assert [p["outcome"] for p in db.probes("claude")] == ["limited"]
    assert len(db.limit_observations("claude")) == 1  # the same Window, still out
    log_text = open(row["folder"] + "/logs/nightshift.log").read()
    assert "probe: claude is still out of usage" in log_text

    env.setenv("FAKE_PROBE", "real")  # still limited, now with a reset time
    reset = (clock() + timedelta(minutes=45)).replace(microsecond=0)
    env.setenv("FAKE_RESETS_AT", str(int(reset.timestamp())))
    clock.advance(minutes=30)
    assert qr.run_next() is None
    row = db.get_task(tid)
    assert row["pause_reset_at"] == iso(reset.astimezone(clock().tzinfo))
    assert row["resume_at"] == row["pause_reset_at"]

    clock.advance(minutes=46)  # the reset time: requeued without another probe
    assert qr.run_next() == tid
    assert db.task_state(tid) == TaskState.DONE
    assert len(log.read_text().splitlines()) == 2
    # Probe usage is recorded outside Attempts.
    assert {r["kind"] for r in db.planning_usage()} == {"probe"}


def test_probe_that_answers_requeues_and_records_usage(cfg, db, env, tmp_path):  # noqa: F811
    clock = Clock()
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    tid = _paused_without_reset(cfg, db, env, clock, qr)
    env.setenv("FAKE_PROBE", "ok")
    clock.advance(minutes=30)
    assert qr.run_next() == tid and db.task_state(tid) == TaskState.DONE
    assert [p["outcome"] for p in db.probes()] == ["available"]
    [pu] = db.planning_usage()
    assert pu["kind"] == "probe" and pu["task_id"] is None and pu["cost_usd"] == 0.0001
    reasons = [t["reason"] for t in db.transitions(tid)]
    assert any("probe: claude answered" in r for r in reasons)
    assert db.window_status("claude")["five_hour"]["status"] == "allowed"  # back in


def test_failed_probe_lets_the_task_try_itself(cfg, db, env):  # noqa: F811
    clock = Clock()
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    tid = _paused_without_reset(cfg, db, env, clock, qr)
    env.setenv("FAKE_PROBE", "error")
    clock.advance(minutes=30)
    assert qr.run_next() == tid
    assert [p["outcome"] for p in db.probes()] == ["error"]
    assert any("probe of claude failed" in t["reason"] for t in db.transitions(tid))


def test_promote_due_probes_once_per_agent(cfg, db):
    clock = Clock()
    ids = []
    for n in range(2):
        tid = create_task(cfg, db, f"t{n}").spec.id
        run = db.start_attempt(tid, "claude", ["claude"], "x")
        db.transition(tid, TaskState.RUNNING, "started", run_id=run)
        pauses.pause_after_attempt(cfg, db, tid, kind=pauses.KIND_LIMIT, limit=None,
                                   reason="limit", at=clock(), run_id=run)
        ids.append(tid)
    calls = []

    def prober(agent):
        calls.append(agent)
        from nightshift.adapters.base import ProbeReply
        return ProbeReply(available=True)

    clock.advance(minutes=31)
    out = pauses.promote_due(cfg, db, clock(), prober=prober)
    assert calls == ["claude"] and sorted(out.resumed) == sorted(ids)


# --------------------------------------------------------------------------
# orch usage
# --------------------------------------------------------------------------


def test_orch_usage_shows_windows_and_baselines(config_dir, cfg, db, env, monkeypatch):  # noqa: F811
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage"])
    assert r.exit_code == 0, r.output
    assert "five_hour" in r.output and "seven_day" in r.output
    assert r.output.count("none yet (0 observations)") == 2
    assert "Interactive use" in r.output

    clock = Clock()
    qr = QueueRunner(cfg, db, FakeHost(), adapter_provider=fake_provider, clock=clock)
    create_task(cfg, db, "earlier")
    qr.run_next()
    env.setenv("FAKE_LIMIT_AT", "6")
    env.setenv("FAKE_RESETS_AT", str(int((clock() + timedelta(hours=1)).timestamp())))
    create_task(cfg, db, "x")
    qr.run_next()
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage"])
    assert "lowest of last 1" in r.output and "out of usage" in r.output
    assert "Limit observations" in r.output and "Paused on its limit" in r.output
    assert "none yet (0 observations)" in r.output  # seven_day has none

    env.setenv("FAKE_PROBE", "ok")
    monkeypatch.setattr(cli, "probe_provider", fake_provider)
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage", "--probe", "claude"])
    assert r.exit_code == 0, r.output
    assert r.output.startswith("probe claude: available")
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage", "--probe", "nope"])
    assert r.exit_code != 0 and "unknown agent" in r.output


def test_window_config_is_validated(config_dir):
    import yaml
    from nightshift.config import ConfigError, load_config
    import pytest
    (config_dir / "config.yaml").write_text(yaml.safe_dump(
        {"paths": {"agents_root": "/tmp/x"}, "usage": {"windows": {"claude": {"five_hour": 0}}}}))
    with pytest.raises(ConfigError):
        load_config(config_dir, host=FakeHost())


def test_events_json_roundtrip_of_fixture_is_valid():
    for line in (FIXTURES / "claude_limit_hit.jsonl").read_text().splitlines():
        json.loads(line)
