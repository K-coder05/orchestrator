# Agent adapters

All agents sit behind `nightshift.adapters.base.AgentAdapter` (§2.9):
`build_command`, `parse_stream`, `detect_rate_limit`, `session_id`,
`resume_command`, `usage_from_run`, `write_permissions`, plus
`planning_command`/`parse_planning_output` and
`probe_command`/`parse_probe_output`. Registered: `claude` (verified),
`codex` (verified on codex-cli 0.163.0) and `gemini` (verified on
gemini-cli 0.65.0 with real model calls on an AI Studio API key, see its
section). One adapter instance
is created per Attempt, so `session_id()` and `usage_from_run()` report what
that Attempt's stream contained. Adapters are looked up by name in
`nightshift.adapters.REGISTRY`; core code never names an agent.

Normalized events (`EventKind`): `text`, `tool_call` (one Step), `tool_result`
(that Step finished; `tool_use_id`, `is_error`), `file_change`,
`usage`, `error`, `session`, `rate_limit_info`, `result`, `other`.

## Claude Code

Verified on this Mac against **Claude Code 2.1.306** (`claude --version`),
using `claude --help` and real headless runs on 2026-10-20.

### Invocation Nightshift uses

```
claude -p --output-format stream-json --verbose \
  [--model <model>] \
  --permission-mode acceptEdits --permission-prompts none \
  --restricted --strict-mcp-config \
  --tools Bash,Read,Write,Edit,Glob,Grep[,WebFetch,WebSearch]  # web only when internet is on
  --settings <agents_root>/data/permissions/<task-id>/claude-settings.json \
  [--add-dir <read-only dir> ...] \
  --append-system-prompt "<agent protocol §2.10>"
```

The settings file holds the per-Task allow/deny rules and sandbox settings
(`ClaudeAdapter.write_permissions`); what each part enforces is in
`docs/safety.md`. Shape:

```json
{
  "permissions": {
    "allow": ["WebFetch", "WebSearch"],
    "deny": ["Read(//<data dir>/**)", "Edit(//<data dir>/**)", "Edit(//<read-only dir>/**)",
             "Bash(curl:*)", "Bash(python3 -c:*)", "WebFetch(domain:linkedin.com)",
             "WebFetch(domain:*.linkedin.com)"],
    "disableBypassPermissionsMode": "disable"
  },
  "sandbox": {
    "enabled": true, "autoAllowBashIfSandboxed": true, "allowUnsandboxedCommands": false,
    "network": {"allowedDomains": []},
    "filesystem": {"denyRead": ["<home>", "<agents_root>", "<no-access dirs>"],
                   "allowRead": ["<task folder>", "<read-only dirs>", "<tool dirs>"],
                   "denyWrite": ["<read-only dirs>", "<no-access dirs>"]}
  }
}
```

- Run with the Task folder as the working directory.
- The prompt is written to **stdin**, not passed as an argument (verified: `-p`
  reads the prompt from stdin). A prompt that starts with `-` can't be taken as
  a flag, and the prompt doesn't show up in `ps`.
- Resume: `claude -p --resume <session_id> ...` with the new prompt on stdin.
  Verified: a resumed session answered from the earlier session's context and
  kept the same `session_id`. Sessions are stored per working directory, so a
  resume must run in the same Task folder. Nightshift passes
  `--append-system-prompt` again on resume, so every Attempt carries the
  protocol.

### Planning calls (§2.4)

Free-form `orch add` makes one-shot planning calls (`planning_command`):

```
claude -p --output-format json [--model <agents.claude.planning_model>] \
  --tools "" --restricted --strict-mcp-config --permission-prompts none \
  --no-session-persistence --system-prompt "<planner instructions>" \
  --json-schema '<schema>'          # prompt on stdin, cwd <agents_root>/data/planning
```

- `--tools ""` leaves no built-in tools; the answer still arrives through
  structured output. `--system-prompt` replaces the default prompt.
  `agents.claude.extra_args` are **not** passed.
- Output is one JSON `result` object (same fields as the stream-json
  `result` line) plus **`structured_output`**: the answer, validated against
  `--json-schema`; `result` holds the same JSON as text. `stop_reason` was
  `tool_use` and `num_turns` 2 (the structured-output tool call). The parser
  falls back to parsing `result` if `structured_output` is missing.
- Errors look like Attempt errors: `is_error: true`, message in `result`,
  non-zero exit. Usage comes from `modelUsage` as for Attempts.
- Verified on 2.1.306 with haiku on 2026-10-20.

### Flags verified in `claude --help`

| Flag | What it does (from `--help`) | Used |
|---|---|---|
| `-p, --print` | Non-interactive: print response and exit. Skips the workspace trust dialog. | yes |
| `--output-format text\|json\|stream-json` | Output format, only with `--print`. | `stream-json` |
| `--verbose` | Override verbose mode. **Required**: `--print` with `--output-format=stream-json` fails with `Error: When using --print, --output-format=stream-json requires --verbose` (exit 1). | yes |
| `--input-format text\|stream-json` | Input format, only with `--print`. | no (text on stdin) |
| `--include-partial-messages` | Partial message chunks, only with stream-json. | no |
| `-r, --resume [value]` | Resume a conversation by session ID or name. | resume path |
| `--session-id <uuid>` | Use a specific session ID (must be a UUID). | no |
| `--fork-session` | When resuming, create a new session ID. | no |
| `--no-session-persistence` | Sessions not saved, can't be resumed (only with `--print`). | planning calls only (Attempts need resume) |
| `--allowedTools, --allowed-tools <tools...>` | Comma or space separated tool names to allow, e.g. `"Bash(git *) Edit"`. | no (allow rules are in `--settings`) |
| `--disallowedTools, --disallowed-tools <tools...>` | Comma or space separated tool names to deny. | no (deny rules are in `--settings`) |
| `--tools <tools...>` | Restrict the built-in tool set (`""` disables all). | yes: the only tools in the session |
| `--permission-mode` | `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`. | `acceptEdits` |
| `--permission-prompts host\|none` | Who answers permission prompts with `--print`. `none`: anything that would prompt is denied automatically. | `none` |
| `--add-dir <directories...>` | Additional directories to allow tool access to. | existing read-only dirs (writes denied by rules) |
| `--settings <file-or-json>` | Extra settings (permissions, sandbox) from a file or JSON. **Settings that fail validation are silently ignored with `-p`.** | yes: per-Task settings file |
| `--setting-sources user,project,local` | Which setting sources load. | no (`--restricted` already ignores them) |
| `--restricted` | Removes Bash/code-running tools and WebFetch unless `--tools` names them, ignores user/project/local settings, confines file tools to working dirs, refuses bypassPermissions. | yes |
| `--strict-mcp-config` | Only MCP servers from `--mcp-config`. | yes (none) |
| `--append-system-prompt <prompt>` | Append to the default system prompt. | yes: agent protocol |
| `--system-prompt <prompt>` | System prompt to use for the session (replaces the default). | planning calls only |
| `--json-schema <schema>` | JSON Schema for structured output validation (only with `--print`). | planning calls only |
| `--model <model>` | Alias (`fable`, `opus`, `sonnet`, `haiku` worked) or full name. | from config `agents.claude.model` |
| `--fallback-model <model>` | Fallback when the default model is overloaded. | no |
| `--max-budget-usd <amount>` | Max dollar spend on API calls (only with `--print`). | no (subscription) |
| `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions` | Bypass all permission checks. | **never** (§1.4) |
| `--bare` | Skip hooks, plugins, CLAUDE.md discovery, keychain reads; auth strictly via `ANTHROPIC_API_KEY`. | no: it disables subscription (OAuth) auth |

`--max-turns` is accepted by 2.1.306 but is **not listed** in `--help`;
Nightshift doesn't rely on it.

### stream-json output format

One JSON object per line. Every line carries `session_id`. Types observed:

