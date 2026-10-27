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
orch add "Research <topic>" --template research-report
orch add "Brief me on <company> for my interview with <names>" --template company-brief --priority urgent
orch add "Study guide for CS 61A from ~/school/cs61a/week5" --template study-guide
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
orch clean-week              # Gate 2: were the last 7 days clean? exits 1 if not
orch dashboard install       # read-only web dashboard: its own launchd agent on
                             # 127.0.0.1 and the Tailscale address, port 8765
```

The dashboard shows the "needs you" inbox (every open request: the agent's
`needs_you.json`, plan approvals and re-approvals, spend caps, each with its
Task, kind and summary; view only, approve with `orch approve` until Phase 3)
and Tasks grouped by status (running, queued, paused, needs you, done,
failed), dark and phone-sized, refreshing every 30 seconds. Tap a Task to
read its `report.md` rendered as Markdown (`/tasks/<id>/report`; raw HTML in
a report shows as text). **Live log** (`/live`) tails the running Task's
logs as they happen, the same lines as `orch logs -f`, and says "Nothing is
running." when it's idle. **Schedules** (`/schedules`) previews tonight's
queue (what should run between midnight and 7am, in queue order, each with
a rough usage estimate from its `estimated_minutes` and past cost per
minute, totalled per agent against its Baseline), then lists each Schedule
with its timing, enabled state, last run and next fire. **Usage**
(`/usage`) is the usage meter: each agent's Windows, as a percent of the
Window's Baseline with a countdown to its reset that ticks live in the
browser (a Window with no Baseline yet says so rather than guess a
percentage), then the spend tracker: today against the $5 daily cap, this
month against the $15 monthly cap (or what `orch raise-cap` raised it to),
and each Task's paid spend against its $2 cap. Open
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
`research-report` and `company-brief` (public sources only, never LinkedIn)
write `output/research.md` / `output/brief.md`; the Attempt fails unless
every claim cites a URL or is labelled *unverified*, at least 5 distinct
sources are cited and it is at least a page (500 words). The report lists
the sources.
`study-guide` reads a folder of lecture slides inside `~/school/` (read-only)
and writes `output/study_guide.md` and `output/practice_problems.md`. The
request names the course, which needs an AI policy in config
(`school.courses`): that policy heads the agent protocol and wins over
everything else. A course with no policy, or a folder outside `~/school/`,
is refused. It never does or submits graded work.

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
It also takes commands from you: `status`, `list [state ...]`, `approve`,
`deny`, `cancel`, `pause`, `resume` and `retry <id>`, and
`priority <id> <urgent|normal|background>`; send `help` for the list.
At 8am Los Angeles time (`telegram.digest_time`) one digest sums up everything
since the previous one, usage pauses included, and what is still waiting; if
the laptop was asleep at 8am it is sent once on wake.

`--agent gemini` runs a Task on the Gemini CLI signed in with an AI Studio
API key (`agents.gemini.auth: gemini-api-key`): put the key in
`~/.config/nightshift/.env` (mode 0600) or the Keychain as `GEMINI_API_KEY`;
a key exported in your shell profile doesn't reach the daemon. The key's
free-tier quota is Gemini's usage Window, so a quota error pauses the Task
like any usage limit. An Attempt that stops producing output while a Step
is in flight and no command runs is ended after `agents.gemini.stall_minutes`
(5) as a Failed Attempt: gemini-cli 0.65.0 can hang after the sandbox
blocks a command.

`--agent gemini-api` runs a Task on paid AI Studio credits (the Gemini CLI
with a billed project's API key from `.env` or the Keychain as
`GEMINI_PAID_API_KEY`). Every model
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
fresh. `orch schedule run-now <name>` fires once by hand. `orch schedule edit
<name> --cron RULE --catch-up RULE --overlap skip|replace` changes any of the
three rules (checked as `add` checks them), as does the dashboard's edit form
on `/schedules`, beside its enable/disable toggle. The daemon uses the new
rules from its next look; a new timing rule counts fires from the edit, so no
run that already happened is repeated.

`orch clean-week` checks the last 7 days (`--days N`, `--until WHEN`) against
Gate 2: every Schedule fire either ran, caught up within its Catch-up rule, or
was skipped by its rules (each fire is listed with its Occurrence); every
Failed Attempt and failed Task has an explanation (a crash with no error
message, a daemon that died mid-Attempt, or an agent that reported failed
without saying why is listed as unexplained); and nothing was done by hand
(`orch cancel|retry|pause|resume|approve|deny|priority|raise-cap`, `orch
schedule run-now|edit|enable|disable|remove`, or the same from Telegram or the
dashboard; approving a plan while `orch add` asks doesn't count). It exits 1 if any of the three fails.
