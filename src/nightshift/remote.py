"""Telegram commands (§5 item 1, §2.12): Nightshift as a remote control.

The daemon's long poll (``alerts.Notifier.poll_updates``) hands every text
message from ``telegram.user_id`` to :func:`run`, which answers through
``reply``. Messages from anyone else never get here.

| Command                 | Does                                              |
|-------------------------|---------------------------------------------------|
| ``status``              | daemon up or down, running Task, queue, needs you |
| ``list [state ...]``    | Tasks grouped by status, optionally some states   |
| ``approve <id>``        | approve a Task awaiting approval                  |
| ``deny <id>``           | deny it (cancelled)                               |
| ``cancel <id>``         | cancel an unfinished Task                         |
| ``pause <id>``          | pause it until resumed                            |
| ``resume <id>``         | queue a paused Task now                           |
| ``retry <id>``          | retry a failed or partial Task                    |
| ``priority <id> <P>``   | set its Priority (urgent, normal, background)     |
| ``add <prompt>``        | a free-form Task (estimate, then Create button)   |
| ``add <template> <req>``| a Task from a template                            |
| ``help``                | these commands                                    |

A leading ``/`` (and a ``@botname`` suffix) is optional; case doesn't
matter. Every change goes through ``nightshift.control`` with ``via:
telegram``, so it is checked, recorded and counted for the clean-week check
exactly as the same ``orch`` command would be.

``add`` is the intake of ``orch add`` (§2.4) over inline buttons, with
``via: telegram add``: a free-form Task's estimate and Task type wait for
**Create** / **Don't create** (the draft is kept in ``telegram_drafts``);
a template Task is created at once. A Task over the plan threshold then
gets its ``plan.md`` (trimmed to one message) with **Approve** / **Deny**.
Like the y/n of ``orch add``, that decision is part of asking for the work,
not an intervention. Alerts for a Task awaiting approval carry Approve /
Deny with ``via: telegram``, as ``approve <id>`` does.

A button press (:func:`press`) is answered and its message edited to show
the decision. Every decision is made once: a second tap, or a tap on a
stale button, only reports the current state.

Comedy Tasks show their id and state only, never their title (§1.6), and
their plan is never sent: the message points to the dashboard or ``orch
show``. The control layer's messages are built from ids, states and fixed
reasons, so they carry no Task content either. The caller redacts secrets
from every reply.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

from . import alerts, control, daemon, planning, queries, templates, views
from . import priority as prio
from .config import Config
from .db import Database
from .states import TERMINAL, TaskState
from .telegram import Buttons
from .timeutil import iso

#: Telegram refuses messages longer than this.
MAX_MESSAGE = 4096

#: How many of the newest finished Tasks a ``list`` group shows.
FINISHED_LIMIT = 10

#: Longest title a ``list`` or ``status`` line shows.
TITLE_CHARS = 50

HELP = """Nightshift commands:
status - daemon, running Task, queue, needs you
list [state ...] - Tasks by status (e.g. list queued failed)
approve <id> - approve a Task awaiting approval
deny <id> - deny it (it is cancelled)
cancel <id> - cancel an unfinished Task
pause <id> - pause a Task until you resume it
resume <id> - queue a paused Task now
retry <id> - retry a failed or partial Task
priority <id> <urgent|normal|background> - set its Priority
add <prompt> - a new Task: estimate and Task type, then tap Create
add <template> <request> - a new Task from a template
help - this list
add takes orch add's options: --type T --agent A --priority P --deadline D --at T --internet on|off
A leading / is optional."""

#: Commands that take a Task id, and what else they take.
USAGE = {
    "approve": "approve <id>",
    "deny": "deny <id>",
    "cancel": "cancel <id>",
    "pause": "pause <id>",
    "resume": "resume <id>",
    "retry": "retry <id>",
    "priority": "priority <id> <urgent|normal|background>",
}

ADD_USAGE = ("Usage: add <prompt>, or add <template> <request>; options --type T, --agent A, "
             "--priority urgent|normal|background, --deadline D, --at T, --internet on|off")

#: Reply(text, buttons=None): one message to you, optionally with inline buttons.
Reply = Callable[..., None]


def parse(text: str) -> tuple[str, list[str]]:
    """The command word (lower case, no ``/`` or ``@botname``) and its arguments."""
    words = text.split()
    if not words:
        return "", []
    verb = words[0].lower().removeprefix("/").split("@", 1)[0]
    return verb, words[1:]


def run(cfg: Config, db: Database, text: str, *, at: datetime, reply: Reply,
        cancel_wait: float | None = None, pause_wait: float | None = None,
        adapter_provider: planning.AdapterProvider | None = None) -> str:
    """Carry out one command and answer it through ``reply`` (once, or twice
    for a cancel or pause the daemon carries out, or an ``add`` with a
    planning call). Returns the command word for the log, or ``unknown``;
    never the message itself.

    ``cancel_wait`` and ``pause_wait`` bound the wait for the daemon (default
    ``control``'s own bounds); ``adapter_provider`` makes ``add``'s planning
    calls (default the configured agent)."""
    verb, args = parse(text)
    if verb == "add":
        rest = text.strip().split(None, 1)
        add(cfg, db, rest[1] if len(rest) > 1 else "", at=at, reply=reply,
            adapter_provider=adapter_provider)
    elif verb in ("help", "start"):
        reply(HELP)
    elif verb == "status":
        reply(status_text(cfg, db))
    elif verb == "list":
        reply(list_text(cfg, db, at, args))
    elif verb in USAGE:
        _control(cfg, db, verb, args, at=at, reply=reply, cancel_wait=cancel_wait,
                 pause_wait=pause_wait)
    else:
        reply("Unknown command. Send help for the list.")
        return "unknown"
    return verb


def _control(cfg: Config, db: Database, verb: str, args: list[str], *, at: datetime,
             reply: Reply, cancel_wait: float | None, pause_wait: float | None) -> None:
    want = 2 if verb == "priority" else 1
    if len(args) != want:
        reply(f"Usage: {USAGE[verb]}")
        return
    tid = args[0]
    via = control.VIA_TELEGRAM
    if verb == "approve":
        res = control.approve(cfg, db, tid, via=via)
    elif verb == "deny":
        res = control.deny(cfg, db, tid, via=via)
    elif verb == "retry":
        res = control.retry(cfg, db, tid, via=via, at=at)
    elif verb == "resume":
        res = control.resume(cfg, db, tid, via=via, at=at)
    elif verb == "priority":
        level = args[1].lower()
        if level not in prio.PRIORITIES:
            reply(f"Unknown priority {args[1]!r}. Usage: {USAGE[verb]}")
            return
        res = control.set_priority(cfg, db, tid, level, via=via, at=at)
    elif verb == "cancel":
        res = control.cancel(cfg, db, tid, via=via, at=at)
        reply(result_text(res))
        if res.action == control.CANCEL_REQUESTED:
            reply(result_text(control.wait_for_cancel(cfg, db, tid, timeout=cancel_wait)))
        return
    else:  # pause
        res = control.pause(cfg, db, tid, via=via, at=at)
        reply(result_text(res))
        if res.action == control.PAUSE_REQUESTED:
            reply(result_text(control.wait_for_pause(cfg, db, tid, timeout=pause_wait)))
        return
    reply(result_text(res))


# `orch resume k3f9` in a control message is `/resume k3f9` here.
_ORCH_COMMAND = re.compile(r"`orch ((?:%s)\b[^`]*)`" % "|".join(USAGE))


def result_text(res: control.Result) -> str:
    """A control's outcome, or its refusal, worded for Telegram."""
    text = res.message if res.ok else f"Not done: {res.message}"
    if res.note:
        text += f"\n{res.note}"
    return _ORCH_COMMAND.sub(r"/\1", text)


def _status_only(task_type: str) -> bool:
    return task_type in alerts.STATUS_ONLY_TYPES


def _task_line(t: queries.TaskSummary) -> str:
    if _status_only(t.task_type):
        return f"{t.id} {t.state}"
    title = t.title if len(t.title) <= TITLE_CHARS else t.title[: TITLE_CHARS - 1] + "…"
    pri = views._priority_cell(t.priority, t.effective_priority)
    return f"{t.id} {t.state} {pri} {t.task_type}: {title}"


def status_text(cfg: Config, db: Database) -> str:
    up = daemon.daemon_running(cfg)
    lines = [f"Nightshift: daemon {'up' if up else 'down'}"]
    running = db.tasks_in_state(TaskState.RUNNING)
    if not running:
        lines.append("Running: nothing")
    for r in running:
        if _status_only(r["task_type"]):
            lines.append(f"Running: {r['id']} {r['state']}")
        else:
            lines.append(f"Running: {r['id']} {r['task_type']}: {r['title'][:TITLE_CHARS]}")
    counts, groups = views.queue_summary(db), dict(queries.STATUS_GROUPS)
    waiting = sum(counts.get(str(s), 0) for s in groups["queued"])
    paused = sum(counts.get(str(s), 0) for s in groups["paused"])
    lines.append(f"Queue: {waiting} waiting to run ({counts.get('queued', 0)} queued, "
                 f"{counts.get('retry_wait', 0)} waiting to retry, "
                 f"{counts.get('scheduled', 0)} scheduled), {paused} paused")
    lines.append(f"Needs you: {len(queries.needs_you_inbox(cfg, db))} open")
    return "\n".join(lines)


def list_text(cfg: Config, db: Database, at: datetime, args: list[str]) -> str:
    """Tasks in the dashboard's status groups, newest first; ``args`` are
    states to show only (as ``orch list --status``)."""
    names = {str(s) for s in TaskState}
    bad = [a for a in args if a.lower() not in names]
    if bad:
        return (f"Unknown state {bad[0]!r}. Usage: list [state ...], states: "
                + ", ".join(str(s) for s in TaskState))
    wanted = {TaskState(a.lower()) for a in args}
    tasks = queries.task_list(cfg, db, at, sorted(wanted) or None)
    lines: list[str] = []
    for name, states in queries.STATUS_GROUPS:
        members = [t for t in tasks if t.state in states]
        if not members:
            continue
        shown = members[:FINISHED_LIMIT] if states <= TERMINAL else members
        more = f", newest {len(shown)} shown" if len(shown) < len(members) else ""
        lines.append(f"{name.capitalize()} ({len(members)}{more})")
        lines += [f"  {_task_line(t)}" for t in shown]
    if not lines:
        return "No Tasks." if not wanted else "No Tasks in " + ", ".join(sorted(wanted)) + "."
    return clip("\n".join(lines))


def clip(text: str) -> str:
    """``text`` cut at a line to fit one Telegram message."""
    if len(text) <= MAX_MESSAGE:
        return text
    tail = "\n… more not shown; orch list has them all."
    cut = text[: MAX_MESSAGE - len(tail)].rsplit("\n", 1)[0]
    return cut + tail


# --------------------------------------------------------------------------
# add (§5 item 1, §2.4): intake over inline buttons
# --------------------------------------------------------------------------

#: ``add``'s options, as ``orch add``'s. A value may be quoted, and a
#: deadline or start time may be ``YYYY-MM-DD HH:MM`` unquoted.
_ADD_OPTIONS = ("template", "type", "agent", "priority", "deadline", "at", "internet")
_OPTION = re.compile(
    r"(?<!\S)--(%s)(?:=|\s+)(\"[^\"]*\"|'[^']*'|\u201c[^\u201d]*\u201d|"
    r"\d{4}-\d{2}-\d{2}[ T]\d{1,2}:\d{2}(?![^\s])|\S+)" % "|".join(_ADD_OPTIONS))
_QUOTES = {('"', '"'), ("'", "'"), ("\u201c", "\u201d")}
_BARE_OPTION = re.compile(r"(?<!\S)--(%s)(?=\s|=|$)" % "|".join(_ADD_OPTIONS))

#: Callback data of the buttons (``<action>:<key>``; at most 64 bytes).
CREATE, DECLINE = "create", "decline"  # key: the telegram_drafts id
PLAN_APPROVE, PLAN_DENY = "plan-approve", "plan-deny"  # key: Task id; the add flow
APPROVE, DENY = "approve", "deny"  # key: Task id; an alert

#: Room a plan leaves for the lines around it in one message.
PLAN_ROOM = MAX_MESSAGE - 400


class AddError(ValueError):
    """An ``add`` that can't be read: its message is the reply."""


def parse_add(cfg: Config, text: str) -> tuple[str, dict[str, str]]:
    """The request and options of ``add <text>``. ``template`` is set from
    ``--template`` or a first word naming a template."""
    opts: dict[str, str] = {}

    def take(m: re.Match) -> str:
        name, value = m.group(1), m.group(2)
        if name in opts:
            raise AddError(f"--{name} is given twice. {ADD_USAGE}")
        if value[:1] in "\"'\u201c" and len(value) >= 2:
            value = value[1:-1]
        opts[name] = value.strip()
        return ""

    rest = _OPTION.sub(take, text)
    if (bare := _BARE_OPTION.search(rest)) is not None:
        raise AddError(f"--{bare.group(1)} needs a value. {ADD_USAGE}")
    request = re.sub(r"[ \t]{2,}", " ", rest).strip()
    if len(request) >= 2 and (request[0], request[-1]) in _QUOTES:
        request = request[1:-1].strip()  # add "the whole request" --type T
    if "template" not in opts:
        first, _, after = request.partition(" ")
        if first in templates.available(cfg):
            opts["template"], request = first, after.strip()
    if not request:
        raise AddError("Usage: add <template> <request>" if "template" in opts else ADD_USAGE)
    if "priority" in opts:
        opts["priority"] = opts["priority"].lower()
        if opts["priority"] not in prio.PRIORITIES:
            raise AddError(f"Unknown priority {opts['priority']!r}. {ADD_USAGE}")
    if "internet" in opts:
        opts["internet"] = opts["internet"].lower()
        if opts["internet"] not in ("on", "off"):
            raise AddError(f"--internet takes on or off. {ADD_USAGE}")
    if "type" in opts and cfg.task_types and opts["type"] not in cfg.task_types:
        raise AddError(f"Unknown Task type {opts['type']!r}; Task types: "
                       + ", ".join(cfg.task_types))
    return request, opts


def add(cfg: Config, db: Database, text: str, *, at: datetime, reply: Reply,
        adapter_provider: planning.AdapterProvider | None = None) -> None:
    """``add``: propose the Task through ``control``. A free-form one asks
    Create / Don't create; a template one is created at once (as ``orch
    add --template`` asks nothing)."""
    try:
        request, opts = parse_add(cfg, text)
    except AddError as exc:
        reply(str(exc))
        return
    res = control.propose(
        cfg, request, at=at, template=opts.get("template"), task_type=opts.get("type"),
        agent=opts.get("agent"), priority=opts.get("priority"), deadline=opts.get("deadline"),
        run_at=opts.get("at"), internet=opts.get("internet"), adapter_provider=adapter_provider,
        progress=reply)
    if not res.ok:
        reply(result_text(res))
        return
    draft = res.draft
    lines = draft_lines(cfg, draft)
    if draft.template is not None:
        reply("\n".join(lines))
        _created(cfg, db, control.create(cfg, db, draft, via=control.VIA_TELEGRAM_ADD),
                 reply=reply, adapter_provider=adapter_provider)
        return
    key = db.insert_telegram_draft(draft.to_json(), at=iso(at))
    reply("\n".join([*lines, "Create this Task?"]),
          buttons=[[("Create", f"{CREATE}:{key}"), ("Don't create", f"{DECLINE}:{key}")]])


def draft_lines(cfg: Config, draft: control.Draft) -> list[str]:
    """``control.describe``, less what may carry a comedy Task's content
    (the planner's reason, the template's inputs)."""
    lines = control.describe(cfg, draft)
    if _status_only(draft.task_type):
        lines = [ln for ln in lines if not ln.startswith(("Reason:", "Input:"))]
    return lines


def approval_buttons(task_id: str, *, via_add: bool = False) -> Buttons:
    yes, no = (PLAN_APPROVE, PLAN_DENY) if via_add else (APPROVE, DENY)
    return [[("Approve", f"{yes}:{task_id}"), ("Deny", f"{no}:{task_id}")]]


def _created(cfg: Config, db: Database, res: control.Result, *, reply: Reply,
             adapter_provider: planning.AdapterProvider | None) -> None:
    """After ``control.create``: say so and, over the plan threshold, send
    the plan with Approve / Deny."""
    reply(created_text(res))
    if res.ok and res.to_state == TaskState.AWAITING_APPROVAL:
        send_plan(cfg, db, res.task_id, reply=reply, adapter_provider=adapter_provider)


def created_text(res: control.Result) -> str:
    """A new Task, by id and state only (its folder name carries its title)."""
    if not res.ok:
        return result_text(res)
    head = (f"Task {res.task_id} is awaiting approval; writing its plan."
            if res.to_state == TaskState.AWAITING_APPROVAL else f"Task {res.task_id} queued.")
    return result_text(control.Result(res.action, "\n".join([*res.details, head]),
                                      note=res.note))


def send_plan(cfg: Config, db: Database, task_id: str, *, reply: Reply,
              adapter_provider: planning.AdapterProvider | None = None) -> None:
    """Write the Task's plan.md and send it with Approve / Deny (a comedy
    Task's plan stays on the machine)."""
    planned = control.plan(cfg, db, task_id, adapter_provider=adapter_provider)
    buttons = approval_buttons(task_id, via_add=True)
    if not planned.ok and planned.refusal != control.NO_PLAN:
        reply(result_text(planned))
        return
    if not planned.ok:
        reply(f"{planned.message}\nApprove Task {task_id} without a plan, or deny it.",
              buttons=buttons)
        return
    row = db.get_task(task_id)
    if _status_only(row["task_type"]):
        reply(f"Task {task_id} has a plan.md ({row['task_type']}: not sent to Telegram). "
              f"Read it on the dashboard or with `orch show {task_id}`, then approve or deny.",
              buttons=buttons)
        return
    reply(f"Plan for Task {task_id} (estimated {row['estimated_minutes']} min):\n\n"
          f"{trim_plan(planned.plan or '')}", buttons=buttons)


def trim_plan(text: str, room: int = PLAN_ROOM) -> str:
    """``text`` cut at a line to fit ``room`` characters."""
    text = text.strip()
    if len(text) <= room:
        return text
    tail = "\n… trimmed; the whole plan.md is in the Task folder."
    return text[: room - len(tail)].rsplit("\n", 1)[0] + tail


# --------------------------------------------------------------------------
# Button presses
# --------------------------------------------------------------------------


def press(cfg: Config, db: Database, data: str, *, original: str, at: datetime,
          answer: Callable[[str], None], edit: Callable[[str], None], reply: Reply,
          adapter_provider: planning.AdapterProvider | None = None) -> str:
    """Act on one button press (its callback ``data``): ``answer`` the press
    with a short toast, ``edit`` the pressed message (``original`` its text)
    to show the decision, ``reply`` with anything that follows (a new
    Task's plan). Returns the action for the log, or ``unknown``."""
    action, _, key = data.partition(":")

    def decided(toast: str, line: str) -> None:
        answer(toast)
        edit(f"{original}\n\n{line}" if original else line)

    if action in (CREATE, DECLINE) and key.isdigit():
        _draft_press(cfg, db, int(key), action == CREATE, at=at, decided=decided, reply=reply,
                     adapter_provider=adapter_provider)
    elif action in (PLAN_APPROVE, PLAN_DENY, APPROVE, DENY) and key:
        via = control.VIA_TELEGRAM_ADD if action in (PLAN_APPROVE, PLAN_DENY) else (
            control.VIA_TELEGRAM)
        _decision_press(cfg, db, key, action in (PLAN_APPROVE, APPROVE), via=via,
                        decided=decided)
    else:
        answer("Nightshift doesn't know this button.")
        return "unknown"
    return action


def _draft_press(cfg: Config, db: Database, key: int, create_it: bool, *, at: datetime,
                 decided: Callable[[str, str], None], reply: Reply,
                 adapter_provider: planning.AdapterProvider | None) -> None:
    row = db.telegram_draft(key)
    if row is None:
        decided("This proposal is gone.", "This proposal is gone; send add again.")
        return
    if not db.decide_telegram_draft(key, "created" if create_it else "declined", at=iso(at)):
        state = draft_state(db, db.telegram_draft(key))
        decided(f"Already decided: {state}", f"Already decided: {state}")
        return
    draft = control.Draft.from_json(row["draft"])
    if not create_it:
        decided("Not queued.", control.decline(db, draft).message)
        return
    res = control.create(cfg, db, draft, via=control.VIA_TELEGRAM_ADD)
    db.finish_telegram_draft(key, decision="created" if res.ok else "failed",
                             task_id=res.task_id if res.ok else None)
    if not res.ok:
        decided("Not created.", result_text(res))
        return
    decided("Created.", created_text(res))
    if res.to_state == TaskState.AWAITING_APPROVAL:
        send_plan(cfg, db, res.task_id, reply=reply, adapter_provider=adapter_provider)


def draft_state(db: Database, row) -> str:
    """What a decided draft came to, by Task id and state."""
    if row["decision"] == "declined":
        return "not queued (you tapped Don't create)."
    if row["decision"] == "failed":
        return "creating the Task failed; send add again."
    if row["task_id"] is None:
        return "created."
    return f"Task {row['task_id']} is {db.task_state(row['task_id'])}."


def _decision_press(cfg: Config, db: Database, task_id: str, approve_it: bool, *, via: str,
                    decided: Callable[[str, str], None]) -> None:
    res = (control.approve if approve_it else control.deny)(cfg, db, task_id, via=via)
    if res.ok:
        decided("Approved." if approve_it else "Denied.", result_text(res))
    elif res.refusal == control.UNKNOWN_TASK:
        decided("No such Task.", result_text(res))
    else:  # decided already (another tap, message or surface), or moved on
        state = decision_state(db, task_id)
        decided(f"Already decided: {state}", f"Already decided: {state}")


def decision_state(db: Database, task_id: str) -> str:
    """A Task's state now and its last plan decision."""
    text = f"Task {task_id} is {db.task_state(task_id)}"
    last = db.approvals(task_id)
    if last:
        text += f" (plan {last[-1]['decision']} via {last[-1]['via']})"
    return text + "."
