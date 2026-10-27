"""Claude Code adapter. Flags and stream format are recorded in docs/adapters.md
(verified against Claude Code 2.1.306)."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .base import (
    AgentAdapter,
    Command,
    Event,
    EventKind,
    PermissionPolicy,
    Permissions,
    PlanningReply,
    ProbeReply,
    RateLimit,
    TaskContext,
    Usage,
    WindowInfo,
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
    #: The coding/writing lane, shared with Codex (§5 item 5).
    lane = "claude"

    def __init__(self, settings: dict[str, Any] | None = None):
        settings = settings or {}
        self.binary: str = settings.get("binary") or "claude"
        self.model: str | None = settings.get("model")
        # Planning calls are quick estimates; a small model is enough.
        self.planning_model: str | None = settings.get("planning_model") or self.model
        self.extra_args: list[str] = list(settings.get("extra_args") or [])
        self._session_id: str | None = None
        self._result: dict[str, Any] | None = None
        self._message_ids: set[str] = set()
        # Per API message: (model, usage) as last seen in the stream; the
        # fallback for an Attempt stopped before its `result` line.
        self._message_usage: dict[str, tuple[str | None, dict[str, Any]]] = {}
        # "Now" for reset times given as a clock time ("resets 3pm"); tests
        # replace it.
        self.clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)

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

    def planning_command(self, prompt: str, *, system: str, schema: dict[str, Any]) -> Command:
        """``claude -p`` with no tools at all, a replaced system prompt and a
        JSON schema for the answer. Nothing is saved as a session.

        ``--tools ""`` leaves the session without file, shell or web tools,
        so no per-Task permission policy is needed; ``--restricted`` still
        keeps the user's settings and hooks out. ``extra_args`` are not used.
        """
        argv = [self.binary, "-p", "--output-format", "json"]
        if self.planning_model:
            argv += ["--model", self.planning_model]
        argv += [
            "--tools", "",
            "--restricted", "--strict-mcp-config",
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--system-prompt", system,
            "--json-schema", json.dumps(schema, separators=(",", ":")),
        ]
        return Command(argv=argv, stdin=prompt)

    def probe_command(self) -> Command:
        """``claude -p`` with no tools, no session, a one-line system prompt
        and a one-word answer: the cheapest request that tells whether the
        Window has reset. stream-json, so the ``rate_limit_event`` (status and
        reset time) comes along. Uses the Task model (``agents.claude.model``),
        since some limits are per model."""
        argv = [self.binary, "-p", "--output-format", "stream-json", "--verbose"]
        if self.model:
            argv += ["--model", self.model]
        argv += [
            "--tools", "",
            "--restricted", "--strict-mcp-config",
            "--permission-prompts", "none",
            "--no-session-persistence",
            "--system-prompt", PROBE_SYSTEM,
        ]
        return Command(argv=argv, stdin=PROBE_PROMPT)

    def parse_probe_output(self, stdout: str, exit_code: int) -> ProbeReply:
        probe = ClaudeAdapter({"model": self.model})
        probe.clock = self.clock
        limit: RateLimit | None = None
        windows: list[WindowInfo] = []
        for line in stdout.splitlines():
            for ev in probe.parse_stream(line):
                windows += probe.window_info(ev)
                hit = probe.detect_rate_limit(ev)
                if hit is not None and (limit is None or (limit.reset_at is None and hit.reset_at)):
                    limit = hit
        usage = probe.usage_from_run()
        if limit is not None:
            return ProbeReply(available=False, limit=limit, usage=usage, windows=windows)
        if exit_code == 0 and probe.result_ok():
            return ProbeReply(available=True, usage=usage, windows=windows)
        err = probe.result_error() or f"no result from claude (exit code {exit_code})"
        return ProbeReply(available=False, error=err, usage=usage, windows=windows)

    def parse_planning_output(self, stdout: str, exit_code: int) -> PlanningReply:
        msg: dict[str, Any] | None = None
        for line in reversed(stdout.strip().splitlines()):
            try:
                cand = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(cand, dict) and cand.get("type") == "result":
                msg = cand
                break
        if msg is None:
            return PlanningReply(ok=False, error=f"no result from claude (exit code {exit_code})")
        usage = _usage_from_result(msg, requests=None)
        structured = msg.get("structured_output")
        if structured is None and isinstance(msg.get("result"), str):
            try:  # older CLIs put the JSON answer in the result text only
                structured = json.loads(msg["result"])
            except json.JSONDecodeError:
                structured = None
        if exit_code != 0 or msg.get("is_error"):
            err = str(msg.get("result") or msg.get("subtype") or f"exit code {exit_code}")
            return PlanningReply(ok=False, error=err, text=msg.get("result"), usage=usage)
        if not isinstance(structured, dict):
            return PlanningReply(ok=False, error="claude did not return a JSON answer",
                                 text=msg.get("result"), usage=usage)
        return PlanningReply(ok=True, structured=structured, text=msg.get("result"), usage=usage)

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
                            "api_error": msg.get("api_error"),
                            "rate_limit_info": (msg.get("api_error_params") or {}).get(
                                "rate_limit_info") if isinstance(msg.get("api_error_params"), dict)
                            else None,
                        },
                    )
                )
            return events
        return [Event(EventKind.OTHER, {"type": mtype, "subtype": msg.get("subtype")})]

    def _assistant_events(self, msg: dict[str, Any]) -> list[Event]:
        message = msg.get("message") or {}
        synthetic = message.get("model") == "<synthetic>"  # an API error, not a request
        if message.get("id") and not synthetic:
            self._message_ids.add(message["id"])
            if isinstance(message.get("usage"), dict):
                seen = self._message_usage.get(message["id"], (None, {}))[1]
                merged = {k: max(v, seen.get(k) or 0) if isinstance(v, int) else v
                          for k, v in message["usage"].items()}
                self._message_usage[message["id"]] = (message.get("model"), merged)
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
            text = " ".join(b.get("text", "") for b in message.get("content") or []
                            if isinstance(b, dict) and b.get("type") == "text").strip()
            params = msg.get("api_error_params") or {}
            events.append(Event(EventKind.ERROR, {
                "message": str(msg["error"]),  # the error code, e.g. rate_limit
                "text": text or None,
                "api_error": msg.get("api_error"),  # e.g. usage_limit_reached
                "api_error_status": msg.get("api_error_status"),
                "rate_limit_info": params.get("rate_limit_info") if isinstance(params, dict) else None,
            }))
        return events

    def _user_events(self, msg: dict[str, Any]) -> list[Event]:
        events: list[Event] = []
        for block in (msg.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                events.append(Event(EventKind.TOOL_RESULT, {
                    "tool_use_id": block.get("tool_use_id"), "is_error": bool(block.get("is_error"))}))
            if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                events.append(
                    Event(EventKind.ERROR, {"tool_use_id": block.get("tool_use_id"), "tool_error": True,
                                            "message": str(block.get("content"))[:500]})
                )
        return events

    # -- results -------------------------------------------------------------
    def detect_rate_limit(self, event_or_exit: Event | int) -> RateLimit | None:
        """A usage-limit hit, from the stream (formats in docs/adapters.md):

        - a ``rate_limit_event`` whose status is ``rejected`` (not
          ``allowed``/``allowed_warning``): ``rateLimitType`` and ``resetsAt``;
        - an API error on the synthetic assistant message (or the ``result``)
          with ``api_error: usage_limit_reached`` and
          ``api_error_params.rate_limit_info`` (same shape as the event);
        - otherwise an agent (not tool) error with code ``rate_limit``, HTTP
          429, or a usage-limit message; the reset time and Window are then
          read from the text ("resets 3pm (America/Los_Angeles)", the older
          "Claude AI usage limit reached|<epoch>") when it has them.

        The exit code alone says nothing (1 is every error).
        """
        if not isinstance(event_or_exit, Event):
            return None
        ev = event_or_exit
        if ev.kind == EventKind.RATE_LIMIT_INFO:
            return _limit_from_info(ev.data)
        if ev.kind != EventKind.ERROR or ev.data.get("tool_error"):
            return None
        info = ev.data.get("rate_limit_info")
        text = " ".join(str(ev.data.get(k) or "") for k in ("text", "message")).strip()
        if isinstance(info, dict):
            hit = _limit_from_info(info)
            if hit is not None or ev.data.get("api_error") == "usage_limit_reached":
                hit = hit or RateLimit(limit_type=info.get("rateLimitType"),
                                       reset_at=_epoch_iso(info.get("resetsAt")))
                if hit.reset_at is None:
                    hit.reset_at = parse_reset_text(text, self.clock())
                return hit
        if (ev.data.get("api_error") == "usage_limit_reached"
                or ev.data.get("message") == "rate_limit"
                or ev.data.get("api_error_status") == 429
                or _LIMIT_TEXT.search(text)):
            return RateLimit(limit_type=window_from_text(text),
                             reset_at=parse_reset_text(text, self.clock()))
        return None

    def window_info(self, event: Event) -> list[WindowInfo]:
        """Window status from a ``rate_limit_event``: the limiting Window
        (``rateLimitType``/``resetsAt``/``utilization``) and every entry of
        ``unifiedWindows``."""
        if event.kind != EventKind.RATE_LIMIT_INFO:
            return []
        d = event.data
        out: dict[str, WindowInfo] = {}
        for name, w in (d.get("unifiedWindows") or {}).items():
            if isinstance(w, dict):
                out[name] = WindowInfo(name, _epoch_iso(w.get("resetsAt")),
                                       _num(w.get("utilization")))
        name = d.get("rateLimitType")
        if isinstance(name, str) and name:
            w = out.setdefault(name, WindowInfo(name))
            w.status = d.get("status")
            w.resets_at = _epoch_iso(d.get("resetsAt")) or w.resets_at
            if _num(d.get("utilization")) is not None:
                w.utilization = _num(d.get("utilization"))
        return list(out.values())

    def session_id(self) -> str | None:
        return self._session_id

    def usage_from_run(self) -> list[Usage]:
        """From the ``result`` line when there is one. An Attempt stopped
        before it (SIGTERM on a pause, the runtime cap) still has the usage
        of each API message seen in the stream: tokens and requests per
        model, but no reported cost (``cost_usd`` None)."""
        if self._result:
            return _usage_from_result(self._result, requests=len(self._message_ids) or None)
        by_model: dict[str | None, Usage] = {}
        for model, u in self._message_usage.values():
            acc = by_model.setdefault(model, Usage(model=model, input_tokens=0, output_tokens=0,
                                                   cache_creation_tokens=0, cache_read_tokens=0,
                                                   requests=0, raw={"source": "stream messages"}))
            acc.input_tokens += u.get("input_tokens") or 0
            acc.output_tokens += u.get("output_tokens") or 0
            acc.cache_creation_tokens += u.get("cache_creation_input_tokens") or 0
            acc.cache_read_tokens += u.get("cache_read_input_tokens") or 0
            acc.requests += 1
        return list(by_model.values())

    def result_ok(self) -> bool | None:
        if self._result is None:
            return None
        return self._result.get("subtype") == "success" and not self._result.get("is_error")

    def result_error(self) -> str | None:
        if self._result is None or self.result_ok():
            return None
        errors = self._result.get("errors")
        if not self._result.get("result") and isinstance(errors, list) and errors:
            # e.g. --resume of an unknown session: "No conversation found ..."
            return "; ".join(str(e) for e in errors)
        return str(self._result.get("result") or self._result.get("subtype"))


_LIMIT_TEXT = re.compile(
    r"usage limit|rate limit|rate_limit|hit your limit|limit reached|too many requests"
    r"|hit your (?:weekly|session|five-hour|5-hour|opus|sonnet)",
    re.IGNORECASE,
)

PROBE_SYSTEM = "Answer with the single word OK."
PROBE_PROMPT = "OK?"

_EPOCH_TEXT = re.compile(r"limit reached\|(\d{9,11})")
_RESET_TEXT = re.compile(
    r"resets?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<mon>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+(?P<day>\d{1,2}),?"
    r"\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>am|pm)\b"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z0-9_+\-]+)*)\))?",
    re.IGNORECASE,
)
_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def parse_reset_text(text: str, now: datetime) -> str | None:
    """The reset time in a limit message, as ISO 8601, or None.

    Understands the older ``Claude AI usage limit reached|<epoch seconds>``
    and clock times such as ``resets 3pm``, ``resets 11:30am
    (America/Los_Angeles)`` or ``resets Oct 25, 3pm (Europe/Paris)``. A clock
    time without a date is the next such time after ``now``; without a zone
    it is in this Mac's local zone.
    """
    if not text:
        return None
    m = _EPOCH_TEXT.search(text)
    if m:
        return _epoch_iso(int(m.group(1)))
    m = _RESET_TEXT.search(text)
    if not m:
        return None
    tz: Any = None
    if m.group("tz"):
        try:
            tz = ZoneInfo(m.group("tz"))
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    local_now = now.astimezone(tz) if tz is not None else now.astimezone()
    hour = int(m.group("h")) % 12 + (12 if m.group("ap").lower() == "pm" else 0)
    minute = int(m.group("m") or 0)
    if hour > 23 or minute > 59:
        return None
    if m.group("mon"):
        month = _MONTHS.index(m.group("mon")[:3].lower()) + 1
        try:
            at = local_now.replace(month=month, day=int(m.group("day")), hour=hour,
                                   minute=minute, second=0, microsecond=0)
        except ValueError:
            return None
        if at < local_now - timedelta(days=1):
            at = at.replace(year=at.year + 1)  # "Jan 2" seen in late December
    else:
        at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if at <= local_now:
            at += timedelta(days=1)
    return at.isoformat(timespec="seconds")


def window_from_text(text: str) -> str | None:
    """The Window a limit message names, if it names one."""
    t = text.lower()
    if "weekly" in t or "week" in t or "7-day" in t:
        if "opus" in t:
            return "seven_day_opus"
        if "sonnet" in t:
            return "seven_day_sonnet"
        return "seven_day"
    if "session" in t or "5-hour" in t or "five-hour" in t or "5 hour" in t:
        return "five_hour"
    return None


def _limit_from_info(info: dict[str, Any]) -> RateLimit | None:
    status = str(info.get("status") or "")
    if status and not status.startswith("allowed"):
        return RateLimit(limit_type=info.get("rateLimitType"),
                         reset_at=_epoch_iso(info.get("resetsAt")))
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _epoch_iso(value: Any) -> str | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return None


def _usage_from_result(r: dict[str, Any], *, requests: int | None) -> list[Usage]:
    """One ``Usage`` per model from a ``result`` line (see docs/adapters.md)."""
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
