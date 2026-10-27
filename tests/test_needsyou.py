"""Resolving an agent's "needs you" request (§1.6, §2.10): approve or deny,
both requeue. The agent is the fake claude (tests/fixtures/fake_claude.py)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

import pytest
from click.testing import CliRunner

from nightshift import cleanweek, cli, control, digest, needsyou, queries, remote, spend
from nightshift.alerts import Notifier
from nightshift.runner import create_task, run_attempt
from nightshift.states import TaskState
from nightshift.timeutil import now

from test_alerts import api, tg_cfg, tg_db  # noqa: F401  (pytest fixtures)
from test_runner import env, fake_provider  # noqa: F401  (pytest fixture)

SESSION = "f5e745a1-aea4-451e-bef1-64b1544eb8de"  # the recorded stream's session
REQUEST = {"kind": "approval", "summary": "May I push?", "details": "",
           "proposed_action": "git push"}


class Clock:
    """A fake clock starting at real time (DB timestamps are real time)."""

    def __init__(self):
        self.t = now("America/Los_Angeles")

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kw) -> None:
        self.t += timedelta(**kw)


def _orch(config_dir, *args):
    return CliRunner().invoke(cli.main, ["--config-dir", str(config_dir), *args])


def _invocations(folder):
    path = folder / "logs" / "fake_invocations.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()]


def _needs_you(cfg, db, env, clock, prompt="Push the fix"):  # noqa: F811
    """A Task whose agent wrote needs_you.json and stopped."""
    env.setenv("FAKE_NEEDS_YOU", "1")
    new = create_task(cfg, db, prompt)
    assert run_attempt(cfg, db, new.spec.id, adapter_provider=fake_provider,
                       clock=clock).state == TaskState.NEEDS_YOU
    env.delenv("FAKE_NEEDS_YOU")
    return new


def _section2(folder) -> str:
    text = (folder / "report.md").read_text()
    return text.split("## 2. Needs you", 1)[1].split("## 3.", 1)[0]


def test_orch_approve_resolves_the_request_and_the_next_attempt_resumes_told(
        config_dir, cfg, db, env):  # noqa: F811
    clock = Clock()
    new = _needs_you(cfg, db, env, clock)
    tid, folder = new.spec.id, new.folder
    raw = (folder / "needs_you.json").read_bytes()
    assert [r.task_id for r in queries.needs_you_inbox(cfg, db)] == [tid]

    r = _orch(config_dir, "approve", tid)
    assert r.exit_code == 0, r.output
    assert f"Task {tid}: request approved; queued" in r.output
    row = db.get_task(tid)
    assert row["state"] == TaskState.QUEUED and row["resume_pending"] == 1
    assert "(orch approve)" in db.transitions(tid)[-1]["reason"]
    [a] = db.approvals(tid)
    assert (a["subject"], a["decision"], a["via"], a["request_kind"]) == (
        "request", "approved", "orch approve", "approval")
    assert a["request_sha256"] == hashlib.sha256(raw).hexdigest()
    assert a["run_id"] == db.attempts(tid)[-1]["id"] and row["request_decision_id"] == a["id"]
    # The request moved to the logs; the inbox no longer lists it.
    assert not (folder / "needs_you.json").exists()
    assert (folder / a["request_file"]).read_bytes() == raw
    assert a["request_file"] == "logs/attempt-1.needs_you.json"
    assert queries.needs_you_inbox(cfg, db) == []
    # The report lists the decision right away, not the request as open.
    needs = _section2(folder)
    assert "Resolved approval request: May I push? (proposed: git push). Approved via " \
           "orch approve" in needs
    assert "**approval:**" not in needs
    assert "approval request approved via orch approve" in _orch(config_dir, "show", tid).output

    # The next Attempt resumes the same session and is told.
    clock.advance(minutes=5)
    assert run_attempt(cfg, db, tid, adapter_provider=fake_provider,
                       clock=clock).state == TaskState.DONE
    second = _invocations(folder)[1]
    assert second["argv"][second["argv"].index("--resume") + 1] == SESSION
    told = second["stdin"]
    assert "APPROVED" in told and "May I push?" in told and "git push" in told
    assert "kind: approval" in told
    assert "told its needs-you request was approved" in db.transitions(tid)[-2]["reason"]
    assert db.get_task(tid)["request_decision_id"] is None
    # The final report still lists the decision; nothing is open.
    needs = _section2(folder)
    assert "Resolved approval request" in needs and "**approval:**" not in needs
    assert needs.count("Resolved approval request") == 1


def test_deny_from_telegram_requeues_and_a_fresh_session_is_told(cfg, db, env):  # noqa: F811
    clock = Clock()
    new = _needs_you(cfg, db, env, clock)
    tid = new.spec.id
    replies = []
    remote.run(cfg, db, f"deny {tid}", at=clock(), reply=lambda text, buttons=None:
               replies.append(text))
    [reply] = replies
    assert reply.startswith(f"Task {tid}: request denied; queued, its next Attempt carries on "
                            "without it.")
    assert db.task_state(tid) == TaskState.QUEUED  # not cancelled
    [a] = db.approvals(tid)
    assert (a["subject"], a["decision"], a["via"]) == ("request", "denied", "telegram")

    clock.advance(hours=25)  # past the 24h session limit: a fresh session
    assert run_attempt(cfg, db, tid, adapter_provider=fake_provider,
                       clock=clock).state == TaskState.DONE
    second = _invocations(new.folder)[1]
    assert "--resume" not in second["argv"]
    told = second["stdin"]
    assert told.startswith("Push the fix") and "plan.md" in told and "progress.md" in told
    assert "DENIED" in told and "Don't take that action" in told and "May I push?" in told
    assert "over 24h" in told
    assert "Denied via telegram" in _section2(new.folder)
    # Counted for the clean week, like any change by hand from Telegram.
    hands = cleanweek.check(cfg, db, end=clock() + timedelta(minutes=1)).interventions
    assert any(i.command == "telegram" and i.task_id == tid for i in hands)


def test_a_failed_resume_falls_back_fresh_and_still_tells_the_decision(cfg, db, env):  # noqa: F811
    clock = Clock()
    new = _needs_you(cfg, db, env, clock)
    tid = new.spec.id
    assert control.approve(cfg, db, tid, via="dashboard").ok
    env.setenv("FAKE_RESUME_FAIL", "1")
    assert run_attempt(cfg, db, tid, adapter_provider=fake_provider,
                       clock=clock).state == TaskState.DONE
    assert [a["outcome"] for a in db.attempts(tid)] == ["needs_you", "resume_failed",
                                                         "completed"]
    third = _invocations(new.folder)[2]
    assert "--resume" not in third["argv"]
    assert "APPROVED" in third["stdin"] and "the resume failed" in third["stdin"]


def test_a_later_request_is_a_fresh_one(config_dir, cfg, db, env):  # noqa: F811
    clock = Clock()
    new = _needs_you(cfg, db, env, clock)
    tid, folder = new.spec.id, new.folder
    assert control.deny(cfg, db, tid, via="orch deny").ok
    env.setenv("FAKE_NEEDS_YOU", "1")  # the agent asks again
    assert run_attempt(cfg, db, tid, adapter_provider=fake_provider,
                       clock=clock).state == TaskState.NEEDS_YOU
    req = needsyou.open_request(db, db.get_task(tid))
    assert req is not None and req.attempt_no == 2
    [r] = queries.needs_you_inbox(cfg, db)
    assert (r.task_id, r.kind, r.summary) == (tid, "approval", "May I push?")
    needs = _section2(folder)
    assert needs.index("**approval:** May I push?") < needs.index("Resolved approval request")
    assert control.approve(cfg, db, tid, via="orch approve").ok
    assert [a["decision"] for a in db.approvals(tid)] == ["denied", "approved"]
    assert (folder / "logs" / "attempt-1.needs_you.json").exists()
    assert (folder / "logs" / "attempt-2.needs_you.json").exists()
    # A second decision on the same request is refused.
    again = control.approve(cfg, db, tid, via="orch approve")
    assert again.refusal == control.WRONG_STATE and len(db.approvals(tid)) == 2


def test_other_needs_you_reasons_are_refused_and_keep_their_handling(cfg, db):
    tid = create_task(cfg, db, "Scrape with paid credits").spec.id
    run = db.start_attempt(tid, "gemini-api", ["gemini"], "log")
    db.finish_attempt(run, exit_code=0, outcome=spend.OUTCOME_CAP,
                      end_reason="Attempt 1 stopped: the per-Task spend cap", session_id=None)
    db.transition(tid, TaskState.RUNNING, "Attempt 1 started", run_id=run)
    db.transition(tid, TaskState.NEEDS_YOU, "Attempt 1 stopped: the per-Task spend cap")
    for res in (control.approve(cfg, db, tid, via="orch approve"),
                control.deny(cfg, db, tid, via="telegram")):
        assert res.refusal == control.WRONG_STATE
        assert "not on an agent's request" in res.message
    assert db.task_state(tid) == TaskState.NEEDS_YOU and db.approvals(tid) == []
    assert control.cancel(cfg, db, tid, via="orch cancel", at=now(cfg.timezone)).ok


def _raised(cfg, db, title="Book the venue"):
    """A Task that raised a request, without running an agent."""
    new = create_task(cfg, db, title)
    tid = new.spec.id
    run = db.start_attempt(tid, "claude", ["claude"], "log")
    db.transition(tid, TaskState.RUNNING, "Attempt 1 started", run_id=run)
    (new.folder / "needs_you.json").write_text(json.dumps(REQUEST))
    db.finish_attempt(run, exit_code=0, outcome="needs_you",
                      end_reason="agent wrote needs_you.json and stopped", session_id=None)
    from nightshift import failures
    failures.finalize(cfg, db, db.get_task(tid), TaskState.NEEDS_YOU, "x", at=now(cfg.timezone))
    db.transition(tid, TaskState.NEEDS_YOU, "agent wrote needs_you.json and stopped")
    return new


def test_telegram_no_longer_lists_a_resolved_request(tg_cfg, tg_db, api):  # noqa: F811
    notifier = Notifier(tg_cfg, tg_db, api)
    new = _raised(tg_cfg, tg_db)
    tid = new.spec.id
    notifier.collect()  # the alert is made, not sent yet (Telegram was down)
    assert control.approve(tg_cfg, tg_db, tid, via="telegram").ok
    notifier.deliver()
    assert api.sent == []  # dropped, never sent
    [alert] = tg_db.conn.execute("SELECT * FROM alerts").fetchall()
    assert alert["dropped_at"] and alert["sent_at"] is None
    at = now(tg_cfg.timezone)
    text = digest.digest_text(tg_db, tg_db.transitions_after(0), since=at - timedelta(hours=9),
                              at=at)
    assert "Needs you" not in text
    status = remote.status_text(tg_cfg, tg_db)
    assert "Needs you: 0 open" in status

    # A request still open is alerted and in the digest, and the alert
    # shows it, not an earlier resolved one.
    other = _raised(tg_cfg, tg_db, "Second venue")
    notifier.collect()
    notifier.deliver()
    [(_, sent)] = api.sent
    assert f"Task {other.spec.id} needs you" in sent and "approval: May I push?" in sent
    text = digest.digest_text(tg_db, tg_db.transitions_after(0), since=at - timedelta(hours=9),
                              at=now(tg_cfg.timezone))
    assert "Needs you (1)" in text and other.spec.id in text


@pytest.mark.parametrize("approve_it", [True, False])
def test_report_keeps_the_history_and_never_repeats_it(cfg, db, approve_it):
    new = _raised(cfg, db)
    tid = new.spec.id
    (control.approve if approve_it else control.deny)(cfg, db, tid, via="orch approve")
    from nightshift import failures
    for _ in range(2):  # re-finalized: still one entry
        failures.finalize(cfg, db, db.get_task(tid), TaskState.CANCELLED, "x",
                          at=now(cfg.timezone))
    needs = _section2(new.folder)
    assert needs.count("Resolved approval request") == 1 and "**approval:**" not in needs
    assert ("Approved" if approve_it else "Denied") in needs
