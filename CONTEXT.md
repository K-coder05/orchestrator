# Nightshift

A single-user orchestrator that queues, schedules, runs and reports on work done by headless AI agents, pushing recurring grunt work into the overnight window.

## Language

### Work

**Task**:
One unit of requested work, with exactly one `task.yaml`, one task folder and one `report.md`.
_Avoid_: Job, run

**Attempt**:
One agent session working on a Task. Retries, resumes after a pause, and failovers each start a new Attempt within the same Task.
_Avoid_: Run, execution

**Task type**:
The category of a Task (e.g. coding, research, resume tailoring, comedy writing) that decides its failover, internet default, long-task gating and runtime cap.
_Avoid_: Kind, category

**Step**:
One tool call observed in an agent's output stream; the unit counted by fault injection.
_Avoid_: Turn, action

**Checkpoint**:
The agent's own update to `progress.md` after a meaningful piece of progress; judged by the agent, not counted by Nightshift.
_Avoid_: Step, save point

### Scheduling

**Schedule**:
A named recurring rule (cron-style timing plus a catch-up rule) that creates Tasks from a template.
_Avoid_: Job, cron

**Occurrence**:
A Task created by a Schedule firing. It is an ordinary Task whose folder lives under `recurring/<schedule-name>/<date>/`.
_Avoid_: Recurring run

**Priority**:
The level the user assigned to a Task: urgent, normal or background.

**Effective priority**:
The Priority used for queue ordering, recalculated each time it is evaluated: the assigned Priority, raised to urgent once the Task's deadline is within 24 hours.
_Avoid_: Computed priority

**Catch-up rule**:
A Schedule's rule for fires missed while the machine slept: run within N hours or skip, or run on next wake. Several missed fires always collapse into one Occurrence.

**Overlap rule**:
A Schedule's rule for when it fires while its previous Occurrence is not yet finished: `skip` the new fire (default) or `replace` the old Occurrence.

### Outcomes

**Failed Attempt**:
An Attempt that ended in a crash, hit its runtime cap, or failed a post-hook. Only Failed Attempts count toward a Task's retry budget; Attempts ended by a usage pause do not.
_Avoid_: Error, crash

**Stuck Attempt**:
An Attempt Nightshift stopped because it went silent (no output and no file changes in the Task folder for too long) or made the same tool call several times in a row. It is not a Failed Attempt: the Task continues once with a note about what it was stuck on, and needs you if it gets stuck again.
_Avoid_: Hung, frozen

### Recruiting

**Tracker**:
The application tracker (`applications.csv`), owned and written only by Nightshift code, never by agents.
_Avoid_: Spreadsheet, sheet

**Proposed row**:
A candidate Tracker row an agent writes to its task folder for Nightshift to validate and merge.

**New posting**:
A posting whose Proposed row was actually appended to the Tracker by the merge; anything deduped away is not new.
_Avoid_: Fresh posting, unseen posting

### School

**Course**:
A class configured under `school.courses`, named in requests by its id, name or an alias. A Task tied to a Course runs under its AI policy.

**AI policy**:
A Course's rules for AI use, from config, with optional hard limits. It heads the agent protocol of every Attempt of that Course's Tasks and wins over everything else; a Course with no AI policy gets no Tasks.
_Avoid_: Course rules, honor code

### Usage

**Window**:
One usage limit period of an agent's plan, such as Claude's rolling 5 hours or its weekly cap.

**Limit observation**:
A record of an actual rate-limit hit in a Window, with the usage Nightshift had recorded in that Window up to the hit.

**Baseline**:
The learned usage capacity of a Window: the lowest of its last 3 Limit observations, measured in reported cost-equivalent. It counts only Nightshift's own usage, not interactive use.
_Avoid_: Quota, limit
