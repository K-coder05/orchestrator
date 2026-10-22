# Agent adapters

All agents sit behind `nightshift.adapters.base.AgentAdapter` (§2.9):
`build_command`, `parse_stream`, `detect_rate_limit`, `session_id`,
`resume_command`, `usage_from_run`, `write_permissions`, plus
`planning_command`/`parse_planning_output` and
`probe_command`/`parse_probe_output`. Registered: `claude` (verified),
`codex` (verified on codex-cli 0.163.0) and `gemini` (written from the
gemini-cli 0.65.0 source; real runs pending). One adapter instance
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
  and `git add` fail in a Task, and `git commit` is auto-denied (needs a
  prompt). A heredoc redirected to a file was also auto-denied.
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
  but Baselines don't use it (yet).

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
form for Codex yet; tokens are recorded. `requests` is the number of turns
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

> **Written from the gemini-cli 0.65.0 source (2026-10-22), not yet run
> against a signed-in CLI.** The CLI is not installed on this Mac yet. The
> npm package (`@google/gemini-cli@0.65.0`, the bundle keeps
> `// packages/...` source markers) was read for: `gemini --help` (the
> yargs options in `packages/cli/src/config/config.ts`), the stream-json
> events (`nonInteractiveCli.ts`, `output/stream-json-formatter.js`),
> resume (`utils/sessions.ts`, `sessionUtils.ts`), the settings layers and
> merge rules (`config/settings.ts`), folder trust, the policy engine
> (`policy/toml-loader.js`, `policy-engine.js`), the per-command shell
> sandbox (`sandbox/macos/*`, `services/sandboxManager.js`) and quota
> errors (`utils/googleQuotaErrors.js`, `errorParsing.js`). `--version`
> and `--help` ran from the unpacked package (0.65.0); no model call was
> made. Everything marked *(source)* below is still to be checked for
> real (see "Still to verify").

### Invocation Nightshift uses

```
gemini --output-format stream-json --approval-mode default \
  --admin-policy <agents_root>/data/permissions/<task-id>/gemini-policy.toml \
  -e none --allowed-mcp-server-names nightshift-none \
  [--include-directories <read-only dir> ...] [-m <agents.gemini.model>]
# env: GEMINI_CLI_SYSTEM_SETTINGS_PATH=<same dir>/gemini-settings.json
#      GEMINI_CLI_TRUST_WORKSPACE=false  GEMINI_SANDBOX=false
#      TMPDIR=<task folder>/.tmp  PATH=<filtered, see below>
# stdin: <agent protocol> \n\n---\n\n <prompt>        cwd = Task folder
# resume: the same, plus --resume <session_id>
```

- **Headless and the prompt.** With stdin not a TTY the CLI runs headless;
  the input is stdin (`-p` text would be appended after it). Nightshift
  passes no `-p`, so the prompt is only on stdin. Gemini has no flag that
  appends to the system prompt (`GEMINI_SYSTEM_MD` *replaces* it), so the
  agent protocol goes in front of the prompt on every Attempt, resumes
  included.
- **Settings.** `GEMINI_CLI_SYSTEM_SETTINGS_PATH` makes the per-Task file
  the *system* layer, which wins over user (`~/.gemini/settings.json`) and
  workspace settings key by key (`mergeSettings`: defaults < system
  defaults < user < workspace < system). Some keys merge instead of
  replacing: `context.includeDirectories` and `hooks.*` concatenate,
  `mcpServers` shallow-merges, `tools.exclude` and policy paths union.
  Hence the flags and switches below, and the refusal described in
  docs/safety.md (user `context.includeDirectories`).
- **Workspace settings never load.** `GEMINI_CLI_TRUST_WORKSPACE=false`
  makes `checkPathTrust` return untrusted, so `.gemini/settings.json` in
  the Task folder (agent-writable) is ignored, and approval mode can't
  leave `default`. `--skip-trust` (which sets it to `true`) is never
  passed and is refused in `extra_args`.
- **Approvals.** `--approval-mode default` in headless mode: the policy
  engine turns every "ask user" into **deny** (`nonInteractive`). The
  admin-tier policy file allows exactly the Task's tools.
- **No extensions, MCP servers, hooks, skills or subagents.** `-e none`;
  `--allowed-mcp-server-names nightshift-none` (an allow-list no server
  matches: `isBlockedBySettings`); `hooksConfig.enabled`, `skills.enabled`,
  `experimental.enableAgents` false; `tools.discoveryCommand`,
  `tools.callCommand`, `mcp.serverCommand` empty.
