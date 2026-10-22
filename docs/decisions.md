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
  Verified with `plutil -lint` on a generated plist; tests use `FakeHost`.
  The real bootstrap/bootout was run later (see "Real launchd" below).
- **Real launchd (2026-10-21, macOS 26.6.2).** Run on this Mac with the real
  `~/agents` and `~/.config/nightshift`:
  - From the repo in `~/Desktop`, `orch daemon install` loaded the agent
    (`launchctl print` showed state running, a pid) but the daemon never
    started: its process hung in dyld `open()` of the venv's Python (which
    lives in the repo's `.python/`). macOS privacy protection (TCC) had put up
    a "python3.12 would like to access files in your Desktop folder" dialog
    and the open blocked until it was answered. `daemon.log` stayed empty and
    `orch daemon status` said "not running" while launchd showed a pid. A
    `launchctl submit` job using `/bin/sh` got `Operation not permitted` on a
    file in `~/Desktop` right away (platform binaries get a silent deny, other
    binaries get a dialog).
  - **Fix:** `orch daemon install` now checks, through the new
    `Host.protected_folders()` (MacHost: `~/Desktop`, `~/Documents`,
    `~/Downloads`, `~/Library/Mobile Documents`, `/Volumes`), every path the
    daemon needs (`daemon.launchd_access_problems`): the Python executable
    (resolved), Nightshift's source, config dir, agents_root, templates dir and
    Tracker are blocking, so install refuses with the fix (move the repo
    together with `.python/` and `.venv/`, or allow that Python the folder,
    then `--allow-protected`). Template input files and
    `permissions.read_only_dirs` in a protected folder only warn. `orch
    doctor` lists the same lines under launchd and is "Not ready" on a
    blocking one. The plist also sets `PYTHONUNBUFFERED=1`: under launchd
    stdout is a file, so log lines otherwise sat in Python's buffer.
  - With a copy of the runtime (`.python`, venv site-packages, `src`,
    `templates`) staged outside protected folders, the real install worked:
    the plist passed `plutil -lint`; `launchctl print` showed state running,
    pid 64165, runs 1; `orch daemon status` and `orch doctor` agreed; daemon.log
    had "daemon started (pid 64165)" at once; `claude` resolved on the plist's
    PATH (`~/.nvm/.../bin/claude`, 2.1.307). `kill 64165` led to "daemon
    stopped", then launchd restarted it (pid 64260, runs 2, last terminating
    signal 15).
  - One haiku smoke Task (`orch add "... output/hi.txt" --yes`) ran on the
    launchd daemon with `caffeinate -i -s -w` as a daemon child only while it
    was running, and nothing held afterwards. It ended `done` with
    `output/hi.txt` = "hi", but only after about 23 minutes: claude's start
    raised a Documents-folder TCC dialog attributed to the daemon's Python
    (responsible process), and the Attempt waited until someone answered it
    at the Mac. So **the first launchd-run Tasks may wait on macOS privacy
    dialogs**. Answer them once (or give the Python Full Disk Access) before
    leaving the daemon unattended; an unanswered one holds the Task until its
    runtime cap. The same claude command and settings run from a `/bin/sh`
    launchd job raised no dialog, so which file claude opened in `~/Documents`
    is unknown.
  - After that dialog had been allowed for the repo's Python, `orch daemon
    install --allow-protected` from the repo started the daemon normally.
  - `orch daemon uninstall` booted the agent out (`launchctl print`: could not
    find service) and deleted the plist; no daemon, caffeinate or claude
    process was left, and daemon.log ended with "daemon stopped".
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

## Phase 1, issue #11: resume traceability checks and Gate Test 1

- **New `trace_changes` post-hook** (`nightshift.trace`, registered in
  `templates.HOOKS`), run in `resume-tailor` after the outputs exist and
  before anything is compiled (cheap checks first). Params: `resume`,
  `changes`, `master` (`master` must be one of the template's `files`
  destinations; the hook reads Nightshift's pristine copy in
  `<data>/inputs/<task-id>/`, so neither the agent nor an edit of the real
  `master.tex` after `orch add` can move the line numbers). A failure is a
  Failed Attempt with the first five problems in the reason and every
  problem in `logs/nightshift.log` (`  traceability: ...`), so a retry knows
  what to fix; a pass logs one summary line.
- **What "changed bullet" means.** Rather than trusting the agent's list,
  Nightshift derives the changes itself: a *changed line* is a line of the
  resume's document body, with visible text, that isn't (comment-stripped,
  whitespace-normalised) a line of `master.tex`. Layout-only lines
  (`\begin{itemize}`, `\vspace{-2pt}`) and the preamble don't count; moved
  bullets are verbatim lines, so they need no entry, and removed ones add
  nothing. Every changed line must be covered by a `changes.md` entry whose
  `New text` contains it (after stripping LaTeX/Markdown and case; or
  `difflib` ratio >= 0.85), so a wrapped bullet or one quoted with or
  without its LaTeX still matches. This catches the agent quietly changing a
  line it didn't list, which a check of `changes.md` alone would miss.
- **`changes.md` format** is now spelled out in the prompt (`### N. label`
  then `- Kind:`, `- New text:`, `- Source: master.tex:L<n>[-L<m>]`,
  `- Reason:`). The parser is lenient because haiku's own layouts varied
  (numbered items, `**Source:**` bold labels, notes sections that cite lines
  for unchanged bullets): an entry is a heading or top-level list item whose
  block has a `New text`/`Kind`/`Reason` field or a real `L<n>` citation;
  notes and titles are ignored. Citations are `L<n>` / `L<n>-L<m>` in the
  `Source` field (with or without `master.tex:`). Each entry needs at least
  one, all inside the file, not all blank/comment lines, and a `Reason`.
  The check can't judge whether the new wording means the same as the cited
  line; that's what the diff in the report is for.
- **Numbers**: every number token (`\d+(,\d{3})*(\.\d+)?`, commas dropped,
  so `1,200` == `1200`) in the resume's body must occur anywhere in
  `master.tex`. LaTeX lengths with a unit attached (`10pt`, `0.5in`,
  `-2pt`, `0.9\textwidth`) and comments are skipped; "3 in class" is not a
  length. Years count as numbers too, so a changed date fails.
