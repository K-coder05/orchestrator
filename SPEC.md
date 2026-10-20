# Nightshift — Build Spec

> **Instructions for Claude Code**
>
> **CURRENT PHASE: 1.** Build only the current phase. Do not start work listed under a later phase, even if it looks easy. Do not implement anything listed under "Open decisions" (§7) until it is resolved in this file.
>
> - If something in this spec is ambiguous or contradicts itself, stop and ask. Do not guess.
> - If a CLI flag, output format or error message named here doesn't match the installed tool, trust the tool (`--help`, real output), record what you found in `docs/adapters.md`, and tell me.
> - Never run `sudo` or `brew`, and never install anything system-wide. Project-local `pip`/`npm` installs are fine.
> - Every phase ends with its gate tests passing on real data. I run the gate; you make sure the tests and tooling exist to run it.

---

## 0. What Nightshift is

Nightshift is a single-user orchestrator that runs on my MacBook. It queues, schedules, runs and reports on tasks done by AI coding/research agents. The agents are driven through their command-line tools in headless mode. The point is to push recurring grunt work into the overnight window so daytime usage stays free for interactive work.

It does three jobs:

- **Recruiting:** find new internship postings, tailor my resume and cover letter per posting, and keep my application tracker current.
- **School:** turn lecture material and deadlines into study plans and practice problems. It never does or submits graded work.
- **YouTube comedy content:** content I create on my own time, from my own video ideas. Agents punch up scripts, research a premise, and draft video descriptions and captions. Nothing involving live shows or in-person performance is in scope.

The core promise: I give it a task, walk away, and come back to a finished `report.md`. If a usage limit is hit, the task pauses cleanly and resumes when the plan resets.

### Agents

| Agent | Access | Default jobs | Arrives in |
|---|---|---|---|
| Claude Code | Claude Pro subscription (may upgrade to Max 5x; plan tier is config) | Coding, resume and cover-letter tailoring, YouTube comedy writing, final report writing | Phase 1 |
| Codex | ChatGPT Work plan (plan tier is config) | Takes over coding tasks when Claude is out of usage | Phase 1 |
| Gemini CLI | Google AI Pro subscription; plus AI Studio API credits (balance and expiry are config) | Web research, scraping and diffing pages, long-document and lecture summaries | Phase 2 |

The orchestrator picks the agent by task type. `--agent <name>` overrides it on any task.

---

## 1. Fixed facts and constraints (all phases)

### 1.1 Machine
- 14" MacBook Pro, Apple M3, 16 GB RAM, ~150 GB free, macOS 26. **These are placeholders and defaults only.** Store them in config and never hard-code them. `orch doctor` reads the real values from the machine.
- It's a laptop. It sleeps in a backpack roughly 9am–6pm on weekdays. Most nights it sits plugged in at home, lid open, roughly midnight–8am.
- Nightshift must survive sleep and missed runs. It holds the machine awake with `caffeinate` **only while a task is running**, and releases it when nothing runs.
- Installed by me: Claude Code, Codex CLI, Gemini CLI, Node 22, Python 3.12, git, Homebrew, VS Code. I will also install the TeX toolchain myself, plus anything else listed in §1.9. No Docker until Phase 4.

### 1.2 Stack
- Python 3.12 core, FastAPI, SQLite.
- Dashboard frontend: HTMX or a small React app. Pick one, explain why in `docs/decisions.md`, then stick with it.
- macOS `launchd` keeps the daemon running.
- Claude Code may choose other libraries if it explains why in `docs/decisions.md`.
- Budget: $0 beyond existing plans and the API caps in §1.5. Everything runs locally except the Telegram Bot API and the agents' own services.
- Single user. Keep all personal config and secrets out of the repo, so it can be open-sourced later as a portfolio project.

### 1.3 Locations

