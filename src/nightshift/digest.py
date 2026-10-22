"""The 8am Telegram digest (§2.12): one message with everything overnight.

Like alerts, the digest is made from the ``transitions`` table, so every
outcome is in it, including the usage pauses that never alert. It covers the
transitions after the previous digest up to the moment it is made.

One digest belongs to each local date's ``telegram.digest_time`` (8am Los
Angeles time). It is made by the first daemon tick at or after that time:
at 8am when the laptop is awake, else on wake. The ``digests`` table keeps
one row per date (unique), so it is never made twice, also across restarts;
a laptop asleep through several 8ams makes one digest on wake, for the
latest. Made, then sent from the same outbox loop as alerts.

Comedy Tasks get status lines only (§1.6), as in alerts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import alerts
from .config import Config
from .db import Database
from .states import TaskState
from .timeutil import iso, parse_iso

S = TaskState

#: Where the digest starts when there is no earlier digest: the newest
#: transition and the due date when the digest first ran (no replayed history).
SINCE_KEY = "digest.since"

#: Telegram's message limit is 4096 characters; leave room for the tail line.
MAX_TEXT = 3900

#: Digest sections, in order: title and the transitions that belong in it.
SECTIONS: list[tuple[str, Callable[[str | None, str], bool]]] = [
    ("Needs you", lambda f, t: t == S.NEEDS_YOU
     or (t == S.AWAITING_APPROVAL and f == S.PAUSED_USAGE)),
    ("Failed", lambda f, t: t == S.FAILED),
    ("Finished", lambda f, t: t in (S.DONE, S.PARTIAL)),
    ("Paused for usage", lambda f, t: t == S.PAUSED_USAGE),
    ("Paused for spend", lambda f, t: t == S.PAUSED_SPEND),
    ("Retrying", lambda f, t: t == S.RETRY_WAIT),
    ("Cancelled", lambda f, t: t == S.CANCELLED),
]

#: States counted in the digest's closing "Now:" line.
NOW_STATES = [(S.RUNNING, "running"), (S.QUEUED, "queued"), (S.RETRY_WAIT, "retry wait"),
              (S.PAUSED_USAGE, "paused for usage"), (S.PAUSED_SPEND, "paused for spend"),
              (S.NEEDS_YOU, "needs you"), (S.AWAITING_APPROVAL, "awaiting approval")]


def due_date(at: datetime, at_time: time) -> date:
    """The local date of the latest digest time at or before ``at``."""
    return at.date() if at.time() >= at_time else at.date() - timedelta(days=1)


def _clock(dt: datetime) -> str:
    return f"{dt.hour % 12 or 12}:{dt.minute:02d}{'am' if dt.hour < 12 else 'pm'}"


def _when(dt: datetime, ref: datetime) -> str:
    return _clock(dt) if dt.date() == ref.date() else f"{dt:%a} {_clock(dt)}"


def event_line(task: Any, from_state: str | None, to_state: str, reason: str,
               at: datetime, ref: datetime) -> str:
    """One digest line for one transition. ``task`` is the Task's DB row."""
    tid, when = task["id"], _when(at, ref)
    state = to_state.replace("_", " ")
    if task["task_type"] in alerts.STATUS_ONLY_TYPES:
        return f"- {when} {task['task_type']} Task {tid} {state}"
    folder = Path(task["folder"])
    if to_state in (S.DONE, S.PARTIAL):
        detail = alerts.result_line(folder) or to_state
        if to_state == S.PARTIAL:
            detail = f"partial: {detail}"
    elif to_state == S.NEEDS_YOU:
        detail = alerts.needs_you_line(folder) or alerts.one_line(reason)
    elif to_state == S.AWAITING_APPROVAL:
        detail = f"paused too long; approve it again with orch approve {tid}"
    else:
        detail = alerts.one_line(reason)
    return f"- {when} {tid} {alerts.one_line(task['title'])}: {detail}"


def digest_text(db: Database, transitions: list[Any], *, since: datetime,
                at: datetime) -> str:
    """The digest message for ``transitions`` (oldest first), made at ``at``."""
    tz = at.tzinfo
    lines = [f"Nightshift digest, {at:%a %b} {at.day}",
             f"Since {_when(since.astimezone(tz), at)}"]
    tasks: dict[str, Any] = {}
    sections: dict[str, list[str]] = {title: [] for title, _ in SECTIONS}
    for t in transitions:
        title = next((name for name, match in SECTIONS
                      if match(t["from_state"], t["to_state"])), None)
        if title is None:
            continue
        task = tasks.get(t["task_id"]) or tasks.setdefault(t["task_id"],
                                                           db.get_task(t["task_id"]))
        sections[title].append(event_line(task, t["from_state"], t["to_state"], t["reason"],
                                          parse_iso(t["at"]).astimezone(tz), at))
    body = [f"\n{title} ({len(items)})\n" + "\n".join(items)
            for title, items in sections.items() if items]
    lines.extend(body or ["\nNothing happened overnight."])
    counts = [f"{n} {label}" for state, label in NOW_STATES
              if (n := len(db.tasks_in_state(state)))]
    lines.append("\nNow: " + (", ".join(counts) if counts else "nothing waiting"))
    lines.append("Details: orch list")
    return _fit("\n".join(lines))


def _fit(text: str) -> str:
    """Cut a digest to one Telegram message, at a line boundary."""
    if len(text) <= MAX_TEXT:
        return text
    kept: list[str] = []
    size = 0
    lines = text.split("\n")
    for line in lines:
        if size + len(line) + 1 > MAX_TEXT:
            break
        kept.append(line)
        size += len(line) + 1
    return "\n".join(kept) + f"\n… {len(lines) - len(kept)} more lines: orch list"


def start(db: Database, at: datetime, at_time: time) -> None:
    """On the first start, begin after the newest transition and skip the
    digest already due, so enabling Telegram doesn't send history."""
    if db.get_meta(SINCE_KEY) is None and db.last_digest() is None:
        db.set_meta(SINCE_KEY, json.dumps({
            "transition_id": db.last_transition_id(),
            "date": due_date(at, at_time).isoformat(), "at": iso(at)}))


def make_due(cfg: Config, db: Database, at: datetime) -> bool:
    """Make the digest if one is due at ``at`` and not made yet. True if made."""
    at = at.astimezone(ZoneInfo(cfg.timezone))
    at_time = cfg.telegram_digest_time
    start(db, at, at_time)
    due = due_date(at, at_time)
    last = db.last_digest()
    if last is not None:
        last_date, cursor = last["digest_date"], int(last["through_transition_id"])
        since = parse_iso(last["created_at"])
    else:
        first = json.loads(db.get_meta(SINCE_KEY) or "{}")
        last_date, cursor, since = first["date"], int(first["transition_id"]), parse_iso(first["at"])
    if due.isoformat() <= last_date:
        return False
    transitions = db.transitions_after(cursor)
    through = transitions[-1]["id"] if transitions else cursor
    text = cfg.secrets.redact(digest_text(db, transitions, since=since, at=at))
    return db.insert_digest(digest_date=due.isoformat(), since=iso(since),
                            through_transition_id=through, text=text, at=iso(at))
