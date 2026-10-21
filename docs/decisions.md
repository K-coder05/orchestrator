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