| Path | Purpose |
|---|---|
| `~/code/nightshift/` | Repo: code, built-in templates, tests, `SPEC.md`, `config.example.yaml`, `docs/` |
| `~/.config/nightshift/config.yaml` | My config, not in the repo |
| `~/.config/nightshift/.env` (mode `0600`) or macOS Keychain | Secrets: API keys, Telegram bot token, calendar iCal URLs. Never committed, never logged, never written into reports |
| `~/agents/` | Task data only (no code) |
| `~/agents/tasks/YYYY-MM-DD_<slug>_<id>/` | One folder per one-off task run. `<id>` is 4 lowercase base36 characters, unique. Example: `2026-10-20_resume-acme-swe-intern_k3f9/` |
| `~/agents/recurring/<schedule-name>/YYYY-MM-DD/` | One folder per recurring run |
| `~/agents/data/nightshift.db` | SQLite database |
| `~/agents/data/applications.csv` | Application tracker. Nightshift owns it; I'll sync it to a Google Sheet later (not in scope) |
| `~/agents/data/snapshots/` | Scrape snapshots and diffs |
| `~/agents/archive/` | Archived outputs and zipped logs (Phase 4) |

**Every task folder contains:**
- `task.yaml`: the request and settings
- `plan.md`
- `progress.md`: checkpoint notes
- `output/`
- `logs/`
- `report.md`

### 1.4 Agent folder access

| Folder | Agent access |
|---|---|
| The task's own folder | Read and write (this is the agent's working directory) |
| `~/Documents/career/` (includes `resume/master.tex`) | Read only |
| `~/Documents/comedy/` (my YouTube comedy material) | Read only |
| `~/school/` (slides, syllabi) | Read only |
| `~/agents/data/` | **No direct agent access.** Nightshift code reads and writes it on the agent's behalf (see §1.7) |
| `~/.ssh`, Keychain, browser profiles, everything else | None |

**Internet:**
- On by default for research, scraping and job tasks.
- Off by default for coding and comedy tasks, unless the task turns it on (`--internet on`).

Enforce these limits with each agent's own permission, sandbox and allowlist settings, generated per task. **No blanket skip-permissions mode outside a sandbox.**

In `docs/safety.md`, list which limits are *technically enforced* and which are *instruction-only*. For example, Bash network access may not be fully blockable without Docker. Be honest about gaps; don't paper over them.

### 1.5 Money and usage

**Paid-per-use spending caps.** These only apply to Gemini API credits until another paid source exists.

| Scope | Cap | When hit |
|---|---|---|
| Per task | $2 | Stop the task, notify me |
| Per day | $5 | Pause all paid calls until midnight |
| Per month | $15 | Pause all paid calls, ask me to raise the cap |

**Paid-credit rule.** Always use the subscription first. Spend API credits only when both are true:
- the subscription quota is gone, and
- the task is urgent, **or** the quota's next reset falls after the end of the task's catch-up window.

**Claude usage windows.** Claude has a rolling 5-hour window plus a weekly cap. I don't know the exact numbers. **Codex (and Gemini, from Phase 2) are tracked the same way:** learned baselines, reset detection and probing, each with its own windows.
- Nightshift learns a **baseline** for each window from real limit hits. The baseline is the usage it recorded in that window up to the moment of the hit.
- Use the **lowest of the last 3 observations** (conservative).
- Until a baseline exists, pause only on actual rate-limit errors.
- Detect resets from reset times in rate-limit errors. If an error has no parseable reset time, probe with a minimal request at a configurable interval (default 30 min).

**Thresholds.** These apply once a baseline exists:
- **80% of a window:** stop *starting* new tasks on that agent. Tasks already running continue.
- **Daytime reserve, 8am–11pm:** background tasks may use at most 75% of Claude's window. The last 25% is mine for interactive work.
- **An actual rate-limit error** is a hard pause (§2.6).

### 1.6 Safety rules

