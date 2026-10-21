# Decisions

Notable implementation decisions, newest last. Architecture decisions with
lasting consequences go in `docs/adr/` instead.

## Phase 1, issue #1: tracer bullet

- **Package layout.** `src/nightshift/` (src layout), installed with
  `pip install -e '.[dev]'`; the `orch` console script points at
  `nightshift.cli:main`. pytest runs with `pythonpath = ["src", "tests"]`.
- **Libraries.** `click` for the CLI (many subcommands with options are
  coming; nicer help and testing than argparse), `PyYAML` for config and
  `task.yaml`. Standard library `sqlite3` for the DB; no ORM.
- **Python 3.12 is project-local.** This Mac only had Python 3.11.5 although
  the spec lists 3.12. Without `brew`, a 3.12 interpreter was installed
  project-locally: `python3 -m venv .bootstrap && .bootstrap/bin/pip install uv
  && UV_PYTHON_INSTALL_DIR=$PWD/.python .bootstrap/bin/uv python install 3.12`,
  then `.venv` was built from `.python/…/bin/python3.12`. All three folders are
  gitignored. `requires-python = ">=3.12"` stays as specified.
- **Hidden `.pth` quirk.** On this Mac the editable-install
  `__editable__.nightshift-0.1.0.pth` sometimes gets the macOS `hidden` flag,
  and Python 3.12.15 skips hidden `.pth` files, so `orch` raises
  `ModuleNotFoundError: nightshift`. Fix: `chflags nohidden
  .venv/lib/python3.12/site-packages/*.pth`, or a non-editable
  `pip install .`. Tests are unaffected (pytest sets `pythonpath`).
- **One source of defaults.** All defaults, placeholder machine/plan/credit
  values included, live in `src/nightshift/defaults.yaml`. The repo's
  `config.example.yaml` is an identical copy, and a test enforces that. Python
  code reads values from config and never hard-codes placeholders (a test
  greps for them). The user's `config.yaml` is deep-merged over the defaults.
- **Overridable locations.** `NIGHTSHIFT_CONFIG_DIR` (or `orch --config-dir`)
  moves `~/.config/nightshift`; `paths.agents_root` moves `~/agents`. Tests
  and dry runs use temporary directories through these.
- **Secrets.** Looked up by name: `.env` in the config dir first, then the
  Keychain (`security find-generic-password -s <secrets.keychain_service> -a
  <NAME> -w`) through the host seam (ADR 0001; `FakeHost` in tests). Nightshift
  refuses a `.env` that isn't mode 0600 and refuses secret values in
  `config.yaml`. `Secrets` never prints values, and every loaded value is
  redacted (`[REDACTED]`) from everything Nightshift writes: stream logs,
  stderr logs, `nightshift.log`, the stored command and `report.md`. Error
  messages about `.env` give line numbers, never content.
- **Attempts table is `runs`**, as SPEC §3.1 item 4 names it; it holds one
  row per Attempt (one agent session). The schema is built by an append-only
  `MIGRATIONS` list tracked by `PRAGMA user_version`. Issue #1 creates
  `tasks`, `runs`, `transitions` and `usage_events`; later issues append
  migrations for `schedules`, `schedule_runs`, `limit_observations`,
  `spend_events` and `approvals`.
- **State machine.** `states.ALLOWED` lists the legal transitions between
  the §2.1 states. `Database.transition()` rejects anything else, requires a
  reason and writes `from_state`, `to_state`, timestamp, reason and the
  Attempt id in the same transaction as the state change. Creation is
  recorded as a transition from `NULL`.
- **Timestamps** are ISO 8601 with offset in the configured timezone
  (default America/Los_Angeles, §2.3). The folder date uses the same zone.
- **Task IDs** are 4 random lowercase base36 chars, retried until unused in
  both the DB and the tasks folder.
- **Free-form `orch add` runs right away in the foreground.** Planning, the
  y/n confirmation and plan approval are issue #5; until then a Task gets
  `approved: true` and the default Task type `general`. `plan.md` is still
  created (§1.3 says every folder has one) with a note that no plan was
  needed. That doesn't conflict with §2.4, which only says no plan is
  *generated*.
- **Unknown Task types default to internet off** (conservative); the
  per-type defaults are in `task_types` in config.
- **Protocol injection.** The §2.10 protocol is passed with
  `--append-system-prompt` on every fresh and resumed Attempt, rather than
  being prepended to the user prompt, so it can't be mistaken for Task content.
  The stored command shows it as `<agent protocol>`.
- **Prompt on stdin.** Avoids flag-parsing surprises and keeps prompts out of
  the process list.
- **Report ownership.** Nightshift seeds `report.md` with the nine §2.11
  headings. The agent fills sections 1-7. At the end Nightshift rewrites the
  report: it leads section 1 with its own verdict (`done`/`partial`/`failed`/
  `needs_you`), fills empty sections ("Nothing." for Needs you; decisions from
  `logs/decisions.jsonl`), and always regenerates section 8 (time per agent
  from Attempt start/end, every token count, requests, reported
  cost-equivalent, and $0.00 spent on a subscription) and section 9 (a link to
  `logs/`, never pasted logs).
- **Outcome rules.** A non-zero exit, `is_error`, or a failure to start the
  binary means `failed`. Otherwise `needs_you.json` means `needs_you`.
  Otherwise the result word the agent wrote in section 1 (`partial`/`failed`)
  decides, with `done` as the default. Ctrl-C during a foreground run means
  `cancelled`.
- **Minimal permissions for now.** File tools are allowed, web tools only
  when internet is on, `--permission-prompts none` so nothing can hang
  waiting for a prompt, and never `--dangerously-skip-permissions`. The full
  §1.4/§1.6 policy and `docs/safety.md` are issue #4.