- **Sessions** are stored per project (working directory) under
  `~/.gemini/tmp/<project>/chats/`. `--resume <uuid>` resolves the session
  by id and **keeps the same session id** (`resolveSessionId`). A resume of
  an unknown id fails before the session starts ("Invalid session
  identifier"), which Nightshift treats as a failed resume (fresh session).
  Planning calls and probes also leave a session behind (there is no
  ephemeral flag); they run in `<agents_root>/data/planning` and `.../probe`.
- `agents.gemini.extra_args` may not contain `--yolo`/`-y`,
  `--approval-mode`, `--sandbox`, `--policy`, `--admin-policy`,
  `--include-directories`, `--allowed-tools`, `--allowed-mcp-server-names`,
  `-e/--extensions`, `--skip-trust`, `--worktree`, `--resume`,
  `--session-id`, `-p`, `-i`, `-o`, `--raw-output`, `--fake-responses`.

### Per-Task settings and policy

`GeminiAdapter.write_permissions` writes both files to
`<agents_root>/data/permissions/<task-id>/` (agent-inaccessible), fresh each
Attempt. Settings (system layer):

```json
{"security": {"toolSandboxing": true, "folderTrust": {"enabled": true},
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

`tools.core` is the list of built-in tools that exist at all; internet-on
Tasks add `web_fetch` and `google_web_search`. The policy file (TOML,
loaded as **admin** tier, which beats user and workspace policies; within a
tier the higher `priority` wins):

| Rule | Priority |
|---|---|
| `toolName = "*"`, deny | 0 |
| the Task tools, allow | 500 |
| `write_file`/`replace` whose `file_path` contains `..`, deny | 900 |
| `write_file`/`replace` into a read-only or no-access dir (absolute, `~/`, `$HOME/`), deny | 900 |
| `run_shell_command` whose command names such a dir, deny | 900 |
| `web_fetch`/`google_web_search` whose arguments mention a denied domain (any case), deny | 900 |

`argsPattern` is a JS regex matched against the arguments as compact JSON
with sorted keys (`stableStringify`: `"file_path":"/abs/path"`);
`commandRegex` is matched as `"command":"<regex>` (anchored at the start of
the command). The tests pin the patterns.

**Shell sandbox** (`security.toolSandboxing`): on macOS every
`run_shell_command` (and, with it on, file-tool reads/writes through
`SandboxedFileSystemService`) runs under `sandbox-exec` with a profile
built per command (`buildSeatbeltProfile`): `(deny default)`, read of
system paths (`/System`, `/usr`, `/bin`, `/opt/homebrew`, `/Library`, ...),
read of the Task folder, the include dirs and each `PATH` entry (the
*parent* of a `.../bin` entry), read+write of the Task folder (default
mode), the include dirs, `/tmp`, `$TMPDIR` and `~/.gemini/tmp`, `.env`
files denied, **no network** unless the mode or a per-command grant allows
it (`sandbox-default.toml`: `network = false` in every mode but yolo).
Nightshift sets `TMPDIR` inside the Task folder and filters `PATH`
(`safe_path`): entries whose readable folder would contain your home or
`agents_root` (e.g. `~/bin` opens all of `~`) are dropped, and entries
under home are kept only inside `permissions.tool_dirs`.

The whole-CLI sandbox (`--sandbox`, `tools.sandbox`, `GEMINI_SANDBOX`:
relaunch of the CLI in Docker/Seatbelt) is switched off: it would put the
CLI's own network access and the shell's in the same box.

### stream-json output format *(source)*

One JSON object per line (`StreamJsonFormatter`), each with `timestamp`:

| `type` | Fields | Normalized to |
|---|---|---|
| `init` | `session_id`, `model` | `session` |
| `message` | `role` (`user` echoes the input; `assistant`), `content`, `delta: true` (assistant text arrives in chunks) | `text` (assistant) / `other` |
| `tool_use` | `tool_name`, `tool_id`, `parameters` | `tool_call` (one Step) |
| `tool_result` | `tool_id`, `status` `success`/`error`, `output`, `error: {type, message}` | `tool_result`; `error` (tool error) on failure; `file_change` for a successful `write_file`/`replace` (`file_path`) |
| `error` | `severity` `warning`/`error`, `message` (loop detected, max turns, invalid stream, blocked by hook) | `error` if severity is error, else `other` |
| `result` | `status` `success`/`error`, `error: {type, message}` on error, `stats` | `usage`, `result`, `error` |