**Never without my approval.** An agent that wants any of these must stop and raise a "needs you" request:
- Spend money or exceed a cap
- Push to GitHub or make a repo public (local commits are fine)
- Deploy anything
- Send emails, LinkedIn messages, DMs or any message as me
- Submit a job application or fill an application form
- Submit homework or upload anything to bCourses or Gradescope
- Post content anywhere
- Install system packages with Homebrew (project-local `pip`/`npm` installs are fine)
- Delete files outside the task workspace
- Log into any account

**Coursework.** Agents make study material and practice problems. They never complete graded assignments. Each course's AI policy in config wins over anything else.

**Scraping.**
- Respect `robots.txt` and site terms.
- No LinkedIn scraping, ever.

**Data.**
- Passwords, SSH keys, and school and job-portal logins never leave the machine.
- Unreleased comedy material may go to the model providers, but must never be posted or shared anywhere else. **This includes Telegram:** Telegram messages about comedy tasks carry status lines only, never script or idea content.

**Decisions mid-task.**
- Low-stakes calls (file names, which of two sources to use, wording) get made, logged to `logs/decisions.jsonl`, and listed in the report.
- Anything on the never-list, or anything that changes the task's scope, pauses the task and creates a "needs you" request.

### 1.7 Application tracker

`~/agents/data/applications.csv` has these columns:

`id, company, role, location, season, posting_url, source, date_found, deadline, status, resume_version, notes`

- `status` is one of: `new, applying, applied, OA, interview, offer, rejected, skipped`.
- **Agents never write the tracker.** An agent writes proposed rows to `output/proposed_rows.csv` in its task folder, using the same columns.
- Nightshift code (deterministic, not the agent) validates the proposed rows, dedupes them against the tracker, and appends only new rows with `status=new`.
  - Dedupe key: normalized `posting_url` (strip query tracking params, trailing slashes, case). As a secondary check: `(company, role, season)` normalized.
- `resume_version` holds the task ID of the resume-tailoring run that produced the resume for that row.

### 1.8 Posting match rules (used by every posting-scan template)

A posting **matches** only when all of these hold:
- It's a **Summer 2027 internship**.
- The role is **SWE, backend, infra or ML engineering**.
- It's located in the **Bay Area, Seattle, NYC, or remote in the US**. Postings elsewhere that meet every other rule are kept but get `notes: low-priority location`.

**Screen out:**
- MS/PhD-only roles
- New-grad or full-time roles
- Roles needing a security clearance

The public tracker repos and the 15 target companies' career page URLs are config lists that I fill in.

### 1.9 Setup checklist (I do these; `orch doctor` verifies them)

- TeX toolchain providing `latexmk` or `pdflatex`, plus a PDF page-count tool. A Python library such as `pypdf` is fine as the page-count tool.
- Optional: a scheduled wake (`pmset repeat wake ...`) so 2am jobs can run when the lid is open. `orch doctor` prints the exact command for me to run. Nightshift never runs it.

---

## 2. Core behavior (applies from the phase where each part lands)

### 2.1 Task lifecycle

States:
- `scheduled`
- `awaiting_approval`
- `queued`
- `running`
- `paused_usage`
- `paused_spend`
- `needs_you`
- `retry_wait`
- `done`
- `partial`
- `failed`
- `cancelled`

Every transition is written to the DB, with a timestamp and reason.

### 2.2 `task.yaml` fields

`id, title, slug, prompt, template, task_type, agent, agent_override, priority (urgent|normal|background), deadline, run_at, internet (on|off), long (bool), estimated_minutes, max_runtime_minutes, approved (bool), approved_at, schedule_name (if recurring), created_at`.

### 2.3 Priority, deadlines and time windows

**Priority levels:**
- **Urgent:** the deadline is within 24 hours, or I marked it urgent. Jumps the queue.
- **Normal:** runs in order.
- **Background:** runs only when nothing else is waiting.

**Deadlines:**
- `--deadline` always wins.
- Otherwise templates fill it in when one exists: a posting's stated close date, or a course due date from a calendar feed.
- Postings with no close date get no deadline and normal priority.

**Long tasks** (coding, research reports, re-scrapes; marked `long: true` in the template) start only between **midnight and 7am**, unless urgent.