| `type` / `subtype` | Contents | Normalized to |
|---|---|---|
| `system` / `init` | `session_id`, `cwd`, `model`, `permissionMode`, `tools`, `claude_code_version`, `apiKeySource` (`none` on subscription) | `session` |
| `assistant` | `message` (Anthropic message: `id`, `model`, `content[]`, `usage`). Content blocks: `text`, `thinking` (signature only), `tool_use` (`id`, `name`, `input`). One message `id` may span several lines (one per content block). On an API failure: `message.model == "<synthetic>"` and a top-level `error` string, e.g. `"model_not_found"`. | `text`, `tool_call`, `file_change` (for `Write`/`Edit`/`MultiEdit`/`NotebookEdit`, path from `input.file_path`), `usage`, `error` |
| `user` | `message.content[]` of `tool_result` blocks (`tool_use_id`, `content`, `is_error`), plus `tool_use_result` with structured detail (e.g. `{"type": "create", "filePath": ...}`) | `error` (tool error) when `is_error` |
| `system` / `permission_denied` | `tool_name`, `tool_use_id`, `decision_reason` (e.g. "no approval surface in this session; permission request denied automatically") | `error` with `permission_denied` |
| `system` / `thinking_tokens` | `estimated_tokens`, `estimated_tokens_delta` | `other` |
| `rate_limit_event` | `rate_limit_info`: `status` (`allowed` seen), `resetsAt` (Unix seconds), `rateLimitType` (`five_hour` seen), `overageStatus`, `isUsingOverage`, `unifiedWindows` (`five_hour`/`seven_day` each with `utilization` 0..1 and `resetsAt`) | `rate_limit_info` |
| `result` / `success` | Final line: `is_error`, `result` (final text), `num_turns`, `duration_ms`, `duration_api_ms`, `total_cost_usd`, `usage` (summed `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`, `server_tool_use.web_search_requests`/`web_fetch_requests`), `modelUsage` (per model: `inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`, `webSearchRequests`, `costUSD`), `permission_denials`, `terminal_reason` (`completed`, `api_error`), `api_error_status` | `result`, `usage`, `error` if `is_error` |

**Errors.** An API failure still ends with `type: result`, **`subtype:
"success"`** but `is_error: true`, `terminal_reason: "api_error"`,
`api_error_status` (e.g. `404`) and the message in `result`; the process exits
**1**, and stderr has a tag such as `[claude-code:unrecognized_model]
{"model":"no-such-model-xyz",...}`. So success is judged by `is_error` and the
exit code, never by `subtype` alone.

Recorded samples (paths and username scrubbed) are the test fixtures
`tests/fixtures/claude_stream.jsonl` and `tests/fixtures/claude_stream_error.jsonl`.

### Usage mapping (`usage_from_run`)