`stats`: `total_tokens`, `input_tokens` (prompt, cached included),
`output_tokens` (candidates), `cached`, `input` (prompt minus cached),
`duration_ms`, `tool_calls`, `models: {<model>: {total_tokens,
input_tokens, output_tokens, cached, input}}`. A fatal error (auth, quota,
config) goes through `handleError`: one `result` line with `status:
"error"`, `error.type` = the error's class name (e.g. `TerminalQuotaError`,
`FatalAuthenticationError`), `error.message` formatted by
`parseAndFormatApiError` (`[API Error: <message>]`, plus a "Please wait
and try again later..." suffix for a 429), then `process.exit(<code>)`
where the code is the error's `exitCode`, else its `code`/`status`, else
1 (a quota error's status 429 exits as **173**, 429 & 0xff).

`tests/fixtures/gemini_stream.jsonl` is shaped from the source, **not a
recording**; `tests/fixtures/fake_gemini.py` replays it. Replace it with a
recorded stream once the CLI is signed in.

### Usage mapping

Per model from `result.stats.models`: input = `input` (uncached prompt
tokens), cache read = `cached`, output = `output_tokens`; no cache-write
count, no request count, **no cost** (`cost_usd` NULL, so Gemini Limit
observations carry tokens but no cost-equivalent, and Baselines can't form
yet, as for Codex). Thinking tokens are in `total_tokens` but not
`output_tokens`; `raw` keeps the whole per-model object. The stats come
from the process's own telemetry (`uiTelemetryService`), so a resumed
Attempt should report only its own tokens *(to verify)*. An Attempt stopped
before its `result` line records no usage.

### Rate limits *(source; no real hit seen)*

The CLI classifies Google API errors (`classifyGoogleError`):

- **`TerminalQuotaError`**: a daily quota (`QuotaFailure` with a
  `PerDay`/`Daily` quota id -> "You have exhausted your daily quota on this
  model."), `QUOTA_EXHAUSTED`, `MODEL_CAPACITY_EXHAUSTED`, insufficient
  credits, "exhausted your capacity", or any retry delay over 300 s. Ends
  the run.
- **`RetryableQuotaError`**: per-minute quotas and short retry delays; the
  CLI retries these itself (backoff) before giving up.

`detect_rate_limit` counts an agent error (never a tool's) as a hit when
its `error.type` is `TerminalQuotaError`, or its text says a quota or
capacity is exhausted ("exhausted your (daily) quota", "exhausted your
capacity", "quota exceeded", "usage limit reached", ...).
`RetryableQuotaError` is **not** a Window hit (per-minute throttling): the
Attempt fails and is retried. The reset time is read from the text:
"Please retry in 34.5s"/"500ms" (Google's message), "Suggested retry after
3600s." (the CLI's, from `RetryInfo`), "reset after 2h31m10s". A daily
quota usually has none: Nightshift probes. Window: `daily` when the text
says so, else the shortest configured Gemini Window (`usage.windows.gemini:
{daily: 24}`; AI Pro quotas are per model per day).

**Model fallback.** On a quota or capacity error the CLI may switch to a
fallback model (`handleFallback`). With action `silent` it does so without
asking and **rotates the session id** (`config.rotateSessionId`), so the
`init` session id would no longer name the session that holds the later
turns. Whether this happens headless on the AI Pro plan is to be verified;
until then a resume after such a fallback may miss the last turns.

### Planning calls and probes

The same base flags with an empty `tools.core` and a policy that denies
`*`, written to a fresh temp folder; no include dirs, no `TMPDIR` change.
Gemini has no JSON-schema flag, so the planner instructions and the schema
go in the prompt and the answer is the reply text parsed as JSON (a ```json
fence is stripped). The probe sends "Answer with the single word OK." /
"OK?" on the Task model.

### Still to verify (needs the CLI installed and signed in)

`tests/test_gemini_real.py` (opt-in: `NIGHTSHIFT_REAL_AGENT=1 .venv/bin/python
-m pytest tests/test_gemini_real.py`, model from `NIGHTSHIFT_GEMINI_MODEL`)
covers item 4 (escapes, data dir, home, read-only dirs, shell network,
web fetch per internet setting), "tools run in an untrusted folder" of item
3, a resume of the same session after Nightshift stops an Attempt (item 5,
usage printed, not judged), a real Task to `done` with a report (item 7,
through `create_task`/`run_attempt`, not the `orch add` CLI), and a real
planning call and probe. The rest of item 3 (no warnings, tool list, MCP,
extensions and hooks off) and items 1, 2 and 6 are by hand.

1. `gemini --version` / `--help` from the installed CLI (0.65.0 read here).
2. A recorded stream of a real Task (replace `gemini_stream.jsonl`).
3. Settings and policy accepted (no warnings), tools limited to
   `tools.core`, MCP/extensions/hooks off; that an untrusted folder still
   runs tools headless.
4. The shell sandbox: writes outside the Task folder, reads of `~`, of
   `agents_root/data`, and network from the shell all refused; read-only
   dirs readable.
5. Resume by id after a SIGTERM; usage of a resumed Attempt.
6. A real quota hit: save the stream and compare with the format above;
   whether the model fallback happens headless.
7. `orch add ... --agent gemini` to `done` on the real CLI.
8. A real Claude -> Gemini failover (§2.8, issue #19): `test_claude_to_gemini_failover`
   in `tests/test_gemini_real.py` (also needs the signed-in `claude` CLI).