**All times are America/Los_Angeles.**

### 2.4 Approval and planning

- **Time estimates:** templates set `estimated_minutes`. For free-form tasks, the agent estimates in a quick planning call.
- **Tasks estimated over 30 minutes:** Nightshift has the agent expand the one-line prompt into a short `plan.md`. The task then needs my approval before it can run.
- **Tasks estimated at 30 minutes or less** start right away.
- **Scheduled templates are pre-approved**, so nothing stalls at 2am waiting for me.

### 2.5 Schedules and catch-up

Each schedule has a cron-style rule plus its own catch-up rule:
- run within N hours, else skip; or
- run on next wake.

**Never stack duplicates.** Several missed runs of the same schedule become one run.

The daemon keeps its own scheduler state in SQLite. On every start and every wake, it compares the time now with each schedule's last run and applies that schedule's catch-up rule.

### 2.6 Pausing and resuming for usage limits

- **Hard limit hit mid-task.** Let the current step finish if the CLI allows it, then stop. The task moves to `paused_usage`, with the reset time if known.
- **Keep checkpoints current.** A hard limit can end a session without warning. So agents are instructed to update `progress.md` (what's done, what's next, open decisions) after **every meaningful step**, not only before stopping.
- **On resume:** first try to resume the same agent session with its full context. If the session is more than 24 hours old, or the resume fails, start a fresh session from `plan.md` + `progress.md`.
- **Resume order:** priority first, then nearest deadline, then oldest.
- **Expiry:**
  - A task paused more than 72 hours needs my re-approval.
  - A task whose deadline has passed is cancelled and reported.

### 2.7 Failures

- **Max runtime:** 2 hours by default, 6 hours for overnight coding tasks. Any template can set its own cap.
- **Retries:** up to 2, waiting 10 minutes before the first and 30 minutes before the second. The second retry may switch to another agent only for task types that allow failover (§2.8): Codex for coding tasks, Gemini for the task types that fail over to Gemini.
- **Stuck detection (Phase 3).** An agent counts as stuck after 15 minutes with no new log output and no file changes, or after the same tool call 3 times in a row. Then:
  1. Save state and stop the agent.
  2. Retry once, with a note about what it was looping on.
  3. If it gets stuck again, move it to `needs_you`.

### 2.8 Failover when Claude is out of usage

| Task type | Out of Claude usage |
|---|---|
| Research, scraping, summaries | Fail over to Gemini |
| Coding projects | Fail over to Codex. If Codex is also out, wait, and resume on whichever of Claude or Codex resets first |
| Resume and cover-letter tailoring | Wait for reset, unless the deadline is within 24 hours |
| YouTube comedy writing (scripts, punch-up, descriptions, captions) | Always wait for Claude |
| Study plans and practice problems | Fail over to Gemini |

- Any failover triggered by a deadline gets flagged in the report's "needs you" list, so I review that task more carefully.
- A task that switches agents can't resume the other agent's session. It starts a fresh session on the new agent from `plan.md` + `progress.md`.

### 2.9 Agent adapters

All agents sit behind one interface. Adding another agent later must not change core code. The interface:

- `build_command(task, workspace, permissions)`: the headless invocation
- `parse_stream(line)`: normalized events (text, tool call, file change, usage, error)
- `detect_rate_limit(event_or_exit)`: returns limit type and reset time if present
- `session_id()` / `resume_command(session_id, prompt)`
- `usage_from_run()`: tokens/requests and dollar cost where reported
- `write_permissions(workspace, policy)`: generates the per-task allowlist, deny rules, sandbox settings and added read-only dirs

**Expected Claude Code invocation:** `claude -p` with JSON or stream-JSON output, `--resume <session_id>`, and tool allow/deny settings. Verify each of these against `claude --help`, and record the real flags and the real rate-limit error formats in `docs/adapters.md`.

**Expected Codex invocation:** its non-interactive mode (expected to be `codex exec`), with its own sandbox and approval settings set to the limits in §1.4 and §1.6. Verify against `codex --help` and record the results in `docs/adapters.md` the same way.

