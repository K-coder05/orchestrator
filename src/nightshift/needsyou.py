"""Resolving an agent's "needs you" request (§1.6, §2.10).

An agent that wants something on the never-list, or a change of scope,
writes ``needs_you.json`` (``{kind, summary, details, proposed_action}``) and
stops; its Task moves to ``needs_you``. ``orch approve|deny <id>``, and the
same control from Telegram (``nightshift.control``), resolve that request:

- Approved or denied, the decision is a row in ``approvals`` (subject
  ``request``, with the request's kind and the SHA-256 of ``needs_you.json``),
  the request file moves to the Task's logs
  (``logs/attempt-<n>.needs_you.json``, n = the Attempt that raised it), and
  the Task is **requeued**. A denial is the user's decision, not a cancel:
  the agent carries on without that action or raises a different request.
- The next Attempt is told the decision and the original request. It
  resumes the same agent session when §2.6 allows
  (``pauses.session_to_resume``), else starts fresh from ``plan.md`` +
  ``progress.md``.
- The report's Needs you section lists the decision. With the file moved
  away, a ``needs_you.json`` a later Attempt writes is a fresh request.

A Task in ``needs_you`` for another reason (the per-Task spend cap) has no
request to decide: approve and deny refuse it, and it keeps its own handling.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import failures, report
from .config import Config
from .db import Database
from .states import TaskState
from .timeutil import iso

#: The Attempt outcome of an agent that wrote ``needs_you.json`` and stopped.
OUTCOME = "needs_you"
#: ``approvals.subject`` of a decision on a request.
SUBJECT = "request"


class NotARequest(Exception):
    """The Task is not waiting on an agent's request."""


@dataclass(frozen=True)
class Request:
    """The open request a Task in ``needs_you`` waits on."""

    run_id: int  # the Attempt that raised it
    attempt_no: int
    path: Path
    raw: bytes

    @property
    def data(self) -> dict[str, Any]:
        return _parse(self.raw)

    @property
    def kind(self) -> str:
        return _field(self.data, "kind") or "request"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()


def _parse(raw: bytes) -> dict[str, Any]:
    try:
        d = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return d if isinstance(d, dict) else {}


def _field(d: dict[str, Any], key: str) -> str | None:
    v = d.get(key)
    if v is None or v == "":
        return None
    return " ".join((v if isinstance(v, str) else json.dumps(v)).split())


def archive_path(folder: Path, attempt_no: int) -> Path:
    """Where a resolved request is kept (``report.RESOLVED_GLOB``)."""
    return folder / "logs" / f"attempt-{attempt_no}.needs_you.json"


def open_request(db: Database, row: Any) -> Request | None:
    """The request a Task waits on: it is in ``needs_you`` because its last
    Attempt wrote ``needs_you.json`` and stopped. None for any other reason."""
    if row["state"] != TaskState.NEEDS_YOU:
        return None
    attempts = db.attempts(row["id"])
    if not attempts or attempts[-1]["outcome"] != OUTCOME:
        return None
    path = Path(row["folder"]) / report.REQUEST_FILE
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    last = attempts[-1]
    return Request(run_id=last["id"], attempt_no=last["attempt_no"], path=path, raw=raw)


def resolve(cfg: Config, db: Database, task_id: str, *, approved: bool, via: str,
            at: datetime) -> str:
    """Approve or deny the Task's open request and requeue the Task. Returns
    the transition reason. Raises :class:`NotARequest` when it waits on
    none, :class:`StateChanged` when it was decided or moved meanwhile."""
    row = db.get_task(task_id)
    if row is None:
        raise KeyError(task_id)
    req = open_request(db, row)
    if req is None:
        raise NotARequest(f"Task {task_id} is {row['state']}, not waiting on a request")
    folder = Path(row["folder"])
    kept = archive_path(folder, req.attempt_no)
    kept.parent.mkdir(parents=True, exist_ok=True)
    kept.write_bytes(req.raw)  # first, so the request is never lost
    word = "approved" if approved else "denied"
    # The kind is the agent's own text: it stays out of the reason (alerts,
    # the clean-week check) and goes to the approvals row.
    reason = f"needs-you request {word} ({via}); queued, the next Attempt is told"
    db.decide_request(task_id, approved=approved, via=via, reason=reason, run_id=req.run_id,
                      kind=req.kind, request_sha256=req.sha256,
                      request_file=str(kept.relative_to(folder)))
    req.path.unlink(missing_ok=True)
    redact = cfg.secrets.redact
    failures.log_line(folder, f"{iso(at)} {reason}; request kept in {kept.relative_to(folder)}",
                      redact)
    report.refresh_needs_you(folder, needs_you=failures.needs_you_extra(cfg, db, task_id),
                             resolved=report_lines(db, task_id, folder), redact=redact)
    return reason


