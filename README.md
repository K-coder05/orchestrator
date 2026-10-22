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
orch schedule add company-rescrape  # 2am every 2 days, catch-up on next wake, overlap skip
orch schedule list           # also enable | disable | run-now | remove <name>
orch dashboard install       # read-only web dashboard: its own launchd agent on
                             # 127.0.0.1 and the Tailscale address, port 8765
```

The dashboard shows Tasks grouped by status (running, queued, paused, needs
you, done, failed), dark and phone-sized, refreshing every 30 seconds. Open
`http://127.0.0.1:8765/` on the Mac, or the Mac's Tailscale address (or its
full `*.ts.net` MagicDNS name) on your phone. It never listens on the LAN or
a public address: with Tailscale not connected it refuses to start, and
launchd retries until it is (set `dashboard.tailscale: false` for localhost
only). `orch dashboard run` serves it in a terminal instead.

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
At 8am Los Angeles time (`telegram.digest_time`) one digest sums up everything
since the previous one, usage pauses included, and what is still waiting; if
the laptop was asleep at 8am it is sent once on wake.

`--agent gemini-api` runs a Task on paid AI Studio credits (the Gemini CLI
with the API key from `.env` or the Keychain as `GEMINI_API_KEY`). Every model
call is priced (`spend.gemini_api_prices`) and recorded; a Task that reaches
$2 (`spend.per_task_usd`) is stopped and moved to `needs_you`. `orch usage`
shows the spend so far and the credit left (`plans.gemini.api_credit_*`).
Paid calls also stop at $5 a day (`spend.per_day_usd`): every paid Task moves
to `paused_spend` (a running one is stopped) until midnight Los Angeles time,
then resumes on its own. At $15 a month (`spend.per_month_usd`) they pause
and Telegram asks you to raise the cap: `orch raise-cap 25` raises it for
this month only, and the Tasks resume. Subscription work is never paused by
spend.

Paid credits are used only when the subscription can't do the work in time
(the paid-credit rule): a Task on Gemini while Gemini is out of usage moves to
`gemini-api` only if it is urgent, or Gemini's next reset falls after the end
of its Catch-up window (an Occurrence of a `within N hours` Schedule).
Otherwise it waits for the reset. A Task the rule moved goes back to Gemini
once Gemini resets, unless it has started again. A Task that failed over to
Gemini, or has `--agent`, never uses paid credits, and a spend cap holds them
like any paid call. Each decision is in the Task's `logs/nightshift.log` and
in section 8 of its report.

`orch usage` lists each agent's Windows with the percent of the Window's
Baseline used and the time to its next reset. Once a Window has a Baseline
(learned from real limit hits), no new Task starts on that agent at 80% of it
(`usage.stop_starting_percent`), and from 8am to 11pm background Tasks on
Claude stop at 75% (`usage.daytime_reserve`), keeping the rest for your
interactive use. Running Tasks always continue. Codex reports no cost, so it
never gets a Baseline and only pauses on an actual limit error.

A Schedule creates pre-approved Occurrences of a template on a cron-style
rule (local time), each an ordinary Task in
`~/agents/recurring/<name>/YYYY-MM-DD/`. Fires missed while the Mac slept (or
the daemon was down) catch up when the daemon starts or the Mac wakes, always
as one Occurrence however many were missed: `--catch-up "within <N>h"` runs it
only if the latest missed fire is at most N hours old, `--catch-up next-wake`
always runs it. `--overlap skip` (default) skips a fire while the previous
Occurrence is unfinished; `--overlap replace` cancels that one and starts
fresh. `orch schedule run-now <name>` fires once by hand.