### 2.10 Agent protocol (instructions Nightshift injects into every task)

- Work only inside the task folder. Treat the read-only folders as read-only.
- Keep `progress.md` current after each step.
- Log low-stakes decisions to `logs/decisions.jsonl` as `{time, decision, options, reason}`.
- To ask for approval, or to raise a scope change, write `needs_you.json` as `{kind, summary, details, proposed_action}` and stop.
- Put deliverables in `output/`.
- Write a draft `report.md` in the format in §2.11. Nightshift fills in the usage, time and cost fields itself.

### 2.11 `report.md` format (skimmable on a phone in under a minute)

1. **Result:** one line: `done`, `partial` or `failed`, plus what that means.
2. **Needs you:** approvals, open questions, deadline-driven failovers, anything I must act on. If there's nothing, say so.
3. **Summary:** five sentences at most.
4. **Files created:** with paths.
5. **How to run or use the result.**
6. **Decisions the agent made on its own.**
7. **Open issues.**
8. **Time taken, usage consumed and dollars spent, per agent.**
9. **A link to the full logs.** Don't paste the logs in.

Rules for specific task types:
- **Research:** every claim cites a URL. Minimum 5 distinct sources. Claims without a source are labeled *unverified*. Length fits the question, but a report-template result is never under one page.
- **Resume:** show a diff against `master.tex` and a one-line reason for each changed bullet. Never invent experience or numbers. Every change must trace to content already in `master.tex`.
- **Comedy:** keep my voice. Flag any premise or tag that sounds close to a well-known comic's bit.

### 2.12 Notifications (Telegram, from Phase 2)

| Event | Channel |
|---|---|
| Finished | Telegram message with the one-line result |
| Failed, or paused and needs me | Telegram message, immediately |
| Paused for usage | Silent; shown in the morning digest |
| Everything from overnight | One Telegram digest at 8am |

Telegram rules:
- Use long polling only, so no port is opened to the internet.
- Accept commands only from my Telegram user ID, which is config. Ignore and log everything else.

### 2.13 Retention (Phase 4)

| What | Kept for | Then |
|---|---|---|
| Reports | Forever | Nothing |
| Task outputs | 90 days | Moved to `~/agents/archive/` |
| Full logs | 30 days | Zipped into the archive |
| Scrape snapshots | 90 days | Deleted; only the diffs stay |

---

## 3. Phase 1 — Core loop (Oct 20 → Nov 2) ✅ CURRENT

**Goal:** one task at a time, queued from the CLI, that pauses and resumes cleanly. Claude runs everything; Codex takes over coding tasks when Claude is out of usage. It must be usable by **Nov 2**.

### 3.1 Build
1. **Repo skeleton** at `~/code/nightshift`, with `pyproject.toml`, the `orch` entry point, `config.example.yaml`, `docs/` (`decisions.md`, `adapters.md`, `safety.md`) and pytest tests.
2. **Config loader.** Reads `~/.config/nightshift/config.yaml` and secrets from Keychain or `.env`. Placeholder values (hardware, plan tier, credit balance and expiry) are defaults, never hard-coded.
3. **`orch doctor`.** Reads RAM, free disk, macOS version, and the installed versions of Claude Code, Codex CLI, Gemini CLI, Node, Python, git and the TeX tools from the machine itself. It reports any mismatch with config, any missing tool, and launchd status, and it prints the optional `pmset` wake command (§1.9).
4. **SQLite schema:** `tasks`, `runs` (attempts), `schedules`, `schedule_runs`, `usage_events`, `limit_observations`, `spend_events`, `approvals`, `transitions`.
5. **Daemon,** run by a `launchd` agent with KeepAlive. `orch daemon install|uninstall|status` manages the plist. Inside the daemon:
   - the queue runner (one task at a time in Phase 1)
   - the scheduler, with the catch-up rules from §2.5
   - `caffeinate` held only while a task runs
