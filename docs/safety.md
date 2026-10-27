# Safety: what limits agents, and how well

Every Attempt runs with a permission policy generated for that Task (§1.4,
§1.6). `nightshift.permissions.build_policy` builds an agent-agnostic
`PermissionPolicy` from config (`permissions:` in `config.yaml`) and the Task;
the Task's adapter turns it into its agent's own settings with
`write_permissions`. The policy is rebuilt before every Attempt, resumes
included.

Each limit below is one of:

- **Technically enforced**: the agent CLI or the operating system blocks it.
  An agent that tries gets an error. Verified on this Mac where marked.
- **Best effort**: a block exists, but it can be sidestepped (deny-lists of
  command names, for example).
- **Instruction-only**: the agent is told the rule in the injected protocol
  (§2.10) and nothing stops it if it ignores the rule.

Verified against Claude Code 2.1.306 on 2026-10-20, with real `claude -p` runs
(haiku). `tests/test_permissions_real.py` repeats the key checks:
`NIGHTSHIFT_REAL_AGENT=1 .venv/bin/python -m pytest tests/test_permissions_real.py`.

## How Claude Code Tasks are confined

Nightshift runs `claude -p` in the Task folder with:

- `--restricted`: ignores the user, project and local settings files (so
  your own `~/.claude/settings.json` allow rules and hooks don't apply),
  confines the file tools (Read, Write, Edit, Glob, Grep) to the working
  directories, and refuses `bypassPermissions`.
- `--strict-mcp-config` with no MCP config: no MCP servers.
- `--tools Bash,Read,Write,Edit,Glob,Grep`, plus `WebFetch,WebSearch` only on
  internet-on Tasks. Tools not named don't exist in the session.
- `--settings <agents_root>/data/permissions/<task-id>/claude-settings.json`:
  allow/deny rules and Claude Code's sandbox settings. The file lives in the
  data dir, which the agent can't read or write, and is regenerated each
  Attempt, so the agent can't tamper with its own policy.
- `--add-dir <dir>` for each read-only folder that exists.
- `--permission-mode acceptEdits --permission-prompts none`: edits inside the
  allowed folders go through; anything that would ask for permission is
  denied automatically, because nobody is there to answer.
- Bash runs in Claude Code's sandbox (macOS Seatbelt) with
  `allowUnsandboxedCommands: false`, so a command can't ask to run outside it.
- Never `--dangerously-skip-permissions`. The adapter refuses to build a
  command without a policy, and refuses `agents.claude.extra_args` that would
  widen it (`--dangerously-skip-permissions`, `bypassPermissions`,
  `--settings`, `--add-dir`, `--tools`, `--permission-mode`, ...).

## How Codex Tasks are confined (coding failover)

> **Verified on codex-cli 0.163.0 (2026-10-21).** "Verified" below means
> the same `-c` overrides were tried for real: with `codex sandbox -c ...
> -P nightshift` (the shell sandbox Codex uses, no model call) and in real
> `codex exec` runs (gpt-6-luna) whose session settings recorded
> `approval_policy: never`, the managed `nightshift` profile and
> `network_access: false`. Re-check after a Codex upgrade.

Codex runs a coding Task only after a failover from Claude (§2.8). It gets
the same `PermissionPolicy`, mapped by `CodexAdapter.write_permissions` to
`codex exec` `-c` overrides (exact argv in docs/adapters.md):

| Limit | Status | How |
|---|---|---|
| No blanket skip-permissions | **Enforced by Nightshift** | Never `--yolo` / `--dangerously-bypass-approvals-and-sandbox` / `danger-full-access`; the adapter refuses to build a command without a policy, and refuses `agents.codex.extra_args` with `-c`, `--sandbox`, `--add-dir`, `--profile`, `--approve-for-me`, ... |
| Approvals | **Technically enforced** (verified) | `approval_policy = "never"`: nothing can ask. A real `apply_patch` outside the Task folder was refused ("patch rejected: writing outside of the project; rejected by user approval settings", stderr only, no stream item); a blocked shell command fails with "Operation not permitted". |
| Your Codex config, rules and hooks don't apply | **Technically enforced** (flags verified) | `--ignore-user-config` (no `~/.codex/config.toml`: no MCP servers, no sandbox/approval overrides), `--ignore-rules` (no execpolicy allow rules), `features.hooks = false`. |
| Desktop, browser, apps and plugins | **Technically enforced** (keys verified) | 0.163.0 turns `computer_use`, `browser_use`, `browser_use_external`, `in_app_browser`, `apps`, `plugins`, `remote_plugin` and `image_generation` on by default (`codex features list`); every Task, planning call and probe sets them `false`, and `agents.codex.extra_args` may not contain `--enable`/`--disable`/`--search`/`--remote`. |
| Writes only in the Task folder | **Technically enforced** (verified) | Permission profile `nightshift`: the Task folder (and its workspace root) `write`, nothing else writable (verified: a sibling folder, `/tmp` and a read-only folder all "Operation not permitted"; `/tmp` is not writable because the profile has no `:tmpdir`/`:slash_tmp`); `TMPDIR` points into the Task folder (`.tmp/`), writable. |
| Read-only folders readable, never written | **Technically enforced** (verified) | `read` entries in the profile: `cat` works in the shell and in a real `codex exec`, a write fails. |
| `~/agents/data/`, `~/.ssh`, `~/.config/nightshift`, Keychains, browser profiles inaccessible | **Technically enforced** (verified) | `deny` entries (deny wins over read/write on the same path). |
| Everything else in your home folder and `~/agents/` unreadable | **Technically enforced** (verified) | Home and `agents_root` `deny` (`ls ~`, `ls ~/Documents`, `cat ~/.ssh/known_hosts`, `ls <agents_root>`, `cat <agents_root>/data/x`: "Operation not permitted"); the Task folder, read-only folders and `permissions.tool_dirs` are more specific `read`/`write` entries, which win (`node --version` from `~/.nvm` runs). |
| Files outside home (`/usr`, `/opt/homebrew`, `/Volumes`) | **Readable** (gap, same as Claude) | `":root" = "read"`; denying `/` would break every tool. |
| No network from shell commands | **Technically enforced** (verified) | `permissions.nightshift.network.enabled = false`: `curl https://example.com` fails "Could not resolve host" (also in a real `codex exec`), a raw socket to an IP fails "Operation not permitted". |
| Internet-off Tasks have no web access | **Technically enforced** (key verified) | `web_search = "disabled"`; Codex has no page-fetch tool. |
| Web search on internet-on Tasks avoids LinkedIn | **Gap** | `web_search = "live"`; Codex's search can't deny domains (only allow-list them), so the LinkedIn rule is instruction-only there. Coding Tasks are internet-off by default. |
| Bash network-command deny-list (`curl`, `wget`, ...) | **Not mapped** (gap) | Codex's equivalent is execpolicy `.rules` files, which can't be passed per run without trusting a file the agent could edit; `--ignore-rules` is used instead. The network block above is what stops traffic. |
| Secrets in the environment | **Technically enforced** (verified) | `shell_environment_policy.inherit = "core"`: shell commands see only `HOME`, `LOGNAME`, `PATH`, `SHELL`, `USER`, `LANG`, `TMPDIR`, `PWD`, `SHLVL` and Codex's own `CODEX_SANDBOX*`; `MY_API_KEY`, `GH_TOKEN`, `ANTHROPIC_API_KEY` and even a harmless `NOT_HIDDEN` were all dropped. |
| A config file the agent writes in the Task folder (`.codex/config.toml`, `AGENTS.md`) | **Gap** (not tried) | Codex reads project config only for trusted projects (trust lives in the ignored user config), but `AGENTS.md` in the folder is read as instructions. It can't widen the `-c` policy, which wins over project files. |
| Settings typos | **Mitigated** (verified) | `--strict-config` fails on unknown fields, `-c` overrides included ("unknown configuration field `x` in -c/--config override", exit 1, before any model call); a `-c` value that isn't valid TOML becomes a plain string silently, so the generated values are pinned by tests. |
| Permission profiles are beta | **Gap (future)** | If Codex drops or renames `default_permissions`/`permissions.*`, `--strict-config` should fail the Attempt rather than run unconfined; the fallback is `sandbox_mode = "workspace-write"` with network off, which does **not** confine reads. |

Planning calls and probes on Codex use `--ephemeral`, the built-in
`:read-only` profile, `features.shell_tool = false` (no shell tool),
`web_search = "disabled"` and no hooks.

## How Gemini Tasks are confined

> **Verified on gemini-cli 0.65.0**: first with scripted model responses
> (`tests/test_gemini_cli.py`), then with a real model on a free-tier AI
> Studio key (`tests/test_gemini_real.py`, 2026-10-23; docs/adapters.md).
> "(seen)" marks what was observed that way. In the real run the model
> carried out the audit's escape attempts and the rows held (the known
> relative-write gap included). Re-check after every Gemini CLI upgrade.

