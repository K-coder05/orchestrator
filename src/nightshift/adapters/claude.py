"""Claude Code adapter. Flags and stream format are recorded in docs/adapters.md
(verified against Claude Code 2.1.306)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import (
    AgentAdapter,
    Command,
    Event,
    EventKind,
    PermissionPolicy,
    Permissions,
    RateLimit,
    TaskContext,
    Usage,
)

# Tools that change files; tool_use blocks for these also yield FILE_CHANGE.
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# The only tools that exist in a Task's session (`--tools`). Bash runs inside
# Claude Code's sandbox; web tools exist only on internet-on Tasks.
BASE_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep"]
WEB_TOOLS = ["WebFetch", "WebSearch"]
# Flags that would switch permission checks off or widen the per-Task policy.
# Nightshift never passes them itself and refuses them in
# agents.claude.extra_args (§1.4).
FORBIDDEN_ARGS = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-mode",
    "--permission-prompts",
    "--setting-sources",
    "--settings",
    "--tools",
    "--allowedTools",
    "--allowed-tools",
    "--add-dir",
    "--mcp-config",
)


def _forbidden(args: list[str]) -> list[str]:
    return [
        a for a in args
        if "bypassPermissions" in a or any(a == f or a.startswith(f + "=") for f in FORBIDDEN_ARGS)
    ]


def _rule_path(path: Path) -> str:
    # In Claude Code permission rules "/x" is relative to the settings file;
    # an absolute path needs a leading "//" (verified on 2.1.306: "/abs/**"
    # did not match and an Edit outside went through).
    return "/" + str(path) + "/**"


class ClaudeAdapter(AgentAdapter):
    name = "claude"

    def __init__(self, settings: dict[str, Any] | None = None):
        settings = settings or {}
        self.binary: str = settings.get("binary") or "claude"
        self.model: str | None = settings.get("model")
        self.extra_args: list[str] = list(settings.get("extra_args") or [])
        self._session_id: str | None = None
        self._result: dict[str, Any] | None = None
        self._message_ids: set[str] = set()

    # -- invocation ----------------------------------------------------------
    def _base_argv(self, permissions: Permissions) -> list[str]:
        if permissions.settings_file is None or not permissions.tools:
            raise ValueError("refusing to start Claude without a per-Task permission policy")
        if permissions.permission_mode == "bypassPermissions":
            raise ValueError("bypassPermissions is never used (§1.4)")
        bad = _forbidden(self.extra_args)
        if bad:
            raise ValueError(f"agents.claude.extra_args may not contain {bad} (§1.4)")
        argv = [self.binary, "-p", "--output-format", "stream-json", "--verbose"]
        if self.model:
            argv += ["--model", self.model]
        argv += ["--permission-mode", permissions.permission_mode]
        # Headless: anything that would prompt is denied, never left hanging.
        argv += ["--permission-prompts", "none"]
        # --restricted ignores user/project/local settings (their allow rules
        # and hooks), confines the file tools to the working dirs and refuses
        # bypassPermissions; --strict-mcp-config drops the user's MCP servers.
        argv += ["--restricted", "--strict-mcp-config"]
        argv += ["--tools", ",".join(permissions.tools)]
        argv += ["--settings", str(permissions.settings_file)]
        for d in permissions.read_only_dirs:
            argv += ["--add-dir", str(d)]
        return argv + self.extra_args

    def build_command(
        self, task: TaskContext, workspace: Path, permissions: Permissions
    ) -> Command:
        argv = self._base_argv(permissions) + ["--append-system-prompt", task.protocol]
        # The prompt goes on stdin so a prompt starting with "-" is never
        # parsed as a flag and it does not show up in `ps`.
        return Command(argv=argv, stdin=task.prompt)

    def resume_command(
        self,
        session_id: str,
        prompt: str,
        *,
        task: TaskContext | None = None,
        permissions: Permissions | None = None,
    ) -> Command:
        if permissions is None:
            raise ValueError("a resumed Attempt needs the Task's permissions too")
        argv = self._base_argv(permissions) + ["--resume", session_id]
        if task is not None:
            argv += ["--append-system-prompt", task.protocol]
        return Command(argv=argv, stdin=prompt)

    def write_permissions(self, workspace: Path, policy: PermissionPolicy) -> Permissions:
        """Claude Code settings for one Task; see docs/safety.md for what each
        part enforces. Writes ``<settings_dir>/claude-settings.json``."""
        tools = list(BASE_TOOLS) + (list(WEB_TOOLS) if policy.internet else [])
        allow = list(WEB_TOOLS) if policy.internet else []
        deny: list[str] = []
        for d in policy.no_access_dirs:
            deny += [f"Read({_rule_path(d)})", f"Edit({_rule_path(d)})"]
        for d in policy.read_only_dirs:
            deny.append(f"Edit({_rule_path(d)})")  # Edit rules cover Write too
        for cmd in policy.denied_commands:
            deny.append(f"Bash({cmd}:*)")
        if policy.internet:
            for dom in policy.denied_domains:
                deny += [f"WebFetch(domain:{dom})", f"WebFetch(domain:*.{dom})"]
        # Only dirs that exist are added; a missing one stays denied for writes.
        read_only = [d for d in policy.read_only_dirs if d.is_dir()]
        sandbox = {
            "enabled": True,
            # Sandboxed commands run without a prompt; the deny rules above
            # still apply first.
            "autoAllowBashIfSandboxed": True,
            # No escape hatch: a command can never ask to run unsandboxed.
            "allowUnsandboxedCommands": False,
            # Shell commands get no network at all, on every Task: web access
            # is only through WebFetch/WebSearch (§1.4).
            "network": {"allowedDomains": []},
            "filesystem": {
                "denyRead": [str(p) for p in [*policy.private_roots, *policy.no_access_dirs]],
                "allowRead": [str(p) for p in [policy.workspace, *read_only, *policy.tool_dirs]],
                "denyWrite": [str(p) for p in [*policy.read_only_dirs, *policy.no_access_dirs]],
            },
        }
        settings = {
            "permissions": {
                "allow": allow,
                "deny": deny,
                "disableBypassPermissionsMode": "disable",
            },
            "sandbox": sandbox,
        }
        if policy.settings_dir is None:
            raise ValueError("PermissionPolicy.settings_dir is required")
        policy.settings_dir.mkdir(parents=True, exist_ok=True)
        settings_file = policy.settings_dir / "claude-settings.json"
        settings_file.write_text(json.dumps(settings, indent=2) + "\n")
        return Permissions(
            tools=tools,
            allow=allow,
            deny=deny,
            read_only_dirs=read_only,
            sandbox=sandbox,
            settings_file=settings_file,
        )

    # -- stream ----------------------------------------------------------------
    def parse_stream(self, line: str) -> list[Event]:
        line = line.strip()
        if not line:
            return []
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return [Event(EventKind.TEXT, {"text": line, "raw": True})]
        if not isinstance(msg, dict):
            return [Event(EventKind.OTHER, {"value": msg})]

        if msg.get("session_id") and not self._session_id:
            self._session_id = msg["session_id"]

        mtype = msg.get("type")
        if mtype == "system" and msg.get("subtype") == "permission_denied":
            return [
                Event(
                    EventKind.ERROR,
                    {
                        "tool_error": True,
                        "permission_denied": True,
                        "tool_use_id": msg.get("tool_use_id"),
                        "name": msg.get("tool_name"),
                        "message": msg.get("decision_reason"),
                    },
                )
            ]
        if mtype == "system" and msg.get("subtype") == "init":
            return [
                Event(
                    EventKind.SESSION,
                    {
                        "session_id": msg.get("session_id"),
                        "model": msg.get("model"),
                        "version": msg.get("claude_code_version"),
                    },
                )
            ]
        if mtype == "assistant":
            return self._assistant_events(msg)
        if mtype == "user":
            return self._user_events(msg)
        if mtype == "rate_limit_event":
            return [Event(EventKind.RATE_LIMIT_INFO, dict(msg.get("rate_limit_info") or {}))]
        if mtype == "result":
            self._result = msg
            events = [
                Event(
                    EventKind.RESULT,
                    {
                        "subtype": msg.get("subtype"),
                        "is_error": bool(msg.get("is_error")),
                        "text": msg.get("result"),
                        "num_turns": msg.get("num_turns"),
                        "duration_ms": msg.get("duration_ms"),
                        "cost_usd": msg.get("total_cost_usd"),
                    },
                ),
                Event(EventKind.USAGE, {"usage": msg.get("usage"), "final": True}),
            ]
            if msg.get("is_error"):
                events.append(
                    Event(
                        EventKind.ERROR,
                        {
                            "message": msg.get("result") or msg.get("subtype"),
                            "api_error_status": msg.get("api_error_status"),
                        },
                    )
                )
            return events
        return [Event(EventKind.OTHER, {"type": mtype, "subtype": msg.get("subtype")})]

    def _assistant_events(self, msg: dict[str, Any]) -> list[Event]:
        message = msg.get("message") or {}
        if message.get("id"):
            self._message_ids.add(message["id"])
        events: list[Event] = []
        for block in message.get("content") or []:
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                events.append(Event(EventKind.TEXT, {"text": block["text"]}))
            elif btype == "tool_use":
                name = block.get("name")
                tool_input = block.get("input") or {}
                events.append(
                    Event(EventKind.TOOL_CALL, {"id": block.get("id"), "name": name, "input": tool_input})
                )
                if name in FILE_TOOLS:
                    path = tool_input.get("file_path") or tool_input.get("notebook_path")
                    events.append(Event(EventKind.FILE_CHANGE, {"path": path, "tool": name}))
        if message.get("usage"):
            events.append(Event(EventKind.USAGE, {"usage": message["usage"], "final": False}))
        if msg.get("error"):
            events.append(Event(EventKind.ERROR, {"message": str(msg["error"])}))
        return events

    def _user_events(self, msg: dict[str, Any]) -> list[Event]:
        events: list[Event] = []
        for block in (msg.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                events.append(
                    Event(EventKind.ERROR, {"tool_use_id": block.get("tool_use_id"), "tool_error": True,
                                            "message": str(block.get("content"))[:500]})
                )
        return events

    # -- results -------------------------------------------------------------
    def detect_rate_limit(self, event_or_exit: Event | int) -> RateLimit | None:
        # Best effort until issue #9 records real hits: a rate_limit_event
        # whose status is not "allowed" counts as a hit.
        if isinstance(event_or_exit, Event) and event_or_exit.kind == EventKind.RATE_LIMIT_INFO:
            info = event_or_exit.data
            if info.get("status") and info.get("status") != "allowed":
                reset = info.get("resetsAt")
                reset_iso = (
                    datetime.fromtimestamp(reset, tz=timezone.utc).isoformat()
                    if isinstance(reset, (int, float))
                    else None
                )
                return RateLimit(limit_type=info.get("rateLimitType"), reset_at=reset_iso)
        return None

    def session_id(self) -> str | None:
        return self._session_id

    def usage_from_run(self) -> list[Usage]:
        if not self._result:
            return []
        r = self._result
        requests = len(self._message_ids) or None
        model_usage = r.get("modelUsage") or {}
        if model_usage:
            out = []
            for model, mu in model_usage.items():
                out.append(
                    Usage(
                        model=model,
                        input_tokens=mu.get("inputTokens"),
                        output_tokens=mu.get("outputTokens"),
                        cache_creation_tokens=mu.get("cacheCreationInputTokens"),
                        cache_read_tokens=mu.get("cacheReadInputTokens"),
                        # Request count is per Attempt; attribute it to the first model.
                        requests=requests if not out else None,
                        cost_usd=mu.get("costUSD"),
                        raw=mu,
                    )
                )
            return out
        u = r.get("usage") or {}
        return [
            Usage(
                model=None,
                input_tokens=u.get("input_tokens"),
                output_tokens=u.get("output_tokens"),
                cache_creation_tokens=u.get("cache_creation_input_tokens"),
                cache_read_tokens=u.get("cache_read_input_tokens"),
                requests=requests,
                cost_usd=r.get("total_cost_usd"),
                raw=u,
            )
        ]

    def result_ok(self) -> bool | None:
        if self._result is None:
            return None
        return self._result.get("subtype") == "success" and not self._result.get("is_error")

    def result_error(self) -> str | None:
        if self._result is None or self.result_ok():
            return None
        return str(self._result.get("result") or self._result.get("subtype"))
