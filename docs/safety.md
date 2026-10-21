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
| Spend money | **Instruction-only** for now | Spending caps arrive with paid credits (Phase 2). |
| Install system packages with Homebrew | **Technically enforced** | `/opt/homebrew` isn't writable from the sandbox and there's no network; `brew` and `sudo` are also on the deny-list. |
| Delete files outside the Task folder | **Technically enforced** | Same as the write limit above. |
| Turn off permission checks | **Technically enforced** | `--restricted` refuses `bypassPermissions`, the settings set `disableBypassPermissionsMode: disable`, and the adapter refuses flags that would do it. |
| Coursework: never complete graded assignments | **Instruction-only** | |
| Unreleased comedy material is never posted or shared | **Instruction-only** for the agent (it has no way to post: no shell network, read-only web) | Telegram handling arrives in Phase 2. |

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
  heredoc written to a file. Agents fall back to the Write tool.
- **Your global `~/.claude/CLAUDE.md`** may still be loaded into Task
  sessions (`--restricted` drops settings files, not memory files). It can't
  widen permissions, but it does add instructions.
- **Bash can read outside your home folder** (see the table). Nothing private
  should live there; external volumes under `/Volumes` would be readable.
- **Only Claude is covered.** Each new adapter (Codex, Gemini) must map the
  same `PermissionPolicy` and update this file with its own verified status.
