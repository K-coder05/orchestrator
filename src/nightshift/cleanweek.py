"""Gate 2 (§4): the clean-week check behind ``orch clean-week``.

For a period (the last 7 days by default) it checks Nightshift's own records
against the three conditions of a clean week:

1. **Scheduled runs.** Every fire of every Schedule in the period is matched
   to the decision that handled it (``schedule_runs``): a decision covers
   every fire after the previous decision's fire, up to its own (several
   missed fires collapse into one, §2.5). Each fire either *ran* (its
   Occurrence was created on time), *caught up* (created late, or collapsed
   into a later fire's Occurrence, within the Catch-up rule) or was
   *skipped* by its Catch-up or Overlap rule. A fire no decision covers, an
   error, a catch-up outside the rule, an Occurrence cancelled (other than
   by the Overlap rule ``replace``) or one still unfinished a day after it
   was created fails the check. Fires while a Schedule was disabled don't
   count (disabling it is listed as an intervention).
2. **Failures.** Every Failed Attempt that ended in the period, and every
   Task that failed in it without a Failed Attempt (the agent's report said
   failed, or Nightshift could not start it), with its explanation. A crash
   with no error message, a daemon that died mid-Attempt, or an agent that
   reported failed without saying why is listed as unexplained.
3. **Manual interventions.** Every change to a Task by hand (``orch cancel``,
   ``retry``, ``pause``, ``resume``, ``priority``, or the same control from
   Telegram or the dashboard), every approval or denial
   (except the plan decision of ``orch add`` or Telegram's ``add`` itself,
   part of asking for the work), every ``orch schedule run-now``, ``enable``, ``disable`` or
   ``remove`` and every ``orch raise-cap``.

The week is clean only if all three hold; ``orch clean-week`` exits 1
otherwise. Times come from the caller, so it is tested on fixture data.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import report, schedules
from .config import Config
from .db import Database
from .states import TERMINAL, TaskState
from .timeutil import parse_iso

DAYS = 7
#: A fire decided within this long counts as on time (the daemon polls every
#: few seconds while awake); later, it caught up.
ON_TIME = timedelta(minutes=10)
#: An Occurrence still unfinished this long after it was created didn't run.
STUCK_AFTER = timedelta(hours=24)

RAN, CAUGHT_UP, SKIPPED, DISABLED = "ran", "caught up", "skipped", "disabled"
MISSED, ERROR, LATE, DID_NOT_RUN = "missed", "error", "caught up late", "did not run"

#: Failed Attempt reasons that say what happened but not why.
_NO_CAUSE = (
    re.compile(r"crashed: agent exited with code \S+$"),
    re.compile(r"daemon stopped unexpectedly"),
)
_AGENT_FAILED = "agent finished; report says failed"
#: A transition a person caused: `orch cancel|retry|pause|resume|approve|deny`
#: or the same control from Telegram or the dashboard (``nightshift.control``),
#: Ctrl-C of a foreground run.
_VIA = r"\((orch [a-z][a-z -]*|telegram|dashboard)\)"
_BY_HAND = re.compile(r"\bby hand\b|" + _VIA + r"|Ctrl-C")
#: Deciding the plan while adding the Task, at `orch add` or Telegram's `add`.
_NOT_BY_HAND = ("(orch add)", "(telegram add)")


@dataclass(frozen=True)
class FireCheck:
    schedule: str
    fire_at: datetime
    status: str
    detail: str
    ok: bool
    task_id: str | None = None


@dataclass(frozen=True)
class FailureCheck:
    task_id: str
    title: str
    at: datetime
    reason: str
    explained: bool
    explanation: str
    attempt_no: int | None = None


@dataclass(frozen=True)
class Intervention:
    at: datetime
    command: str
    detail: str
    task_id: str | None = None
    schedule: str | None = None


@dataclass
class CleanWeek:
    start: datetime
    end: datetime
    fires: list[FireCheck] = field(default_factory=list)
    failures: list[FailureCheck] = field(default_factory=list)
    interventions: list[Intervention] = field(default_factory=list)

    @property
    def schedules_ok(self) -> bool:
        return all(f.ok for f in self.fires)

    @property
    def failures_ok(self) -> bool:
        return all(f.explained for f in self.failures)

    @property
    def hands_off(self) -> bool:
        return not self.interventions

    @property
    def clean(self) -> bool:
        return self.schedules_ok and self.failures_ok and self.hands_off


def check(cfg: Config, db: Database, end: datetime, *, days: int = DAYS) -> CleanWeek:
    """Check the ``days`` days up to ``end`` against Gate 2."""
    start = end - timedelta(days=days)
    week = CleanWeek(start=start, end=end)
    week.fires = _fires(cfg, db, start, end)
    week.failures = _failures(db, start, end)
    week.interventions = _interventions(db, start, end)
    return week


def _within(at: str | None, start: datetime, end: datetime) -> bool:
    return at is not None and start < parse_iso(at) <= end


# --------------------------------------------------------------------------
# 1. Scheduled runs
# --------------------------------------------------------------------------


def _disabled_at(row: Any, events: list[Any], t: datetime) -> bool:
    """Whether the Schedule ``row`` was disabled at ``t``, from the recorded
    `orch schedule enable|disable` (else its current state since its last
    change)."""
    before = [e for e in events if parse_iso(e["at"]) <= t]
    if before:
        return before[-1]["command"].endswith("disable")
    if events:
        return events[0]["command"].endswith("enable")
    return not row["enabled"] and t >= parse_iso(row["updated_at"])


def _fires(cfg: Config, db: Database, start: datetime, end: datetime) -> list[FireCheck]:
    tz = ZoneInfo(cfg.timezone)
    toggles = [i for i in db.interventions()
               if i["command"] in ("orch schedule enable", "orch schedule disable")]
    out: list[FireCheck] = []
    for row in db.schedules():
        s = schedules.Schedule.from_row(row)
        created = parse_iso(s.created_at)
        lo = max(start, created)
        if lo >= end:
            continue
        events = [e for e in toggles if e["schedule_name"] == s.name
                  and parse_iso(e["at"]) >= created]
        # Decisions of this Schedule (not of a removed one of the same name),
        # in fire order; `run-now` fires by hand and handles no cron fire.
        decisions = sorted(
            (r for r in db.schedule_runs(s.name)
             if r["trigger"] != schedules.TRIGGER_RUN_NOW and parse_iso(r["decided_at"]) >= created),
            key=lambda r: (parse_iso(r["fire_at"]), r["id"]))
        for f in s.cron.fires_in(lo, end, tz):
            if _disabled_at(row, events, f):
                out.append(FireCheck(s.name, f, DISABLED,
                                     "not counted: the Schedule was disabled", True))
                continue
            d = next((r for r in decisions if parse_iso(r["fire_at"]) >= f), None)
            if d is None:
                if end - f <= ON_TIME:
                    continue  # just fired; the daemon's next poll decides it
                out.append(FireCheck(s.name, f, MISSED,
                                     "no decision: the daemon never handled this fire", False))
                continue
            out.append(_fire_check(db, s, f, d, end))
    return sorted(out, key=lambda c: (c.fire_at, c.schedule))


def _fire_check(db: Database, s: schedules.Schedule, f: datetime, d: Any,
                end: datetime) -> FireCheck:
    fire_at, decided = parse_iso(d["fire_at"]), parse_iso(d["decided_at"])
    late = decided - fire_at
    hours = f"{late.total_seconds() / 3600:.1f}h"
    detail = d["detail"] or ""
    outcome = d["outcome"]
    if outcome == schedules.ERROR:
        return FireCheck(s.name, f, ERROR, f"could not create an Occurrence: {detail}", False)
    if outcome == schedules.SKIPPED_OVERLAP:
        return FireCheck(s.name, f, SKIPPED, f"skipped (Overlap rule: skip): {detail}", True)
    if outcome == schedules.SKIPPED_CATCH_UP:
        if s.catch_up.allows(fire_at, decided):
            return FireCheck(s.name, f, ERROR, f"skipped {hours} late, although its Catch-up "
                             f"rule is {s.catch_up}", False)
        return FireCheck(s.name, f, SKIPPED, f"skipped (Catch-up rule): {detail}", True)
    tid = d["task_id"]
    if f != fire_at:
        return FireCheck(s.name, f, CAUGHT_UP, f"collapsed into Occurrence {tid} (fire at "
                         f"{fire_at:%Y-%m-%d %H:%M}, {d['fires']} fires made one)", True, tid)
    if late > ON_TIME and not s.catch_up.allows(fire_at, decided):
        return FireCheck(s.name, f, LATE, f"Occurrence {tid} created {hours} late, outside its "
                         f"Catch-up rule ({s.catch_up})", False, tid)
    status = CAUGHT_UP if late > ON_TIME else RAN
    what = f"Occurrence {tid}" + (f" created {hours} late ({s.catch_up})"
                                  if status == CAUGHT_UP else "")
    task = db.get_task(tid) if tid else None
    if task is None:
        return FireCheck(s.name, f, DID_NOT_RUN, f"{what}: the Task is gone", False, tid)
    state = TaskState(task["state"])
    if state == TaskState.CANCELLED:
        last = db.transitions(tid)[-1]["reason"]
        if "Overlap rule: replace" in last:
            return FireCheck(s.name, f, status, f"{what}, later replaced by the next fire "
                             "(Overlap rule: replace)", True, tid)
        return FireCheck(s.name, f, DID_NOT_RUN, f"{what} was cancelled: {last}", False, tid)
    if state not in TERMINAL and end - decided > STUCK_AFTER:
        waited = (end - decided).total_seconds() / 3600
        return FireCheck(s.name, f, DID_NOT_RUN, f"{what} is still {state} after "
                         f"{waited:.0f}h", False, tid)
    return FireCheck(s.name, f, status, f"{what}: {state}", True, tid)


# --------------------------------------------------------------------------
# 2. Failures
# --------------------------------------------------------------------------


def explain_reason(reason: str | None) -> tuple[bool, str]:
    """Whether a Failed Attempt's reason says why it failed."""
    text = (reason or "").strip()
    if not text:
        return False, "no reason recorded"
    for pattern in _NO_CAUSE:
        if pattern.search(text):
            return False, f"{text} (no error message says why)"
    return True, text