`GeminiAdapter.write_permissions` maps the same `PermissionPolicy` to a
per-Task CLI home (`GEMINI_CLI_HOME`, its settings file is the user layer),
an admin-tier policy file and a few env vars and flags (exact shape in
docs/adapters.md). It signs in with an AI Studio API key
(`agents.gemini.auth: gemini-api-key`, the default; the key comes from
`.env` or the Keychain) or with Google (`oauth-personal`).

**Technically enforced:**

| Limit | Status | How |
|---|---|---|
| No blanket skip-permissions | **Enforced by Nightshift** | Never `--yolo`/`--approval-mode yolo`; `security.disableYoloMode`; the adapter refuses to build a command without a policy and refuses `agents.gemini.extra_args` with `--yolo`, `--approval-mode`, `--sandbox`, `--policy`, `--include-directories`, `--skip-trust`, `-e`, `--fake-responses`, ... |
| Only the Task's tools exist; anything else is denied | **Enforced (seen)** | `tools.core` lists the only built-in tools (others are "not found"); the admin policy denies `*` and allows those; headless, every "ask" becomes deny. |
| Your Gemini settings, policies and the Task folder's own settings can't widen this | **Enforced (seen)**, with one refusal | `GEMINI_CLI_HOME` makes Nightshift's file the user settings layer: your `~/.gemini` settings, policies and extensions aren't read (on Google sign-in only the sign-in files are linked in; on the API key nothing is). The only layer above it is a root-owned system settings file. `GEMINI_CLI_TRUST_WORKSPACE=false` keeps the agent-writable `.gemini/settings.json` in the Task folder from loading (seen: one switching the sandbox off changed nothing) and project hooks off. The policy is admin tier. `-e none`, `--allowed-mcp-server-names nightshift-none`, `hooksConfig.enabled: false`. Nightshift **refuses to run** a Gemini Attempt if `~/.gemini/policies/sandbox.toml` exists (read from your real home even under `GEMINI_CLI_HOME`: persistent shell-sandbox grants, including network). |
| Writes only in the Task folder | **Enforced (seen)** for file tools and shell, except the gaps below | File tools only reach the workspace (Task folder + include dirs) and the CLI home's project temp dir; shell commands run under a per-command Seatbelt profile with `(deny default)`; a shell write outside is "Operation not permitted". |
| Read-only folders readable, never written | **Enforced (seen)** for file tools; **best effort** for shell | Added with `--include-directories`; `read_file` reads them. Admin rules deny `write_file`/`replace` into them and any write path with `..` (seen). **Gap (seen):** Gemini's sandbox makes include dirs writable to shell commands; commands that *name* a read-only folder are denied (absolute, `~/`, `$HOME/` forms), but `cd` then a relative path got through. |
| `~/agents/data/`, no-access dirs, the rest of home and `~/agents/` unreadable | **Enforced (seen)** | Not in the workspace (file tools refuse them); the shell profile is deny-by-default (seen: a file in `~`, `~/.gemini`, the CLI home with the linked sign-in, and `data/` through a relative path all refused). `PATH` is filtered so no entry opens home, `agents_root` or `/` (`~/bin` or `/bin` would); `/bin`'s programs are reached through links. Shell commands naming a no-access dir are also denied by policy. |
| No network from shell commands | **Enforced (seen)** | Per-command profile without network; `curl` can't resolve hosts. The CLI process itself keeps network (the API, `web_fetch`). |
| Internet-off Tasks have no web access | **Enforced (seen)** | `web_fetch`/`google_web_search` aren't in `tools.core` ("not found") and are denied by policy. |
| `web_fetch` never fetches LinkedIn | **Enforced (seen)** | Admin rule denies `web_fetch`/`google_web_search` whose arguments mention `linkedin.com`, `lnkd.in`, `licdn.com` (any case, any subdomain); seen for both tools. Search results may still list LinkedIn links (gap, as for Claude). |
| Git | **Denied (seen)** | The folder is untrusted, so the CLI turns git commands into "ask", i.e. deny; `.git` isn't writable from the sandbox anyway. Gemini Tasks can't use git. |