One `Usage` per model from `result.modelUsage`: input, output, cache-write and
cache-read tokens, and `costUSD` as the **reported cost-equivalent**. On a
subscription this is not money spent; the report shows it next to
"$0.00 (subscription)". Request count is the number of distinct assistant
`message.id`s in the Attempt (the API calls made; synthetic error messages
don't count). The raw per-model object is stored too, so no reported field
is lost.

An Attempt stopped before its `result` line (SIGTERM on a pause, the
runtime cap) still records usage: per model, the tokens of each assistant
message seen in the stream (largest value per message id; one message spans
several lines) and one request per message, with **no cost** (`cost_usd`
NULL, `raw: {"source": "stream messages"}`). Window sums count such rows as
"without cost".

### Rate limits (real format, recorded 2026-10-21)

**A real limit hit was observed** on 2.1.306 (Pro plan, haiku) at 00:52 PDT
on 2026-10-21, with the five-hour Window used up. Recorded sample:
`tests/fixtures/claude_limit_hit.jsonl`. `claude -p` refuses the request
at once (no retries) and ends the session; exit code **1**; stderr empty.
Three lines say so, in this order:

1. `rate_limit_event` with `rate_limit_info.status: "rejected"`,
   `rateLimitType: "five_hour"`, `resetsAt: <Unix seconds>`,
   `overageStatus`, `isUsingOverage: false`, and `unifiedWindows`
   (`five_hour.utilization` was `1.04`, `seven_day` `0.18`, each with its
   `resetsAt`).
2. A **synthetic assistant message** (`message.model: "<synthetic>"`, a UUID
   as `message.id`, zero usage) whose text is
   `You've hit your session limit · resets 3:20am (America/Los_Angeles)`,
   with top-level `error: "rate_limit"`, `is_api_error_message: true`,
   `api_error_status: 429`, **`api_error: "usage_limit_reached"`**,
   `api_error_params.rate_limit_info` (same shape as the event, without
   `unifiedWindows`) and `api_error_origin: "first_party"`.
3. The `result` line: `subtype: "success"` but `is_error: true`,
   `terminal_reason: "api_error"`, `api_error_status: 429`,
   `api_error: "usage_limit_reached"` (no `api_error_params`), the same text
   in `result`, `total_cost_usd: 0`, empty `modelUsage`.

The reset time was right: a Task run at 03:24 the same night went through.
`rateLimitType` values in the CLI's own schema: `five_hour`, `seven_day`,
`seven_day_opus`, `seven_day_sonnet`, `seven_day_overage_included`,
`overage`; `status`: `allowed`, `allowed_warning`, `rejected`. The schema
also says `error: "rate_limit"` without `usage_limit_reached` can be a
temporary server throttle, an org policy, or a 429 on an API key, and
carries no `rate_limit_info`.

`detect_rate_limit` (one hit per Attempt; the first report wins, a later
one adds a missing reset time):

- `rate_limit_event` with a status that is not `allowed`/`allowed_warning`
  -> `rateLimitType`, `resetsAt`. (`allowed` events arrive during normal
  runs; `allowed_warning` is not a hit.)
- An agent error (not a tool's) carrying `api_error_params.rate_limit_info`
  or `api_error: usage_limit_reached` -> type and reset from it, else from
  the text.
- Any other agent error with code `rate_limit`, HTTP 429, or usage-limit
  wording ("hit your limit", "usage limit", "limit reached", ...) -> the
  Window and reset time are read from the **text** if it has them:
  `resets 3:20am (America/Los_Angeles)`, `resets Oct 25, 3pm (Zone)`, the
  older `Claude AI usage limit reached|<epoch>`; "session"/"5-hour" ->
  `five_hour`, "weekly" -> `seven_day` (`_opus`/`_sonnet` when named). A
  clock time is the next such time; no zone means the Mac's zone. No reset
  time at all -> Nightshift probes.
- Tool output that mentions a rate limit is never a hit; the exit code
  alone is not used (1 is every error).

On a hit Nightshift gives the CLI `usage.limit_exit_wait_seconds` (10) to
end the session itself (it does so at once), so the `result` line with the
Attempt's usage is read before any SIGTERM.

### Probing (§1.5)

When a limit hit gave no parseable reset time, the paused Task is not just
retried: every `usage.probe_interval_minutes` (30) the daemon sends one
minimal probe request per agent (`probe_command`):

```
claude -p --output-format stream-json --verbose [--model <agents.claude.model>] \
  --tools "" --restricted --strict-mcp-config --permission-prompts none \
  --no-session-persistence --system-prompt "Answer with the single word OK."
# stdin "OK?", cwd <agents_root>/data/probe, timeout usage.probe_timeout_seconds (120)
```

`parse_probe_output` runs the stream through the same parser: a limit hit
-> still out (with the reset time if it now has one: the paused Tasks wait
for it); a successful `result` -> available (paused Tasks are queued); else
the probe failed (the Task is queued and finds out for itself). Uses the Task
model, since some limits are per model. Verified for real on 2026-10-21: at
00:52 the probe reported `five_hour`, reset 03:20 PDT; at 03:24 it answered
OK (haiku: 612 cache-write tokens, about $0.0001 cost-equivalent).
`orch usage --probe claude` runs one by hand. Probe usage is stored in
`planning_usage` (kind `probe`), every probe in `probes`.

### Usage Windows

Every `rate_limit_event` (also `allowed` ones) updates `window_status`:
per Window the latest `resetsAt`, `utilization` and status. `utilization`
is plan-wide (interactive use included); `orch usage` shows it as "AGENT
REPORTS". The current Window is `[resetsAt - length, now]` while that reset
is ahead, else the trailing `length` (config `usage.windows.claude`:
`five_hour: 5`, `seven_day: 168` hours).

### Pause and resume (§2.6)

- On a limit hit, or `orch pause`, Nightshift waits until no Step is in
  flight (every `tool_use` id has its `tool_result`), at most
  `usage.step_grace_seconds` (120), then SIGTERMs `claude` (exit 143). With
  a real limit the CLI usually ends the session itself first.
- **A session ended by SIGTERM resumes fine.** Verified 2026-10-21 on
  2.1.306 with haiku: an Attempt stopped right after its 2nd Step (a Write)
  was resumed with `--resume <same id>` 30s later; the resumed session kept
  the same `session_id`, knew what it had done (it checked `output/a.txt`),
  and finished the remaining files.
- **Resume of an unknown session** (verified): stderr `No conversation found
  with session ID: <id>`, then one `result` line with `subtype:
  "error_during_execution"`, `is_error: true`, `num_turns: 0`, no `result`
  text and the message in an **`errors`** list; exit 1. `result_error()`
  reads `errors` when `result` is empty. Nightshift treats a resume that
  fails before any Step as "resume failed" and starts a fresh session.

### Behaviour notes

- With `--permission-mode acceptEdits` and `--permission-prompts none` alone,
  read-only Bash commands ran without being in `--allowedTools`, so
  `--allowedTools` isn't an exhaustive allowlist. Hence `--tools` (tools not
  named don't exist), `--restricted` and the sandbox.
- **Permission rule paths:** `Edit(/abs/path/**)` is relative to the settings
  file and did *not* match (an Edit in a "denied" folder went through).
  Absolute paths need `//`: `Edit(//abs/path/**)` was denied with "File is in a
  directory that is denied by your permission settings." `Edit` rules also
  block `Write`.
- `--restricted` refuses file tools outside the working dirs with
  `permission_denied` / "--restricted: path outside the working directory".
- Sandbox blocks show up as the shell's own error (`operation not
  permitted`, exit 1); a blocked network connection as `CONNECT tunnel failed,
  response 403` plus a `<sandbox_violations>` block, and a `permission_denied`
  event with `tool_name: SandboxNetworkAccess`.
- Sandbox `filesystem.allowRead` re-allows paths inside a `denyRead` root
  (`~` denied, `~/.nvm` allowed: `node --version` ran, `ls ~/Documents` failed).
  `~` is accepted in sandbox paths; Nightshift passes resolved absolute paths.
- Bash's `$TMPDIR` inside the sandbox is `/tmp/claude-<uid>` (writable);
  other `/tmp` paths are not.
- The sandbox blocks writes to `.git/config` and `.git/hooks`, so `git init`
  fails in a Task. A heredoc redirected to a file was also auto-denied.
  Re-checked on 2.1.311 (2026-10-27, #46): every git command first failed
  with `fatal: unable to access '~/.gitconfig': Operation not permitted`
  (home is unreadable). With `GIT_CONFIG_GLOBAL=/dev/null` in the agent's
  environment and a repo Nightshift created (`git init --template=`),
  `git add`, `git commit` and `git checkout -b` work without any allow
  rule; `git config` (`could not lock config file`) and writing
  `.git/hooks/pre-push` still fail.
- Internet-on Tasks get explicit `WebFetch`/`WebSearch` allow rules so a
  fetch never hits a (auto-denied) per-domain prompt. Denied domains get both
  `WebFetch(domain:x)` and `WebFetch(domain:*.x)`; with both, `x` and `www.x`
  were denied ("WebFetch denied access to domain:www.x") and other domains
  were fetched. Deny wins over allow.

- **Stopping an Attempt** (runtime cap, `orch cancel`, daemon stop): Nightshift
  sends SIGTERM; real `claude` 2.1.306 exited 143 within about a second
  (checked 2026-10-21 with haiku: a 1-minute cap stopped a `sleep 100` Task
  twice, and `orch cancel` stopped a running Task in about 1s). An agent that
  ignores SIGTERM is SIGKILLed after 15s. A Codex adapter needs no change for
  this; it goes through the same watchdog.

### Web fetch on big pages (posting-scan, 2026-10-21)

WebFetch hands a page to a small model with the agent's question and returns
its answer, not the raw page. On a ~140 KB internship-tracker README, haiku
read it "in two filtered chunks" and two runs an hour apart proposed
different subsets (186 vs 204 rows, 30 of run 2's not in run 1). Treat a
posting-scan as a sample, not an exhaustive read; the Tracker merge dedupes
whatever comes back. Shell network is off, so the agent can't `curl` the
raw file instead.

### Known gap: interactive use is invisible to Nightshift

Nightshift records usage only from its own calls (Attempts, planning calls,
probes), never from interactive use of the same plan (the Claude app,
`claude` in a terminal, this Mac or another device). So:

- usage recorded in a Window, and at a limit hit, **undercounts** the
  Window; Baselines learned from Limit observations err **low**
  (conservative: Nightshift would stop earlier, never later);
- a Window used up interactively gives a Limit observation with little or
  no recorded usage; observations with no reported cost are skipped for the
  Baseline;
- the plan-wide `utilization` in `rate_limit_event` does include
  interactive use; `orch usage` shows it next to Nightshift's own numbers,
  but Baselines and thresholds don't use it (yet);
- the thresholds (80% of a Baseline, the daytime 75% reserve) compare
  Nightshift's own usage in the current Window with that Baseline, so
  interactive use earlier in the same Window is not counted against them.

## Codex CLI

> **Verified against codex-cli 0.163.0 on this Mac (2026-10-21)**, signed in
> with the ChatGPT plan: `codex --help`, `codex exec --help`, `codex exec
> resume --help`, `codex features list`, `codex debug models`, the
> permission profile via `codex sandbox` (no model call) and six short real
> runs on `gpt-6-luna` with `model_reasoning_effort = "low"` (a hello, a
> sandbox audit, a resume, a probe, two Acceptance Test 3 Attempts). What
> is still from the docs/source only: the usage-limit message formats (no
> real hit seen) and planning calls with `--output-schema` (not run).

The section was first written from these sources (before a CLI existed here):

Sources used:

- Non-interactive mode docs: <https://learn.chatgpt.com/docs/non-interactive-mode>
  (was developers.openai.com/codex/noninteractive): `codex exec`, `--json`,
  `-` for a stdin prompt, `--ephemeral`, `--skip-git-repo-check`,
  `--output-schema`, `-o`, `codex exec resume <SESSION_ID>`, the default
  read-only sandbox, `--full-auto` no longer supported, the event list and
  sample lines.
- Config reference: <https://learn.chatgpt.com/docs/config-file/config-reference.md>
  (`approval_policy`, `sandbox_mode`, `default_permissions`,
  `permissions.<name>.filesystem|network`, `web_search`, `features.*`,
  `shell_environment_policy.*`, `developer_instructions`,
  `history.persistence`, `check_for_update_on_startup`).
- Permission profiles (beta): <https://learn.chatgpt.com/docs/permissions.md>
  (`read`/`write`/`deny`, `deny` wins on the same path, more specific
  paths win, tokens `:root`, `:minimal`, `:workspace_roots`, `:tmpdir`,
  `:slash_tmp`; `[permissions.<name>.network] enabled`; domain rules only
  through the network proxy).
- Source: `codex-rs/exec/src/cli.rs` (exec flags, `resume` args),
  `codex-rs/utils/cli/src/shared_options.rs` (`-m`, `-s/--sandbox`,
  `--add-dir` = extra **writable** dirs, `--yolo`, `--approve-for-me`,
  `-C`, `-p/--profile`), `codex-rs/utils/cli/src/config_override.rs`
  (`-c key=value`, value parsed as TOML, **falls back to a raw string if
  it doesn't parse**), `codex-rs/exec/src/exec_events.rs` and
  `event_processor_with_jsonl_output.rs` (the `--json` events),
  `codex-rs/exec/src/lib.rs` (exec defaults to `approval_policy = never`),
  `codex-rs/protocol/src/error.rs` (usage-limit messages).

### Invocation Nightshift uses

```
codex exec --json --skip-git-repo-check --ignore-user-config --ignore-rules --strict-config \
  [-m <agents.codex.model>] [-c model_reasoning_effort="<agents.codex.reasoning_effort>"] \
  -c approval_policy="never" \
  -c default_permissions="nightshift" \
  -c 'permissions.nightshift.filesystem={":root" = "read", ":minimal" = "read",
        "<home>" = "deny", "<agents_root>" = "deny", "<tool dirs>" = "read",
        "<read-only dirs>" = "read", "<no-access dirs>" = "deny",
        ":workspace_roots" = {"." = "write"}, "<task folder>" = "write"}' \
  -c 'permissions.nightshift.network={"enabled" = false}' \
  -c web_search="disabled"            # "live" on internet-on Tasks
  -c features.apps=false -c features.plugins=false -c features.remote_plugin=false \
  -c features.browser_use=false -c features.browser_use_external=false \
  -c features.in_app_browser=false -c features.computer_use=false \
  -c features.image_generation=false -c features.hooks=false \
  -c check_for_update_on_startup=false \
  -c history.persistence="none" \
  -c shell_environment_policy.inherit="core" \
  -c shell_environment_policy.ignore_default_excludes=false \
  -c 'shell_environment_policy.set={"TMPDIR" = "<task folder>/.tmp"}' \
  -c developer_instructions="<agent protocol §2.10>" \
  -                                    # prompt on stdin, cwd = Task folder
# resume: the same flags, then: resume <thread_id> -
```

- Every setting goes in as a `-c` override; `--ignore-user-config` keeps
  `~/.codex/config.toml` (its MCP servers, sandbox and approval settings,
  hooks) out, while auth still comes from `CODEX_HOME` (`codex login` with
  the ChatGPT plan). `--ignore-rules` keeps user/project execpolicy
  `.rules` files out (an `allow` rule there could run a command outside
  the sandbox). `write_permissions` also writes the same settings to
  `<agents_root>/data/permissions/<task-id>/codex-config.toml` as an audit
  copy; Codex never reads that file.
- `-c` values are serialized as inline TOML (`toml_value`; tested to parse
  back with `tomllib`). Because Codex treats an unparseable value as a raw
  string, a quoting bug would be silent; the tests pin the shape.
- `--strict-config` makes Codex fail on config fields it doesn't know (a
  renamed key fails the Attempt instead of silently dropping a limit).
  Verified: every key above is accepted by 0.163.0, and an unknown one
  fails at once with `Error loading config.toml: unknown configuration
  field `x` in -c/--config override` (exit 1, no model call, nothing on
  stdout).
- **Default-on features switched off.** 0.163.0 enables `computer_use`,
  `browser_use`, `browser_use_external`, `in_app_browser`, `apps`
  (ChatGPT connectors), `plugins`, `remote_plugin` and `image_generation`
  by default (`codex features list`). They could reach past the shell
  sandbox (desktop/browser control, remote connectors), so every run sets
  them false (`DISABLED_FEATURES`). Real exec runs used only the `exec`
  (code-mode) tool.
- **Model.** `codex debug models` lists gpt-6.1-sol, gpt-6-astra,
  gpt-6-sol, gpt-6-luna ("fast and affordable"), gpt-5.6-sol/terra/luna.
  `agents.codex.model: null` uses the CLI default; the real checks used
  `gpt-6-luna` with `agents.codex.reasoning_effort: low`
  (`-c model_reasoning_effort="low"`), the cheapest combination. A tiny
  sandbox-audit turn was still ~80k input tokens (mostly cached): Codex's
  system context is large.
- The prompt goes on stdin with `-`; the agent protocol goes in
  `developer_instructions` (stored commands show it as `<agent protocol>`).
- No `--sandbox`: the custom permission profile replaces it (the docs say
  `sandbox_mode` and `default_permissions` must not be combined). No
  `--add-dir`: it adds **writable** dirs; read-only dirs are `read`
  entries in the profile instead. No `--ephemeral` on Attempts (they must
  be resumable); planning calls and probes use it.
- Session id = the `thread_id` of `thread.started` (a UUIDv7 such as
  `01a1607f-2f68-70b0-8a15-3d716566f489`). Resume: `codex exec ... resume
  <thread_id> -` with all exec flags and `-c` overrides **before**
  `resume` (verified: the resumed turn reported the same `thread_id`,
  remembered the earlier turn, and its sandbox still refused a write
  outside the folder). A resume that fails before any Step falls back to a
  fresh session as for Claude.
- Sessions are saved under `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
  even with `history.persistence = "none"` (that only covers the prompt
  history file); `--ephemeral` runs save nothing.
- Shell commands run as `/bin/zsh -lc '<cmd>'` (the `command` field).
- `agents.codex.extra_args` may not contain `-c/--config`, `--sandbox`,
  `--yolo`, `--dangerously-bypass-*`, `--approve-for-me`, `--full-auto`,
  `--add-dir`, `--profile`, `--cd`, `--worktree`, `--oss`, `-a`.

### `--json` events (from `exec_events.rs`)

| Event | Fields | Normalized to |
|---|---|---|
| `thread.started` | `thread_id` | `session` |
| `turn.started` | - | `other` |
| `item.started` / `item.updated` / `item.completed` | `item: {id, type, ...}` | see items |
| `turn.completed` | `usage: {input_tokens, cached_input_tokens, cache_write_input_tokens (default 0), output_tokens, reasoning_output_tokens}` | `usage`, `result` |
| `turn.failed` | `error: {message}` | `error`, `result` (is_error) |
| `error` | `message` | `error` (not if it starts with "Reconnecting"/"Retrying": `other`) |

Items (`type`, snake_case): `command_execution` (`command`,
`aggregated_output`, `exit_code`, `status` in_progress/completed/failed/declined),
`file_change` (`changes: [{path, kind add|delete|update}]`, `status`),
`mcp_tool_call`, `collab_tool_call`, `web_search` (`query`, `action`) -
each is **one Step**: `tool_call` (id = item id) on the first
`item.started`/`item.completed` seen, `tool_result` (`tool_use_id`,
`is_error` for failed/declined) on `item.completed`; a completed
`file_change` adds a `file_change` event per path. `agent_message` ->
`text` (the last one is the final answer); `reasoning`, `todo_list` ->
`other`; `error` items are warnings (config/deprecation notices) -> `other`.

Verified on 0.163.0: exactly these events and fields. `item.started`
then `item.completed` for `command_execution` and `file_change` (with
absolute paths in `changes[].path`); `agent_message` only as
`item.completed`; no `reasoning` items at low effort; a failed command is
`status: "failed"` with its `exit_code` (e.g. curl's 6). A rejected
`apply_patch` produced no item at all (only a stderr log line). Item ids
are `item_0`, `item_1`, ... per turn. stderr is empty on a clean run.

Recorded sample: `tests/fixtures/codex_stream.jsonl` is the real stream of
Acceptance Test 3's Codex Attempt (Task folder prefix removed, so paths are
relative); `tests/fixtures/fake_codex.py` replays it.

### Usage mapping

From the last `turn.completed`: input = `input_tokens - cached_input_tokens`
(OpenAI counts cached tokens inside input), cache read =
`cached_input_tokens`, cache write = `cache_write_input_tokens`, output =
`output_tokens` (includes reasoning tokens), requests = turns seen, the raw
object stored. **Codex reports no cost**, so `cost_usd` is NULL: Codex
Limit observations get no cost and Baselines (cost-equivalent, §1.5) can't
form for Codex; tokens are recorded. **Known gap:** with no Baseline the
proactive thresholds (stop starting at 80% of a Window, the daytime
reserve) never apply to Codex; it runs until an actual limit error pauses
it, and `orch usage` shows `-` under OF BASELINE. `requests` is the number of turns
(one per Attempt), not model requests.

**`turn.completed` carries the thread's running total** (verified: a turn
reported 80,730 input / 355 output tokens, and the resumed turn after it
110,414 / 463; the session rollout's `token_count.total_token_usage`
grows the same way). So on a resume the runner hands the adapter the raw
total the session's earlier Attempts ended at (`adapter.prior_usage`,
from `usage_events.raw`) and `usage_from_run` records only the
difference; `raw` keeps the reported total. An Attempt stopped before
`turn.completed` records no usage, so its tokens land on the next resumed
Attempt of that session.

