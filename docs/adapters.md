# Agent adapters

All agents sit behind `nightshift.adapters.base.AgentAdapter` (§2.9):
`build_command`, `parse_stream`, `detect_rate_limit`, `session_id`,
`resume_command`, `usage_from_run`, `write_permissions`. One adapter instance
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
`message.id`s in the Attempt (the API calls made). The raw per-model object is
stored too, so no reported field is lost.

### Rate limits

- Seen so far: only `rate_limit_event` with `status: "allowed"`. It arrives
  during a normal run and includes window utilization and reset times.
- **Not yet observed: an actual limit hit.** `detect_rate_limit` treats as a
  hit (best guess; real formats still to be recorded here when one happens):
  a `rate_limit_event` whose `status` doesn't start with `allowed` (so
  `allowed_warning` is not a hit, `rejected` is), with `rateLimitType` and
  `resetsAt` (Unix seconds -> ISO 8601) as limit type and reset time; or a
  non-tool error (assistant `error` string or an `is_error` result) with
  `api_error_status` 429 or a message matching usage limit / rate limit /
  "hit your limit" / "limit reached" / "too many requests" (no reset time).
  Tool output that mentions a rate limit is never a hit. The exit code alone
  is not used (1 is every error).

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

### Known gap: Nightshift sees only its own usage

Nightshift records usage only from its own sessions, not from interactive use
of the same plan. Usage recorded at a limit hit therefore undercounts the
Window, so learned Baselines err low (conservative). The `rate_limit_event`
`utilization` field may let a later phase see whole-plan utilization; not used
yet.