6. **Claude Code and Codex adapters** (§2.9), with per-task permissions per §1.4 and §1.6. Add the **coding failover** from §2.8: a coding task moves from Claude to Codex when Claude is out of usage. This includes the retry-with-switch to Codex from §2.7. Every other failover waits for Phase 2.
7. **Task workspaces and the agent protocol** (§1.3, §2.10).
8. **Approvals in the CLI.** `orch add` prints the plan (when one is needed per §2.4) and asks y/n right there. `orch approve <id>` and `orch deny <id>` cover anything queued earlier.
9. **Template engine.** Templates are YAML files in the repo's `templates/` directory. Each defines `task_type`, `agent`, `estimated_minutes`, `max_runtime_minutes`, `long`, `internet`, `prompt` (with variables), its deadline source, and its post-processing hooks. Phase 1 ships only the two templates the gate needs:
   - **`resume-tailor`** (input: posting URL)
     - Reads `~/Documents/career/resume/master.tex` and the cover-letter LaTeX template path from config.
     - Outputs `output/resume.tex`, `output/resume.pdf`, `output/cover_letter.pdf`, `output/cover_letter.txt` and `output/changes.md` (each changed bullet → its reason).
     - Post-hook: compile both, and **fail the task if the resume PDF isn't exactly 1 page**. Include a unified diff against `master.tex` in the report. Pull the posting's close date as the deadline when one is stated.
     - Never submits anything.
   - **`posting-scan`** (daily)
     - Checks the configured tracker repos and target company career pages, applying the rules in §1.8.
     - Writes `output/proposed_rows.csv`. A post-hook merges new rows into the tracker (§1.7).
     - The report lists only postings that are new since the last run.
     - **Runs on Claude in Phase 1**; moves to Gemini in Phase 2.
10. **Schedules.** `orch schedule add|list|enable|disable|run-now|remove`. Set up the daily posting scan at **6:30am**, catch-up "within 12 hours, else skip".
11. **Priorities, deadlines, time windows and long-task gating** (§2.3).
12. **Runtime caps and retries** (§2.7), same agent only. Stuck detection waits for Phase 3.
13. **Rate-limit hard pause and resume** (§2.6), including detecting the reset time and probing when the error doesn't give one. Record every limit hit in `limit_observations`, so the baseline starts building now. Proactive thresholds wait for Phase 2.
14. **Reports** (§2.11). Nightshift fills in time and usage from adapter data.
15. **Fault injection for testing:** `orch add ... --simulate-rate-limit-after <N steps>`. It must exercise the real pause/resume path, not a mock of it.
16. **CLI commands:**
    - `orch add "<prompt>" [--template T] [--agent A] [--priority P] [--deadline D] [--at TIME] [--internet on|off]`
    - `orch list [--status S]`, `orch show <id>`, `orch logs <id> [-f]`, `orch report <id>`
    - `orch cancel | pause | resume | retry <id>`, `orch priority <id> <P>`
    - `orch usage`, `orch doctor`

### 3.2 Not in Phase 1
Gemini, failover other than coding to Codex, Telegram, the dashboard, spend caps, proactive usage thresholds, parallel agents, stuck detection, GitHub, Docker, auto-archiving.

### 3.3 Gate 1: both tests pass on real data
- **Test 1, resume tailoring.** Run `orch add "Tailor my resume and a cover letter for <posting URL>" --template resume-tailor`. Passes if:
  - the report shows a diff against `master.tex`;
  - every change traces to real experience in `master.tex`;
  - the resume compiles to exactly 1 page;
  - nothing is submitted.

  Run it again with `--simulate-rate-limit-after` set midway. It must pause, have a current `progress.md`, and resume to an equivalent result.
- **Test 2, recurring scrape.** Use the daily 6:30am `posting-scan` schedule. Passes if:
  - two runs in a row produce no duplicate tracker rows;
  - the second run reports only new postings;
  - a run missed while the laptop slept catches up exactly once.

