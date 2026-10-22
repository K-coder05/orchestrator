"""The Gemini API path (AI Studio credits), spend and the per-Task cap (§1.5), Phase 2.

The paid path is the Gemini CLI signed in with an API key; every model call
it makes is read from the CLI's local telemetry file, priced and recorded as
spend. Tests run it against the fake `gemini` (tests/fixtures/fake_gemini.py).
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml
from click.testing import CliRunner

from nightshift import cli
from nightshift.adapters.gemini import GeminiApiAdapter
from nightshift.config import ConfigError, load_config
from nightshift.db import Database
from nightshift.host import FakeHost
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState

from test_gemini import FAKE_GEMINI, _gemini_runs, fake_gemini, genv  # noqa: F401  (fixture)

KEY = "AIzaFAKE-studio-key-0123456789"
#: Simple prices so costs are easy to work out by hand (USD per million tokens).
PRICES = {"gemini-3.1-pro": {"input": 1.0, "cached_input": 0.1, "output": 10.0}}


def fake_gemini_api(settings) -> GeminiApiAdapter:
    a = GeminiApiAdapter(settings)
    a.home = Path(os.environ["FAKE_GEMINI_HOME"])
    orig = a._base_argv

    def base_argv(model, policy_file):
        return FAKE_GEMINI + orig(model, policy_file)[1:]

    a._base_argv = base_argv
    return a


def provider(name, settings):
    return fake_gemini_api(settings) if name == "gemini-api" else fake_gemini(settings)


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    d = tmp_path / "config"
    d.mkdir()
    (d / "config.yaml").write_text(yaml.safe_dump({
        "paths": {"agents_root": str(tmp_path / "agents")},
        "spend": {"per_task_usd": 2, "gemini_api_prices": PRICES},
    }))
    env = d / ".env"
    env.write_text(f"GEMINI_API_KEY={KEY}\n")
    env.chmod(0o600)
    return d


@pytest.fixture
def cfg(config_dir):
    return load_config(config_dir, host=FakeHost())


@pytest.fixture
def db(cfg):
    d = Database(cfg.db_path, tz=cfg.timezone)
    yield d
    d.close()


def _task(cfg, db, agent="gemini-api"):
    return create_task(cfg, db, "Summarize lecture 3", agent=agent, task_type="summary")


# --------------------------------------------------------------------------
# Spend
# --------------------------------------------------------------------------


def test_paid_task_records_every_call_as_spend(cfg, db, genv):
    # Each call: 24,100 prompt tokens of which 18,000 cached, 410 output.
    # 6,100 x $1 + 18,000 x $0.10 + 410 x $10 per million = $0.0120 a call.
    new = _task(cfg, db)
    result = run_attempt(cfg, db, new.spec.id, adapter_provider=provider)

    assert result.state == TaskState.DONE
    rows = db.spend_events(new.spec.id)
    assert len(rows) == 4  # the fake makes four model calls
    for r in rows:
        assert r["agent"] == "gemini-api" and r["model"] == "gemini-3.1-pro"
        assert r["run_id"] == result.run_id
        assert r["cost_usd"] == pytest.approx(0.0120)
    assert db.task_spend(new.spec.id) == pytest.approx(0.048)


def test_task_that_reaches_the_cap_is_stopped_and_needs_you(cfg, db, genv):
    # 100,000 output tokens a call at $10 per million: $1.00 a call, so the
    # second call reaches the $2 cap and the Attempt is stopped there.
    genv.setenv("FAKE_GEMINI_CALL_TOKENS", json.dumps(
        {"promptTokenCount": 0, "cachedContentTokenCount": 0, "candidatesTokenCount": 100_000}))
    genv.setenv("FAKE_GEMINI_STEP_DELAY", "0.5")
    new = _task(cfg, db)
    tid = new.spec.id
    result = run_attempt(cfg, db, tid, adapter_provider=provider)

    assert result.state == TaskState.NEEDS_YOU
    assert [r["cost_usd"] for r in db.spend_events(tid)] == pytest.approx([1.0, 1.0])
    [attempt] = db.attempts(tid)
    assert attempt["outcome"] == "spend_cap"
    why = db.transitions(tid)[-1]["reason"]
    assert "$2.00 per-Task spend cap" in why and "$2.00 spent" in why
    report = (new.folder / "report.md").read_text()
    assert "Spend cap:" in report and "$2.00 per-Task spend cap" in report
    assert not (new.folder / "output" / "notes.md").exists()  # stopped before it finished


def test_a_task_already_at_the_cap_makes_no_paid_call(cfg, db, genv):
    new = _task(cfg, db)
    tid = new.spec.id
    db.record_spend(tid, run_id=None, agent="gemini-api", model="gemini-3.1-pro", at="2026-10-22",
                    input_tokens=0, cached_tokens=0, output_tokens=0, thoughts_tokens=0,
                    cost_usd=2.0, raw=None)
    assert run_attempt(cfg, db, tid, adapter_provider=provider).state == TaskState.NEEDS_YOU
    assert _gemini_runs(new.folder) == []  # the CLI never started
    assert db.task_spend(tid) == pytest.approx(2.0)
    assert "$2.00 per-Task spend cap" in db.transitions(tid)[-1]["reason"]


def test_calls_flushed_at_exit_are_recorded_without_undoing_a_finished_task(cfg, db, genv):
    # Four calls of $0.60 written as the agent finishes: they cross the cap only
    # in the last pass after the exit. Recorded, but the Task stays done.
    genv.setenv("FAKE_GEMINI_CALL_TOKENS", json.dumps(
        {"promptTokenCount": 0, "cachedContentTokenCount": 0, "candidatesTokenCount": 60_000}))
    new = _task(cfg, db)
    assert run_attempt(cfg, db, new.spec.id, adapter_provider=provider).state == TaskState.DONE
    assert db.task_spend(new.spec.id) == pytest.approx(2.4)


#: A fixed afternoon in Los Angeles, so "today" is known.
NOON = datetime(2026, 10, 22, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles"))


def _earlier_spend(cfg, db, usd):
    """``usd`` spent this morning by another paid Task."""
    other = _task(cfg, db).spec.id
    db.transition(other, TaskState.CANCELLED, "test: only its spend matters")
    db.record_spend(other, run_id=None, agent="gemini-api", model="gemini-3.1-pro",
                    at="2026-10-22T09:00:00-07:00", input_tokens=0, cached_tokens=0,
                    output_tokens=0, thoughts_tokens=0, cost_usd=usd, raw=None)


def test_task_that_crosses_the_daily_cap_pauses_and_resumes_its_work(cfg, db, genv):
    # $4 spent this morning; $1.00 a call, so the first call reaches the $5 day.
    _earlier_spend(cfg, db, 4.0)
    genv.setenv("FAKE_GEMINI_CALL_TOKENS", json.dumps(
        {"promptTokenCount": 0, "cachedContentTokenCount": 0, "candidatesTokenCount": 100_000}))
    genv.setenv("FAKE_GEMINI_STEP_DELAY", "0.5")
    new = _task(cfg, db)
    tid = new.spec.id
    result = run_attempt(cfg, db, tid, adapter_provider=provider, clock=lambda: NOON)

    assert result.state == TaskState.PAUSED_SPEND
    [attempt] = db.attempts(tid)
    assert attempt["outcome"] == "paused"  # not a Failed Attempt
    row = db.get_task(tid)
    assert row["retries_used"] == 0 and row["resume_pending"] == 1
    assert row["pause_kind"] == "spend_day" and row["resume_at"] == "2026-10-23T00:00:00-07:00"
    why = db.transitions(tid)[-1]["reason"]
    assert why.startswith("Attempt 1 stopped: paid calls reached the $5.00 daily spend cap")
    assert not (new.folder / "output" / "notes.md").exists()  # stopped before it finished


def test_paid_task_started_while_a_cap_holds_makes_no_paid_call(cfg, db, genv):
    _earlier_spend(cfg, db, 5.0)
    new = _task(cfg, db)
    tid = new.spec.id
    assert run_attempt(cfg, db, tid, adapter_provider=provider,
                       clock=lambda: NOON).state == TaskState.PAUSED_SPEND
    assert _gemini_runs(new.folder) == []  # the CLI never started
    assert "daily spend cap" in db.transitions(tid)[-1]["reason"]


def test_subscription_path_runs_while_a_cap_holds(cfg, db, genv):
    _earlier_spend(cfg, db, 15.0)
    new = _task(cfg, db, agent="gemini")
    assert run_attempt(cfg, db, new.spec.id, adapter_provider=provider,
                       clock=lambda: NOON).state == TaskState.DONE


def test_subscription_path_never_spends_or_sees_the_key(cfg, db, genv):
    new = _task(cfg, db, agent="gemini")
    assert run_attempt(cfg, db, new.spec.id, adapter_provider=provider).state == TaskState.DONE
    assert db.spend_events() == []
    [run] = _gemini_runs(new.folder)
    assert run["env"]["GEMINI_API_KEY"] == ""  # an empty key is no key to the CLI
    assert "GEMINI_TELEMETRY_OUTFILE" not in run["env"]
    settings = json.loads(Path(run["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text())
    assert settings["security"]["auth"]["selectedType"] == "oauth-personal"


# --------------------------------------------------------------------------
# The API key
# --------------------------------------------------------------------------


def test_api_path_signs_in_with_the_key_and_never_logs_it(cfg, db, genv):
    new = _task(cfg, db)
    run_attempt(cfg, db, new.spec.id, adapter_provider=provider)
    [run] = _gemini_runs(new.folder)
    assert run["env"]["GEMINI_API_KEY"] == KEY
    assert run["env"]["GEMINI_TELEMETRY_LOG_PROMPTS"] == "false"
    settings = json.loads(Path(run["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text())
    assert settings["security"]["auth"]["selectedType"] == "gemini-api-key"
    [attempt] = db.attempts(new.spec.id)
    assert KEY not in attempt["command"]
    for name in ("nightshift.log", "attempt-1.gemini-api.jsonl"):
        assert KEY not in (new.folder / "logs" / name).read_text()
    assert KEY not in (new.folder / "report.md").read_text()


def test_api_path_without_the_key_does_not_start(tmp_path, genv):
    cdir = tmp_path / "nokey"
    cdir.mkdir()
    (cdir / "config.yaml").write_text(yaml.safe_dump(
        {"paths": {"agents_root": str(tmp_path / "agents")}}))
    cfg = load_config(cdir, host=FakeHost())
    db = Database(cfg.db_path, tz=cfg.timezone)
    new = _task(cfg, db)
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        run_attempt(cfg, db, new.spec.id, adapter_provider=provider)
    assert db.attempts(new.spec.id) == []
    db.close()


# --------------------------------------------------------------------------
# orch usage
# --------------------------------------------------------------------------


def test_orch_usage_shows_spend_and_remaining_credit(config_dir, cfg, db, genv):
    data = yaml.safe_load((config_dir / "config.yaml").read_text())
    data["plans"] = {"gemini": {"api_credit_balance_usd": 10, "api_credit_balance_as_of":
                                "2026-10-01", "api_credit_expiry": "2027-01-31"}}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    tid = _task(cfg, db).spec.id
    for at, cost in (("2026-09-15T10:00:00-07:00", 1.0), ("2026-10-20T10:00:00-07:00", 2.5)):
        db.record_spend(tid, run_id=None, agent="gemini-api", model="m", at=at, input_tokens=0,
                        cached_tokens=0, output_tokens=0, thoughts_tokens=0, cost_usd=cost,
                        raw=None)

    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage"])
    assert r.exit_code == 0, r.output
    assert "Gemini API credits" in r.output
    assert "spent $3.50 so far" in r.output
    assert "$7.50 left of $10.00 (balance as of 2026-10-01)" in r.output
    assert "expires 2027-01-31" in r.output


def test_orch_usage_shows_the_day_and_month_against_their_caps(config_dir, cfg, db,
                                                               monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: NOON)
    _earlier_spend(cfg, db, 5.0)
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage"])
    assert r.exit_code == 0, r.output
    assert "today $5.00 of $5.00, this month $5.00 of $15.00" in r.output
    assert "Paid calls paused: paid calls reached the $5.00 daily spend cap" in r.output


def test_orch_raise_cap_raises_this_months_cap_and_needs_more_than_it(config_dir, cfg, db,
                                                                      monkeypatch):
    monkeypatch.setattr(cli, "clock", lambda: NOON)
    run = CliRunner().invoke
    r = run(cli.main, ["--config-dir", str(config_dir), "raise-cap", "20"])
    assert r.exit_code == 0, r.output
    assert "raised from $15.00 to $20.00 for 2026-10" in r.output
    r = run(cli.main, ["--config-dir", str(config_dir), "usage"])
    assert "this month $0.00 of $20.00" in r.output
    r = run(cli.main, ["--config-dir", str(config_dir), "raise-cap", "18"])
    assert r.exit_code != 0 and "above this month's cap of $20.00" in r.output


def test_orch_usage_with_expired_credits_has_none_left(config_dir, cfg, db):
    data = yaml.safe_load((config_dir / "config.yaml").read_text())
    data["plans"] = {"gemini": {"api_credit_balance_usd": 10, "api_credit_expiry": "2026-01-31"}}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    r = CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), "usage"])
    assert r.exit_code == 0, r.output
    assert "$0.00 left of $10.00" in r.output and "expired 2026-01-31" in r.output


def test_bad_credit_dates_are_rejected(config_dir):
    data = yaml.safe_load((config_dir / "config.yaml").read_text())
    data["plans"] = {"gemini": {"api_credit_expiry": "next spring"}}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="api_credit_expiry"):
        load_config(config_dir, host=FakeHost())