def report_explanation(folder: str | Path) -> str | None:
    """Why the agent says it failed: section 1 of its report.md beyond the
    result word, else its Open issues or Summary. None if it said nothing."""
    path = Path(folder) / "report.md"
    if not path.exists():
        return None
    _, sections = report.parse_sections(path.read_text())
    result = sections.get(1, "")
    if "Agent:" in result:
        agent = result.split("Agent:", 1)[1]
    elif result.startswith("**"):  # finalized; the agent wrote nothing here
        agent = ""
    else:
        agent = result
    if re.sub(r"\b(done|partial|failed)\b|[^a-z0-9]", "", agent.lower()):
        return agent.strip().splitlines()[0]
    for n, name in ((7, "Open issues"), (3, "Summary")):
        body = sections.get(n, "")
        if not report._is_empty(body) and body != "_(the agent did not fill this in)_":
            return f"{name}: {body.strip().splitlines()[0]}"
    return None


def _failures(db: Database, start: datetime, end: datetime) -> list[FailureCheck]:
    out: list[FailureCheck] = []
    attempts = {a["id"]: a for a in db.all_attempts()}
    tasks: dict[str, Any] = {}

    def task(tid: str) -> Any:
        if tid not in tasks:
            tasks[tid] = db.get_task(tid)
        return tasks[tid]

    for a in attempts.values():
        if a["outcome"] != "failed" or not _within(a["ended_at"], start, end):
            continue
        ok, why = explain_reason(a["end_reason"])
        out.append(FailureCheck(a["task_id"], task(a["task_id"])["title"],
                                parse_iso(a["ended_at"]), a["end_reason"] or "", ok, why,
                                a["attempt_no"]))
    for t in db.all_transitions():
        if t["to_state"] != str(TaskState.FAILED) or not _within(t["at"], start, end):
            continue
        run = attempts.get(t["run_id"]) if t["run_id"] is not None else None
        if run is not None and run["outcome"] == "failed":
            continue  # listed as its Failed Attempt
        row = task(t["task_id"])
        if t["reason"].startswith(_AGENT_FAILED):
            why = report_explanation(row["folder"])
            ok = why is not None
            why = why or "the agent reported failed without saying why in report.md"
        else:
            ok, why = explain_reason(t["reason"])
        out.append(FailureCheck(t["task_id"], row["title"], parse_iso(t["at"]), t["reason"],
                                ok, why, run["attempt_no"] if run is not None else None))
    return sorted(out, key=lambda c: c.at)


