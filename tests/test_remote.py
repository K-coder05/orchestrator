"""Telegram commands (§5 item 1), against a fake Bot API and a real DB."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from nightshift import cleanweek, control, daemon, remote
from nightshift.alerts import OFFSET_KEY, Notifier
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import now

from test_alerts import TOKEN, USER_ID, _msg, api, notifier, tg_cfg, tg_db  # noqa: F401
from test_planning import plan_provider, planner  # noqa: F401  (pytest fixture)


class Chat:
    """You, typing commands to the bot."""

    def __init__(self, notifier, api):
        self.notifier = notifier
        self.api = api
        self.next_id = 100

    def send(self, text: str, *, sender: int = USER_ID) -> list[str]:
        """Send ``text`` and return the replies it got."""
        before = len(self.api.sent)
        self.api.updates.append(_msg(self.next_id, sender, text))
        self.next_id += 1
        assert self.notifier.poll_updates() is True
        new = self.api.sent[before:]
        assert all(chat == USER_ID for chat, _ in new)
        return [text for _, text in new]

    def reply(self, text: str) -> str:
        [answer] = self.send(text)
        return answer


@pytest.fixture
def chat(notifier, api):  # noqa: F811
    notifier.cancel_wait = notifier.pause_wait = 0
    return Chat(notifier, api)


def _running(db, tid):
    run = db.start_attempt(tid, "claude", ["claude"], "x")
    db.transition(tid, TaskState.RUNNING, "Attempt 1 started", run_id=run)
    return run


# -- help, status, list --------------------------------------------------------

def test_help_lists_every_command(chat):
    text = chat.reply("help")
    for cmd in ("status", "list", "approve", "deny", "cancel", "pause", "resume", "retry",
                "priority", "add", "help"):
        assert f"\n{cmd} " in text, cmd
    assert chat.reply("/help") == text == chat.reply("/HELP@nightshift_bot")
    assert chat.reply("hello there") == "Unknown command. Send help for the list."


def test_status_on_an_empty_queue(chat):
    assert chat.reply("status") == (
        "Nightshift: daemon down\nRunning: nothing\n"
        "Queue: 0 waiting to run (0 queued, 0 waiting to retry, 0 scheduled), 0 paused\n"
        "Needs you: 0 open")


def test_status_shows_daemon_running_task_queue_and_needs_you(chat, tg_cfg, tg_db,  # noqa: F811
                                                             monkeypatch):
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    run = create_task(tg_cfg, tg_db, "Fix the flaky test", task_type="coding").spec.id
    _running(tg_db, run)
    create_task(tg_cfg, tg_db, "Queued one")
    create_task(tg_cfg, tg_db, "Queued two")
    rw = create_task(tg_cfg, tg_db, "Waits to retry").spec.id
    _running(tg_db, rw)
    tg_db.transition(rw, TaskState.RETRY_WAIT, "Attempt 1 crashed; retry 1 of 2")
    create_task(tg_cfg, tg_db, "Big plan", needs_approval=True)
    ny = create_task(tg_cfg, tg_db, "Pick a venue").spec.id
    _running(tg_db, ny)
    tg_db.transition(ny, TaskState.NEEDS_YOU, "agent wrote needs_you.json and stopped")
    text = chat.reply("/status")
    assert text.splitlines() == [
        "Nightshift: daemon up",
        f"Running: {run} coding: Fix the flaky test",
        "Queue: 3 waiting to run (2 queued, 1 waiting to retry, 0 scheduled), 0 paused",
        "Needs you: 2 open",
    ]


def test_list_groups_tasks_by_status(chat, tg_cfg, tg_db):  # noqa: F811
    assert chat.reply("list") == "No Tasks."
    q = create_task(tg_cfg, tg_db, "Summarize the Q3 notes", task_type="summary").spec.id
    r = create_task(tg_cfg, tg_db, "Fix the flaky test", task_type="coding",
                    priority="urgent").spec.id
    _running(tg_db, r)
    f = create_task(tg_cfg, tg_db, "Scrape the jobs page").spec.id
    tg_db.transition(f, TaskState.FAILED, "boom")
    lines = chat.reply("list").splitlines()
    assert lines == [
        "Running (1)", f"  {r} running urgent coding: Fix the flaky test",
        "Queued (1)", f"  {q} queued normal summary: Summarize the Q3 notes",
        "Failed (1)", f"  {f} failed normal general: Scrape the jobs page",
    ]
    # Some states only, as `orch list --status`.
    assert chat.reply("list queued FAILED").splitlines() == [
        "Queued (1)", f"  {q} queued normal summary: Summarize the Q3 notes",
        "Failed (1)", f"  {f} failed normal general: Scrape the jobs page",
    ]
    assert chat.reply("list done") == "No Tasks in done."
    bad = chat.reply("list finished")
    assert bad.startswith("Unknown state 'finished'.") and "paused_usage" in bad


def test_list_shows_only_the_newest_finished_tasks_and_fits_one_message(chat, tg_cfg,  # noqa: F811
                                                                         tg_db):
    for i in range(12):
        tid = create_task(tg_cfg, tg_db, f"Cancelled {i}").spec.id
        tg_db.transition(tid, TaskState.CANCELLED, "t")  # in the done group
    lines = chat.reply("list").splitlines()
    assert lines[0] == "Done (12, newest 10 shown)" and len(lines) == 11
    long = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
    clipped = remote.clip(long)
    assert len(clipped) <= remote.MAX_MESSAGE and clipped.endswith("orch list has them all.")


# -- comedy and secrets ------------------------------------------------------------

def test_comedy_tasks_show_only_id_and_state_in_every_reply(chat, tg_cfg, tg_db,  # noqa: F811
                                                            monkeypatch):
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    run = create_task(tg_cfg, tg_db, "Tighten the dentist bit", task_type="comedy").spec.id
    _running(tg_db, run)
    q = create_task(tg_cfg, tg_db, "Punch up the landlord tag", task_type="comedy").spec.id
    replies = [chat.reply("status"), chat.reply("list")]
    assert f"Running: {run} running" in replies[0]
    assert f"  {run} running\n" in replies[1] and replies[1].endswith(f"  {q} queued")
    replies += chat.send(f"priority {q} urgent") + chat.send(f"pause {q}")
    replies += chat.send(f"resume {q}") + chat.send(f"cancel {q}") + chat.send(f"retry {q}")
    replies += chat.send(f"cancel {run}")
    for text in replies:
        for leaked in ("dentist", "landlord", "bit", "tag", "comedy"):
            assert leaked not in text, (leaked, text)


def test_secrets_are_redacted_from_every_reply(chat, tg_cfg, tg_db):  # noqa: F811
    tg_cfg.secrets.require("TELEGRAM_BOT_TOKEN")
    create_task(tg_cfg, tg_db, f"Rotate {TOKEN} now")
    text = chat.reply("list")
    assert TOKEN not in text and "[REDACTED]" in text


# -- controls, end to end ----------------------------------------------------------

def test_each_control_works_and_replies_with_the_outcome(chat, tg_cfg, tg_db):  # noqa: F811
    a = create_task(tg_cfg, tg_db, "Plan A", needs_approval=True).spec.id
    assert chat.reply(f"/approve {a}").startswith(f"Task {a} approved and queued.")
    assert tg_db.task_state(a) == TaskState.QUEUED
    assert tg_db.approvals(a)[-1]["via"] == "telegram"

    d = create_task(tg_cfg, tg_db, "Plan D", needs_approval=True).spec.id
    assert chat.reply(f"deny {d}") == f"Task {d} denied and cancelled."
    assert tg_db.task_state(d) == TaskState.CANCELLED

    p = create_task(tg_cfg, tg_db, "Pause me").spec.id
    assert chat.reply(f"priority {p} URGENT") == f"Task {p} priority normal -> urgent"
    assert tg_db.get_task(p)["priority"] == "urgent"
    paused = chat.reply(f"pause {p}")
    # Worded for Telegram: the control layer's `orch resume` is /resume here.
    assert paused == f"Task {p} paused (was queued). /resume {p} queues it again."
    assert tg_db.task_state(p) == TaskState.PAUSED_USAGE
    assert chat.reply(f"resume {p}").startswith(f"Task {p}: resumed by hand (telegram)")
    assert tg_db.task_state(p) == TaskState.QUEUED

    f = create_task(tg_cfg, tg_db, "Retry me").spec.id
    tg_db.transition(f, TaskState.FAILED, "boom")
    assert chat.reply(f"retry {f}").startswith(f"Task {f} queued: retried by hand from failed")
    assert tg_db.task_state(f) == TaskState.QUEUED

    assert chat.reply(f"cancel {f}") == f"Task {f} cancelled (was queued)."
    assert tg_db.task_state(f) == TaskState.CANCELLED

    # Every one of them counts as an intervention for the clean-week check.
    at = now(tg_cfg.timezone)
    week = cleanweek.check(tg_cfg, tg_db, at + timedelta(minutes=5))
    hands = [(i.command, i.task_id) for i in week.interventions]
    for expected in [("telegram", a), ("telegram", d), ("telegram", p), ("telegram", f)]:
        assert expected in hands, (expected, hands)
    assert hands.count(("telegram", p)) == 3  # priority, pause, resume
    assert hands.count(("telegram", f)) == 2  # retry, cancel


def test_bad_ids_wrong_states_and_malformed_commands_get_a_clear_reply(chat, tg_cfg,  # noqa: F811
                                                                       tg_db):
    q = create_task(tg_cfg, tg_db, "Queued").spec.id
    assert chat.reply("cancel nope") == "Not done: no Task 'nope'"
    assert chat.reply(f"approve {q}") == f"Not done: Task {q} is queued, not awaiting approval"
    assert chat.reply(f"retry {q}").startswith(f"Not done: Task {q} is queued; only failed")
    assert chat.reply(f"resume {q}") == f"Not done: Task {q} is queued, not paused"
    assert chat.reply("cancel") == "Usage: cancel <id>"
    assert chat.reply(f"pause {q} {q}") == "Usage: pause <id>"
    assert chat.reply(f"priority {q}") == "Usage: priority <id> <urgent|normal|background>"
    assert chat.reply(f"priority {q} asap") == (
        "Unknown priority 'asap'. Usage: priority <id> <urgent|normal|background>")
    tg_db.transition(q, TaskState.CANCELLED, "t")
    assert chat.reply(f"priority {q} urgent") == (
        f"Not done: Task {q} is cancelled; its priority is final")
    assert tg_db.task_state(q) == TaskState.CANCELLED
    assert tg_db.interventions() == []


def test_cancel_and_pause_of_a_running_task_reply_twice(chat, tg_cfg, tg_db,  # noqa: F811
                                                        monkeypatch):
    monkeypatch.setattr(daemon, "daemon_running", lambda cfg: True)
    tid = create_task(tg_cfg, tg_db, "Long one").spec.id
    _running(tg_db, tid)
    assert chat.send(f"pause {tid}") == [
        f"Task {tid} is running; asked the daemon to pause it after the current Step.",
        f"Pause requested for Task {tid}; the current Step hasn't finished yet.",
    ]
    assert tg_db.request_via(tid, "pause") == "telegram"
    assert chat.reply(f"resume {tid}").startswith(f"Task {tid}: ")
    assert tg_db.get_task(tid)["pause_requested_at"] is None
    assert chat.send(f"cancel {tid}") == [
        f"Task {tid} is running; asked the daemon to stop it.",
        f"Cancel requested for Task {tid}; the daemon hasn't stopped it yet.",
    ]
    assert tg_db.request_via(tid, "cancel") == "telegram"


# -- who may command, and replays ------------------------------------------------------

def test_other_users_get_no_reply_and_are_logged_by_sender_id_only(chat, notifier,  # noqa: F811
                                                                   tg_cfg, tg_db):
    tid = create_task(tg_cfg, tg_db, "Mine").spec.id
    assert chat.send(f"cancel {tid}", sender=999001) == []
    assert chat.send("status", sender=999001) == []
    assert tg_db.task_state(tid) == TaskState.QUEUED
    stranger = [m for m in notifier.logs if "999001" in m]
    assert len(stranger) == 2 and all("ignored" in m for m in stranger)
    assert not any(tid in m or "cancel" in m for m in stranger)


def test_command_text_is_never_logged(chat, notifier):  # noqa: F811
    chat.send("my password is hunter2")
    chat.send("status")
    assert not any("hunter2" in m for m in notifier.logs)
    assert "Telegram: unknown command from you" in notifier.logs
    assert "Telegram: status command from you" in notifier.logs


def test_the_update_offset_survives_a_restart(chat, api, tg_cfg, tg_db):  # noqa: F811
    tid = create_task(tg_cfg, tg_db, "Once").spec.id
    tg_db.transition(tid, TaskState.FAILED, "boom")
    chat.send(f"retry {tid}")
    chat.send(f"cancel {tid}")
    assert tg_db.task_state(tid) == TaskState.CANCELLED
    sent, moves = len(api.sent), len(tg_db.transitions(tid))
    # A restarted daemon starts from the stored offset: nothing is replayed.
    Notifier(tg_cfg, tg_db, api).poll_updates()
    assert api.offsets[-1] == chat.next_id
    assert len(api.sent) == sent and len(tg_db.transitions(tid)) == moves


def test_a_crash_mid_command_never_replays_it(api, tg_cfg, tg_db, monkeypatch):  # noqa: F811
    tid = create_task(tg_cfg, tg_db, "x").spec.id
    api.updates = [_msg(50, USER_ID, f"cancel {tid}")]
    calls = []

    def dies(*a, **kw):
        calls.append(a)
        raise KeyboardInterrupt  # the daemon is killed mid-command

    monkeypatch.setattr(remote, "run", dies)
    with pytest.raises(KeyboardInterrupt):
        Notifier(tg_cfg, tg_db, api).poll_updates()
    assert tg_db.get_meta(OFFSET_KEY) == "51"
    monkeypatch.undo()
    Notifier(tg_cfg, tg_db, api).poll_updates()
    assert len(calls) == 1 and api.sent == [] and api.offsets[-1] == 51


def test_a_failing_command_or_reply_never_stops_the_poll(chat, notifier, api, tg_cfg,  # noqa: F811
                                                         tg_db, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("bug")

    monkeypatch.setattr(remote, "status_text", broken)
    assert "hit an error" in chat.reply("status")
    assert any("RuntimeError: bug" in m for m in notifier.logs)
    monkeypatch.undo()
    # Telegram takes the command but not the reply: the change still happens.
    tid = create_task(tg_cfg, tg_db, "x").spec.id
    real_send = api.send_message

    def refuse(chat_id, text, buttons=None):
        from nightshift.telegram import TelegramError
        raise TelegramError("sendMessage: HTTP 502")

    api.send_message = refuse
    api.updates.append(_msg(900, USER_ID, f"cancel {tid}"))
    assert notifier.poll_updates() is True
    api.send_message = real_send
    assert tg_db.task_state(tid) == TaskState.CANCELLED
    assert any("reply not sent" in m for m in notifier.logs)


def test_non_message_updates_from_you_are_ignored(chat, notifier, api, tg_cfg, tg_db):  # noqa: F811
    tid = create_task(tg_cfg, tg_db, "x").spec.id
    api.updates.append({"update_id": 300, "edited_message": {
        "from": {"id": USER_ID}, "chat": {"id": USER_ID}, "text": f"cancel {tid}"}})
    assert notifier.poll_updates() is True
    assert api.sent == [] and tg_db.task_state(tid) == TaskState.QUEUED
    # A photo (no text) is a message, just not a command.
    api.updates.append({"update_id": 301, "message": {"from": {"id": USER_ID},
                                                       "chat": {"id": USER_ID}, "photo": []}})
    notifier.poll_updates()
    assert api.sent[-1][1] == "Unknown command. Send help for the list."


# -- add and inline buttons (#34) ----------------------------------------------------

class Phone(Chat):
    """You, typing commands and tapping inline buttons. A message's id is its
    index in ``api.sent``; an edit replaces the text you see."""

    def text_of(self, message_id: int) -> str:
        edits = [t for _, mid, t in self.api.edits if mid == message_id]
        return edits[-1] if edits else self.api.sent[message_id][1]

    def with_buttons(self) -> list[int]:
        return [i for i, b in enumerate(self.api.buttons) if b]

    def tap(self, message_id: int, label: str, *, sender: int = USER_ID,
            text: str | None = None) -> str | None:
        """Tap ``label`` on a message; return the toast (None: no answer)."""
        [data] = [d for row in self.api.buttons[message_id] for lab, d in row if lab == label]
        before = len(self.api.answers)
        self.api.updates.append({"update_id": self.next_id, "callback_query": {
            "id": f"q{self.next_id}", "from": {"id": sender, "is_bot": False}, "data": data,
            "chat_instance": "x",
            "message": {"message_id": message_id, "chat": {"id": USER_ID, "type": "private"},
                        "text": self.api.sent[message_id][1] if text is None else text}}})
        self.next_id += 1
        assert self.notifier.poll_updates() is True
        new = self.api.answers[before:]
        assert len(new) <= 1
        return new[0][1] if new else None


@pytest.fixture
def phone(notifier, api, planner):  # noqa: F811
    notifier.cancel_wait = notifier.pause_wait = 0
    notifier.adapter_provider = plan_provider
    return Phone(notifier, api)


def _tasks(db):
    return db.conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()


def _hands_on(cfg, db):
    week = cleanweek.check(cfg, db, now(cfg.timezone) + timedelta(minutes=5))
    return [(i.command, i.task_id) for i in week.interventions]


def test_free_form_add_confirms_the_estimate_with_buttons(phone, api, tg_cfg, tg_db,  # noqa: F811
                                                          planner):
    replies = phone.send("add Fix the typo in README.md --type coding --priority URGENT")
    assert replies[0] == "Planning: asking for an estimate and a Task type..."
    assert replies[1].splitlines() == [
        "Estimate:  10 minutes", "Task type: coding (from --type; proposed general)",
        "Reason:    a small job", "Create this Task?"]
    [msg] = phone.with_buttons()
    assert [lab for lab, _ in api.buttons[msg][0]] == ["Create", "Don't create"]
    assert _tasks(tg_db) == []  # nothing until you tap

    assert phone.tap(msg, "Create") == "Created."
    [row] = _tasks(tg_db)
    tid = row["id"]
    assert (row["state"], row["task_type"], row["priority"]) == ("queued", "coding", "urgent")
    assert row["prompt"] == "Fix the typo in README.md"
    assert tg_db.transitions(tid)[0]["reason"].startswith("created by telegram add (estimated")
    # Coding Tasks are long: the timing note says when it can start.
    assert phone.text_of(msg).endswith(
        "Create this Task?\n\nLong Task: starts only between 00:00 and 07:00 "
        f"(America/Los_Angeles) unless urgent\nTask {tid} queued.\n"
        + control.daemon_note(tg_cfg))
    assert str(row["folder"]) not in "".join(t for _, t in api.sent)
    # A second tap (or the same button on a stale copy) only reports the state.
    assert phone.tap(msg, "Create") == f"Already decided: Task {tid} is queued."
    assert phone.tap(msg, "Don't create") == f"Already decided: Task {tid} is queued."
    assert len(_tasks(tg_db)) == 1
    assert [c["kind"] for c in planner.calls()] == ["estimate"]
    assert tg_db.approvals(tid) == [] and _hands_on(tg_cfg, tg_db) == []


def test_dont_create_queues_nothing_and_a_later_tap_reports_it(phone, tg_db):
    phone.send("add Rewrite the docs")
    [msg] = phone.with_buttons()
    assert phone.tap(msg, "Don't create") == "Not queued."
    assert phone.text_of(msg).endswith("Create this Task?\n\nNot queued.")
    assert _tasks(tg_db) == []
    assert tg_db.conn.execute("SELECT COUNT(*) FROM planning_usage").fetchone()[0] == 1
    assert phone.tap(msg, "Create") == (
        "Already decided: not queued (you tapped Don't create).")
    assert _tasks(tg_db) == []


def test_a_long_task_gets_its_plan_and_one_tap_approval(phone, api, tg_cfg, tg_db,  # noqa: F811
                                                        planner):
    planner.setenv("FAKE_MINUTES", "45")
    replies = phone.send("add Write a long research report on vector DBs")
    assert "Over 30 minutes: a plan.md will be written for your approval." in replies[-1]
    [confirm] = phone.with_buttons()
    assert phone.tap(confirm, "Create") == "Created."
    [row] = _tasks(tg_db)
    tid = row["id"]
    assert row["state"] == "awaiting_approval"
    assert phone.text_of(confirm).endswith(
        f"Task {tid} is awaiting approval; writing its plan.")
    [_, plan_msg] = phone.with_buttons()
    assert api.sent[plan_msg][1] == (f"Plan for Task {tid} (estimated 45 min):\n\n"
                                     "# Plan\n\n## Goal\nDo it.\n\n## Steps\n1. Step one")
    assert [lab for lab, _ in api.buttons[plan_msg][0]] == ["Approve", "Deny"]

    assert phone.tap(plan_msg, "Approve") == "Approved."
    assert tg_db.task_state(tid) == TaskState.QUEUED
    [approval] = tg_db.approvals(tid)
    assert (approval["decision"], approval["via"]) == ("approved", "telegram add")
    assert phone.text_of(plan_msg).endswith(f"\n\nTask {tid} approved and queued.\n"
                                            + control.daemon_note(tg_cfg))
    # Double and stale taps change nothing and say what happened.
    assert phone.tap(plan_msg, "Approve") == (
        f"Already decided: Task {tid} is queued (plan approved via telegram add).")
    assert phone.tap(plan_msg, "Deny").startswith("Already decided:")
    assert tg_db.task_state(tid) == TaskState.QUEUED and len(tg_db.approvals(tid)) == 1
    # Approving the plan while adding the Task is asking for the work, as at
    # `orch add`: not an intervention.
    assert _hands_on(tg_cfg, tg_db) == []
    assert [c["kind"] for c in planner.calls()] == ["estimate", "plan"]


def test_deny_in_the_add_flow_cancels_the_task(phone, tg_cfg, tg_db, planner):  # noqa: F811
    planner.setenv("FAKE_MINUTES", "60")
    phone.send("add Big job")
    phone.tap(phone.with_buttons()[0], "Create")
    [tid] = [r["id"] for r in _tasks(tg_db)]
    assert phone.tap(phone.with_buttons()[1], "Deny") == "Denied."
    assert tg_db.task_state(tid) == TaskState.CANCELLED
    assert [(a["decision"], a["via"]) for a in tg_db.approvals(tid)] == [("denied",
                                                                          "telegram add")]
    assert _hands_on(tg_cfg, tg_db) == []


def test_a_failed_plan_call_still_offers_the_buttons(phone, api, tg_db, planner):
    planner.setenv("FAKE_MINUTES", "60")
    phone.send("add Big job")
    planner.setenv("FAKE_PLAN_FAIL", "plan")
    phone.tap(phone.with_buttons()[0], "Create")
    [tid] = [r["id"] for r in _tasks(tg_db)]
    [_, msg] = phone.with_buttons()
    assert api.sent[msg][1].startswith("Could not write a plan: plan call on claude failed")
    assert api.sent[msg][1].endswith(f"Approve Task {tid} without a plan, or deny it.")
    assert phone.tap(msg, "Approve") == "Approved."
    assert tg_db.task_state(tid) == TaskState.QUEUED


def test_a_draft_survives_a_daemon_restart(phone, api, tg_cfg, tg_db):  # noqa: F811
    phone.send("add Fix a typo")
    [msg] = phone.with_buttons()
    phone.notifier = Notifier(tg_cfg, tg_db, api)  # a restarted daemon
    assert phone.tap(msg, "Create") == "Created."
    assert len(_tasks(tg_db)) == 1


@pytest.fixture
def tpl_notifier(tmp_path, tg_cfg, api):  # noqa: F811
    from nightshift.config import load_config
    from nightshift.db import Database
    from nightshift.host import FakeHost
    import yaml

    tdir = tmp_path / "tpl"
    tdir.mkdir()
    base = {"task_type": "general", "agent": "claude", "max_runtime_minutes": 90,
            "long": False, "internet": "off", "prompt": "Do {{request}}"}
    (tdir / "quick-job.yaml").write_text(yaml.safe_dump({**base, "estimated_minutes": 5}))
    (tdir / "big-job.yaml").write_text(yaml.safe_dump({**base, "estimated_minutes": 60}))
    data = yaml.safe_load((tg_cfg.config_dir / "config.yaml").read_text())
    data["templates"] = {"dir": str(tdir)}
    (tg_cfg.config_dir / "config.yaml").write_text(yaml.safe_dump(data))
    cfg = load_config(tg_cfg.config_dir, host=FakeHost())
    db = Database(cfg.db_path, tz=cfg.timezone)
    n = Notifier(cfg, db, api)
    n.adapter_provider = plan_provider
    yield n
    db.close()


def test_template_adds_are_created_at_once(tpl_notifier, api, planner):
    phone = Phone(tpl_notifier, api)
    cfg, db = tpl_notifier.cfg, tpl_notifier.db
    proposal, created = phone.send("add quick-job the weekly cleanup --deadline 2027-01-15 17:00")
    [row] = _tasks(db)
    tid = row["id"]
    assert proposal == ("Template:  quick-job\nEstimate:  5 minutes (from the template)\n"
                        "Task type: general")
    assert row["state"] == "queued" and row["template"] == "quick-job"
    assert row["prompt"] == "Do the weekly cleanup" and row["deadline"].startswith("2027-01-15T17:00")
    assert created.startswith("Deadline:  2027-01-15T17:00") and f"Task {tid} queued." in created
    assert phone.with_buttons() == [] and planner.calls() == []
    assert db.transitions(tid)[0]["reason"].startswith(
        "created by telegram add, template quick-job")

    # --template works too; over 30 minutes the plan follows.
    replies = phone.send('add "the big migration" --template big-job --priority background')
    assert replies[-1].startswith("Plan for Task ")
    [msg] = phone.with_buttons()
    big = [r for r in _tasks(db) if r["template"] == "big-job"][0]
    assert big["priority"] == "background" and big["state"] == "awaiting_approval"
    assert big["title"] == "the big migration"
    assert phone.tap(msg, "Approve") == "Approved."
    assert db.approvals(big["id"])[-1]["via"] == "telegram add"
    assert phone.reply("add quick-job") == "Usage: add <template> <request>"


def test_alerts_for_awaiting_approval_tasks_carry_working_buttons(phone, notifier, api,  # noqa: F811
                                                                  tg_cfg, tg_db):
    tid = create_task(tg_cfg, tg_db, "Long research", estimated_minutes=45).spec.id
    _running(tg_db, tid)
    tg_db.transition(tid, TaskState.PAUSED_USAGE, "usage limit")
    tg_db.transition(tid, TaskState.AWAITING_APPROVAL, "paused too long; needs re-approval")
    notifier.tick()
    [msg] = phone.with_buttons()
    assert "needs re-approval" in api.sent[msg][1]
    assert api.buttons[msg] == [[("Approve", f"approve:{tid}"), ("Deny", f"deny:{tid}")]]
    assert phone.tap(msg, "Approve") == "Approved."
    assert tg_db.task_state(tid) == TaskState.QUEUED
    assert tg_db.approvals(tid)[-1]["via"] == "telegram"
    # Unlike the add flow, approving from an alert is an intervention.
    assert ("telegram", tid) in _hands_on(tg_cfg, tg_db)
    assert phone.tap(msg, "Deny") == (
        f"Already decided: Task {tid} is queued (plan approved via telegram).")
    assert tg_db.task_state(tid) == TaskState.QUEUED
    # A Task that is gone: the button says so.
    api.buttons[msg] = [[("Approve", "approve:nope")]]
    assert phone.tap(msg, "Approve") == "No such Task."


def test_presses_from_other_users_are_ignored_and_logged(phone, notifier, api, tg_cfg,  # noqa: F811
                                                         tg_db):
    tid = create_task(tg_cfg, tg_db, "Plan", needs_approval=True).spec.id
    api.sent.append((USER_ID, "x"))
    api.buttons.append(remote.approval_buttons(tid))
    assert phone.tap(0, "Approve", sender=999001) is None
    assert phone.tap(0, "Deny", sender=999001) is None
    assert tg_db.task_state(tid) == TaskState.AWAITING_APPROVAL and tg_db.approvals(tid) == []
    assert api.edits == [] and len(api.sent) == 1
    stranger = [m for m in notifier.logs if "999001" in m]
    assert len(stranger) == 2 and all("ignored" in m for m in stranger)
    # Unknown or malformed button data from you is answered, never acted on.
    api.buttons[0] = [[("Odd", "explode:all")], [("Bad", "create:abc")]]
    assert phone.tap(0, "Odd") == "Nightshift doesn't know this button."
    assert phone.tap(0, "Bad") == "Nightshift doesn't know this button."
    assert "Telegram: unknown button from you" in notifier.logs


def test_comedy_plans_and_ideas_never_go_to_telegram(phone, api, tg_cfg, tg_db,  # noqa: F811
                                                     planner):
    planner.setenv("FAKE_MINUTES", "45")
    planner.setenv("FAKE_TYPE", "comedy")
    replies = phone.send("add Punch up the dentist bit")
    assert replies[-1].splitlines() == [
        "Estimate:  45 minutes", "Task type: comedy (proposed)",
        "Over 30 minutes: a plan.md will be written for your approval.", "Create this Task?"]
    phone.tap(phone.with_buttons()[0], "Create")
    [row] = _tasks(tg_db)
    tid = row["id"]
    assert (row["state"], row["task_type"]) == ("awaiting_approval", "comedy")
    assert (Path(row["folder"]) / "plan.md").exists()
    [_, msg] = phone.with_buttons()
    assert api.sent[msg][1] == (
        f"Task {tid} has a plan.md (comedy: not sent to Telegram). Read it on the dashboard "
        f"or with `orch show {tid}`, then approve or deny.")
    assert phone.tap(msg, "Approve") == "Approved."
    everything = [t for _, t in api.sent] + [t for *_, t in api.edits] + [
        t for _, t in api.answers if t]
    for text in everything:
        for leaked in ("dentist", "bit", "# Plan", "Do it.", "Step one", "a small job"):
            assert leaked not in text, (leaked, text)


def test_bad_adds_get_a_clear_reply_and_no_planning_call(phone, tg_db, planner):
    assert phone.reply("add") == remote.ADD_USAGE
    assert phone.reply("add   --type coding") == remote.ADD_USAGE
    assert phone.reply("add x --priority asap").startswith("Unknown priority 'asap'.")
    assert phone.reply("add x --priority").startswith("--priority needs a value.")
    assert phone.reply("add x --internet maybe").startswith("--internet takes on or off.")
    assert phone.reply("add x --type poetry").startswith("Unknown Task type 'poetry'")
    assert phone.reply("add x --at 1 --at 2").startswith("--at is given twice.")
    assert phone.reply("add x --deadline someday").startswith("Not done: ")
    assert phone.reply("add x --template nope").startswith("Not done: no template 'nope'")
    assert planner.calls() == [] and _tasks(tg_db) == []
    assert phone.with_buttons() == []


def test_parse_add_reads_orch_add_options(tg_cfg):  # noqa: F811
    req, opts = remote.parse_add(
        tg_cfg, "Compare  vector DBs --deadline 2027-01-15 17:00 --at=23:00 "
                "--agent “codex” --internet ON --type research")
    assert req == "Compare vector DBs"
    assert opts == {"deadline": "2027-01-15 17:00", "at": "23:00", "agent": "codex",
                    "internet": "on", "type": "research"}
    req, opts = remote.parse_add(tg_cfg, "Don't break --deadline '2027-01-15 09:30'\nline two")
    assert req == "Don't break \nline two" and opts == {"deadline": "2027-01-15 09:30"}
    req, opts = remote.parse_add(tg_cfg, "resume-tailor https://example.com/job/1")
    assert (req, opts) == ("https://example.com/job/1", {"template": "resume-tailor"})


def test_long_plans_are_trimmed_to_one_message():
    plan = "\n".join(f"{i}. " + "step " * 30 for i in range(100))
    out = remote.trim_plan(plan)
    assert len(out) <= remote.PLAN_ROOM < remote.MAX_MESSAGE
    assert out.endswith("trimmed; the whole plan.md is in the Task folder.")
    assert remote.trim_plan("# Plan\n") == "# Plan"


def test_a_draft_round_trips_through_json(tg_cfg, planner):  # noqa: F811
    draft = control.propose(tg_cfg, "Fix it", at=now(tg_cfg.timezone), task_type="coding",
                            priority="urgent", adapter_provider=plan_provider).draft
    back = control.Draft.from_json(draft.to_json())
    assert back == draft and back.proposal.usage[0].output_tokens == 50
