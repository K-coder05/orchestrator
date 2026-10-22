# Nightshift

A single-user orchestrator that queues, schedules, runs and reports on work
done by headless AI agents on a Mac. See `SPEC.md`, `CONTEXT.md` and `docs/`.

## Development

```sh
python3.12 -m venv .venv            # see docs/decisions.md if 3.12 isn't installed
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
```

## Usage

```sh
cp config.example.yaml ~/.config/nightshift/config.yaml   # optional; all keys have defaults
orch daemon install          # launchd agent (KeepAlive) that runs queued Tasks;
                             # refuses if the repo is in ~/Desktop, ~/Documents or
                             # ~/Downloads (macOS privacy protection, see docs/decisions.md #3)
orch add "Write a haiku about the night shift to output/haiku.txt"   # asks y/n
orch add "Tailor my resume" --priority urgent --deadline 2026-11-01 --at 02:00
orch add "Tailor my resume and a cover letter for <posting URL>" --template resume-tailor
orch priority <id> background   # urgent | normal | background
orch approve <id>            # or `orch deny <id>`, for Tasks awaiting approval
orch retry <id>              # failed/partial: run again; retry_wait: retry now
orch cancel <id>             # any unfinished Task; stops a running agent
orch list --status running
orch logs <id> -f
orch report <id>
orch schedule add posting-scan   # daily 6:30am, catch-up within 12h else skip, overlap skip
orch schedule list           # also enable | disable | run-now | remove <name>
```

`orch add` first makes a quick planning call that estimates the minutes and
proposes a Task type, prints both and asks y/n (`--type T` overrides the type,
`--yes` skips the question). It then creates
`~/agents/tasks/YYYY-MM-DD_<slug>_<id>/`, queues the Task and returns. A Task
estimated over 30 minutes gets a `plan.md` first; `orch add` prints it and asks
for approval, and a no leaves it `awaiting_approval` for `orch approve` or
`orch deny`. The daemon runs queued Tasks one at a time, holding the Mac awake
with `caffeinate` only while one runs, and leaves a finished `report.md` in the
folder. `orch daemon status` shows whether it is up; `orch daemon run` runs it
in a terminal instead of under launchd. `orch show <id>` lists a Task's
Attempts and state history. Set `NIGHTSHIFT_CONFIG_DIR` to use a different
config directory.

`--template T` makes the Task from `templates/T.yaml` instead: no planning
call or question; the template sets the estimate, Task type, agent, runtime
cap, internet and long-task flag, renders the prompt, and adds post-hooks
that check the output. `resume-tailor` copies `master.tex` and your
cover-letter template (paths in `templates.resume_tailor` in config) into the
Task folder, and Nightshift compiles the agent's `output/resume.tex` and
`output/cover_letter.tex` itself (sandboxed; needs TeX and `pypdf`). The
Attempt fails unless the resume PDF is exactly 1 page. The posting's stated
close date becomes the deadline unless `--deadline` is given. Nothing is ever
submitted: you review `output/` and apply yourself.

The queue runs by Effective priority: urgent first (nearest deadline first),
then normal in order, then background only when nothing else can start. A
Task counts as urgent within 24 hours of its `--deadline`. `--at` holds a Task
until that time, and long Tasks (Task types `coding` and `research` by
default) start only between midnight and 7am unless urgent. All times are
America/Los_Angeles. `orch show <id>` prints the Effective priority and why a
queued Task is waiting.

An Attempt that crashes, hits its runtime cap (2 hours, 6 for `coding`;
`defaults.max_runtime_minutes`, `task_types.<t>.max_runtime_minutes`) or fails
a post-hook is a Failed Attempt: the Task waits in `retry_wait` 10 minutes,
then 30, and is retried on the same agent; after 2 retries it is `failed`.
`orch retry <id>` requeues a failed Task with a fresh budget (or ends a
`retry_wait` early), and `orch cancel <id>` cancels any unfinished Task.

With `telegram.user_id` in config and the bot token in `.env` (or the
Keychain) as `TELEGRAM_BOT_TOKEN`, the daemon sends you a Telegram message when
a Task finishes (its one-line Result), fails, or needs you; usage pauses stay
silent, and comedy Tasks get status lines only. It only long-polls the Bot
API, so no port is opened, and it ignores (and logs) messages from anyone else.

A Schedule creates pre-approved Occurrences of a template on a cron-style
rule (local time), each an ordinary Task in
`~/agents/recurring/<name>/YYYY-MM-DD/`. Fires missed while the Mac slept (or
the daemon was down) catch up when the daemon starts or the Mac wakes, always
as one Occurrence however many were missed: `--catch-up "within <N>h"` runs it
only if the latest missed fire is at most N hours old, `--catch-up next-wake`
always runs it. `--overlap skip` (default) skips a fire while the previous
Occurrence is unfinished; `--overlap replace` cancels that one and starts
fresh. `orch schedule run-now <name>` fires once by hand.