---

## 4. Phase 2 — Backup + visibility (Nov 3 → Nov 16)

1. **Gemini CLI adapter** (subscription sign-in), and a **Gemini API path** (AI Studio credits) for paid calls. Verify the headless flags and limit-error formats; record them in `docs/adapters.md`.
2. **Gemini failover** per §2.8, and **retry-with-switch to Gemini** per §2.7.
3. **Spend tracking and caps** (§1.5), plus the **paid-credit rule**.
4. **Usage meter.** Per-plan usage (Claude, Codex, Gemini), with a countdown to the next reset. **Proactive thresholds** start: stop starting tasks at 80% of a window, and apply the daytime 75% reserve. Gemini quotas get learned the same way as Claude's (§1.5).
5. **Move `posting-scan` to Gemini.**
6. **`company-rescrape`** schedule, every 2 days at 2am, on Gemini, catch-up "run on next wake".
   - Re-scrapes the 15 target companies' career pages.
   - Snapshots go to `~/agents/data/snapshots/`.
   - Diffs against the last run.
   - New matching postings go through the tracker merge.
7. **Telegram outbound only:** alerts and the 8am digest (§2.12).
8. **Read-only dashboard.**
   - FastAPI, phone-friendly, dark mode, clean and minimal.
   - Binds to `localhost` and the Tailscale interface only. Never a public URL.
   - Shows:
     - the "needs you" inbox (view only)
     - tasks grouped by status (running, queued, paused, needs you, done, failed)
     - a live log tail for the running task
     - the usage meter, with reset countdowns
     - the spend tracker against caps
     - the schedule list
     - the report viewer (Markdown rendered)
     - a **"tonight's queue" preview**: what will run, and roughly how much usage it will take

**Gate 2: a clean week.** For 7 days in a row, all three of these hold:
- every scheduled run either ran or caught up correctly;
- nothing failed without an explanation;
- I never had to step in manually.

---

## 5. Phase 3 — Remote control (Nov 17 → Dec 6)

1. **Telegram task intake** and commands: add a task (free-form or by template), status, list, approve, deny, cancel, pause, resume, retry, and set priority. The 30-minute plan approval works as one tap, using inline buttons.
2. **Approve/deny inbox** with working buttons in the dashboard. Dashboard controls go live: cancel, pause, resume, retry, change priority, and edit, enable or disable schedules.
3. **Remaining starter templates:**
   - **Company and interviewer research brief:** research-report rules from §2.11.
   - **Weekly school deadline roundup and study plan,** every Sunday at 8pm, on Claude, catch-up "run on next wake".
     - Reads the bCourses calendar iCal feed and my Google Calendar's private iCal link. Both URLs are stored as secrets.
     - Applies each course's AI policy from config.
     - Course due dates feed task deadlines.
   - **Study guide from a folder of lecture slides** (in `~/school/`).
   - **Punch up a bit:** a script or idea for one of my YouTube comedy videos, in my voice. Flag anything close to a well-known comic's bit.
   - **Research report on a topic.**
4. **Stuck detection** (§2.7).
5. **Two agents in parallel:** at most one coding/writing task (Claude, or Codex when it has taken over for Claude) and one Gemini task at a time.
6. **GitHub.** A fine-grained token, scoped to repos I pick and stored as a secret. New repos default to private. Every push goes through the approve/deny inbox. Phases 1–2 use local repos only.

**Gate 3:** freeze before finals. After this gate, only bug fixes until winter break.

---

## 6. Phase 4 — Hardening (winter break)

1. **Docker** for scraping and untrusted code.
2. **Auto-archiving and retention** per §2.13.
3. **Usage forecasting:** better estimates for the tonight's-queue preview, based on usage history per template.
4. **Chained templates:** fixed multi-step chains. For example: scrape a posting → tailor the resume → update the tracker.
5. **PDF export** of reports, on request.

---

## 7. Open decisions

None right now. If new ones come up, add them here, and don't implement them until they're resolved in this file.