### Rate limits (from `codex-rs/protocol/src/error.rs`; no real hit seen yet)

A usage-limit hit is an `error` event and/or `turn.failed` whose message is
`UsageLimitReachedError`'s text, e.g.:

- `You’ve hit your usage limit. Try again at 3:20 PM.` (reset today, Mac local time)
- `You’ve hit your usage limit. Upgrade to Plus to continue using Codex (https://chatgpt.com/explore/plus), or try again at Apr 30th, 2026 11:21 AM.` (another day)
- `... To get more access now, send a request to your admin or try again at ...` (Team/Business plans)
- `... Try again later.` / `... or try again later.` (no reset known)
- older CLIs (forum reports): `Try again in 1 day 23 hours 37 minutes.`

plus `Quota exceeded. Check your plan and billing details.`, workspace
"out of credits" / "spend cap" messages and `exceeded retry limit, last
status: 429 Too Many Requests`. `detect_rate_limit` treats an agent error
(never a tool's output) matching usage limit / quota exceeded / rate limit
exceeded / 429 / too many requests / out of credits / spend cap as a hit
and reads the reset from "try again at|in ..." (note the curly `’`). The
message never names the Window, so the hit counts as Codex's shortest
configured Window (`usage.windows.codex`: `five_hour: 5`, `seven_day:
168`, assumed like Claude's). No reset -> Nightshift probes.

### Planning calls and probes

```
codex exec --json --skip-git-repo-check --ignore-user-config --ignore-rules --strict-config \
  --ephemeral [-m <model>] -c approval_policy="never" -c default_permissions=":read-only" \
  -c features.shell_tool=false -c features.<each DISABLED_FEATURES>=false -c web_search="disabled" \
  -c check_for_update_on_startup=false -c history.persistence="none" \
  -c developer_instructions="<system>" [--output-schema <tmp schema.json>] -
```

The probe ran for real (0.163.0, gpt-6-luna, low): all keys accepted, one
`agent_message` "OK", `turn.completed` with ~9.5k input tokens, exit 0 ->
available. Planning with `--output-schema` was not run for real.
The planning answer is the last `agent_message`, parsed as JSON; the
schema is made strict (every property required, `additionalProperties:
false`) for OpenAI structured outputs. The probe asks "OK?" with "Answer
with the single word OK."; a `turn.completed` -> available, a limit message
-> still out.

### Verification log (codex-cli 0.163.0, 2026-10-21)

1. Flags: every flag Nightshift passes exists in `codex exec --help` and
   `codex exec resume --help`. New in 0.163.0 and refused in `extra_args`:
   `--enable`/`--disable` (feature toggles), `--search`, `--remote`,
   `--remote-auth-token-env`, `--cyber-access-program`, `--local-provider`.
2. Stream: recorded (see above); the adapter's parser needed no change.
3. Permission profile: `default_permissions` + `permissions.nightshift.*`
   are accepted (no fallback to `sandbox_mode` needed). Checked with
   `codex sandbox <same -c overrides> -P nightshift -C <task> -- sh -c ...`
   and in real exec runs; results in docs/safety.md.
4. Resume by thread id: works (see Invocation).
5. Usage-limit message: **not observed** (the plan wasn't exhausted).
   When one happens, save the stream and compare with the formats above.
6. Acceptance Test 3 on the real CLIs: passed (docs/decisions.md, #15).

## Gemini CLI

> **Verified on gemini-cli 0.65.0 (2026-10-23) with real model calls**,
> signed in with an **AI Studio API key on the free tier**
> (`agents.gemini.auth: gemini-api-key`, the default). Google sign-in
> (`oauth-personal`, the AI Pro plan) still exists in the adapter, but
> Google refuses this client for that account: every run stops at sign-in
> with `IneligibleTierError: This client is no longer supported for Gemini
> Code Assist for individuals ... migrate to the Antigravity suite`. The
> adapter was first written from the 0.65.0 npm bundle (`// packages/...`
> source markers kept), then checked on the installed CLI with scripted
> model responses (the hidden `--fake-responses-non-strict <file>` flag:
> everything but the model is the real CLI), then end to end with the key
> ("Verification log" below). Two opt-in suites repeat the checks; re-run
> both after every Gemini CLI upgrade: `tests/test_gemini_cli.py`
> (`NIGHTSHIFT_GEMINI_CLI=1`, scripted, no key or quota needed) and
> `tests/test_gemini_real.py` (`NIGHTSHIFT_REAL_AGENT=1`, real calls on
> `GEMINI_API_KEY`).

### Invocation Nightshift uses

```
gemini --output-format stream-json --approval-mode default \
  --admin-policy <agents_root>/data/permissions/<task-id>/gemini-policy.toml \
  -e none --allowed-mcp-server-names nightshift-none \
  [--include-directories <read-only dir> ...] [-m <agents.gemini.model>]
# env: GEMINI_CLI_HOME=<same dir>/gemini-home   (its .gemini/settings.json is Nightshift's)
#      GEMINI_CLI_TRUST_WORKSPACE=false  GEMINI_SANDBOX=false
#      GEMINI_API_KEY=<secret agents.gemini.api_key_secret>  GOOGLE_API_KEY=""
#        (Google sign-in: both "")
#      TMPDIR=<task folder>/.tmp  PATH=<filtered, see below>
# process: its own process group (start_new_session); stops signal the group
# stdin: <agent protocol> \n\n---\n\n <prompt>        cwd = Task folder
# resume: the same, plus --resume <session_id>
```

All flags exist in the installed `gemini --help` (0.65.0); `--fake-responses*`
are hidden.

- **Headless and the prompt.** With stdin not a TTY the CLI runs headless;
  the input is stdin. Nightshift passes no `-p`. Gemini has no flag that
  appends to the system prompt (`GEMINI_SYSTEM_MD` *replaces* it), so the
  agent protocol goes in front of the prompt on every Attempt, resumes
  included (the `user` message line echoes it).
- **Sign-in** (`agents.gemini.auth`). `gemini-api-key` (default): the
  settings pin `security.auth.selectedType: "gemini-api-key"` and
  `GEMINI_API_KEY` carries the key from the secret named by
  `agents.gemini.api_key_secret` (default `GEMINI_API_KEY`, in
  `~/.config/nightshift/.env` or the Keychain; a key in your shell profile
  doesn't reach the launchd daemon). `GOOGLE_API_KEY` is blanked (the CLI
  would prefer it). Without the key the adapter refuses to build the
  command (Attempts, planning calls and probes). No Google sign-in files
  are linked in. `oauth-personal`: Google sign-in as below, both key
  variables blanked. The paid `gemini-api` path has its own secret
  (`GEMINI_PAID_API_KEY`), so the free key is never priced as spend nor
  the paid key used on the free path.
- **Settings: a per-Task CLI home** (changed after the real check). The
  plan was a *system* settings file via `GEMINI_CLI_SYSTEM_SETTINGS_PATH`;
  the real CLI **skips** it ("Security Warning: Skipping system settings
  file ... is not owned by root (uid 0)": every folder above a system
  settings file must be owned by root). Instead `GEMINI_CLI_HOME` points
  the CLI at `<settings dir>/gemini-home`, whose `.gemini/settings.json`
  (Nightshift's, rewritten each Attempt) becomes the *user* layer. Your
  `~/.gemini/settings.json`, policies, extensions, skills and history are
  then not read at all; on Google sign-in `oauth_creds.json` and
  `google_accounts.json` are symlinked in from your `~/.gemini` (seen: the
  run reaches Google's tier check with your account). The home is kept
  between Attempts, since the Task's sessions live in it. The only layer
  above it is a root-owned system settings file
  (`/Library/Application Support/GeminiCli/settings.json`), which only an
  admin can write.
- **Workspace settings never load; headless still runs.** With
  `GEMINI_CLI_TRUST_WORKSPACE=false` the Task folder is untrusted, so its
  agent-writable `.gemini/settings.json` is ignored (checked: one that
  switches the shell sandbox off had no effect on the next Attempt) and
  "Project hooks disabled because the folder is not trusted". But 0.65.0
  refuses to run headless in an untrusted folder ("Gemini CLI is not
  running in a trusted directory", exit 55) **unless** folder trust is off,
  so the settings set `security.folderTrust.enabled: false`; the env var
  still marks the folder untrusted. Side effect: **git commands are denied**
  ("Git command evaluated in untrusted workspace", ask, i.e. deny headless),
  and `.git` isn't writable from the sandbox anyway.
- **Approvals.** `--approval-mode default` headless: every "ask user"
  decision becomes **deny** (seen: "Tool execution denied by policy.").
  The admin-tier policy file (not subject to the root-ownership check) allows
  exactly the Task's tools. The shell allow rule needs `allowRedirection =
  true`: without it any `>`, `>>` or `2>&1` turns the allow into ask, i.e.
  deny (seen). The CLI still asks, so denies, for `rm -f/-rf`, `ln -s`,
  `find -exec/-delete` and commands run outside the workspace
  (`applyShellHeuristics`).
- **No extensions, MCP servers, hooks, skills or subagents.** `-e none`;
  `--allowed-mcp-server-names nightshift-none`; `hooksConfig.enabled`,
  `skills.enabled`, `experimental.enableAgents` false;
  `tools.discoveryCommand`, `tools.callCommand`, `mcp.serverCommand` empty.
- **Sessions** are stored in the CLI home,
  `gemini-home/.gemini/tmp/<project>/chats/session-<time>-<id>.jsonl`.
  `--resume <uuid>` keeps the **same session id** and appends to the same
  file (seen: one session file, same `init.session_id`, earlier turns
  kept), also after the previous Attempt was SIGKILLed mid-tool. A resume
  that can't find the session fails before any event with exit **42** and
  "Error resuming session: No previous sessions found for this project." (or
  "Invalid session identifier"); Nightshift then starts a fresh session.
  Planning calls and probes leave a session in the private temp CLI home.
- **Stops.** `gemini` relaunches itself as a child `node` process and the
  parent **does not pass SIGTERM on** (seen: after SIGTERM the child ran the
  rest of its turns to the end; after SIGKILL of the parent the orphaned
  child kept working on the Task folder until it finished). So Nightshift
  starts every agent in its own process group and signals the group: on
  SIGTERM the real CLI exits within a second (seen: exit 0, no `result`
  line), and whatever is left in the group is SIGKILLed when the Attempt
  ends. An Attempt stopped this way has no `result` line, so **no usage is
  recorded for it** (a pause loses the stopped Attempt's tokens; recorded
  usage undercounts), but the session resumes fine.
- **PATH.** `safe_path` drops entries whose readable folder would contain
  your home or `agents_root` (the sandbox opens each `PATH` entry, the
  *parent* of a `.../bin` one). That drops `/bin` too (its parent is `/`),
  and the real sandbox then couldn't start a shell at all
  ("sandbox-exec: execvp() of 'bash' failed"). So the programs of `/bin`
  are symlinked into `gemini-home/sysbin`, which goes on `PATH` instead
  (the base profile already lets the shell read `/bin`). Planning calls and
  probes keep your `PATH` unfiltered: they have no tools, and filtering it
  without `tool_dirs` dropped an nvm-installed `gemini`/`node`.
- `agents.gemini.extra_args` may not contain `--yolo`/`-y`,
  `--approval-mode`, `--sandbox`, `--policy`, `--admin-policy`,
  `--include-directories`, `--allowed-tools`, `--allowed-mcp-server-names`,
  `-e/--extensions`, `--skip-trust`, `--worktree`, `--resume`,
  `--session-id`, `-p`, `-i`, `-o`, `--raw-output`, `--fake-responses`.

### Per-Task settings and policy

`GeminiAdapter.write_permissions` writes both to
`<agents_root>/data/permissions/<task-id>/` (agent-inaccessible): the policy
file and `gemini-home/.gemini/settings.json`, fresh each Attempt:

```json
{"security": {"auth": {"selectedType": "gemini-api-key"},
              "toolSandboxing": true, "folderTrust": {"enabled": false},
              "disableYoloMode": true, "disableAlwaysAllow": true,
              "enablePermanentToolApproval": false, "autoAddToPolicyByDefault": false,
              "environmentVariableRedaction": {"enabled": true}},
 "tools": {"core": ["read_file", "read_many_files", "write_file", "replace", "glob",
                    "grep_search", "list_directory", "run_shell_command"],
           "sandbox": false, "sandboxNetworkAccess": false,
           "discoveryCommand": "", "callCommand": "",
           "shell": {"enableInteractiveShell": false}},
 "mcp": {"serverCommand": ""}, "hooksConfig": {"enabled": false},
 "skills": {"enabled": false},
 "experimental": {"enableAgents": false, "worktrees": false, "extensionReloading": false},
 "general": {"checkpointing": {"enabled": false}, "enableAutoUpdate": false,
             "enableAutoUpdateNotification": false, "defaultApprovalMode": "default"},
 "ide": {"enabled": false}}
```

`tools.core` is the list of built-in tools that exist at all (seen: a call
to `web_fetch` on an internet-off Task is `Tool "web_fetch" not found`);
internet-on Tasks add `web_fetch` and `google_web_search`. The policy file
(TOML, loaded as **admin** tier, which beats user and workspace policies;
within a tier the higher `priority` wins):

| Rule | Priority |
|---|---|
| `toolName = "*"`, deny | 0 |
| the Task tools, allow (`allowRedirection = true`) | 500 |
| `write_file`/`replace` whose `file_path` contains `..`, deny | 900 |
| `write_file`/`replace` into a read-only or no-access dir (absolute, `~/`, `$HOME/`), deny | 900 |
| `run_shell_command` whose command names such a dir, deny | 900 |
| `web_fetch`/`google_web_search` whose arguments mention a denied domain (any case), deny | 900 |

`argsPattern` is a JS regex matched against the arguments as compact JSON
with sorted keys (`"file_path":"/abs/path"`); `commandRegex` is matched as
`"command":"<regex>`. **The CLI drops any rule whose pattern looks like a
ReDoS risk** (`isSafeRegExp`: a group with a quantifier inside, followed
by a quantifier), printing only "Unsafe regex pattern (potential ReDoS)" on
stderr. The first shell rules (`(?:[^"\\]|\\.)*<dir>`) were all dropped
that way on the real CLI; they now use `.*<dir>` (which may also match the
`description` argument: that only denies more), and a unit test runs every
pattern through the same check. Seen on the real CLI: writes into a
read-only dir and shell commands naming the data dir are denied with
Nightshift's messages; LinkedIn fetches and searches too.

**Shell sandbox** (`security.toolSandboxing`): every `run_shell_command`
runs under `sandbox-exec` with a profile built per command: `(deny
default)`, read of system paths (`/System`, `/usr`, `/bin`, `/sbin`,
`/opt/homebrew`, `/Library`, ...), of node's install, of each `PATH` entry,
read+write of the Task folder, the include dirs, `$TMPDIR` (Nightshift's,
inside the Task folder), `/tmp` and `/private/tmp`, `.env` files denied,
`.git`/`.gitignore`/`.geminiignore` not writable, **no network**. Seen on
the real CLI: reads of a file in `~`, of `~/.gemini`, of the CLI home (the
linked sign-in) and of `agents_root/data` through a relative path all fail
with "Operation not permitted"; `curl` can't resolve hosts; a relative
write into a read-only dir succeeds (the known gap); `/tmp` is writable.

**Known 0.65.0 bug: a stopped command that fails hangs the run.** When a
shell command fails (non-zero exit) and its output looks like a sandbox
denial (a path outside the workspace with "Operation not permitted", or a
network error such as "Could not resolve host"), the CLI asks for a
"Sandbox Expansion Request" and **waits for an answer that never comes in
headless mode**: no more output, the process never exits (seen; the
scheduler calls `resolveConfirmation` directly, past the policy engine).
The confinement holds (nothing is granted). Commands whose last step
succeeds (`...; true`) don't hang.

**The stall guard** (`agents.gemini.stall_minutes`, default 5; `null` turns
it off) ends such an Attempt: when a Step is in flight (a `tool_use`
without its `tool_result`), the agent has written nothing and changed no
file for that long, and no process runs under the agent other than its own
(taken at its first line: the CLI and its relaunched `node` child), the
runner stops it as **stuck** ("... (stall guard)"). Since #43 the guard is
the silence trigger of stuck detection (§2.7, `nightshift.stuck`) with a
shorter threshold for this case: the Task continues once, told that it went
silent and which call it was on, and needs you if it hangs again (#17 made
it a Failed Attempt). Silence without a Step in flight (the CLI retrying
the model, seen for 3.5 minutes on HTTP 503s) only counts after
`stuck.silence_minutes` (15), and a long silent command (a process runs
under the CLI) never does. Seen on the real CLI: the hang ended by the
guard, a silent `sleep 12` left alone (`test_the_stall_guard_ends_the_hang`).
The threshold is generic (`AgentAdapter.stall_seconds`); only Gemini sets it.

The whole-CLI sandbox (`--sandbox`, `tools.sandbox`, `GEMINI_SANDBOX`) is
off: it would put the CLI's own network access and the shell's in one box.

### stream-json output format (seen on 0.65.0)

One JSON object per line, each with `timestamp`. Non-JSON noise
("Ripgrep is not available. Falling back to GrepTool.", startup metrics,
policy warnings) goes to stderr.

| `type` | Fields | Normalized to |
|---|---|---|
| `init` | `session_id`, `model` (the `-m` model, or `auto`) | `session` |
| `message` | `role` (`user` echoes the input; `assistant`), `content`, `delta: true` (assistant text arrives in chunks) | `text` (assistant) / `other` |
| `tool_use` | `tool_name`, `tool_id` (`<tool>__<tool>_<ms>_<n>`), `parameters` | `tool_call` (one Step) |
| `tool_result` | `tool_id`, `status` `success`/`error`, `output` (absent for `write_file`; the error text on failure), `error: {type, message}` (`invalid_tool_params`, `policy_violation`, `file_not_found`, `tool_not_registered`, ...) | `tool_result`; `error` (tool error) on failure; `file_change` for a successful `write_file`/`replace` (`file_path`) |
| `error` | `severity` `warning`/`error`, `message` (loop detected, ...) | `error` if severity is error, else `other` |
| `result` | `status` `success`/`error`, `error: {type, message}` on error, `stats` | `usage`, `result`, `error` |

Several tool calls of one model turn come as consecutive `tool_use` lines,
then their `tool_result`s. `stats`: `total_tokens`, `input_tokens`
(prompt, cached included), `output_tokens` (candidates), `cached`, `input`
(prompt minus cached), `duration_ms`, `tool_calls`, `models: {<model>:
{total_tokens, input_tokens, output_tokens, cached, input}}`. Thinking
tokens are in `total_tokens` only. With the default `auto` model the
router's call is its own entry (e.g. `gemini-3.1-flash-lite`) next to the
model it picked (`gemini-3.5-flash` in the recording); a failed router call
is listed with all zeros.

An API error during a turn ends the run with one `result` line,
`status: "error"`, `error.type: "unknown"` (the turn passes a plain
`{message, status}`, not an Error class) and `error.message`
`[API Error: <message>]`, then exit 1, or 173 for an HTTP 429 (429 & 0xff,
from the source). Errors thrown outside a turn keep their class name as
`error.type`. A sign-in failure (`IneligibleTierError`) writes **nothing**
to stdout: the stack trace goes to stderr, exit 1.

`tests/fixtures/gemini_stream.jsonl` is a recording of the real CLI
(Nightshift's settings, `auto` model, scripted responses: a failed
`read_file`, a `write_file`, a `run_shell_command`, two text chunks), with
the Task path and the prompt replaced; `tests/fixtures/fake_gemini.py`
replays it. Real model streams (2026-10-23) have the same lines and fields;
only the tool ids differ (`read_file__call_121276`), which Nightshift treats
as opaque, so the recording was kept. Real models also call tools that
don't exist here (`update_topic`, seen on `gemini-3.5-flash-lite`): a
failed Step ("Tool ... not found"), harmless.

### Usage mapping

Per model from `result.stats.models`: input = `input` (uncached prompt
tokens), cache read = `cached`, output = `output_tokens`; no cache-write
count, no request count, **no cost** (`cost_usd` NULL, so Gemini Limit
observations carry tokens but no cost-equivalent, and Baselines can't form,
as for Codex: only an actual limit error pauses Gemini). Entries with no
tokens at all are skipped. The stats are the process's own, so a resumed
Attempt reports only its own tokens (seen). An Attempt stopped before its
`result` line records no usage. **Real counts** (2026-10-23,
`gemini-3.5-flash-lite`): a one-word probe is ~3,600 input tokens and a
planning call ~5,900; a three-file Task was 36,388 uncached input, 109,382
cached and 1,979 output tokens over about 12 model calls, the cached part
growing turn by turn; thinking tokens appear only in `total_tokens`. With
the default `auto` model the router's own call is listed as a second model
(seen: `gemini-3.5-flash-lite` routing to `gemini-3.8-flash`).

### Rate limits (real API-key format, recorded 2026-10-23)

On the API key a quota error is the Gemini API's HTTP 429
(`RESOURCE_EXHAUSTED`), as one `result` line and exit **173**, recorded in
`tests/fixtures/gemini_api_key_quota.jsonl`:

```
{"type":"result","status":"error","error":{"type":"unknown","message":
 "[API Error: You exceeded your current quota, please check your plan and billing
 details. ... \n* Quota exceeded for metric:
 generativelanguage.googleapis.com/generate_content_free_tier_input_token_count,
 limit: 0, model: gemini-3.1-pro\n* Quota exceeded for metric:
 ..._free_tier_requests, limit: 0, model: gemini-3.1-pro\n...
 Please retry in 10h56m39.249192747s.]\nPlease wait and try again later. To
 increase your limits, request a quota increase through AI Studio, or switch
 to another /auth method"},"stats":{... all zeros ...}}
```

(stderr names it `TerminalQuotaError`; that type doesn't reach the stream).
The quotas behind it (from the raw API answer) are per model:
`GenerateRequestsPerDayPerProjectPerModel-FreeTier`,
`GenerateContentInputTokensPerModelPerDay-FreeTier` and their PerMinute
pairs; "retry in 10h56m" pointed at midnight Pacific, the daily reset. It
was seen by asking for `gemini-3.1-pro-preview`, which has **no** free-tier
quota (`limit: 0`): cheap to trigger, nothing spent. **A used-up quota
(`limit: N`, N > 0) was not observed**: that would mean burning a day's
free quota. Its text should differ only in the numbers; the fake CLI's
limit text is this recording with `limit: 20` and a 2 h retry time.

So `detect_rate_limit` also matches "exceeded your current quota" and the
CLI's API-key suffix ("To increase your limits, request a quota
increase"), and reads the reset time from "Please retry in 10h56m39.2s".
**`limit: 0` on every quota listed is not a limit hit**: the model can't be
used on this key's tier at all, so waiting can't help; the Attempt fails
with the API's text (a Failed Attempt) and the fix is `agents.gemini.model`.
The CLI retries per-minute 429s itself first.

The rest of this section is from the source; it also covers Google sign-in.

The CLI classifies Google API errors (`classifyGoogleError`):
`TerminalQuotaError` for a daily quota ("You have exhausted your daily quota
on this model."), `QUOTA_EXHAUSTED`, `MODEL_CAPACITY_EXHAUSTED`,
"exhausted your capacity", or a retry delay over 300 s;
`RetryableQuotaError` for per-minute quotas, which it retries itself first.
As seen above, such an error raised in a turn reaches the stream untyped
(`"unknown"`), so the text decides. Under Google sign-in the CLI appends to
any 429: "Possible quota limitations in place or slow response times
detected. Switching to the <flash> model for the rest of this session."

`detect_rate_limit` counts an agent error (never a tool's) as a hit when
its text says a quota or capacity is exhausted ("exhausted your (daily)
quota", "exhausted your capacity", "quota exceeded", "QUOTA_EXHAUSTED",
...) or carries that 429 note, or its type is `TerminalQuotaError`. A typed
`RetryableQuotaError` is not a hit; an untyped per-minute 429 that the CLI
gave up on will count as one (its reset is usually seconds away). The reset
time is read from the text: "Please retry in 34.5s"/"500ms", "Suggested
retry after 3600s.", "reset after 2h31m10s"; without one Nightshift probes.
Window: `daily` when the text says so, else the shortest configured Gemini
Window (`usage.windows.gemini: {daily: 24}`, the free tier's daily quotas;
the API-key text above doesn't say "daily", so it lands here too).

**Model fallback.** On a quota error the CLI may switch model; with action
`silent` it **rotates the session id**. Not seen on the API key (the quota
error above ended the run without a fallback); unverified on the plan.

### Planning calls and probes

The same base flags in a private temp CLI home
(`$TMPDIR/nightshift-gemini-<uid>/home`) with an empty `tools.core` and a
policy that denies `*`; no include dirs, no `TMPDIR` change. Gemini has no
JSON-schema flag, so the planner instructions and the schema go in the
prompt and the answer is the reply text parsed as JSON (a ```json fence is
stripped). The probe sends "Answer with the single word OK." / "OK?" on the
Task model. Both ran with real model calls on the API key: `orch add` got
an estimate and a Task type back as JSON, `usage.probe` found the Window
"available". Both need the key like an Attempt (without it they fail
cleanly). With the default `auto` model one test call took 3.5 minutes
while the CLI retried HTTP 503 "This model is currently experiencing high
demand" (`planning.timeout_seconds` is 180): pin `agents.gemini.model` /
`planning_model` to a model with spare capacity.

### The paid API path (`gemini-api`, AI Studio credits)

`GeminiApiAdapter`: the same invocation, settings and policy as above, plus

```
# settings: security.auth.selectedType = "gemini-api-key"   (always)
# env:      GEMINI_API_KEY=<secret agents.gemini-api.api_key_secret, default GEMINI_PAID_API_KEY>
#           GOOGLE_API_KEY=""
#           GEMINI_TELEMETRY_ENABLED=true  GEMINI_TELEMETRY_TARGET=local
#           GEMINI_TELEMETRY_OUTFILE=<settings dir>/gemini-api-calls.json
#           GEMINI_TELEMETRY_LOG_PROMPTS=false  GEMINI_TELEMETRY_USE_COLLECTOR=false
```

- **Auth** *(source)*: `validateNonInteractiveAuth` uses the configured
  `security.auth.selectedType` first, else the env (`getAuthTypeFromEnv`:
  `GEMINI_API_KEY` -> `gemini-api-key`). With both `GOOGLE_API_KEY` and
  `GEMINI_API_KEY` set the CLI uses `GOOGLE_API_KEY`, so it is blanked.
  Seen on the free path: a key signs in headless with the settings pinned
  to `gemini-api-key`. The paid path itself (billing, telemetry) is still
  unverified.
- **Per-call usage** *(source)*: 0.65.0 has no telemetry flags; settings
  `telemetry.*` lose to the `GEMINI_TELEMETRY_*` env. With an outfile the
  file exporters write (append) each span, log record and metric batch as
  `JSON.stringify(x, null, 2)` + newline; logs go through a
  `BatchLogRecordProcessor` (5 s delay) and are flushed at shutdown. Each
  model call is a log record whose `attributes` hold `event.name:
  "gemini_cli.api_response"`, `model`, `input_token_count`
  (promptTokenCount, cached included), `output_token_count`
  (candidatesTokenCount), `cached_content_token_count`,
  `thoughts_token_count`, `tool_token_count`, `total_token_count`,
  `auth_type`, `status_code`. Every call counts, including the CLI's own
  utility calls (loop detection, next-speaker checks).
- **Pricing** is Nightshift's (`spend.gemini_api_prices`), see
  docs/decisions.md (#20).
- **No planning calls, no probes**; `detect_rate_limit` is always None (a
  quota or credit error is a Failed Attempt).

### Verification log (gemini-cli 0.65.0)

2026-10-22, on the installed CLI with scripted model responses:

1. `gemini --version` is 0.65.0; every flag Nightshift passes is in
   `gemini --help`.
2. **Recording**: `tests/fixtures/gemini_stream.jsonl` is a real stream
   (scripted model responses, see above).
3. Settings and policy: the per-Task CLI home's settings load (the system
   settings path didn't, fixed); admin policy rules load once they pass the
   ReDoS check (fixed); tools limited to `tools.core`; the untrusted folder
   runs headless with folder trust off (fixed); workspace settings ignored.
4. Shell sandbox: no writes outside the Task folder (a relative write into a
   read-only dir gets through: known gap), no reads of `~`, `~/.gemini`,
   the CLI home or `agents_root/data`, no network; `bash` starts (fixed:
   `/bin` links); read-only dirs readable with `read_file`, not writable
   with `write_file`/`replace`; LinkedIn fetches and searches denied.
5. Resume by id after a stop keeps the session; a resumed Attempt reports
   only its own tokens.
6. `orch add ... --agent gemini --yes --simulate-rate-limit-after 2` paused
   and resumed the same session and finished `done` (scripted replies).

2026-10-23, **real model calls** on a free-tier AI Studio key
(`auth: gemini-api-key`), throwaway config dir and agents root, model
`gemini-3.5-flash-lite`:

7. **Sign-in**: the key signs in headless; Google sign-in still fails with
   `IneligibleTierError`. Shell commands don't see `GEMINI_API_KEY`
   (`echo $GEMINI_API_KEY` prints nothing; scripted check).
8. **End to end**: `orch add "<write a few small files, then report.md>" --agent
   gemini --yes --simulate-rate-limit-after 1` made a real planning call
   (estimate 2 min, Task type proposed), the daemon (`orch daemon run`)
   started Attempt 1, paused it after the first Step (`paused_usage`),
   resumed **the same session** (`--resume`, same `session_id`) a minute
   later, and the Task finished **`done` with report.md** (Result `done`,
   section 8: gemini, 2 Attempts). Run twice: the first time showed the
   SIGTERM problem above (the CLI worked on for 17 s, killed with -9); after
   the process-group fix the pause stopped Attempt 1 within a second (exit
   0) and Attempt 2 resumed and finished.
9. **Usage**: `usage_events` got one row per model per finished Attempt
   (e.g. 36,388 input / 1,979 output / 109,382 cache read), planning calls
   and the probe went to `planning_usage`, and `orch usage` shows the Gemini
   `daily` Window with the tokens ("not reported +N w/o cost": no
   cost-equivalent, no Baseline). The paused Attempt (stopped before its
   `result` line) recorded none.
10. **Probe**: `usage.probe` on the key: "available" (3,602 input tokens).
11. **Quota error**: the real API-key format (above), seen with a model
    that has no free quota; matched by `detect_rate_limit` (`limit: 0` is a
    failure, `limit: N` a hit with its reset time). A used-up quota was not
    triggered on purpose.
12. `tests/test_gemini_real.py` with a real model: the Task couldn't write
    outside its folder or into the read-only dir with file tools, couldn't
    read the data dir or a canary in `~`, `web_fetch` was "not found" on an
    internet-off Task, a relative shell write into the read-only dir got
    through (known gap); on an internet-on Task a denied domain was refused
    by policy, another fetched, `curl` had no network; pause and resume kept
    the session; a planning call and a probe worked.
13. **Stall guard** on the real CLI (scripted): the sandbox-expansion hang
    ended as a Failed Attempt within seconds; a 12 s silent command was not
    a stall. (Since #43 the same hang ends as a stuck Attempt; not re-run on
    the real CLI yet.)

2026-10-23, **real Claude -> Gemini failover** (issue #19), real Claude Code
2.1.309 (haiku) and gemini-cli 0.65.0 (`gemini-3.5-flash-lite`, API key),
throwaway config and agents root:

14. `orch add "<three small parts>" --type research --priority urgent --yes
    --simulate-rate-limit-after 5` and `orch daemon run --exit-when-idle`:
    Claude did Part 1 (and recorded it in `progress.md`) and wrote Part 2,
    was stopped after 5 Steps; the Task moved to gemini (`simulated`), the
    Gemini Attempt started a fresh session (no `--resume`) with the
    takeover note, read `plan.md` + `progress.md`, wrote only Part 3 and
    finished `done` with both agents in report section 8. Details in
    docs/decisions.md (issue #19).

### Still to verify

1. A **used-up** free-tier quota (`limit: N` > 0) through Nightshift: that a
   Limit observation is recorded with the reset time, and whether the CLI's
   model fallback rotates the session. Not triggered on purpose (it would
   burn a day's quota); the format is expected to match the recorded one.
2. The paid path (`gemini-api`): a billed key, the telemetry file's
   `api_response` records, and costs against AI Studio's billing.
3. A real `posting-scan` on Gemini (`test_posting_scan_on_gemini`, needs
   `NIGHTSHIFT_SCAN_REPO`). (The real Claude -> Gemini failover is item 14
   above; it was run through `orch`, the same steps as
   `test_claude_to_gemini_failover`.)
4. Google sign-in (`oauth-personal`), should Google let this client back in.
5. **The hang itself** stays until the CLI answers its own sandbox-expansion
   request headless; `test_a_denied_command_that_fails_hangs_the_cli` tells
   when a CLI upgrade fixes it (then the guard just never fires).
