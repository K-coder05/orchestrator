"""Telegram commands (§5 item 1), against a fake Bot API and a real DB."""

from __future__ import annotations

from datetime import timedelta

import pytest

from nightshift import cleanweek, daemon, remote
from nightshift.alerts import OFFSET_KEY, Notifier
from nightshift.runner import create_task
from nightshift.states import TaskState
from nightshift.timeutil import now

from test_alerts import TOKEN, USER_ID, _msg, api, notifier, tg_cfg, tg_db  # noqa: F401


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
                "priority", "help"):
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

    def refuse(chat_id, text):
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