# --------------------------------------------------------------------------
# 3. Manual interventions
# --------------------------------------------------------------------------


def _interventions(db: Database, start: datetime, end: datetime) -> list[Intervention]:
    out: list[Intervention] = []
    for t in db.all_transitions():
        reason = t["reason"]
        if (t["from_state"] is None or not _within(t["at"], start, end)
                or not _BY_HAND.search(reason) or any(n in reason for n in _NOT_BY_HAND)):
            continue
        m = re.search(_VIA, reason)
        command = m.group(1) if m else "by hand"
        out.append(Intervention(parse_iso(t["at"]), command,
                                f"{t['from_state']} -> {t['to_state']}: {reason}",
                                task_id=t["task_id"]))
    for r in db.schedule_runs():
        if r["trigger"] == schedules.TRIGGER_RUN_NOW and _within(r["decided_at"], start, end):
            what = (f"created Occurrence {r['task_id']}" if r["task_id"] else r["outcome"])
            out.append(Intervention(parse_iso(r["decided_at"]), "orch schedule run-now",
                                    f"Schedule {r['schedule_name']} run now: {what}",
                                    task_id=r["task_id"], schedule=r["schedule_name"]))
    for i in db.interventions():
        if _within(i["at"], start, end):
            out.append(Intervention(parse_iso(i["at"]), i["command"], i["detail"],
                                    task_id=i["task_id"], schedule=i["schedule_name"]))
    return sorted(out, key=lambda i: i.at)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def format_check(week: CleanWeek, tz: str) -> str:
    z = ZoneInfo(tz)

    def t(d: datetime) -> str:
        return d.astimezone(z).strftime("%Y-%m-%d %H:%M")

    def verdict(ok: bool) -> str:
        return "PASS" if ok else "FAIL"

    lines = [f"Clean week check (Gate 2): {t(week.start)} to {t(week.end)} ({tz})", ""]

    counted = [f for f in week.fires if f.status != DISABLED]
    bad = [f for f in week.fires if not f.ok]
    lines.append(f"1. Scheduled runs: {verdict(week.schedules_ok)} ("
                 + (f"{len(bad)} of {len(counted)} fires not handled correctly"
                    if bad else f"{len(counted)} fire{'s' if len(counted) != 1 else ''}, "
                    "all ran, caught up or skipped by their rules") + ")")
    for f in week.fires:
        mark = "  " if f.ok else "! "
        lines.append(f"  {mark}{t(f.fire_at)}  {f.schedule}: {f.status}: {f.detail}")
    if not week.fires:
        lines.append("    No Schedule fired in this period.")

    lines.append("")
    unexplained = [f for f in week.failures if not f.explained]
    lines.append(f"2. Failures: {verdict(week.failures_ok)} ("
                 + (f"{len(unexplained)} of {len(week.failures)} without an explanation"
                    if unexplained else f"{len(week.failures)}, all explained") + ")")
    for f in week.failures:
        mark = "  " if f.explained else "! "
        attempt = f" Attempt {f.attempt_no}" if f.attempt_no is not None else ""
        lines.append(f"  {mark}{t(f.at)}  {f.task_id}{attempt} ({f.title}): "
                     + (f.explanation if f.explained else f"UNEXPLAINED: {f.explanation}"))

    lines.append("")
    n = len(week.interventions)
    lines.append(f"3. Manual interventions: {verdict(week.hands_off)} ("
                 + (f"{n} intervention{'s' if n != 1 else ''}" if n else "none") + ")")
    for i in week.interventions:
        lines.append(f"  ! {t(i.at)}  {i.command}: {i.detail}")

    lines.append("")
    failed = [name for name, ok in (("scheduled runs", week.schedules_ok),
                                    ("failures", week.failures_ok),
                                    ("manual interventions", week.hands_off)) if not ok]
    lines.append("Clean week: yes." if week.clean
                 else f"Clean week: no ({', '.join(failed)}).")
    return "\n".join(lines)