**Gaps and instruction-only:**

| Limit | Status | How |
|---|---|---|
| Shell can read/write `/tmp` and `/private/tmp` | **Gap (seen)** | The base profile always allows them. `TMPDIR` points into the Task folder, but `/tmp` stays shared. (`~/.gemini/tmp` is no longer one: sessions live in the per-Task CLI home, which the shell can't read.) |
| The Task's own session transcripts | **Gap (minor)** | File tools may read and write the CLI home's project temp dir (`gemini-home/.gemini/tmp/<project>`), which holds this Task's own sessions; an agent could edit what a later resume replays. Other Tasks' and your sessions are elsewhere. |
| A failing command that the sandbox stopped hangs the Attempt | **Gap (seen, CLI bug), bounded by Nightshift** | After a sandbox denial with a non-zero exit, 0.65.0 waits forever for a "sandbox expansion" answer it can't get headless. Nothing is granted. Nightshift's **stall guard** (`agents.gemini.stall_minutes`, default 5) stops an Attempt with a Step in flight, no command running and no output for that long, as a Failed Attempt (seen on the real CLI); before, it ran to its runtime cap. The retry may hit the same hang if the agent repeats the command. |
| Stopping an Attempt reaches every agent process | **Enforced by Nightshift (seen)** | `gemini` relaunches itself as a child process and doesn't pass SIGTERM on; killing only the parent left the child working on the Task folder after the Attempt had ended (seen). Every agent now runs in its own process group: stops signal the group, and the group is SIGKILLed when the Attempt ends. |
| Bash network-command deny-list (`curl`, `wget`, ...) | **Not mapped** | The shell has no network at all; the deny-list adds nothing there. |
| Secrets in the environment | **Enforced for the API key (seen)**, best effort otherwise | The CLI process holds `GEMINI_API_KEY` (only Nightshift's key from `.env`/Keychain; a key in your shell is overwritten, `GOOGLE_API_KEY` blanked); shell commands don't get it (seen: `echo $GEMINI_API_KEY` prints nothing), and the shell sandbox can't read `~/.config/nightshift`. `security.environmentVariableRedaction.enabled` and Gemini's own sanitizing cover other variables. The paid path's key is a separate secret, never given to the free path. Nightshift masks every loaded key in logs and reports. |
| What the agent does inside the Task folder, report.md honesty, not retrying denied actions | **Instruction-only** | The agent protocol, as for every agent. |
| Model fallback rotates the session | **Gap (to verify)** | On a quota/capacity error the CLI may switch model silently and start a new session id; a later resume would miss the last turns. |

Planning calls and probes on Gemini have an empty `tools.core`, a policy
that denies `*`, no extensions, MCP servers or hooks, and run in a private
temp CLI home from `<agents_root>/data/planning` / `.../probe`.

## Planning calls

Free-form `orch add` makes up to two quick planning calls (estimate, plan)
before a Task exists or runs. They are confined harder than Attempts:
`--tools ""` (no file, shell or web tools at all: technically enforced),
`--restricted --strict-mcp-config` (no user settings, hooks or MCP servers),
no saved session, and a working directory of `<agents_root>/data/planning/`.
The prompt is the Task's one-line prompt; the agent's plan is saved to
`plan.md` by Nightshift, not by the agent. Nothing an agent writes during
planning can approve a Task: only `orch approve` or a yes at the `orch add`
prompt can, and every decision is recorded in the `approvals` table.

## Usage probes

A probe (an agent out of usage with no known reset time, or `orch usage
--probe`) is confined the same way as a planning call: `--tools ""`,
`--restricted --strict-mcp-config`, `--no-session-persistence`, a fixed
one-line system prompt and the fixed prompt "OK?" (no Task content), in
`<agents_root>/data/probe/`. Its answer is never used, only whether it went
through.

## Scheduled Occurrences

An Occurrence (a Task a Schedule created) is pre-approved (§2.4), but it is
otherwise an ordinary Task: its folder under `recurring/<name>/YYYY-MM-DD/`
is its only writable place, and it gets the same per-Task permission policy,
template post-hooks and caps as any other Task. Pre-approval skips only the
plan approval; nothing in §1.6 that needs approval is ever done unasked.
A template's `schedule:` block (posting-scan's 6:30am rules) only supplies
defaults for `orch schedule add`: no Schedule exists, and nothing runs
pre-approved, until you add it yourself.

## Template post-hooks (Nightshift-side tools)

Some templates run tools on what the agent wrote, after the Attempt and
outside the agent's session: `resume-tailor` compiles `output/resume.tex` and
`output/cover_letter.tex` with LaTeX. That input is untrusted (the agent read
a web page to write it), so the compile runs through `Host.run_confined`:

| Limit | Status | How |
|---|---|---|
| The compile can't read private files into the PDF | **Technically enforced** (verified) | macOS Seatbelt (`sandbox-exec`) profile: reads under your home folder and `agents_root` are denied except the Task folder and `templates.latex.readable_dirs` (`~/Library/texmf`, `~/Library/texlive`). A real `\input{<file outside the Task folder>}` failed with "File not found". TeX's own `openin_any=p` is also set but does **not** stop `\input` of absolute paths in pdfTeX (verified), so it is not relied on. |
| The compile writes only inside the Task folder | **Technically enforced** | Same profile: writes only to the Task folder and the per-user temp dir (`/private/var/folders`). Nightshift's own copy of the PDF never follows a symlink the agent left (paths are checked; a link at the PDF path is replaced). |
| No shell escape / network from TeX | **Technically enforced** | `-no-shell-escape` (`shell_escape=f`), and the profile denies all network. |
| `sandbox-exec` itself | **Gap (future)** | Deprecated by Apple, though still shipped in macOS 26 and used by Claude Code's own sandbox. If it goes away, `MacHost.run_confined` needs another confinement. |

`resume-tailor` also never runs on a LinkedIn posting: `orch add` refuses a
posting URL on any of `permissions.denied_domains`.

`resume-tailor` must never invent experience or numbers (§2.11). That is
checked by Nightshift, not left to the agent:

| Limit | Status | How |
|---|---|---|
| Every changed resume line traces to `master.tex` | **Technically enforced** (mechanically; meaning is for you) | The `trace_changes` post-hook fails the Attempt if a changed body line of `output/resume.tex` has no `changes.md` entry, an entry cites no `master.tex` line or a line that doesn't exist (or only blank ones), or has no reason. It reads Nightshift's pristine copy of `master.tex`, which the agent can't reach. It can't tell whether a reword *means* the same as the cited line (e.g. "multi-user" -> "collaborative"): the unified diff in the report is there for that review. |
| No invented numbers | **Technically enforced** | Every number in the resume's document body (percentages, counts, amounts, years) must appear somewhere in `master.tex`. LaTeX lengths and comments are skipped. A real number used in a new context (a "40%" moved to another bullet) still passes. |

### Research results (§2.11)

`research-report` and `company-brief` write their result to
`output/research.md` / `output/brief.md`; the `research_rules` post-hook
(`nightshift.research`, Nightshift code) checks it after the Attempt and
fails the Attempt on any miss.

| Limit | Status | How |
|---|---|---|
| Every claim cites a URL or is labelled *unverified* | **Technically enforced** (mechanically; meaning is for you) | Each body sentence of 4+ words (not a heading, question, `...:` lead-in, code or the Sources section) must carry a URL or a `[n]` naming a Sources entry, or the `*unverified*` label. It can't tell whether the cited page says what the sentence claims: the Sources list in the report is there for that review. Sentence splitting is heuristic (abbreviations and initials are kept whole). |
| At least 5 distinct sources | **Technically enforced** | Distinct cited URLs, normalized like Tracker posting URLs; entries nothing cites don't count. Five pages of one site count as five. |
| Never under one page | **Technically enforced** | At least 500 words of body text (`min_words`), Sources section, headings and URLs not counted. |
| No LinkedIn | **Technically enforced** twice | The agent's web tools deny `permissions.denied_domains` (as for every internet-on Task), and the hook fails a result that cites one. |
| Interviewers: public professional work only, no contacting anyone | **Instruction-only** | The prompt says so; nothing checks what a sentence is about. The never-list (§1.6) and the agents' tool limits stop sending messages or logging in. |

### The Tracker (§1.7)

`posting-scan` agents never write the Tracker (`applications.csv`). They
write Proposed rows to `output/proposed_rows.csv` in their own Task folder;
the `merge_tracker` post-hook (Nightshift code, after the Attempt) validates,
dedupes and appends them (`nightshift.tracker`).

| Limit | Status | How |
|---|---|---|
| Agents can't read or write the Tracker | **Technically enforced** | It lives in the data dir (`<agents_root>/data/applications.csv`), which is no-access for every Task. If `tracker.path` puts it elsewhere, its folder is added to the Task's no-access dirs (a clash with a read-only dir is a config error). |
| Only valid rows are appended, always `status=new` | **Technically enforced** | Header must be the Tracker's columns; required fields; `posting_url` must be http(s) and not on a denied domain (no LinkedIn rows); season must be `templates.posting_scan.season`; dates `YYYY-MM-DD`. `id`, `status` and `resume_version` are Nightshift's: the agent's values are ignored. Values are flattened to one line, capped at 500 chars, and a leading `=`, `+` or `@` gets a `'` so a later spreadsheet sync can't run a formula. |
| §1.8 match rules | **Instruction-only**, with a **best-effort** backstop | The agent applies the rules (prompt). Nightshift rejects rows whose season isn't the configured one, and roles whose title says PhD/MS-only, new grad, full-time or clearance (word patterns; a title that doesn't say it passes). |
| No duplicate Tracker rows | **Technically enforced** | Dedupe by normalized `posting_url`, then normalized `(company, role, season)`, against the Tracker and within the file; under a file lock; the file is rewritten via a temp file and an atomic rename, existing rows copied byte for byte. A Tracker whose header isn't the Tracker's columns is never touched (the Attempt fails). |
| The report's New postings are only rows really appended | **Technically enforced** | Written by Nightshift from the `tracker_merges` record, not from anything the agent wrote. |

## The dashboard's write surface

The dashboard (`nightshift.dashboard`) is a separate launchd process. Agent
shell commands have no network, so they can't reach it. An internet-on
Task's web fetch tool could GET a page from localhost; those tools only
GET, and an action also needs a same-origin POST (below). What the guard
is mainly for is a web page in *your* browser, or someone else on the
network, driving the buttons. Since Phase 3 it has one write surface:
the Task controls (`POST /tasks/<id>/cancel|pause|resume|retry|priority`),
the inbox's Approve / Deny (`POST /tasks/<id>/approve|deny`) and the
Schedule controls (`POST /schedules/<name>/enable|disable|edit`), which call
`nightshift.control` with `via: dashboard` and do nothing else.

| Limit | Status | How |
|---|---|---|
| Not reachable off this Mac and your tailnet | **Technically enforced** | Binds 127.0.0.1 and this Mac's Tailscale IPv4 only (`check_bind_address` refuses anything else before a socket opens); no public URL. Anyone on your tailnet can reach the page; Tailscale ACLs are what limit that. |
| DNS rebinding | **Technically enforced** | Host header allowlist (`localhost`, `127.0.0.1`, the Tailscale address, `*.ts.net`); anything else is a 400, actions included. |
| Changes only by POST | **Technically enforced** | The action routes accept POST only (a GET is a 405); every other route is GET only. |
| Cross-site requests (CSRF) | **Technically enforced** | Each action needs an `Origin` that is exactly `http://` + the request's `Host` (scheme, name and port; the name one of the allowed hosts); a missing or `null` Origin is refused. `Sec-Fetch-Site`, when the browser sends it, must be `same-origin`. And the form must carry the CSRF token, 32 random bytes made when the dashboard process starts, compared in constant time. A refusal is a 403 that changes nothing and carries no token. |
| What a control can do | **Enforced by Nightshift** | Exactly what `orch cancel|pause|resume|retry|priority` can, with the same state checks and refusals; no new power. Each change is recorded with `(dashboard)` in its reason or intervention record, so the clean-week check counts it. |
| What Approve / Deny can do | **Enforced by Nightshift** | Exactly what `orch approve|deny` can: decide a plan awaiting approval (or re-approval) or an agent's "needs you" request, recorded in `approvals` with `via: dashboard`. A Task held by a spend cap has no buttons; raising a cap stays `orch raise-cap`. Each button names the request it was shown for (the Task's last transition); if that request was decided elsewhere, or the Task moved on or asked again since, the press only reports the Task's state and decides nothing. |
| What the Schedule controls can do | **Enforced by Nightshift** | Exactly what `orch schedule enable\|disable\|edit` can: turn a Schedule on or off, or change its timing, Catch-up and Overlap rules, each value checked as `orch schedule add` checks it (a bad one refuses the whole edit and changes nothing). Not its template, request, agent or priority, and no add, remove or run-now: those stay on the command line. Each change is an intervention record (`dashboard schedule enable\|disable\|edit`) the clean-week check lists. |
| Script injection | **Technically enforced** | Jinja autoescaping on every page and fragment; HTMX 2.0.4 is vendored and served by the dashboard (no CDN), with `allowEval` and `allowScriptTags` off and `selfRequestsOnly` on. |
| No login | **Gap (by design)** | Single user: anyone who can open the page (this Mac, your tailnet) can press the buttons. There is no account or session. |
| Token lifetime | **Gap (minor)** | The token lasts as long as the dashboard process; it is not tied to a session or rotated per page. A page left open across a dashboard restart gets "stale CSRF token (reload the page)". |