## Phase 1, issue #2: `orch doctor`

- **Host seam grows read-only probes.** `Host` gained `hardware()`,
  `os_version()`, `free_disk_bytes()`, `which()`, `tool_version()`,
  `python_version()`, `python_package_version()` and `launchd_status()`.
  `MacHost` uses `system_profiler SPHardwareDataType -json` (model name, chip;
  serial and UUID are dropped), `sysctl` (`hw.memsize`, `hw.model`, chip
  fallback), `sw_vers -productVersion`, `shutil.disk_usage` and `launchctl
  print gui/<uid>/<label>`. `FakeHost` is a dataclass with those facts as
  fields, records every version command in `commands_run`, and is what all
  doctor tests use. Nothing in the seam changes system state.
- **Free disk** is measured on the volume holding `paths.agents_root` (or its
  nearest existing parent), since that is where Task data lands.
- **Mismatch rules** against `machine` in config: RAM compares whole GiB;
  macOS compares the configured components as a prefix (`"26"` matches
  `26.6.2`); chip compares case-insensitively; model passes when the
  machine's family name ("MacBook Pro") appears in the configured model
  (the machine doesn't report the screen size); free disk is a mismatch
  only when it differs by more than `doctor.free_disk_tolerance` (default
  20%), because free space drifts. A fact that can't be read is reported as
  a mismatch ("could not read …") so it is never silently unverified.
- **Tool checks.** Each check passes if any candidate is found: TeX is
  `latexmk` or `pdflatex`; the PDF page-count tool is `pdfinfo`, `qpdf` or
  the `pypdf` package in Nightshift's environment. Agent binaries come from
  `agents.<name>.binary` when configured. Version commands run on the
  resolved path with `--version` (`-v` for `latexmk`/`pdfinfo`), stdin
  closed, 10 s timeout. Python and Node below the §1.1 versions (3.12, 22)
  get a warning but don't fail doctor: Nightshift runs on its own `.venv`
  interpreter, which doctor also reports.
- **Exit code.** `orch doctor` exits 1 when any tool is missing or any
  mismatch is found, 0 otherwise. launchd "not installed" is a valid state
  and never fails doctor.
- **New config keys** (in `defaults.yaml`/`config.example.yaml`):
  `daemon.label` (`com.nightshift.daemon`; plist expected at
  `~/Library/LaunchAgents/<label>.plist`, for the daemon issue to reuse),
  `wake.days`/`wake.time` (validated: letters from `MTWRFSU`, `HH:MM:SS`),
  and `doctor.free_disk_tolerance`. Doctor prints `sudo pmset repeat wake
  <days> <time>` (default 01:55:00, just before the 2am schedules) and never
  runs it.
- **Real-machine check (2026-10-20).** This Mac is an Apple M2 Pro, so doctor
  correctly reports a chip mismatch against the M3 placeholder; Codex CLI,
  Gemini CLI and a PDF page-count tool are missing; `python3` on PATH is
  3.11.5 (warning). The user should set `machine.chip` in their config.

## Phase 1, issue #3: background daemon and queue runner

- **`orch add` only queues.** It creates the Task in `queued` and returns; it
  prints a note when no daemon holds the lock, so a Task never silently
  waits. The foreground run from issue #1 is gone from the CLI; `orch daemon
  run --exit-when-idle` runs the queue in a terminal instead.
- **One Task at a time, twice over.** The queue runner (`daemon.QueueRunner`)
  is one serial loop, and only the process holding an exclusive `flock` on
  `<agents_root>/data/daemon.lock` may run Tasks. A second `orch daemon run`
  exits 1. The kernel drops the lock when a process dies, so it is never
  stale; `orch add` and `orch daemon status` probe it to tell whether a
  daemon is up. The runner writes `data/daemon.json` (pid, current Task,
  whether awake is held) for `orch daemon status`.
- **Queue order for now:** assigned Priority (urgent, normal, background),
  then oldest. Effective priority, deadlines, `--at` and long-task windows
  are issue #11. The idle daemon polls every `daemon.poll_seconds` (new key,
  default 5).
- **Wakefulness through the host seam.** `Host.hold_awake()` /
  `release_awake()` / `awake_held()`. `MacHost` runs `caffeinate -i -s -w
  <daemon pid>` (no idle sleep, no system sleep on AC; `-w` makes caffeinate
  exit if the daemon dies, so a crash never leaves the Mac held awake). The
  runner holds it right before an Attempt and releases it in `finally`, so it
  is released between Tasks and whenever nothing runs. `FakeHost` records
  `awake_log` for tests. Lid-closed sleep on battery can't be prevented by
  `caffeinate`; that's what the §1.9 `pmset` wake is for.
- **launchd plist.** `orch daemon install` writes
  `~/Library/LaunchAgents/<daemon.label>.plist` and loads it with `launchctl
  bootstrap gui/<uid>` (booting out a loaded copy first); `uninstall` boots it
  out and deletes the plist. Keys: `KeepAlive`, `RunAtLoad`, `ExitTimeOut 30`,
  stdout/stderr to `<agents_root>/data/daemon.log`. It runs `<venv
  python> -m nightshift --config-dir <dir> daemon run` (new
  `nightshift/__main__.py`) with `PYTHONPATH=<repo>/src`, so the daemon starts
  even when macOS hides the editable-install `.pth`, and with the `PATH` from
  install time, because launchd's default PATH wouldn't find `claude` in
  `~/.local/bin`. Re-run `orch daemon install` after moving the repo or venv.
  Verified with `plutil -lint` on a generated plist; the real
  bootstrap/bootout has not been run on this Mac (tests use `FakeHost`).
- **Stopping mid-Attempt requeues.** SIGTERM (launchd stop/bootout) or SIGINT
  makes the daemon terminate the agent through `runner.AttemptControl`; the
  Attempt ends with outcome `interrupted` and the Task goes `running ->
  queued` (new allowed transition), then the daemon exits. The next Attempt
  starts fresh; resuming the session is issue #13. Verified for real: a
  SIGTERM'd daemon left the Task queued with no agent or caffeinate running,
  and a restarted daemon finished it as Attempt 2.
- **Crash recovery.** On startup the lock holder fails any Task still
  `running` (a daemon that died without cleanup), finishing its open Attempt
  as `failed` and writing the report. A Python exception from one Attempt
  fails that Task (`queued -> failed` is now allowed for Tasks that couldn't
  even start) and the daemon carries on. Retries are issue #12.
- **SQLite WAL + busy timeout** so CLI reads don't block on the daemon.
- **`orch logs`** prints `logs/nightshift.log` lines as `[nightshift] …` and
  each Attempt's stream rendered through its adapter (`[step]`, `[agent]`,
  `[file]`, `[error]`, `[result]`); `--raw` prints the JSON lines. `-f`
  follows new lines and new Attempts until the Task is no longer queued or
  running. `orch list --status` is repeatable.

## Phase 1, issue #4: per-Task agent permissions

- **Policy in core, settings in the adapter.** `nightshift.permissions.build_policy`
  builds an agent-agnostic `PermissionPolicy` (workspace, internet, read-only
  dirs, no-access dirs, private roots, tool dirs, denied domains, denied
  commands, settings dir) from the new `permissions:` config section and the
  Task. `AgentAdapter.write_permissions(workspace, policy)` now takes that
  dataclass instead of a dict. It's rebuilt before every Attempt.
- **Claude: four layers.** `--restricted` (ignores user/project/local
  settings, confines file tools to the working dirs, refuses bypass),
  `--tools` (web tools exist only when internet is on), a per-Task
  `--settings` file with allow/deny rules, and Claude Code's Bash sandbox with
  no unsandboxed escape. Full status of each limit is in `docs/safety.md`.
- **Settings file lives in `<agents_root>/data/permissions/<task-id>/`**,
  not the Task folder: the agent can't read or edit its own policy, and the
  stored command shows a real path to audit.
- **Shell commands get no network on any Task.** The spec allows web access
  only through the built-in fetch/search tools, so the sandbox's network
  allowlist is empty even on internet-on Tasks. The `curl`/`wget`/`python -c`
  deny-list is kept as the best-effort second layer the spec asks for.
  Consequence: no `pip`/`npm install` from the network inside Tasks.
- **Bash reads under home are allowlisted.** The sandbox denies reads of `~`
  and `agents_root` and re-allows the Task folder, read-only folders and
  `permissions.tool_dirs` (`~/.nvm`, `~/.local/bin`, ...), so the agent can't
  read anything personal through the shell. Reads outside home stay open
  (denying `/` breaks every tool); documented as a gap.
- **Missing read-only folders are skipped** for `--add-dir`/`allowRead` (this
  Mac has none of `~/Documents/career`, `~/Documents/comedy`, `~/school`) but
  keep their write-deny rules. A read-only dir that overlaps a no-access dir
  (or the Task folder inside one) is a `ConfigError`.
- **The adapter refuses unsafe commands.** `build_command`/`resume_command`
  raise without a generated policy, and `agents.claude.extra_args` may not
  contain flags that skip or widen permissions.
- **Protocol** now names the read-only folders, the internet rule, the denied
  domains, `robots.txt`/site terms (instruction-only) and says a denied tool
  call must never be worked around.
- **Real-machine verification** is opt-in: `NIGHTSHIFT_REAL_AGENT=1 pytest
  tests/test_permissions_real.py` runs two haiku Tasks through `run_attempt`
  with the real policy. They swap the protocol for an audit brief, because
  with the real protocol the agent refuses to even try an escape (it wrote
  `needs_you.json` instead), which would leave the limits untested. Both
  passed on 2026-10-20. `example.org` stands in for LinkedIn so the test never
  contacts LinkedIn.
- **Known side effect: no local git commits inside Tasks** (the sandbox
  protects `.git/config` and `.git/hooks`). Stricter than §1.6; to revisit
  when coding Tasks need commits.

## Phase 1, issue #5: free-form planning, confirmation and plan approvals

- **Two planning calls, both tool-less.** The estimate call returns
  `estimated_minutes`, `task_type` and a one-sentence reason as JSON. Only a
  Task estimated over `planning.approval_threshold_minutes` (default 30, per
  §2.4) gets a second call that returns the `plan.md` content. Splitting them
  keeps the estimate quick and spends nothing on plans for short Tasks or
  declined estimates. Both are new adapter methods, `planning_command` and
  `parse_planning_output` (§2.9 interface, so core code still names no agent).
- **The agent writes the plan; Nightshift saves it.** The plan call has no
  tools (`--tools ""`), so it needs no permission policy and can't touch any
  file; its JSON answer's `plan_markdown` is written to the Task's `plan.md`.
  Planning calls run in `<agents_root>/data/planning/` (empty, Nightshift-owned)
  so no project files or `CLAUDE.md` from the current directory get in.
- **Planning model.** `agents.claude.planning_model` (default `haiku`; null
  uses `model`). `planning.agent` (null = `defaults.agent`) picks the agent.
  `planning.timeout_seconds` (180) bounds each call.
- **Order of questions in `orch add`.** Estimate call, print estimate and
  type, then "Create this Task?" (skipped by `--yes`). A no creates nothing.
  At or under the threshold: the Task is created `queued`, approved, with the
  "no plan was needed" stub, and never enters `awaiting_approval`. Over it: the
  Task is created in `awaiting_approval` (stub says the plan is pending), the
  plan call runs, the plan is printed and "Approve this plan?" asked. A no
  leaves it in `awaiting_approval`.
- **`--yes` does not approve plans.** §2.4 calls the confirmation and plan
  approval separate, and an over-30 Task "needs my approval", so `--yes` only
  skips the estimate/type question. With no terminal answer (EOF) every y/n
  counts as no, so a scripted `orch add --yes` of a long Task leaves it
  awaiting approval rather than hanging.
- **Failures.** A failed estimate call fails `orch add` and queues nothing. A
  failed plan call leaves the Task in `awaiting_approval` with the pending
  stub; `orch approve` can still run it, `orch deny` cancels it.
- **Unknown proposed types** (not in `task_types`) fall back to
  `defaults.task_type` and the reason says so. `--type` accepts any type, as
  before. The schema's `enum` normally prevents this anyway.
- **Approvals table** (migration 2): one row per decision on a Task awaiting
  approval: `decision` (`approved`/`denied`), `at`, `via` (`orch add`, `orch
  approve`, `orch deny`), `estimated_minutes` and the SHA-256 of the
  `plan.md` that was approved, so a later edit is detectable. The decision,
  the `approved`/`approved_at` columns and the transition are written in one
  transaction; `task.yaml` is updated to match. `orch deny` moves the Task to
  `cancelled` and writes a cancelled `report.md`. `orch approve`/`deny`
  refuse Tasks that aren't awaiting approval. The free-form y/n confirmation
  isn't an approval and isn't recorded there.
- **Planning usage** goes to a new `planning_usage` table (task_id NULL when
  the estimate was declined), not `usage_events`, whose rows belong to an
  Attempt. Usage-window/Baseline code must add both tables.
- **Templates.** `planning.from_template(template)` takes `estimated_minutes`
  and `task_type` from the template, with no planning call; the result feeds
  `create_task(..., template=, estimated_minutes=, needs_approval=
  planning.needs_plan(...))`. The template engine and `orch add --template`
  come later and should use this; scheduled Occurrences pass
  `needs_approval=False` (pre-approved, §2.4).
- **Real-machine check (2026-10-20).** On Claude Code 2.1.306 with haiku:
  a haiku Task estimated 2 min (queued, no plan); a CLI todo app 40 min and a
  literature review 210 min got real plans, approved via `orch approve` and
  the prompt; a no left one awaiting approval; `orch deny` cancelled one. Each
  call cost about $0.0002-0.0008 reported cost-equivalent. Haiku proposed
  `comedy` for a haiku; that's what the y/n and `--type` are for.

## Phase 1, issue #6: priority, deadlines and time windows

- **One pure module.** `nightshift.priority` computes the Effective priority,
  the start gates and the queue order from Task rows and a `now` passed in,
  so every rule is unit tested with a fake clock. `QueueRunner` takes a
  `clock` (default: real time) and calls `pick_next` over all queued Tasks on
  every pick, so the Effective priority is recalculated each time the queue is
  evaluated and is never stored: `tasks.priority` and `task.yaml` keep only the
  assigned Priority. `Database.next_queued_task` (the placeholder) is gone.
- **Order.** Effective priority (urgent, normal, background); within urgent,
  nearest deadline first (none last), then oldest; within normal and
  background, oldest first ("runs in order"). A deadline exactly 24h away, or
  already passed, counts as urgent. A passed deadline is not cancelled here;
  expiry belongs to pause/resume (§2.6).
- **"Background runs only when nothing else waits"** means nothing else that
  may start now. A Task held back by `--at` or the long-task window isn't
  waiting to run, so it doesn't block the queue (otherwise one daytime coding
  Task would stall all background work until midnight). Since one Task runs
  at a time, a background Task that starts at 23:50 can delay a long Task past
  midnight; accepted.
- **Gates.** A Task with `run_at` in the future never starts, whatever its
  priority. A long Task starts only inside `queue.long_window_start` to
  `long_window_end` (default 00:00 to 07:00, end exclusive; a window may wrap
  midnight) unless its Effective priority is urgent, so a near deadline also
  lifts the gate. The gate only governs starting; a running long Task isn't
  stopped at 7am.
- **Which Tasks are long.** A template's `long` (later, via
  `create_task(long=...)`) wins; otherwise the Task type's new
  `task_types.<type>.long` (default false). `coding` and `research` are long
  by default, per §2.3's examples; re-scrapes will be marked by their
  template. `orch add` has no `--long` flag (not in §3.1); `--type` and
  `orch priority <id> urgent` are the overrides.
- **Times.** All rules use the configured `timezone` (America/Los_Angeles by
  default; an unknown IANA name is now a config error) whatever zone `now` is
  in, and deadlines compare absolute time. `--deadline`: `YYYY-MM-DD` (end of
  that day, 23:59:59), `YYYY-MM-DD HH:MM` or ISO 8601. `--at`: `HH:MM` (next
  occurrence, today or tomorrow), `YYYY-MM-DD HH:MM` or ISO 8601. Naive values
  are LA time; stored values are ISO 8601 with the LA offset (DST-correct). A
  deadline or explicit `--at` already passed is rejected. Both are parsed
  before the planning call, so a typo costs nothing.
- **`orch priority <id> <P>`** changes the assigned Priority in the DB and
  `task.yaml` and logs it to the Task's `nightshift.log`; it isn't a state
  transition. Refused for terminal Tasks. It prints the Effective priority
  when a deadline overrides the new value. `orch list` shows a raised Priority
  as `normal>urgent`; `orch show` adds `effective_priority` and, for a held
  Task, a `Waiting:` line.
- **New config section `queue:`** (`deadline_urgent_hours: 24`,
  `long_window_start`, `long_window_end`); defaults.yaml ==
  config.example.yaml still.
- **Real-machine check (2026-10-21, 00:05 PDT).** With a scratch config, a
  real haiku-model `orch add --type coding --deadline 2026-10-30 --at 23:00
  --yes` stored the LA-offset times, `orch priority` changed `task.yaml`, and
  `orch daemon run --exit-when-idle` left it queued (held by `--at`) without
  starting an agent.

## Phase 1, issue #7: runtime caps, retries and cancel

- **One module.** `nightshift.failures` holds the §2.7 rules: what a Failed
  Attempt does to the retry budget (`after_failed_attempt`), the wait schedule
  (`retry_wait`), requeueing due retries (`promote_due`), and `orch retry` /
  `orch cancel` (`retry_by_hand`, `cancel`). Every time comes from the caller,
  so the schedule is tested with a fake clock end to end through
  `QueueRunner(clock=...)` and the fake agent.
- **Failed Attempts** (CONTEXT.md): a crash (non-zero exit, an error result,
  a binary that won't start, a daemon that died mid-Attempt), the runtime cap,
  or a failed post-hook. They alone charge the budget. **Not** Failed
  Attempts: the agent finishing and reporting `failed` itself (it judged its
  own work; retrying the same prompt is unlikely to help, so the Task is
  `failed` at once), `needs_you`, a daemon stop (requeued, as before), a
  Nightshift exception before or during an Attempt (`fail_unstartable`, still
  straight to `failed`: our bug, retrying would repeat it), and a usage pause
  (when pausing lands it must bypass `after_failed_attempt`).
- **Budget and waits.** New config `retries:` (`max_retries: 2`,
  `wait_minutes: [10, 30]`, last value repeats). New columns (migration 3):
  `tasks.retries_used` (charged since creation or the last `orch retry` of a
  finished Task), `tasks.retry_at`, `tasks.cancel_requested_at`. A Failed
  Attempt with budget left moves `running -> retry_wait` with `retry_at` =
  end + wait; the daemon's `run_next` first moves every due `retry_wait` Task
  to `queued` (a visible transition), then the normal priority pick applies.
  With the budget spent the Task goes to `failed` with a final `report.md`
  ("no retries left (2 of 2 used)"); 3 Attempts in total. Retries are same
  agent, fresh session (no resume yet); switching agent on the second retry is
  failover work. `report.md` is only finalized when the Task really ends, not
  on each `retry_wait`. A Task waiting to retry doesn't block the queue.
- **Runtime cap.** Per Attempt, not per Task (each retry gets the full cap).
  `max_runtime_minutes` is set when the Task is created: the template's value
  (`create_task(max_runtime_minutes=)`), else `task_types.<type>.
  max_runtime_minutes` (coding: 360, the "6h for overnight coding"), else
  `defaults.max_runtime_minutes` (120). Enforced by a watchdog thread in
  `run_attempt` measuring `time.monotonic()`, which on macOS doesn't advance
  during sleep; the daemon holds the machine awake while an Attempt runs
  anyway. At the cap it SIGTERMs the agent and, if the agent ignores it for
  15s (`KILL_GRACE_SECONDS`), SIGKILLs it.
- **`AttemptControl.request_stop(reason)`** now carries why: `daemon_stop`
  (default; requeue), `cancel`, `runtime_cap`. The first reason wins. A daemon
  stop of a Task whose cancel was already requested ends it cancelled.
- **Post-hooks** are callables `(folder, row)` that raise to fail
  (`failures.PostHook`, `PostHookFailed`). They run in order after an Attempt
  the agent finished `done` or `partial` (not `failed`/`needs_you`).
  `QueueRunner(post_hooks_for=)` supplies them; `failures.post_hooks_for`
  returns none for now. The template engine plugs its hooks in there.
- **`orch cancel <id>`** works in every unfinished state. Not running: a
  conditional transition (`db.transition(expect=...)`, raising `StateChanged`
  if the daemon moved the Task meanwhile, then re-checked) to `cancelled`
  plus a cancelled `report.md`. Running under a live daemon: the CLI can't
  touch the daemon's process, so it sets `cancel_requested_at`; the watchdog
  polls it about once a second through its own SQLite connection and stops
  the agent, and `run_attempt` ends the Task `cancelled`. The CLI waits up to
  30s for that (`--no-wait` to skip). Running with no daemon alive (orphan):
  cancelled directly, open Attempt closed as `cancelled`. If the daemon dies
  before acting on a request, recovery cancels instead of retrying.
- **`orch retry <id>`**: `failed` or `partial` -> `queued` with the budget
  reset (a new decision by me, so a fresh 2 retries); `retry_wait` -> `queued`
  now, keeping the budget used. Other states are refused (`cancelled` stays
  final; `needs_you` is answered, not retried).
- **Recovery change.** A Task left `running` by a dead daemon was `failed`;
  it is now a crash and gets a retry like any Failed Attempt.
- **Fixed:** writing the prompt to an agent that was already stopped raised
  `BrokenPipeError` out of `run_attempt`; it is now ignored.
- `orch show` prints `retries_used` ("1 of 2"), `retry_at`, a `Waiting:
  retry N at ...` line and a pending cancel request; `orch daemon status`
  counts Tasks waiting to retry. `orch daemon run --exit-when-idle` exits
  with Tasks still in `retry_wait` (idle = nothing queued).
- **Real-machine check (2026-10-21, 00:14 PDT).** Scratch config, haiku,
  `defaults.max_runtime_minutes: 1`, `retries: {max_retries: 1,
  wait_minutes: [0.1]}`, real `orch daemon run`: a Task told to `sleep 100`
  hit the cap at 61s (exit 143), waited in `retry_wait` 6s, retried, hit it
  again and ended `failed` with "no retries left (1 of 1 used)" in
  `report.md`. `orch cancel` on a running Task returned "cancelled (was
  running)" in about 1s, with no `claude` process left; `orch retry` requeued
  the failed Task, refused the cancelled one, and `orch cancel` cancelled the
  queued one.

## Phase 1, issue #8: hard pause and resume, with fault injection

- **One module.** `nightshift.pauses` holds §2.6: `pause_after_attempt`,
  `promote_due` (expiry and requeue, called by the daemon before every
  pick), `held_agents`, `session_to_resume`, and `orch pause` / `orch resume`
  (`pause_by_hand`, `resume_by_hand`). Times come from the caller (fake
  clocks in tests). New columns (migration 4): `simulate_rate_limit_after`,
  `simulated_limit_at`, `pause_kind` (`usage_limit`, `simulated`,
  `by_hand`), `pause_limit_type`, `pause_reset_at`, `resume_at`,
  `resume_pending`, `pause_requested_at`. New config `usage:`
  (`resume_session_max_hours: 24`, `reapproval_after_hours: 72`,
  `probe_interval_minutes: 30`, `step_grace_seconds: 120`,
  `simulated_reset_minutes: 5`).
- **The pause path.** `run_attempt` asks the adapter `detect_rate_limit` on
  every event (and the exit code). On a hit it calls
  `AttemptControl.stop_after_step`: it tracks tool calls in flight (new
  normalized event `tool_result`) and stops the agent once none is, or after
  the grace; the watchdog enforces the grace even when the stream is quiet.
  The Attempt ends `outcome = paused`, the Task `running -> paused_usage`
  with the reset time if known. **Not a Failed Attempt**: the retry budget
  is untouched; no `report.md` is written until the Task really ends. A
  limit that comes up while the agent was finishing on its own (exit 0, no
  stop) doesn't turn a finished Attempt into a pause. A pending limit wins
  over a daemon stop or the runtime cap (the limit is the real reason); a
  cancel wins over everything.
- **Fault injection.** `orch add ... --simulate-rate-limit-after N` stores N
  on the Task. In the first Attempt that reaches N Steps (tool calls in the
  stream; counted per Attempt), the Nth Step's `tool_call` triggers the same
  `stop_after_step` with a `RateLimit("simulated", now + 5 min)`; from there
  it is the real path (the Nth Step finishes, SIGTERM, pause, resume). It
  fires once per Task (`simulated_limit_at`), so the resumed Attempt runs
  through. N isn't in `task.yaml` (§2.2's field list is fixed); `orch show`
  prints it.
- **When a paused Task comes back.** At `resume_at`: the reported reset
  time; with no parseable reset, `probe_interval_minutes` (30) after the hit,
  i.e. the Task itself is the probe until a minimal probe request exists. A
  simulated limit's reset is `simulated_reset_minutes` ahead. **Agent hold:**
  while a Task is paused on a real limit with its reset ahead, no queued Task
  starts on that agent (`priority.evaluate(held_agents=)`); a simulated limit
  holds only its own Task. `orch daemon run --exit-when-idle` exits while
  Tasks are paused, as with `retry_wait`.
- **Resume.** `resume_pending` marks a Task whose next Attempt continues
  paused work. That Attempt resumes the last Attempt's session (`claude -p
  --resume <id>`, prompt `pauses.RESUME_PROMPT`, protocol appended again)
  if it ran on the same agent and the session is under 24h old, counted from
  the first Attempt that used that session id. Otherwise it starts a fresh
  session whose prompt is the original prompt plus a note to read `plan.md`
  and `progress.md` first. A resume that fails before any Step (non-zero
  exit / error result, 0 Steps) ends that Attempt `resume_failed` (not a
  Failed Attempt) and a fresh Attempt starts at once in the same call. A
  daemon stop of a resumed Attempt keeps `resume_pending`.
- **Resume order** (§2.6: priority, then nearest deadline, then oldest).
  `promote_due` handles paused Tasks in `priority.resume_order`, and the
  queue order now puts Tasks with `resume_pending` ahead of fresh Tasks of
  the same Effective priority, ordered among themselves by nearest deadline
  (any level), then oldest. Urgent still beats everything; a fresh urgent
  Task runs before a resumed normal one. Fresh Tasks keep issue #6's order.
- **Expiry**, checked by the daemon in resume order before each pick: a
  paused Task whose deadline has passed is `cancelled` with a final
  `report.md` ("deadline ... passed while paused"). A Task paused more than
  72h (since it entered `paused_usage`) moves to `awaiting_approval` with
  `approved` false in the DB and `task.yaml`; `orch approve` queues it (its
  session is then over 24h old, so it starts fresh). Expiry applies only to
  paused Tasks; a queued Task past its deadline still runs (as urgent).
- **`orch pause <id>`**: queued or retry_wait -> `paused_usage` with
  `pause_kind = by_hand` and no `resume_at` (only `orch resume` ends it; the
  72h and deadline rules still apply). A running Task under a live daemon
  gets `pause_requested_at`; the watchdog sees it, lets the Step finish and
  pauses (the CLI waits up to 30s or the grace; `--no-wait`). An orphaned
  running Task is paused directly. A Task already paused on a limit becomes
  paused by hand (no automatic resume). New transitions: queued ->
  paused_usage, retry_wait -> paused_usage (paused_usage stays the state:
  §2.1 has no other pause state).
- **`orch resume <id>`**: paused_usage -> queued now, whatever the reset time
  (if the Window is still out, it just pauses again); a running Task's
  pending pause request is withdrawn; a Task awaiting re-approval is pointed
  to `orch approve`.
- **Claude adapter changes.** `allowed_warning` is no longer a hit (any
  non-`allowed` status used to be); 429 / usage-limit error messages are;
  `tool_result` events are emitted; `result_error` reads the `errors` list.
- **Real-machine check (2026-10-21, 00:34 PDT)**, scratch config, haiku,
  `simulated_reset_minutes: 0.5`: `orch add "Create three files ..."
  --simulate-rate-limit-after 2 --yes`, real `orch daemon run`. Attempt 1
  paused after 2 Steps (exit 143, `output/a.txt` written), resumed at the
  reset with `--resume` on the same session id and finished `done` with all
  three files and a current `progress.md`. `orch pause` on a running Task
  (Bash `sleep 8` Steps) returned after 4s, once the Step finished; Attempt
  1 `paused by hand after 2 Steps`; `orch resume` resumed the same session
  and it finished `done`. No agent or daemon process was left running.

## Phase 1, issue #9: reset detection, probing, Limit observations, `orch usage`

- **Real format recorded.** A real five-hour limit hit happened on this Mac
  on 2026-10-21 (00:52 PDT); the stream is `tests/fixtures/claude_limit_hit.jsonl`
  and the format is in `docs/adapters.md`. `detect_rate_limit` now reads
  `api_error: usage_limit_reached` / `api_error_params.rate_limit_info`
  and the reset time and Window from the message text ("resets 3:20am
  (America/Los_Angeles)", "session" -> `five_hour`, "weekly" ->
  `seven_day`). A text clock time is resolved against the adapter's
  `clock` (run_attempt passes its clock in). A later report of the same hit
  can add a reset time the first one lacked.
- **Probing replaces "the Task is the probe".** A Task paused on a real
  limit with no reset time: at its `resume_at` (probe interval, 30 min) the
  daemon calls `probe_command` once per agent (`pauses.promote_due(...,
  prober=)`, default `usage.probe`; `QueueRunner(prober=)` is the seam).
  Available -> queued; still limited -> stays paused until the reported
  reset or the next interval (no state change, a log line); probe failed
  (crash, timeout, unknown error) -> queued, so a broken probe can never
  hold a Task forever. Known reset times still requeue without a probe.
  New adapter abstract methods `probe_command()` / `parse_probe_output()`
  (a Codex adapter must implement them) and an optional `window_info(event)`.
- **Migration 5:** `limit_observations`, `window_status`, `probes`.
  Probe usage goes to `planning_usage` (kind `probe`, task_id NULL); that
  table now holds every non-Attempt call. `db.agent_usage(agent)` unions
  both tables. `record_usage(..., at=)` dates usage with the runner's
  clock.
- **Usage of a stopped Attempt.** With no `result` line the Claude adapter
  sums the per-message usage of the stream (tokens, one request per
  message, no cost). To get the real cost on a limit hit, the agent gets
  `usage.limit_exit_wait_seconds` (10) to end the session itself before
  it is stopped (only for real limits, not simulated or by hand).
- **Limit observations.** Recorded for every real hit (Attempt or probe),
  never for a simulated one (it would teach a fake Baseline). Window:
  the hit's `rateLimitType`; a hit that names none counts as the agent's
  shortest configured Window (less usage: conservative). Window start:
  `reset - length` when the reset is known, else the current Window from
  `window_status`, else the trailing length. Usage up to the hit: every
  row of that agent dated in `(start, hit]` (an Attempt's row is dated at
  its end), cost-equivalent summed where reported, plus tokens, requests,
  rows without cost and the last plan-wide utilization. One observation per
  Window: a hit while the last observation's Window is still out (same
  reset, reset still ahead, or no reset and within one length) is skipped.
- **Baseline** = lowest cost-equivalent of the last 3 observations of a
  Window, skipping observations with no own cost (0 or NULL: the Window
  was used up interactively, which says nothing about capacity). It exists
  from the first usable observation. Nothing acts on Baselines yet
  (thresholds are Phase 2).
- **Window lengths** in config `usage.windows.<agent>` (`claude:
  {five_hour: 5, seven_day: 168}`); unlisted `seven_day*` types are 168h.
- **`orch usage [--probe AGENT]`**: per agent and Window the current period,
  Nightshift's usage in it (cost-equivalent, tokens, requests), the
  agent-reported utilization, and the Baseline or "none yet (n
  observations)"; then the latest Limit observations, the last probe and
  Tasks paused on a limit. `cli.probe_provider` is the test seam.
- **Real-machine check (2026-10-21).** Scratch config, haiku. At 00:52 the
  plan was really out (five_hour 104%): `orch usage --probe claude`
  reported `five_hour`, reset 03:20 PDT, and stored a Limit observation
  ($0: none of the Window's usage was Nightshift's, so no Baseline from
  it). At 03:24 a real Task ran `done`, and `orch usage --probe claude`
  answered "available". A full real Attempt pausing on a real hit was not
  run (the Window had reset by then); that path is covered with the fake
  CLI replaying the recorded format.

## Phase 1, issue #10: template engine and `resume-tailor`

- **Templates are YAML in the repo's `templates/`** (`templates.dir` in
  config overrides it). Required keys: `task_type`, `agent`,
  `estimated_minutes`, `max_runtime_minutes`, `long`, `internet`, `prompt`;
  optional: `description`, `inputs`, `files`, `deadline`, `post_hooks`.
  Unknown keys, unknown variables, unknown hooks and paths that leave the
  Task folder are load errors (`nightshift.templates.parse`), so a typo
  fails `orch add`, never a 2am Occurrence. `tests/test_templates.py` loads
  every repo template.
- **Variables are `{{name}}`**, not `{name}`/`$name`, because prompts talk
  about LaTeX and JSON, which are full of braces and dollar signs. Sources:
  `{{request}}` (the `orch add` text), `inputs` (`from: url` = the first
  http(s) URL in the request) and `files` (the relative path of a copied
  input file). The rendered prompt is the Task's `prompt` (what the agent
  gets, also on resume/fresh retries); the request names the Task (`title`,
  slug, folder) and is quoted inside the prompt.
- **`orch add "<request>" --template T`** makes no planning call and asks no
  y/n: §2.4's confirmation is for free-form Tasks, and the estimate and type
  come from the template (`planning.from_template`). A template over the
  plan threshold would still go through plan.md + approval (none does).
  `--type`, `--agent`, `--internet`, `--priority`, `--deadline`, `--at` and
  `--simulate-rate-limit-after` override as for free-form Tasks; the
  template's agent is not an `agent_override`.
- **Input files are copied, not referenced.** `files:` names a config key
  (`templates.resume_tailor.master_tex`, `.cover_letter_template`); at
  `orch add` the file is read (missing = nothing queued) and written to the
  Task folder (`input/master.tex`, `input/cover_letter_template.tex`) plus a
  pristine copy in `<agents_root>/data/inputs/<task-id>/`, which agents can't
  reach. The snapshot keeps line numbers stable for the `changes.md`
  citations even if `master.tex` is edited while the Task waits, and works
  when the configured path is outside the read-only folders. The
  `inputs_unchanged` hook fails an Attempt whose agent edited its copy (and
  restores it). The traceability checks (citations, numbers, the diff in
  the report) should compare against the pristine copy.
- **Deadline source** `deadline: {from_output: output/posting.json, field:
  close_date}`: the posting is only read by the agent, so its close date
  can't be known at `orch add`. After **every** Attempt (paused ones too, so
  the queue and pause expiry see it) Nightshift reads that file and, if the
  Task has no deadline yet, sets it (`YYYY-MM-DD` = 23:59:59 LA, or ISO
  8601), in the DB and `task.yaml`, with a log line. `--deadline` always
  wins (a Task that has a deadline is never changed). A null/missing date
  means no deadline (normal priority); a non-date is logged and ignored; a
  passed date is kept (it's a fact about the posting). The prompt tells the
  agent to write `posting.json` first thing and never guess a date.
- **Post-hooks** are named in the template and built by
  `templates.HOOKS`: `require_outputs` (regular, non-empty files; symlinks
  don't count), `compile_latex` (source, pdf, optional exact `pages`) and
  `inputs_unchanged`. `failures.post_hooks_for(cfg, row, host)` returns a
  template Task's hooks; the daemon passes its host. resume-tailor:
  inputs unchanged -> `posting.json`, `resume.tex`, `cover_letter.tex`,
  `cover_letter.txt`, `changes.md` exist -> compile resume (exactly 1 page)
  -> compile cover letter -> both PDFs exist. Any failure is a Failed
  Attempt with the reason (e.g. "output/resume.pdf has 2 pages; it must be
  exactly 1"), so it gets the normal retries; the prompt tells a retry to
  read `logs/nightshift.log` and fix what the hook named.
- **Nightshift compiles, the agent doesn't have to.** The PDFs in
  `output/` are always Nightshift's own compile (anything the agent put
  there is replaced). The agent may try `pdflatex` in its sandbox to check
  the page count, but nothing depends on it. Compiler: `templates.latex.
  binary`, else `latexmk` on PATH, else the engine (`pdflatex`, run twice);
  `-interaction=nonstopmode -halt-on-error -no-shell-escape
  -file-line-error`, aux files and `compile.log` in `logs/latex/<name>/`,
  `templates.latex.timeout_seconds` (120). Page count with `pypdf` (new
  dependency; the §1.9 page-count tool, pure Python, installed into
  `.venv` only).
- **The compile is sandboxed** (`Host.run_confined`, macOS Seatbelt via
  `sandbox-exec`): agent-written TeX comes from an agent that read a web
  page, and `\input{/any/file}` would pull a private file into the PDF.
  TeX's own `openin_any=p` does **not** stop `\input` of absolute paths in
  pdfTeX (checked on TeX Live 2026: it read `~/…` happily), so it is set
  only as an extra layer. The profile denies network, denies reads under
  your home folder and `agents_root` except the Task folder and
  `templates.latex.readable_dirs` (`~/Library/texmf`, `~/Library/texlive`),
  and allows writes only in the Task folder and the per-user temp dir.
  Verified: a real compile could not `\input` a file outside the Task folder
  ("File not found"). The compile never follows the agent's symlinks out of
  the Task folder (source, PDF and build paths are checked; a link at the
  PDF path is replaced, not written through).
- **LinkedIn posting URLs are refused** at `orch add` (any of
  `permissions.denied_domains`), since the agent couldn't fetch them anyway
  (§1.6).
- **Nothing is ever submitted**: the template prompt says so and points to
  `needs_you.json`; technically the Task has no tool that can submit
  anything (WebFetch is read-only, shell has no network, no MCP/browser
  tools). See `docs/safety.md`.
- **Out of scope here** (the traceability issue): failing on uncited
  bullets, nonexistent cited lines or numbers missing from `master.tex`,
  and the unified diff in `report.md`. The prompt already asks for
  `master.tex:L<n>` citations in `changes.md`.
- **Real-machine check (2026-10-21, 03:39 PDT).** Scratch config, haiku,
  a sample one-page `master.tex` and letter template, TeX Live 2026
  (`latexmk` 4.88): `orch add "Tailor my resume and a cover letter for
  <Figma SWE intern posting on Greenhouse>" --template resume-tailor`, real
  `orch daemon run`. The agent fetched the posting, wrote all outputs with
  `master.tex:L<n>` citations, didn't touch its inputs, and said the
  application is for me to submit; Nightshift compiled both (resume 1 page)
  and the Task ended `done` in 1m 33s ($0.019 cost-equivalent). The posting
  states no close date, so `close_date` was null and no deadline was set
  (the close-date path is covered with the fake agent). On a first run the
  agent's own `pdflatex` was auto-denied (it needs a permission prompt), and
  it reported `partial` for the unchecked page count; the prompt now tells
  it not to compile and that this is no reason for partial.