def _decisions(db: Database, task_id: str) -> list[Any]:
    return [a for a in db.approvals(task_id) if a["subject"] == SUBJECT]


def _kept(folder: Path, decision: Any) -> dict[str, Any]:
    try:
        return _parse((folder / decision["request_file"]).read_bytes())
    except (OSError, TypeError):
        return {}


def report_lines(db: Database, task_id: str, folder: Path) -> list[str]:
    """The report's Needs you entries for resolved requests, oldest first:
    one line each (``report.RESOLVED_PREFIX``), never read as open."""
    out = []
    for a in _decisions(db, task_id):
        d = _kept(folder, a)
        line = f"{report.RESOLVED_PREFIX}{a['request_kind'] or 'request'} request"
        if summary := _field(d, "summary"):
            line += f": {summary}"
        if action := _field(d, "proposed_action"):
            line += f" (proposed: {action})"
        out.append(f"{line}. {a['decision'].capitalize()} via {a['via']} at {a['at']}; "
                   "the Task was requeued.")
    return out


# --------------------------------------------------------------------------
# The next Attempt's prompt
# --------------------------------------------------------------------------

APPROVED_NOTE = """\
Nightshift: you stopped and raised a "needs you" request, and the user \
APPROVED it. You may now take the proposed action (within this Task's \
permissions), then carry on with the Task. Don't raise this request again."""

DENIED_NOTE = """\
Nightshift: you stopped and raised a "needs you" request, and the user \
DENIED it. Don't take that action. Carry on with the Task without it; if the \
Task can't be done without it, write a different request to `needs_you.json` \
and stop."""

RESUME_TAIL = """\
Continue where you left off: check `progress.md` first and don't redo \
finished work. Keep `progress.md` current as before."""

FRESH_NOTE = """

---
Nightshift: this Task stopped on a "needs you" request and is resuming in a \
new session; {why}. Earlier work is already in the Task folder. Read \
`plan.md` and `progress.md` first, continue from there, and don't redo \
finished work.

"""


def decision_note(db: Database, decision_id: int | None, folder: Path) -> str | None:
    """What the next Attempt is told: the decision and the original request."""
    a = db.approval(decision_id) if decision_id is not None else None
    if a is None:
        return None
    d = _kept(folder, a)
    lines = [APPROVED_NOTE if a["decision"] == "approved" else DENIED_NOTE, "",
             "Your request was:", f"- kind: {a['request_kind'] or 'request'}"]
    for key, label in (("summary", "summary"), ("details", "details"),
                       ("proposed_action", "proposed action")):
        if value := _field(d, key):
            lines.append(f"- {label}: {value}")
    return "\n".join(lines)


def resume_prompt(note: str) -> str:
    """The prompt of an Attempt that resumes the session that raised the request."""
    return f"{note}\n\n{RESUME_TAIL}"


def fresh_prompt(prompt: str, note: str, why: str) -> str:
    """The prompt of a fresh session after a resolved request."""
    return prompt + FRESH_NOTE.format(why=why) + note


def resolved_since(db: Database, task_id: str, transition_id: int) -> bool:
    """Whether the Task's request of a transition into ``needs_you`` (or a
    later one) has been resolved since: a needs_you -> queued transition."""
    return any(t["id"] > transition_id and t["from_state"] == TaskState.NEEDS_YOU
               and t["to_state"] == TaskState.QUEUED for t in db.transitions(task_id))