## Limits

### Files (§1.4)

| Limit | Status | How |
|---|---|---|
| The agent writes only its own Task folder | **Technically enforced** (verified) | File tools: `--restricted` refuses paths outside the working dirs. Bash: the sandbox allows writes only in the working directory. A Write and a `echo > file` outside the folder both failed. |
| Read-only folders (`~/Documents/career/`, `~/Documents/comedy/`, `~/school/`) can be read, never written | **Technically enforced** (verified) | `Edit(//<dir>/**)` deny rules (they cover Write too) and sandbox `denyWrite`. Edit, Write and `echo >>` all failed; `cat` worked. A folder that doesn't exist is not added, and stays denied for writes. |
| `~/agents/data/` is inaccessible | **Technically enforced** (verified) | `Read`/`Edit` deny rules, `--restricted` (it's outside the working dirs) and sandbox `denyRead`/`denyWrite`. `Read` and `cat` both failed. Always on, whatever the config says. |
| `~/.ssh`, `~/.config/nightshift`, Keychain files, browser profiles are inaccessible | **Technically enforced** for files | Listed in `permissions.no_access_dirs`; same rules as the data dir. |
| Everything else in your home folder and `~/agents/` is inaccessible | **Technically enforced** (verified) | File tools: `--restricted`. Bash: sandbox `denyRead` on `~` and `agents_root`, with `allowRead` only for the Task folder, the read-only folders and `permissions.tool_dirs` (tool installs such as `~/.nvm`, so `node` still runs). `ls ~/Documents` failed. |
| Files outside your home folder (`/etc`, `/usr`, `/opt/homebrew`, `/Volumes/...`) | **Readable by Bash** (gap) | The sandbox denies reads only where told to. Denying `/` would break every tool. The file tools still can't reach them. |
| Bash can write Claude Code's own temp dir (`/tmp/claude-<uid>`) | **Gap** | The sandbox allows it for `$TMPDIR`. Other `/tmp` paths are blocked (verified). Nothing private lives there, but other Claude sessions share it. |
| Keychain items | **Best effort** | Keychain *files* are unreadable, but the Keychain is reached through a system service, not files. `security` is on the Bash deny-list (best effort); the Seatbelt profile may not block the service itself. |

### Internet (§1.4)

| Limit | Status | How |
|---|---|---|
| Internet default by Task type (on for research, scraping, job; off for coding, comedy) and `--internet on\|off` overrides | **Technically enforced** | `task_types.<type>.internet` in config; the Task's `internet` field decides the tool list. Unknown types default to off. |
| Internet-off Tasks have no web tools | **Technically enforced** | `WebFetch`/`WebSearch` aren't in `--tools`, so they don't exist. |
| No network from shell commands, on every Task | **Technically enforced** (verified) | Sandbox `network.allowedDomains: []`: `git ls-remote https://github.com/...` failed with a sandbox network violation. Internet-on Tasks get web access only through WebFetch/WebSearch. This also means `pip`/`npm install` from the network don't work inside Tasks. |
| Network commands in Bash (`curl`, `wget`, `nc`, `python -c`, ...) are denied | **Best effort** (verified for `curl`, `python3 -c`) | `Bash(<cmd>:*)` deny rules from `permissions.denied_commands`. A deny-list matches command names, so `/usr/bin/curl`, a script file or an alias can get past it. It's a second layer: the sandbox network block above is what stops the traffic. |
| WebFetch never fetches LinkedIn | **Technically enforced** (verified with a stand-in domain) | `WebFetch(domain:linkedin.com)` and `WebFetch(domain:*.linkedin.com)` deny rules, for each of `permissions.denied_domains` (`linkedin.com`, `lnkd.in`, `licdn.com`). Verified with `example.org` in place of LinkedIn: `www.example.org` and `example.org` were denied, `example.com` was fetched. |
| WebSearch results may include LinkedIn links | **Gap** | WebSearch can't be filtered by domain through permission rules; it only returns titles and links, and fetching them is denied. |
| No LinkedIn scraping, ever (§1.6) | **Instruction-only** beyond the fetch block | The protocol says so. |
| Respect `robots.txt` and site terms | **Instruction-only** | The protocol says so; nothing checks it. |

### Never without approval (§1.6)

| Limit | Status | How |
|---|---|---|
| Push to GitHub, deploy, send messages, post, submit applications or coursework, log into accounts | **Technically enforced** for anything that needs network from the shell; **instruction-only** through the web tools | Shell commands have no network. WebFetch only reads pages (no forms, no POSTs). The protocol tells the agent to raise `needs_you.json`. `git push`, `gh` are also on the Bash deny-list. |
| Submit a job application (`resume-tailor`) | **Technically enforced** that it can't (no tool can submit: WebFetch only reads, shell has no network, no MCP or browser tools); **instruction-only** beyond that | The template prompt says never to submit, fill in forms, log in or contact anyone, and to say in Needs you that the application is ready for you to submit. |
| Spend money | **Technically enforced** that only `--agent gemini-api` Tasks spend, and that one stops at `spend.per_task_usd` ($2); **instruction-only** for anything else an agent might pay for | Subscription agents never get an API key: the Gemini plan path pins `security.auth.selectedType: oauth-personal` and blanks `GEMINI_API_KEY`/`GOOGLE_API_KEY`. On the API path every model call is read from the CLI's telemetry file, priced and recorded in `spend_events`; the Task is stopped (SIGTERM) and moved to `needs_you` once it reaches the cap. Gap: the CLI writes calls in batches every ~5 s, so a call or two may land after the cap before the stop, and prices are config placeholders (`spend.gemini_api_prices`). Daily and monthly caps are issue #25. |
| The AI Studio API key | **Technically enforced** | A secret (`.env` or Keychain, `agents.gemini-api.api_key_secret`), passed only in the CLI's environment, never in argv or settings files; registered for redaction, so it never reaches logs, stored commands or reports. Shell commands get it redacted from their environment (`security.environmentVariableRedaction`). The telemetry file sits in the agent-inaccessible settings folder and logs no prompts. |
| Install system packages with Homebrew | **Technically enforced** | `/opt/homebrew` isn't writable from the sandbox and there's no network; `brew` and `sudo` are also on the deny-list. |
| Delete files outside the Task folder | **Technically enforced** | Same as the write limit above. |
| Turn off permission checks | **Technically enforced** | `--restricted` refuses `bypassPermissions`, the settings set `disableBypassPermissionsMode: disable`, and the adapter refuses flags that would do it. |
| Coursework: never complete graded assignments | **Instruction-only** | The protocol of a course Task (study-guide) says so in its course section, and the template prompt repeats it. Nothing checks what the output is. |
| Each course's AI policy wins over everything else (§1.6) | **Instruction-only** for what the agent does; **technically enforced** that no course Task runs without one | A request must name exactly one course in `school.courses` with a `policy` (and `ai_allowed` not false), or `orch add` and the Telegram intake refuse it. The policy is read from config again before every Attempt and heads the agent protocol; if it has been removed the Attempt never starts and the Task fails with the reason. Following the policy's text is up to the agent. |
| study-guide reads only inside `~/school/` | **Technically enforced** at intake; the agent's read access is the usual read-only-folders rule | The slides folder is resolved (symlinks and `..` included) and refused unless it is inside `school.dir`, exists and holds a file, and `school.dir` is in `permissions.read_only_dirs`. The agent can still read the other read-only folders, as every Task can (§1.4). |
| Unreleased comedy material is never posted or shared | **Instruction-only** for the agent (it has no way to post: no shell network, read-only web); **technically enforced** for Telegram | A comedy Task's Telegram alert, and its line in a `status` or `list` reply, is built from its id and state only (`nightshift.alerts`, `nightshift.remote`): never its title, Result, failure reason or Needs you request. |

### Telegram (§2.12)

| Limit | Status | How |
|---|---|---|
| No port opened to the internet | **Technically enforced** | Nightshift only calls the Bot API (`sendMessage`, long-polling `getUpdates`); it never sets a webhook or listens on a socket. |
| Nightshift writes only to you | **Technically enforced** | Every alert and command reply goes to `telegram.user_id`, with secrets redacted. |
| Messages from anyone else are ignored and logged | **Technically enforced** | Only a text message whose sender is `telegram.user_id` is a command (`nightshift.remote`); one from another user is never answered or acted on and is logged with its sender ID, never its content. Your own commands are logged by their command word only. |
| Telegram commands change Tasks only as the CLI would | **Technically enforced** | Every command goes through `nightshift.control` with `via: telegram`: the same state checks and refusals, recorded as an intervention for the clean-week check. The update offset is stored before a command runs, so none is ever run twice. |
| Bot token and user ID never logged or written into reports | **Technically enforced** | The token comes from `.env` or the Keychain (`TELEGRAM_BOT_TOKEN`); the user ID is registered with the same redaction as secrets, applied to the daemon log, reports and alert texts. Bot API errors are built from the method and status, never the request URL (which holds the token). |
| A Telegram outage never fails or blocks a Task | **Technically enforced** | Alerts go to the `alerts` outbox first and are sent from their own daemon thread; a failed send is retried (backing off) until `telegram.alert_max_age_hours`, then dropped and logged. |

## Known gaps and side effects

- **Settings that fail validation are silently ignored** by `claude -p`
  (`claude --help`). A wrong key in the generated settings could drop the
  sandbox without any error. Mitigations: the generated shape is pinned by
  tests, and the real-agent tests prove the limits hold; re-run them after
  upgrading Claude Code.
- **Local git commits don't work inside a Task.** Claude Code's sandbox
  blocks writes to `.git/config` and `.git/hooks`, so `git init` and `git add`
  fail, and `git commit` needs a permission prompt that is auto-denied.
  §1.6 allows local commits, so this is stricter than the spec. Revisit when
  coding Tasks need commits (for example by running git outside the sandbox
  with tight allow rules).
- **Some shell constructs need approval and are auto-denied**, for example a
  heredoc written to a file. The agent protocol tells agents to write
  files with their file-writing tool instead.
- **Your global `~/.claude/CLAUDE.md`** may still be loaded into Task
  sessions (`--restricted` drops settings files, not memory files). It can't
  widen permissions, but it does add instructions.
- **Bash can read outside your home folder** (see the table). Nothing private
  should live there; external volumes under `/Volumes` would be readable.
- **Codex was verified on 0.163.0**, mostly through `codex sandbox` with the
  same overrides plus a few real `codex exec` runs; the model itself
  declined to try some escapes (it skipped a write outside the folder
  rather than run it), so those rows rest on the sandbox check. Codex
  sessions are still saved under `~/.codex/sessions` (needed for resume).
  Shell commands run as `/bin/zsh -lc`. Re-check after Codex upgrades.
- **Gemini was verified on 0.65.0** with scripted model responses and with
  a real model on a free-tier AI Studio key (Google sign-in is refused for
  the AI Pro account). Its shell sandbox has a hang bug on failing denied
  commands, which the stall guard turns into a Failed Attempt after 5
  minutes (see the table), and git is unavailable to Gemini Tasks.