- **The diff in the report**: new optional template key `report_diffs:
  [{file, against}]` (`against` must be a `files` destination; diffed
  against the pristine copy). `templates.report_appendix(cfg, row)` renders
  each as a `diff` code fence (fence longer than any backtick run in it; cut
  at 400 lines) under `## Appendix (written by Nightshift)` after section 9.
  The heading is unnumbered, so a later finalize reads it as part of section
  9 (Nightshift's) and rewrites it rather than duplicating it.
  `report.finalize(..., appendix=)`; both the runner and
  `failures.finalize` pass it, so a failed resume Task's report shows the
  diff too.
- **Pause log line**: when an Attempt pauses, the runner logs whether
  `progress.md` was updated during that Attempt (mtime compared with its
  start), as evidence for "pauses with a current `progress.md`".
- **fake_claude**: `FAKE_RESUME` now writes `resume.tex` as `master.tex`
  with its first body line reworded plus a matching `changes.md` entry;
  `FAKE_TRACE=uncited|badline|number|unlisted|noreason` breaks one rule;
  `FAKE_PROGRESS=<text>` appends a Checkpoint to `progress.md` before the
  stream. Real-run fixtures: `tests/fixtures/sample_{master,resume}.tex`,
  `sample_changes.md` (a haiku run from the #10 check).
- **Gate Test 1 on this Mac (2026-10-21, 03:52-04:01 PDT)**, scratch config,
  haiku, TeX Live 2026, live Figma SWE intern posting on Greenhouse, sample
  one-page `master.tex` (no real `~/Documents/career/resume/master.tex`
  exists on this machine yet; the gate should be re-run on it once it does).
  Run 1: `orch add "Tailor my resume and a cover letter for <URL>"
  --template resume-tailor`, `orch daemon run --exit-when-idle`: `done` in
  1m 33s, $0.020; 2 changed lines, 2 entries, 12 numbers, all trace; resume
  1 page; the report ends with the diff; the only network tool calls were
  two WebFetch GETs of the posting (nothing submitted). Run 2: same with
  `--simulate-rate-limit-after 10`: paused after 10 Steps with
  `posting.json` and `resume.tex` written and `progress.md` updated
  ("progress.md was updated during this Attempt"), resumed the same session
  after the 5-minute simulated reset, `done`; the same 7 output files, all
  post-hook checks passed (2 changed lines traced, 1 page), $0.020 over 2
  Attempts.
- **Gate Test 1 re-run on the user's real `master.tex` (2026-10-21,
  21:26-21:46 PDT)**, scratch config, haiku, real TeX. It compiles on its own
  to 1 page in the sandbox. No cover-letter template was configured and
  `orch add` refuses without one (the default path doesn't exist), so a
  generic placeholder letter template was used. Run 1 (live Cloudflare SWE
  intern posting, Greenhouse): `done` in 2m 20s, $0.028; 2 changed lines,
  3 entries (one a removed bullet), 38 numbers, all trace; 1 page; only
  WebFetch GETs. Run 2 (live Figma SWE intern posting,
  `--simulate-rate-limit-after 10`): paused, resumed the same session after
  the 5-minute reset, `done` with all post-hooks passing, but the log said
  "progress.md was NOT updated during this Attempt": haiku's only update was
  a `cat > progress.md <<EOF` heredoc, which is auto-denied, and it didn't
  redo it. Two fixes, then a re-run of run 2: `done` in 2m 16s over 2
  Attempts, $0.029 (the stopped Attempt's cost isn't known), "progress.md
  was updated during this Attempt", 3 changed lines + moved blocks, 14
  entries, 40 numbers, all trace, 1 page, GETs only.
  - The agent protocol (rule 2) now says to write files, `progress.md`
    included, with the file-writing tool rather than shell redirection or
    heredocs, and to redo a denied `progress.md` update that way.
  - `trace.unified_diff` now emits diff's `\ No newline at end of file`
    marker. The real `master.tex` has no final newline, and difflib glued
    the next line on (`-\end{document}+\end{document}` on one line), which
    also made the report's "N line(s) added" count wrong.

## Phase 1, issue #12: Schedules with catch-up and overlap rules

- **Two modules, both pure.** `nightshift.cron` parses the timing rule and
  answers `next_after(t)` / `fires_between(after, until)`;
  `nightshift.schedules` holds the §2.5 rules (`apply_catch_up`, `fire`,
  `run_now`, `add`/`remove`/`set_enabled`). Every time comes from the
  caller, so Catch-up and Overlap are unit tested with a fake clock
  (`tests/test_schedules.py`), including through `QueueRunner`.
- **Own cron parser, no dependency.** Standard five fields (`*`, numbers,
  ranges, `/step`, lists, `jan`/`mon` names, `7` = Sunday) plus `@daily`
  etc.; Vixie-cron day rule (two restricted day fields match either; a field
  starting with `*`, even `*/2`, restricts together with the other). Rules
  are wall-clock times in the configured timezone (LA), so 6:30am stays
  6:30am across DST; a gap time (2:30 on spring-forward day) fires just after
  the gap (3:30 PDT), a repeated time (fall back) fires once. A rule that
  never fires (`0 0 31 2 *`) is rejected. `croniter` was not used: small
  enough to own, and its DST behavior would still need testing here.
- **Scheduler state in SQLite** (migration 6, the `schedules` and
  `schedule_runs` tables the spec names). Schedules are made only with
  `orch schedule add` (not config), so the DB is the one source.
  `schedules.last_fire_at` means "every fire up to here is handled"; it
  starts at the moment the Schedule was added, so fires before that never
  count as missed. `schedule_runs` has one row per decision (`created`,
  `replaced`, `skipped_overlap`, `skipped_catch_up`, `error`) with the
  trigger (`start`, `wake`, `poll`, `run_now`), the fire time and how many
  fires it collapsed. It has no foreign key, so history survives
  `orch schedule remove`; `orch schedule list` only shows rows newer than
  the current Schedule of that name.
- **One decision per look, about the latest fire.** All fires in
  `(last_fire_at, now]` collapse into one decision (§2.5: never stack
  duplicates). The Catch-up rule judges the *latest* of them: `within N
  hours` creates one Occurrence if it is at most N hours old, else skips it
  (logged); `next wake` always creates one. So after three days asleep,
  waking at 8am runs today's 6:30 fire once; waking at 7pm skips it and the
  next morning fires normally. An on-time fire is just a fire 0 hours late.
- **Exactly once.** The fires are claimed (conditional `UPDATE ...
  WHERE last_fire_at = <old>`) *before* the Occurrence is created. A crash
  between the two loses that one decision; it can never create a second
  Occurrence for the same fires, and two lookers can't both act.
- **When the daemon looks.** On start (after crash recovery), right after a
  detected wake, and before every queue pick (each poll). Wake detection
  goes through the host seam: `Host.awake_seconds()` (`MacHost`:
  `CLOCK_UPTIME_RAW`, which stops while the Mac sleeps) compared with wall
  time; a gap of 60s or more (`daemon.WAKE_GAP_SECONDS`) is a sleep. Phase 1
  runs one Task at a time in one loop, so a fire during a long Attempt is
  looked at when the Attempt ends; its Catch-up rule still judges it by its
  own fire time (with the posting-scan's 12h and runtime caps of at most
  6h, it still runs).
- **Overlap.** "Unfinished" = any Occurrence of the Schedule not in a
  terminal state (`done`, `partial`, `failed`, `cancelled`), so one paused,
  in `needs_you` or waiting to retry also blocks. `skip`: nothing is created,
  the skip is recorded and written to the unfinished Occurrence's
  `logs/nightshift.log`. `replace`: each unfinished Occurrence is cancelled
  through `failures.cancel` (reason "replaced by a new Occurrence of
  Schedule <name> (Overlap rule: replace)"; a running one under a live
  daemon gets a cancel request, as `orch cancel` does) and a fresh one is
  created. `failures.cancel` gained `why=` for that reason text.
- **Occurrences** are `create_task(..., schedule_name=, occurrence_date=)`:
  pre-approved whatever the template's estimate (§2.4), template settings
  as in `orch add --template` (`templates.load` + `instantiate` +
  `install_inputs`), title `<name> YYYY-MM-DD`, `schedule_name` in the DB
  and `task.yaml`, folder `<agents_root>/recurring/<name>/YYYY-MM-DD/` (the
  fire's date in LA; `Config.recurring_dir`). A second Occurrence on the
  same date (run-now, replace, an hourly rule) gets `YYYY-MM-DD_<id>/`, so
  no two share a folder. Schedule names are `[a-z0-9-]`, up to 40 chars,
  because they name a folder.
- **Validated at add, not at 2am.** `orch schedule add` parses the cron and
  Catch-up rules and loads and fills in the template with the request
  (missing input files, a URL the template needs, a denied domain and an
  unknown agent fail there). If the template breaks later, the fire is
  recorded as `error` (claimed, so no retry storm on every poll) and the
  daemon log says why.
- **CLI.** `orch schedule add NAME --template T --cron RULE --catch-up RULE
  [--overlap skip|replace] [--request TEXT] [--priority P] [--agent A]
  [--disabled]`; `--catch-up` is required (the spec gives no default) and
  accepts `within 12h`, `within:12`, `12h`, `next-wake`. `list` shows each
  Schedule's rules, next fire, last decision (with the Occurrence's state)
  and unfinished Occurrences. `enable` counts fires from that moment: fires
  while a Schedule was disabled are not "missed while the machine slept", so
  they don't catch up. `run-now` fires once now through the Overlap rule
  (a skip is an error exit naming the unfinished Occurrence) and doesn't
  move the cron state. `remove` deletes the Schedule but leaves its
  Occurrences and their folders (it names unfinished ones).
- **Real-machine check (2026-10-21, 04:14-04:21 PDT).** Scratch config,
  haiku, a tiny template estimated at 45 min: `orch schedule add ping
  --cron "14 * * * *" --catch-up "within 12h"` at 04:13, daemon not running
  at 04:14 (the missed fire). `orch daemon run --exit-when-idle` at 04:21
  logged "on daemon start: Schedule ping: fire at 04:14: created Occurrence
  dpx8", which ran on real claude and ended `done` in
  `recurring/ping/2026-10-21/`, pre-approved (`approved: true`,
  `schedule_name: ping` in `task.yaml`). A second daemon start created
  nothing. A real lid-close sleep/wake was not run (it needs the user;
  Nightshift never runs `pmset`); wake detection is covered with
  `FakeHost(awake_clock=...)` and a fake clock.

## Phase 1, issue #13: `posting-scan` template and Tracker merge

- **Template** `templates/posting-scan.yaml`: Task type `scraping`
  (internet on, not long), Claude, estimate 20 min (no plan approval for a
  daily scan), cap 60 min. The prompt carries the §1.8 match and screen-out
  rules, the Tracker's header line and the configured sources. Post-hooks:
  `require_outputs [output/proposed_rows.csv]`, then `merge_tracker`.
- **Config-sourced prompt variables.** New template key `values:
  {var: {config: <dotted key>}}`; a list renders as one `- item` per line,
  empty/missing as `(none configured)`. posting-scan uses
  `templates.posting_scan.{season, tracker_repos, career_pages}` (lists
  default empty: "config lists that I fill in"). Both empty: the prompt
  says write `needs_you.json` and stop (one cheap Attempt) rather than a
  config error, so a Schedule added before the lists are filled is visible
  in the morning instead of silently erroring. A variable defined twice
  (request/inputs/files/values) is a template error.
- **Tracker location**: `tracker.path` (null = `<agents_root>/data/
  applications.csv`, §1.3), `Config.tracker_path`. Outside the data dir,
  its folder is added to every Task's no-access dirs.
- **Merge** (`nightshift.tracker`, deterministic, pure parts testable):
  - *Proposed rows file*: the header must be exactly the Tracker's 12
    columns (any order or case); otherwise the hook fails the Attempt (a
    retry gets to fix it). A row with the wrong field count, or one that
    fails validation, is rejected on its own (logged with its line number,
    counted in the report), not the whole file.
  - *Validation*: `company, role, location, season, posting_url, source`
    required; http(s) URL not on `permissions.denied_domains`; season equal
    (normalized) to `templates.posting_scan.season`; a backstop screen-out
    of role titles saying PhD/MS-only, new grad/entry level, full-time or
    clearance; `date_found` blank -> today, else `YYYY-MM-DD`; a
    non-date `deadline` ("rolling") is moved into `notes` instead of
    rejecting the posting. `id` = next integer after the highest numeric
    Tracker id; `status=new`; `resume_version` empty. Values are flattened
    to one line, capped at 500 chars, and formula-leading values get `'`.
  - *URL normalization* (dedupe key only; the stored URL is the agent's):
    lowercase, `http`=`https`, `www.` dropped, default ports, fragment and
    trailing/double slashes dropped, tracking params dropped (`utm_*`,
    `gclid`, `fbclid`, `ref`, `source`, `gh_src`, `lever-source`, ... see
    `TRACKING_PARAMS`), other params kept and sorted (Greenhouse's
    `gh_jid` is the job id, not tracking).
  - *(company, role, season)*: words lowercased, punctuation dropped;
    company suffixes (Inc, LLC, Corp, Co, ...) dropped; role synonyms
    (internship=intern, engineering/developer=engineer, SWE/SDE=software
    engineer, ML=machine learning, infra=infrastructure); season forms
    `2027 Summer`, `Summer '27`, `SU27` -> `summer 2027`.
  - *Order*: URL first, then the key; against the Tracker and earlier rows
    of the same file. Consequence of the spec's secondary key: one role
    listed per city (separate URLs) is one Tracker row. The merge keeps a
    matching-location listing over a `low-priority location` one (the
    real run had 17 such collapses, e.g. American Express listing one role
    in 7 cities).
  - *Writing*: under an `fcntl` lock file next to the Tracker; the full
    file is written to a temp file, fsynced and renamed over the old one.
    Existing bytes are kept exactly (a missing final newline is added).
    No change -> the file isn't rewritten. A Tracker with other columns
    is never touched (the Attempt fails, retries can't fix it; you can).
- **Merge record, retries.** Migration 7 `tracker_merges` (one row per
  hook run: counts plus the JSON `MergeResult`, appended rows with their
  ids). A second merge for the same Task (another Attempt, e.g. after a
  retry) passes the ids this Task already appended as `own_ids`: matches
  against them are "already appended by this Task", not duplicates and
  not appended twice, and the report still lists them as this Task's New
  postings.
- **Report.** `templates.report_appendix` adds `### New postings` under the
  Nightshift appendix for any template with a `merge_tracker` hook, from
  `tracker_merges` only: a table of every row this Task appended
  (company, role, location, deadline, link, notes), then "Dropped as
  duplicates: N." and, if any, "Rejected as invalid or screened out: N"
  (from the latest merge). No merge -> "The Tracker merge did not run".
  The prompt tells the agent not to list postings in its own sections.
- **Real-machine check (2026-10-21, 04:33-04:54 PDT).** Scratch config,
  real `claude` (haiku), one tracker repo
  (`github.com/vanshb03/Summer2027-Internships`), no career pages, two
  one-off `orch add "Daily posting scan" --template posting-scan` runs
  through `orch daemon run --exit-when-idle`. Run 1 (`partial`, 6m31s,
  $0.16 cost-equivalent): 186 Proposed rows, 169 appended, 17 dropped as
  duplicates (same role listed per city), 0 rejected. Run 2 (`done`,
  13m25s, $0.65): 204 proposed, 30 appended, 174 dropped as duplicates.
  Tracker afterwards: 199 rows, 199 distinct normalized URLs and 199
  distinct keys; run 1's bytes unchanged at the head of the file. Each
  report's New postings section listed exactly its appended rows. The 30
  "new" in run 2 are mostly postings run 1 missed: the agent reads the big
  README through WebFetch's summarizing fetch, so coverage varies from run
  to run (dedupe makes that harmless). Haiku also let a few non-matching
  rows through (e.g. a Toronto co-op, defense contractors) that the
  title-based backstop can't catch. Cost grows with the README size; a
  daily scan of several big repos may want a cheaper source format later.
- **Not done here**: giving the agent a read-only snapshot of the Tracker
  (it would save it re-proposing known postings; dedupe makes it
  unnecessary for correctness). The Gate Test 2 schedule run belongs to
  the Gate work.

## Phase 1, issue #14: the daily posting-scan Schedule and Gate Test 2

- **Standard Schedules live in the template.** New optional template key
  `schedule: {cron, catch_up, overlap, request}` (overlap defaults to
  `skip`, request to empty), validated with the same parsers as `orch
  schedule add`. `templates/posting-scan.yaml` carries the §3.1 item 10
  rules: `30 6 * * *`, `within 12h`, `skip`, request "Daily scan for new
  internship postings". Schedules are still only made by `orch schedule
  add` (the DB stays the one source, #12); the template only supplies
  defaults, so setting up the Phase 1 scan is one command:
  `orch schedule add posting-scan`. `--template` now defaults to the
  Schedule's name, and `--cron` / `--catch-up` / `--overlap` / `--request`
  default to the template's block (any flag overrides it). A template
  without a block still needs `--cron` and `--catch-up` (the spec gives no
  default Catch-up rule). Nightshift never adds the Schedule by itself:
  on the real `~/agents` you run that command once.
- **Gate Test 2, simulated** (`tests/test_gate2.py`): the built-in template
  and its standard Schedule added through the CLI, the daemon's
  `QueueRunner`, the fake `claude` through the real adapter, the real merge
  hook and report. Days pass on a fake clock; the laptop sleeping is a
  `FakeHost` whose awake clock stops while wall time jumps. Covered: the
  6:30 fire on time (poll), a fire missed while asleep caught up exactly
  once on wake (more polls, a second wake check and a daemon restart make
  nothing), three days asleep collapse into one Occurrence, a wake more than
  12h late skips (next morning fires normally), a fire while the previous
  scan is unfinished is skipped, and across the two runs no duplicate
  Tracker rows (by URL and by key) and the second report lists only its
  appended row.
- **Real-machine check (2026-10-21).** Scratch config, real `claude`
  (haiku), the posting-scan template and its standard rules except the
  timing: `orch schedule add posting-scan --cron "6,30 5 * * *"` (two fires
  a day so two runs happen within one morning; Catch-up within 12h and
  Overlap skip from the template). `orch schedule add posting-scan` alone on
  another scratch config showed `30 6 * * *`, `within 12h, else skip`,
  `skip`. Run 1: daemon up at 05:06, on-time fire (trigger `poll`),
  Occurrence `partial` in 7m32s ($0.20 cost-equivalent): 125 proposed, 116
  appended, 9 dropped as duplicates. The daemon was then stopped across the
  05:30 fire (the "laptop asleep" stand-in) and started at 09:52: "on
  daemon start: Schedule posting-scan: fire at 05:30: created Occurrence"
  (4.4h late, inside 12h), `partial` in 6m53s ($0.21): 133 proposed, 17
  appended, 116 dropped as duplicates. Tracker afterwards: 133 rows, 133
  distinct normalized URLs and keys, run 1's bytes unchanged at the head;
  run 2's New postings table listed exactly its 17 appended rows and none
  of run 1's. A third daemon start created nothing (`schedule_runs`: one
  `poll/created`, one `start/created`). Both `partial`s are the agent's own
  verdict (career pages not configured, some posting pages wouldn't load).
  Not run for real: a lid-close sleep and wake (needs the user; covered by
  `tests/test_gate2.py` with `FakeHost(awake_clock=...)`), and the real
  6:30am time (the cron parser and DST handling are tested in #12).

## Phase 1, issue #15: Codex adapter, coding failover and Acceptance Test 3

- **Codex adapter built without Codex** (later verified, see "Verified on
  real Codex" below). The CLI wasn't installed on this Mac at first. `nightshift.adapters.codex`
  implements the whole interface (`build_command`, `resume_command`,
  `parse_stream`, `detect_rate_limit`, `usage_from_run`,
  `write_permissions`, `planning_command`/`parse_planning_output`,
  `probe_command`/`parse_probe_output`) from the Codex docs and the
  openai/codex source (cited in docs/adapters.md, marked unverified, with
  a "Verify once installed" checklist). Tests run it against
  `tests/fixtures/fake_codex.py`, which replays a stream **constructed**
  from the source's event types.
- **All settings as `-c` overrides, user config ignored.** Pointing
  `CODEX_HOME` at a per-Task dir would lose the ChatGPT sign-in, and
  `--profile` only reads from `CODEX_HOME`; `--ignore-user-config` keeps
  auth but drops the user's config, so the policy is passed entirely as
  `-c key=<inline TOML>`. An audit copy goes to
  `data/permissions/<id>/codex-config.toml` (Codex doesn't read it).
- **A permission profile, not `--sandbox`.** `workspace-write` confines
  writes but leaves the whole disk readable; the (beta) permission
  profiles can `deny` paths, so the PermissionPolicy maps one to one:
  `:root` read, home and `agents_root` deny, workspace write, read-only and
  tool dirs read, no-access dirs deny, network off. `--strict-config`
  makes a renamed key fail the Attempt instead of dropping a limit. No
  `--add-dir` (in Codex it means *writable*). The Bash deny-list has no
  per-run Codex equivalent (execpolicy rules are files) and is a
  documented gap; the network block is what matters.
- **Codex reports no cost.** Usage rows get tokens, cost NULL, so Codex
  Baselines can't form (and the issue #21 thresholds never apply to it). Codex's limit
  messages don't name a Window: a hit counts as its shortest configured
  Window (`usage.windows.codex`, assumed 5h + weekly like Claude).
- **Failover is per Task type, in config.** `task_types.<type>.failover:
  {agents: [...], when: always|deadline}` lists the agents a type may run
  on, in order of preference; only `coding: [claude, codex], always` is
  set (Phase 1 does only coding failover). The core names no agent. A Task
  created with `--agent` never moves (an explicit choice). Every switch
  is a `failovers` row (migration 8: from/to agent, trigger, deadline-
  driven, reason), a transition reason, a `nightshift.log` line, the
  Task's `agent` column and `task.yaml` `agent` field; `orch show` lists
  them. `agent` joined `TRANSITION_FIELDS`.
- **When an agent is out** (`usage.agent_out_until`): its Window status
  is `rejected` (set by every real limit hit, Attempt or probe) with a
  reset still ahead; a hit with no reset holds it for one probe interval;
  a probe that goes through clears it. Simulated limits never mark an
  agent out. `pauses.held_agents` now also holds agents that are out this
  way (a Task that failed over leaves no paused Task to hold Claude), and
  the daemon moves queued failover-capable Tasks off a held agent before
  each pick (`failover.reassign_held`), so they don't wait for a reset.
- **Limit hit -> failover.** `run_attempt` now ends a limit Attempt
  through `failover.after_usage_limit`: with an agent of the type free, the
  Task goes `running -> queued` on it at once (`resume_pending` set, no
  retry budget charged; a simulated limit counts, which is what Acceptance
  Test 3 uses). The next Attempt sees the last Attempt ran on another
  agent and starts a **fresh session** with the prompt plus a takeover note
  ("read `plan.md` and `progress.md` first"). `orch pause` never fails over.
- **Both out.** The Task pauses on its own agent (`paused_usage`), but
  `resume_at` becomes the earliest known reset of any of its agents.
  `pauses.promote_due` then asks `failover.agent_on_resume`: if the Task's
  own agent is still out and another has reset, it is requeued on that one
  (trigger `reset`); otherwise the normal path (resume the same session).
  A probe that finds the Task's agent still out also checks the others.
- **Retry with switch.** In `failures.after_failed_attempt`, retry 2 (the
  second retry) moves the Task to the next agent of its type that isn't
  out; retry 1 never switches, nor do types without `failover`.
- **Deadline-driven failovers.** With `when: deadline` a type fails over
  only while its deadline is within `queue.deadline_urgent_hours` (24).
  Such failovers are flagged: `report.finalize(needs_you=...)` puts
  "Deadline-driven failover: ... moved from X to Y because its deadline was
  near. Review the result more carefully." first in section 2 (not
  duplicated on re-finalize). No Phase 1 type uses `deadline` (the
  resume-tailoring row of §2.8 needs Phase 2's Gemini/other targets);
  tests configure it on `resume` to exercise the flag. A coding failover
  (`always`) is not deadline-driven, even for a Task with a deadline.
- **Acceptance Test 3, Claude side for real (2026-10-21).** Scratch config
  (`scratchpad/real15/config`, haiku, `agents.codex.binary` = a wrapper
  around the fake Codex). `orch add "Coding task in three parts ..."
  --type coding --priority urgent --yes --simulate-rate-limit-after 3`
  (urgent so the long-Task window doesn't hold it at 10am), then `orch
  daemon run --exit-when-idle`: real Claude wrote `output/fizzbuzz.py`,
  checkpointed it in `progress.md` ("progress.md was updated during this
  Attempt"), was stopped after its 3rd Step (exit 143), the Task went
  `running -> queued` on codex ("failing over from claude to codex"), and
  the Codex Attempt started at once with a fresh session whose prompt had
  the takeover note; it saw Claude's `progress.md`, kept Claude's file and
  ended `done`. Report section 8 lists both agents. Three such runs, about
  10s and well under $0.01 each; no processes left. The Codex side was
  the fake at that point (real Codex: see below).
- **To finish AT3 on the real CLIs** (after installing Codex and `codex
  login`; first the "Verify once installed" list in docs/adapters.md):
  1. Use a scratch config, e.g. `paths.agents_root: /tmp/at3/agents`,
     `agents.claude.model: haiku`, and leave `agents.codex.binary: codex`.
  2. `export NIGHTSHIFT_CONFIG_DIR=<that dir> PYTHONPATH=<repo>/src`
  3. `orch add "Coding task in three parts. Part 1: write
     output/fizzbuzz.py (FizzBuzz 1..30) with Write, then immediately record
     it in progress.md with Edit. Part 2: write output/test_fizzbuzz.py with
     asserts, then record it in progress.md. Part 3: write output/README.md,
     then record it in progress.md. Don't run any commands." --type coding
     --priority urgent --yes --simulate-rate-limit-after 3`
  4. `orch daemon run --exit-when-idle`
  5. `orch show <id>`: passes if Attempt 1 is claude `paused` after 3
     Steps, Failovers shows `claude -> codex (simulated)`, Attempt 2 is
     codex `completed` with no `resume` in its command (`orch logs <id>`),
     the Task is `done`, all three files exist in `output/`, `progress.md`
     has Claude's Part 1 entry and Codex's later ones, and `report.md`
     section 8 lists both agents.
  6. If Codex fails to start or rejects a setting, `logs/attempt-2.codex.
     stderr.log` says why; fix the adapter per docs/adapters.md.
- **Verified on real Codex (codex-cli 0.163.0, 2026-10-21).** Flags, `-c`
  keys under `--strict-config`, the `--json` stream, the permission profile
  and resume by thread id all behaved as documented (details and evidence
  in docs/adapters.md "Verification log" and docs/safety.md). Changes:
  - **Default-on features off.** 0.163.0 enables computer use, browser
    use, the in-app browser, ChatGPT apps, plugins and image generation by
    default; every Codex run now sets them false, and `extra_args` refuses
    the new `--enable`/`--disable`/`--search`/`--remote`/... flags.
  - **Cumulative usage.** `turn.completed` reports the thread's running
    total (seen on a real resume), so a resumed Attempt subtracts the total
    its session had reached (`adapter.prior_usage`, set by the runner from
    the earlier Attempts' `usage_events.raw`).
  - **`agents.codex.reasoning_effort`** (null = model default) ->
    `-c model_reasoning_effort=...`. The checks used `gpt-6-luna` + `low`.
  - `tests/fixtures/codex_stream.jsonl` is now a real recording (AT3's
    Codex Attempt, folder prefix stripped).
  Not verified: a real Codex usage-limit message (none happened) and a
  planning call with `--output-schema` on Codex.
- **Acceptance Test 3 passed on the real CLIs (2026-10-21).** Scratch config
  (`scratchpad/realcx/at3/config`: haiku, `gpt-6-luna`, effort low), the
  steps above. Task `xpub` (`--simulate-rate-limit-after 3`): Attempt 1
  claude `paused` after 3 Steps (exit 143), Failovers `claude -> codex
  (simulated)`, Attempt 2 codex `completed` (exit 0, no `resume` in its
  argv, "fresh session from plan.md + progress.md (failover from
  claude)"), Task `done`, all three files in `output/`, report section 8
  lists claude (7s) and codex (27s, 13k input + 158k cached, 2k output
  tokens). Claude's 3 Steps there were Read, Read, Write, so it never wrote
  progress.md and Codex redid Part 1. A second run, `ivac` with
  `--simulate-rate-limit-after 5`, shows the real hand-off: Claude wrote
  `fizzbuzz.py` and its progress.md entry ("progress.md was updated during
  this Attempt") and then the test file; Codex `cat plan.md progress.md`,
  kept Claude's `fizzbuzz.py`, wrote the test and README, recorded both in
  progress.md and ended `done`. Each run was about 35s; both agents are
  on subscriptions (no dollars spent); no processes left.

## Phase 2, issue #16: views split into data queries and CLI rendering

- **`nightshift.queries` returns the data, `nightshift.views` renders it.**
  `task_list`, `task_detail` (with Attempts, approvals, Failovers and
  history), `usage_report` (per agent and Window) and `schedule_list`
  return frozen dataclasses; the CLI passes them to `views.format_*`. The
  dashboard (§4 item 8) is meant to call the same queries.
- **Effective priority and `blocked` are None when they don't apply.**
  `effective_priority` is None for a finished Task, and `blocked` is None
  unless the Task is queued, so a client can't show a stale "Waiting".
- **`TaskDetail.task` stays a dict of every stored column.** `orch show`
  prints the columns generically (`SHOW_FIELDS`), and a typed copy of
  ~30 columns would need updating with every migration. If the dashboard
  needs typed settings, they can be added then.
- **Output is unchanged, checked against the old code.** A throwaway pytest
  plugin rendered every test's database with the old `views.py` and the
  new query + render path, at three different times: 2016 comparisons
  over 153 tests, no differences.

## Phase 2, issue #18: Telegram alerts (outbound only)

- **Alerts come from the `transitions` table, not from the code that moves
  Tasks.** Every way a Task can end (the runner, retries running out, a
  crash in Nightshift, an orphaned Task at start) already records a
  transition, so one reader covers them all and no new path can forget to
  alert. A cursor in the new `meta` table remembers the last transition
  read; on the first start it begins at the newest one, so enabling
  Telegram doesn't replay history.
- **Outbox, then send.** Each alert is written to the `alerts` table (text
  already redacted) and then sent from a daemon thread with its own DB
  connection. A Telegram outage or hang only stalls that thread; the alert
  is retried with backoff and dropped (and logged) once older than
  `telegram.alert_max_age_hours` (12), since the 8am digest (#27) covers
  the night anyway. The queue kicks the thread after each Attempt, so a
  finished or failed Task alerts within a second; Tasks moved by the
  scheduler or the CLI alert on the next `daemon.poll_seconds` tick. A
  stopping daemon sends what is pending before it exits (bounded at 2s).
- **Which transitions alert.** done/partial (finished, with the report's
  one-line Result: the agent's own line, else Nightshift's verdict),
  failed (with the reason), needs_you (with the first Needs you entry),
  and paused_usage → awaiting_approval (a pause over 72h needs you to
  re-approve: §2.12's "paused and needs me"). retry_wait, usage pauses,
  cancels and everything else are silent.
- **Comedy is decided by Task type** (`comedy`): its alerts carry the id
  and state only, since the title, Result, reason or request may quote
  material.
- **Stdlib `urllib`, no Telegram library.** Two Bot API methods don't need
  a dependency, and owning the request code means errors are built from
  the method and status, never from the URL that holds the token.
- **Incoming updates are long-polled and all ignored** (commands are
  Phase 3). The offset is stored in `meta` so a restart doesn't log the
  same strangers again. Telegram user ID is config (`telegram.user_id`),
  not a secret, but it is redacted like one.

## Phase 2, issue #19: Gemini failover and retry-with-switch to Gemini

- **Config only; the mechanism is #15's.** `failover.py` already moved a
  Task along its type's `task_types.<type>.failover.agents` list, so the
  §2.8 table is entirely in `defaults.yaml`:
  - `research`, `scraping`, `summary`, `study`: `[claude, gemini]`,
    `when: always`.
  - `resume`: `[claude, gemini, codex]`, `when: deadline` (waits for
    Claude's reset unless the deadline is within
    `queue.deadline_urgent_hours`; then Gemini, or Codex if Gemini is also
    out; flagged in the report's Needs you).
  - `comedy`, `general`, `job`: no failover, so they always wait for
    their agent.
  The second retry of a Failed Attempt switches along the same list (the
  first never does); for `resume` only while the deadline is near.
- **New Task type `summary`** (long-document and lecture summaries, §1.1's
  Gemini row), internet off since the material is local. Planning offers it
  automatically (the type list comes from config).
- **`resume` is gated by `queue.deadline_urgent_hours`**, the same
  setting that makes a Task urgent in the queue (#15's choice): changing
  it moves both.
- **A Task on Gemini that runs out** moves to the next agent of its list
  that isn't out (Claude first). That's the same rule; nothing Gemini-
  specific.
- **Tests:** `tests/test_gemini_failover.py` runs each row through the
  queue with the fake Claude, Gemini and Codex CLIs.
  `tests/test_gemini_real.py::test_claude_to_gemini_failover` is the real
  Claude -> Gemini check (opt-in, needs both CLIs signed in).
- **Real run, Claude side only (2026-10-22).** Gemini CLI isn't installed
  on this Mac yet, so the real test ran with real Claude (haiku) and a
  `gemini` wrapper around `tests/fixtures/fake_gemini.py` on PATH, with
  `--simulate-rate-limit-after 5`: Claude wrote `output/part1.md`, recorded
  it in `progress.md`, wrote `output/part2.md` and was stopped; the Task
  went `running -> queued` on gemini (trigger `simulated`), and the Gemini
  Attempt started a fresh session (no `--resume`) whose prompt had the
  takeover note, saw Claude's `progress.md`, and the Task ended `done` with
  both agents in the report. About 10s. The test's last check (all three
  part files) needs the real Gemini: docs/adapters.md item 8.

## Phase 2, issue #20: Gemini API path, spend and the $2 per-Task cap

- **The paid path is the Gemini CLI with an API key, not a second agent
  implementation.** Agent `gemini-api` (`GeminiApiAdapter`) reuses the
  Gemini adapter: same flags, settings, policy and sandbox, plus
  `security.auth.selectedType: gemini-api-key` and the key in
  `GEMINI_API_KEY` (env only). The plan path now pins `oauth-personal` and
  blanks `GEMINI_API_KEY`/`GOOGLE_API_KEY`, so a key in your shell can't
  silently bill credits (the CLI picks the configured type before env, and
  `GOOGLE_API_KEY` would win over `GEMINI_API_KEY`). It runs Tasks given
  `--agent gemini-api`; when Nightshift itself picks credits is the
  paid-credit rule (#26).
- **Paid calls come from the CLI's local telemetry file.** stream-json only
  has session totals at the end, so the cap couldn't stop a running
  Attempt. With `GEMINI_TELEMETRY_ENABLED/TARGET=local/OUTFILE` (env, which
  beats every settings layer; 0.65.0 has no telemetry flags) the CLI writes
  one `gemini_cli.api_response` log record per model call, with
  `input_token_count` (prompt, cached included), `cached_content_token_count`,
  `output_token_count`, `thoughts_token_count` and `tool_token_count`
  (`ApiResponseEvent` in the 0.65.0 source). Records are pretty-printed JSON
  with spans and metrics in between; the adapter reads them incrementally
  (`paid_calls()`). Prompt logging is off. The file is in the Task's
  agent-inaccessible settings folder, fresh each Attempt.
- **Nightshift prices each call** from `spend.gemini_api_prices` (USD per
  million tokens: uncached prompt and tool-use prompt at `input`, cached at
  `cached_input`, output and thinking at `output`; unknown models at
  `default`, the dearest). The values are placeholders; where a model has a
  long-context tier the higher price is used so spend errs high. One
  `spend_events` row per call (migration 10).
- **The cap is checked while the Attempt runs.** A watcher thread (own DB
  connection) collects calls every 0.5 s, records them, and stops the
  Attempt as soon as the Task's total reaches `spend.per_task_usd`; one last
  pass after the CLI exits collects what it flushed at shutdown (the CLI
  flushes telemetry on SIGTERM), recording it without turning an Attempt
  that finished on its own into a stop. A Task already at the cap doesn't
  start the CLI at all. The Task
  goes `running -> needs_you` (Attempt outcome `spend_cap`) with the reason
  in the transition (so the Telegram alert says it) and in the report's
  Needs you. Known gap: the CLI's log batch delay (~5 s) means a call or two
  may land after the cap, and a CLI that ignores SIGTERM for 15 s is
  killed before it flushes, so its last batch is lost. The cap is per
  Task, all Attempts.
- **No planning calls or probes on the paid path**, and its quota/credit
  errors are not usage-Window hits (there is no plan to wait for): they're
  Failed Attempts, retried like crashes. `orch usage` doesn't list Windows
  for it; it shows a credits block instead: spend so far, and
  `plans.gemini.api_credit_balance_usd` minus the spend recorded after the
  day `api_credit_balance_as_of` (new key; that day's spend is taken to be
  in the balance, local dates), zero once `api_credit_expiry` has passed.
  Shown when `gemini-api` is configured or any spend exists.
- **Not verified on the real CLI yet** (no key on this Mac); the fake
  `gemini` writes telemetry records shaped from the source. To check: a real
  `--agent gemini-api` Task records one row per call, and the costs match
  AI Studio's billing page.

## Phase 2, issue #21: proactive usage thresholds and reset countdowns

- **Rules** (`usage.threshold_holds`, recalculated before every pick with
  the daemon's clock). For each configured agent and each Window that has a
  Baseline: percent = cost-equivalent recorded in the Window's current
  period (`usage.current_window`) / Baseline. At
  `usage.stop_starting_percent` (80) or more, no new Task starts on that
  agent. Inside `usage.daytime_reserve` (`08:00`-`23:00` in `timezone`,
  start inclusive, end exclusive) agents listed there (`[claude]`) start
  no Task whose **Effective** priority is background at
  `background_max_percent` (75) or more. A background Task whose deadline
  made it urgent is not held. A Window with no Baseline never holds
  anything: only actual limit errors pause, as before.
- **Holds only gate starts.** They are checked in `pick_next`, never while
  an Attempt runs, so a running Task finishes even if its own usage
  crosses a threshold.
- **No failover on a threshold.** Queued Tasks on a held agent wait; they
  don't fail over the way they do when the agent is actually out (§2.8).
  The agent still has usage left (that's what the reserve is for), and the
  wait ends when usage ages out of the Window or it resets. An actual
  limit hit still fails over as before.
- **`orch usage`** adds two columns per Window: OF BASELINE (the percent
  above, `-` with no Baseline) and RESETS IN (countdown to the reset the
  agent last reported, `not known` when none is ahead; a Window with an
  unknown reset is measured as the trailing `usage.windows` length), and a
  line saying why new (or background) Tasks are held, if they are.
- **Codex and Gemini report no cost**, so they never form a Baseline and
  the thresholds never apply to them (documented in `docs/adapters.md`).
- Tested with fixed datetimes for the rules and the view, and with the fake
  clock through the daemon (a running Task crossing 80% finishes, the next
  waits; a background Task waits at 10am and runs at 11pm).

## Phase 2, issue #22: `posting-scan` on Gemini

- **Template only.** `templates/posting-scan.yaml` now says `agent:
  gemini` (the subscription sign-in, not `gemini-api`: a daily scan
  shouldn't spend credits). Nothing else changed: the prompt already asks
  for "your web fetch and search tools", which on Gemini are `web_fetch`
  and `google_web_search` (added for internet-on Tasks; the LinkedIn deny
  rules come with them), and the merge, dedupe and New postings section
  are Nightshift's, so they don't depend on the agent.
- **Failover back to Claude.** `scraping` fails over along `[claude,
  gemini]`, so a scan that finds Gemini out moves to Claude (the next agent
  on the list that isn't out) instead of waiting. The Schedule itself is
  unchanged (6:30am, catch-up within 12h else skip, overlap skip).
  An existing Schedule picks the agent up from the template when it fires,
  unless it was added with its own `--agent`.
- **Tests.** The posting-scan tests in `tests/test_tracker.py` and Gate
  Test 2 (`tests/test_gate2.py`) now run through the fake `gemini` CLI
  (which writes `FAKE_PROPOSED` to `output/proposed_rows.csv`, as the fake
  `claude` does): two scans in a row add no duplicate Tracker rows and the
  second reports only its New postings. A new test covers a scan that hits
  a Gemini usage limit and finishes on Claude with the merge intact.
- **Not run on the real CLI yet**: Gemini CLI isn't installed on this Mac.
  `tests/test_gemini_real.py::test_posting_scan_on_gemini` is the opt-in
  check (`NIGHTSHIFT_REAL_AGENT=1 NIGHTSHIFT_SCAN_REPO=<repo URL>`); its
  logic was dry-run with a `gemini` wrapper around the fake on PATH.

## Phase 2, issue #23: `company-rescrape` Schedule

- **Template** `templates/company-rescrape.yaml`: `scraping` on Gemini,
  `long: true` (so it starts only in the midnight-7am window unless
  urgent), internet on, 30 min estimate (no plan approval for a one-off
  `orch add --template company-rescrape`), 120 min cap. Its standard
  Schedule is `0 2 */2 * *`, catch-up `next-wake`, overlap skip.
  `*/2` in the day field is days 1, 3, ..., 31, so a 31-day month fires on
  the 31st and the 1st: plain cron, accepted rather than adding an
  "every N days" rule to the cron parser.
- **Pages come from `templates.posting_scan.career_pages`**, the one list
  of the 15 target companies (§1.8), shared with posting-scan rather than a
  second copy to keep in sync.
- **What the agent writes.** `output/career_pages.json` (per page: url,
  company, loaded, error, and every internship posting listed, matching or
  not) and `output/proposed_rows.csv` (the §1.8 matches, Tracker columns).
  The agent never sees the previous snapshot and doesn't decide what is
  new: snapshots and the Tracker live in `data/`, which agents can't reach.
- **Snapshot and diff: `snapshot_pages` post-hook** (`nightshift.snapshots`),
  Nightshift code. Writes
  `data/snapshots/company-rescrape/<UTC stamp>_<task id>/{snapshot.json,
  diff.json, diff.md}`; a later Attempt of the same Task replaces its folder
  and is never diffed against itself. Each page is compared, by normalized
  posting URL, with the latest earlier snapshot where that page loaded: a
  page that fails to load is `failed` (its postings don't count as removed)
  and is compared with its last good snapshot when it loads again; a page
  never seen loading is `first`. `loaded` must be a real true/false (a
  guessed one would diff an unread page as every posting removed). A bad
  listing file is a Failed Attempt, before the merge runs. The diff files
  sit apart from `snapshot.json` so Phase 4 retention can delete snapshots
  and keep diffs (§2.13).
- **New postings still come only from the Tracker merge**, not from the
  diff: a matching posting already in the Tracker is not new even if it is
  new on the page, and one missed by an earlier run is still added. The
  report has the usual New postings section, then a Career pages snapshot
  section with counts only (added/removed since Task X, pages that failed,
  path to `diff.md`), so postings are listed only as New postings.
- **Catch-up on wake** is the existing Schedule machinery: missed fires
  collapse into one Occurrence when the daemon sees the wake. Being long,
  that Occurrence waits for the overnight window (the scheduled 01:55 wake,
  §1.9, normally makes the 2am fire run on time).
- **Tests.** `tests/test_snapshots.py` (parse, diff, failed pages, reruns);
  `tests/test_company_rescrape.py`: two runs through the fake `gemini` CLI
  (which now writes `FAKE_PAGES` to `output/career_pages.json`, as the fake
  `claude` does), a bad listing, long-task gating, and the Schedule on the
  fake clock (missed 2am fire caught up once on wake and run at 00:45; three
  missed fires collapse into one). Not run on the real Gemini CLI (not
  installed on this Mac).

## Phase 2, issue #24: dashboard tracer (Tasks grouped by status)

- **Frontend: HTMX, over server-rendered Jinja2 pages.** Single user, read
  only and phone-first: every view is a query from `nightshift.queries`
  rendered as HTML, so the dashboard stays in Python with no Node build,
  bundle or second copy of the data model in TypeScript. The live parts
  coming next (log tail, usage countdowns, then Phase 3's buttons) are
  partial swaps and polling, which HTMX does with attributes. This tracer
  needs no script at all (a 30 s meta refresh), so `htmx.min.js` gets
  vendored into the package (no CDN: the page must work with only the
  tailnet) by the first view that swaps a fragment.
- **Served by its own launchd agent** (`orch dashboard install`, label
  `com.nightshift.dashboard`, KeepAlive, log `data/dashboard.log`), not
  inside the daemon. A web server crash or hang can't stall the queue, the
  daemon can restart mid-night without dropping the page, and the
  dashboard reads SQLite through its own connection (WAL), one per request.
  The plist is the daemon's (`daemon.launch_agent_plist`), and install
  refuses the same privacy-protected paths.
- **Binds 127.0.0.1 and the Tailscale IPv4 only.** Addresses aren't
  configurable: `dashboard.tailscale` (default true) adds the address from
  `tailscale ip -4` (Homebrew CLI or the one in Tailscale.app, behind the
  host seam). Nightshift binds the sockets itself and hands them to
  uvicorn, after a guard (`check_bind_address`) that refuses anything not
  loopback or that exact address inside 100.64.0.0/10, so a probe that
  returned a LAN address can't open it up. Tailscale wanted but not
  connected: refuse to start rather than quietly serve localhost only;
  under KeepAlive it comes up once Tailscale does. IPv6 loopback isn't
  bound (use `127.0.0.1`, not `localhost`, if a browser tries `::1` first).
- **Host header allowlist** (`localhost`, `127.0.0.1`, the Tailscale
  address, `*.ts.net`): anything else gets a 400, so a web page that
  rebinds its own name to 127.0.0.1 can't read Task titles. Short MagicDNS
  names (`http://mac:8765`) are refused; use the IP or the full name.
  `proxy_headers` off, no OpenAPI or docs routes; only `GET /` exists.
- **Groups** (`queries.task_groups`): running; queued (+ `retry_wait`,
  `scheduled`); paused (usage, spend); needs you (+ `awaiting_approval`,
  which also waits on you); done (+ `partial`, `cancelled`); failed. Every
  state is in exactly one group, and a Task whose state isn't the group's
  name shows it (e.g. "retry wait" under Queued). Done and failed show the
  newest 20 with "N older not shown"; open groups show everything.
- **Tests** (`tests/test_dashboard.py`): bind addresses and the guard (0.0.0.0,
  `::`, LAN, public, a foreign 100.64/10 address), real sockets on
  127.0.0.1, the routes through FastAPI's TestClient (grouping order,
  escaping, Host header, read-only), the plist and `orch dashboard
  install|uninstall|run` on FakeHost. Smoke-run for real on localhost only:
  Tailscale isn't installed on this Mac, so the Tailscale bind is untested
  on a real interface.

## Phase 2, issue #25: daily and monthly spend caps → `paused_spend`

- **What counts.** Every `spend_events` row (only paid paths write them),
  dated in America/Los_Angeles whatever offset it was written with. A day
  is local midnight to midnight; a month is the calendar month. The rule
  (`spend.spend_pause`) is one function of the clock, so the daemon,
  the spend watcher and `orch usage` agree, and the tests drive it with a
  fake clock.
- **The month wins over the day.** At both caps the pause is the monthly
  one, since midnight alone would not lift it.
- **Who pauses.** Only Tasks on a paid agent (`adapters.is_paid`). Before
  every pick the daemon moves queued paid Tasks to `paused_spend` (a new
  `queued -> paused_spend` transition), so each gets a recorded reason
  rather than waiting silently in `queued`. A running paid Attempt is
  stopped by the spend watcher as soon as a recorded call reaches a cap,
  at once like the per-Task cap (letting the Step finish would mean more
  paid calls). It is not a Failed Attempt; `resume_pending` is set, so the
  next Attempt continues the same session. `run_attempt` also refuses to
  start a paid call while a cap holds. Calls flushed after the agent
  exited on its own are recorded but never turn a finished Task into a
  pause (as with the per-Task cap). The per-Task cap is checked first.
- **Resuming.** Before every pick, `spend.promote_paused` queues
  `paused_spend` Tasks again, in resume order, as soon as no cap holds:
  midnight for the day; for the month, a raised cap or the 1st. `resume_at`
  holds midnight or the 1st, for display. A Task whose deadline passes
  while paused is cancelled and reported, as for usage pauses. The 72-hour
  re-approval rule is not applied: `paused_spend` has no way to
  `awaiting_approval` in the state machine, and a monthly pause can last
  weeks by design.
- **Raising the cap.** `orch raise-cap USD` stores the raise in `meta`
  (`spend.month_cap.YYYY-MM`), so the running daemon sees it at once (it
  loads `config.yaml` only at start) and it lapses with the month. It must
  be above the current cap. `orch resume` refuses `paused_spend` Tasks and
  says how they resume.
- **The "needs you" request** is the Telegram alert made from the
  `paused_spend` transition whose reason names the monthly spend cap
  (`spend.MONTHLY_CAP`), with the reason as its detail (it says to run
  `orch raise-cap USD`). The Task stays `paused_spend`, not `needs_you`:
  it resumes by itself once the cap is raised. Daily pauses stay silent,
  like usage pauses.
- **Known gaps.** The request goes out when a paid Task is paused, so a
  month crossed by calls flushed after the last paid Task finished raises
  it only once the next paid Task is queued (nothing paid is waiting until
  then), and each paused paid Task alerts once. A usage limit or `orch
  pause` already pending when a cap stops an Attempt wins (the Limit
  observation is kept); the Task is paused on spend before its next pick
  if the cap still holds. A cap that holds when `run_attempt` starts
  records an Attempt that launched nothing, as the per-Task cap does; the
  daemon's pre-pick pause makes that a race only.

## Phase 2, issue #26: paid-credit rule (subscription first)

- **Which subscription.** A paid path names the subscription it stands in
  for (`AgentAdapter.subscription`; `gemini-api` → `gemini`). The rule
  (`nightshift.paid`) only looks at Tasks whose *own* agent is such a
  subscription; Claude and Codex have no paid path, so their Tasks fail
  over or wait as before.
- **"The subscription quota is gone"** means the agent is out of usage:
  its Window status (`usage.agent_out_until`), a Task paused on its limit,
  or the Task's own usage pause (a simulated limit counts for that Task
  only, as for failover). A proactive threshold (80%, daytime reserve) is
  not "gone": nothing moves to paid credits for it.
- **"The next reset"** is the latest known end of that outage. With no
  reset time given it is the next probe: the earliest the agent can be
  back. So a Task with an unknown reset keeps waiting while the probe time
  stays inside its Catch-up window, and moves to paid credits once it no
  longer does. It still starts inside the window.
- **"The end of the Catch-up window"** exists only for an Occurrence of a
  `within N hours` Schedule: the fire time of the Schedule decision that
  created it plus N. `run on next wake`, a removed Schedule and a
  free-form Task have no window, so for them only urgency (Effective
  priority, so a deadline within 24 hours counts) qualifies.
- **Subscription first.** The daemon applies the rule before every pick,
  after `failover.reassign_held`, so another subscription that isn't out
  always gets the Task first. A Task that has failed over (any `failovers`
  row other than the rule's own) never uses paid credits: it waits, like
  any Task whose agents are all out (SPEC §7, resolved). A Task with
  `--agent` never moves, as for failover.
- **Spend caps apply.** The rule doesn't move a Task while a daily or
  monthly cap holds, once it is at its per-Task cap, or when the paid path
  has no API key. It waits on its subscription instead of sitting in
  `paused_spend`, and comes back on whichever happens first: the reset or
  the cap lifting.
- **The move** is a failover row (trigger `paid_credit`). A queued Task
  stays queued; a Task paused on usage is queued at once, and its next
  Attempt starts a fresh session from `plan.md` + `progress.md`, like any
  switch of agent. **Back to the subscription:** once the subscription is no
  longer out, a Task the rule moved goes back (trigger
  `subscription_reset`) if it is queued or `paused_spend`. A running one
  finishes on paid credits.
- **Logged and reported.** Every decision is a `paid_credit_decisions` row
  (migration 11). A waiting decision is recorded again only when its
  verdict or rule changes, not on every pick. It is also a line in the
  Task's `logs/nightshift.log` and in report section 8, under the usage
  table, which now shows each paid agent's real dollars instead of
  "$0.00 (subscription)".
- **Tests** (`tests/test_paid_credits.py`, fake clock and the fake `gemini`
  CLI): urgent and can-wait Tasks, an Occurrence whose reset falls before
  or after its window, `run on next wake`, a Task that failed over from
  Claude to an out Gemini, a limit hit mid-run continuing on paid credits
  (with the report), the daily cap, no API key, `--agent`, and the move
  back from queued and `paused_spend`.
