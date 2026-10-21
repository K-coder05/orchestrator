# Agent adapters

All agents sit behind `nightshift.adapters.base.AgentAdapter` (§2.9):
`build_command`, `parse_stream`, `detect_rate_limit`, `session_id`,
`resume_command`, `usage_from_run`, `write_permissions`. One adapter instance
is created per Attempt, so `session_id()` and `usage_from_run()` report what
that Attempt's stream contained. Adapters are looked up by name in
`nightshift.adapters.REGISTRY`; core code never names an agent.

Normalized events (`EventKind`): `text`, `tool_call` (one Step), `file_change`,
`usage`, `error`, `session`, `rate_limit_info`, `result`, `other`.

## Claude Code

Verified on this Mac against **Claude Code 2.1.306** (`claude --version`),
using `claude --help` and real headless runs on 2026-10-20.

### Invocation Nightshift uses

```
claude -p --output-format stream-json --verbose \
  [--model <model>] \
  --permission-mode acceptEdits --permission-prompts none \
  --allowedTools Read,Write,Edit,Glob,Grep \
  --disallowedTools WebFetch,WebSearch        # internet off; allowed when on
  [--add-dir <read-only dir> ...] \
  --append-system-prompt "<agent protocol §2.10>"
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
| `--no-session-persistence` | Sessions not saved, can't be resumed (only with `--print`). | no (we need resume) |
| `--allowedTools, --allowed-tools <tools...>` | Comma or space separated tool names to allow, e.g. `"Bash(git *) Edit"`. | yes |
| `--disallowedTools, --disallowed-tools <tools...>` | Comma or space separated tool names to deny. | yes |
| `--tools <tools...>` | Restrict the built-in tool set (`""` disables all). | no (issue #4 may use it) |
| `--permission-mode` | `acceptEdits`, `auto`, `bypassPermissions`, `manual`, `dontAsk`, `plan`. | `acceptEdits` |
| `--permission-prompts host\|none` | Who answers permission prompts with `--print`. `none`: anything that would prompt is denied automatically. | `none` |
| `--add-dir <directories...>` | Additional directories to allow tool access to. | read-only dirs (issue #4 policy) |
| `--settings <file-or-json>` | Extra settings (permissions, sandbox) from a file or JSON. | not yet (issue #4) |
| `--setting-sources user,project,local` | Which setting sources load. | not yet |
| `--restricted` | Removes Bash/code-running tools and WebFetch unless `--tools` names them, confines file tools to working dirs, refuses bypassPermissions. | not yet (candidate for issue #4) |
| `--append-system-prompt <prompt>` | Append to the default system prompt. | yes: agent protocol |
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
- **Not yet observed: an actual limit hit.** `detect_rate_limit` currently
  treats a `rate_limit_event` whose `status` isn't `allowed` as a hit and
  converts `resetsAt` to ISO 8601. This is a best guess; issue #9 must record
  the real hit format here when one happens.

### Behaviour notes

- With `--permission-mode acceptEdits` and `--permission-prompts none`,
  read-only Bash commands (`cd … && ls`, `cat`) ran without being in
  `--allowedTools`; a `cd` plus output redirection was denied automatically
  and reported as `permission_denied`. So `--allowedTools` isn't an exhaustive
  allowlist under `acceptEdits`. Issue #4 must set the real policy
  (`--tools`/`--restricted`/`--settings`) and document it in `docs/safety.md`.
- The user's own `~/.claude` settings, hooks and CLAUDE.md load into agent
  sessions unless `--setting-sources` limits them (issue #4).

### Known gap: Nightshift sees only its own usage

Nightshift records usage only from its own sessions, not from interactive use
of the same plan. Usage recorded at a limit hit therefore undercounts the
Window, so learned Baselines err low (conservative). The `rate_limit_event`
`utilization` field may let a later phase see whole-plan utilization; not used
yet.
