"""``report.md`` (§2.11).

The agent writes a draft with sections 1-7. Nightshift owns sections 8
(time, usage and cost per agent) and 9 (link to the full logs) and rewrites
them from Attempt and usage data when the Task finishes. A template Task
may also get an appendix after section 9 (e.g. resume-tailor's diff against
``master.tex``), rewritten each time from the Task's files.
"""

from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .timeutil import format_duration, parse_iso

SECTIONS: list[str] = [
    "1. Result",
    "2. Needs you",
    "3. Summary",
    "4. Files created",
    "5. How to run or use the result",
    "6. Decisions the agent made on its own",
    "7. Open issues",
    "8. Time, usage and cost per agent",
    "9. Full logs",
]
NIGHTSHIFT_OWNED = {8, 9}
PLACEHOLDER = "_(not written yet)_"
NIGHTSHIFT_PLACEHOLDER = "_(filled in by Nightshift when the Task finishes)_"
APPENDIX_HEADING = "## Appendix (written by Nightshift)"
GITHUB_HEADING = "### GitHub pushes and repo creations"


def skeleton(title: str) -> str:
    """The draft report Nightshift places in a new Task folder."""
    parts = [f"# {title}", ""]
    for heading in SECTIONS:
        num = int(heading.split(".")[0])
        parts += [f"## {heading}", "", NIGHTSHIFT_PLACEHOLDER if num in NIGHTSHIFT_OWNED else PLACEHOLDER, ""]
    return "\n".join(parts)


_HEADING_RE = re.compile(r"^##\s+(\d+)\.?\s*(.*)$")


def parse_sections(text: str) -> tuple[str, "OrderedDict[int, str]"]:
    """Split a report into its title block and numbered ``## N.`` sections."""
    head: list[str] = []
    sections: OrderedDict[int, list[str]] = OrderedDict()
    current: int | None = None
    for line in text.splitlines():
        m = _HEADING_RE.match(line)
        if m:
            current = int(m.group(1))
            sections[current] = []
            continue
        if current is None:
            head.append(line)
        else:
            sections[current].append(line)
    return "\n".join(head).strip(), OrderedDict(
        (k, "\n".join(v).strip()) for k, v in sections.items()
    )


def _is_empty(body: str | None) -> bool:
    return not body or body.strip() in ("", PLACEHOLDER, NIGHTSHIFT_PLACEHOLDER)


RESULTS = ("done", "partial", "failed")


def agent_result(text: str) -> str | None:
    """The result word the agent wrote in section 1, if any."""
    _, sections = parse_sections(text)
    body = sections.get(1, "")
    m = re.search(r"\b(done|partial|failed)\b", body.lower())
    return m.group(1) if m else None


@dataclass
class AgentUsage:
    agent: str
    attempts: int = 0
    seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    requests: int = 0
    cost_usd: float = 0.0
    cost_reported: bool = False


def summarize_usage(attempts: list, usage_rows: list) -> list[AgentUsage]:
    """Aggregate Attempts and usage events per agent."""
    by_agent: dict[str, AgentUsage] = {}
    for a in attempts:
        u = by_agent.setdefault(a["agent"], AgentUsage(a["agent"]))
        u.attempts += 1
        if a["started_at"] and a["ended_at"]:
            u.seconds += (parse_iso(a["ended_at"]) - parse_iso(a["started_at"])).total_seconds()
    for r in usage_rows:
        u = by_agent.setdefault(r["agent"], AgentUsage(r["agent"]))
        u.input_tokens += r["input_tokens"] or 0
        u.output_tokens += r["output_tokens"] or 0
        u.cache_creation_tokens += r["cache_creation_tokens"] or 0
        u.cache_read_tokens += r["cache_read_tokens"] or 0
        u.requests += r["requests"] or 0
        if r["cost_usd"] is not None:
            u.cost_usd += r["cost_usd"]
            u.cost_reported = True
    return list(by_agent.values())


def usage_section(rows: list[AgentUsage], spent: Mapping[str, float] | None = None,
                  paid_credits: list[str] | None = None, stuck: list[str] | None = None) -> str:
    """The usage table; ``spent``: dollars per paid agent (§1.5);
    ``paid_credits``: the paid-credit decisions, listed under it; ``stuck``:
    the stuck Attempts and why (§2.7), listed last."""
    spent = spent or {}
    if not rows:
        return "No Attempts were recorded."
    lines = [
        "| Agent | Attempts | Time | Input tok | Output tok | Cache write tok | Cache read tok | Requests | Reported cost-equivalent | Dollars spent |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for u in rows:
        cost = f"${u.cost_usd:.4f}" if u.cost_reported else "not reported"
        dollars = (f"${spent[u.agent]:.4f} (paid credits)" if u.agent in spent
                   else "$0.00 (subscription)")
        lines.append(
            f"| {u.agent} | {u.attempts} | {format_duration(u.seconds)} | {u.input_tokens:,} | "
            f"{u.output_tokens:,} | {u.cache_creation_tokens:,} | {u.cache_read_tokens:,} | "
            f"{u.requests:,} | {cost} | {dollars} |"
        )
    total = sum(u.seconds for u in rows)
    lines += ["", f"Total time: {format_duration(total)}."]
    if paid_credits:
        lines += ["", "Paid credits (subscription first, §1.5):", *paid_credits]
    if stuck:
        lines += ["", "Stuck Attempts (stopped by Nightshift's stuck detection, §2.7):", *stuck]
    return "\n".join(lines)


def logs_section(folder: Path) -> str:
    logs = folder / "logs"
    return f"[logs/](logs/) (`{logs}`)"


def _decisions_list(folder: Path) -> str | None:
    path = folder / "logs" / "decisions.jsonl"
    if not path.exists():
        return None
    items = []
    for line in path.read_text().splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("decision"):
            reason = f" ({d['reason']})" if d.get("reason") else ""
            items.append(f"- {d['decision']}{reason}")
    return "\n".join(items) or None


#: The agent's "needs you" request in the Task folder (§2.10).
REQUEST_FILE = "needs_you.json"
#: Resolved requests, kept in the Task's logs (``nightshift.needsyou``).
RESOLVED_GLOB = "attempt-*.needs_you.json"
#: How a resolved request's entry in section 2 starts: not an open request.
RESOLVED_PREFIX = "- Resolved "


def request_text(path: Path) -> str | None:
    """The Needs you entry of an agent's request file, or None if there is none."""
    if not path.exists():
        return None
    try:
        d = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "- The agent wrote `needs_you.json`, but it is not valid JSON. Check the Task folder."
    if not isinstance(d, dict):
        return "- The agent wrote `needs_you.json`; check the Task folder."
    out = f"- **{d.get('kind', 'request')}:** {d.get('summary', '')}".rstrip()
    from . import github  # github imports the config and DB layers

    if (does := github.describe_request(d)) is not None:
        # What approving it makes Nightshift do, in Nightshift's words.
        out = f"- **{d['kind']}:** {does}.\n  Agent's summary: {d.get('summary', '')}".rstrip()
    if d.get("proposed_action"):
        out += f"\n  Proposed action: {d['proposed_action']}"
    return out


def needs_you_section(folder: Path, body: str, needs_you: list[str] | None = None,
                      resolved: list[str] | None = None) -> str:
    """Section 2: Nightshift's own entries (``needs_you``), then the open
    request in ``needs_you.json``, then what the agent wrote there, then the
    resolved requests (``resolved``). What Nightshift wrote here before (an
    earlier finalize, a request since resolved) is never repeated."""
    needs_you, resolved = needs_you or [], resolved or []
    open_request = request_text(folder / REQUEST_FILE)
    own = [*needs_you, *filter(None, [open_request])]
    known = {*needs_you, *resolved, "Nothing."}
    for path in [folder / REQUEST_FILE, *sorted((folder / "logs").glob(RESOLVED_GLOB))]:
        known.update((request_text(path) or "").splitlines())
    agent = "" if _is_empty(body) else "\n".join(
        ln for ln in body.splitlines() if ln not in known).strip()
    return "\n".join(filter(None, [*own, agent, *resolved])) or "Nothing."


def refresh_needs_you(folder: Path, *, needs_you: list[str] | None = None,
                      resolved: list[str] | None = None, redact=lambda s: s) -> None:
    """Rewrite only section 2 of an existing ``report.md`` (a request was
    resolved; the rest of the report waits for the next finalize)."""
    path = folder / "report.md"
    if not path.exists():
        return
    lines = path.read_text().splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if (m := _HEADING_RE.match(ln)) and m.group(1) == "2"), None)
    if start is None:
        return
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))
    body = "\n".join(lines[start + 1:end]).strip()
    section = needs_you_section(folder, body, needs_you, resolved)
    lines[start + 1:end] = ["", section, ""]
    path.write_text(redact("\n".join(lines) + "\n"))


RESULT_MEANING = {
    "done": "the Task finished.",
    "partial": "the Task finished with only part of the work done.",
    "failed": "the Task did not finish.",
    "needs_you": "the agent stopped and needs you before it can continue.",
}


def finalize(
    folder: Path,
    *,
    title: str,
    result: str,
    result_detail: str | None,
    attempts: list,
    usage_rows: list,
    redact=lambda s: s,
    appendix: str | None = None,
    needs_you: list[str] | None = None,
    spent: Mapping[str, float] | None = None,
    paid_credits: list[str] | None = None,
    resolved: list[str] | None = None,
    stuck: list[str] | None = None,
    github: list[str] | None = None,
) -> str:
    """Rewrite ``report.md`` from the agent's draft plus Nightshift's data.

    ``needs_you`` are entries Nightshift itself adds to section 2 (e.g. a
    deadline-driven failover); they lead the section, and ``resolved``
    (decided "needs you" requests) close it. ``spent`` and
    ``paid_credits`` and ``stuck`` go to section 8 (:func:`usage_section`).
    ``github`` (pushes and repo creations, ``nightshift.github``) is listed
    in the appendix."""
    path = folder / "report.md"
    draft = path.read_text() if path.exists() else ""
    head, sections = parse_sections(draft)
    head = head or f"# {title}"

    body: dict[int, str] = {}
    for i in range(1, 10):
        body[i] = sections.get(i, "")

    # 1. Result: Nightshift's verdict leads; keep the agent's explanation.
    agent_line = body[1] if not _is_empty(body[1]) else ""
    verdict = f"**{result}**: {RESULT_MEANING.get(result, '')}".rstrip()
    if result_detail:
        verdict += f" {result_detail}"
    body[1] = verdict + (f"\n\nAgent: {agent_line}" if agent_line else "")

    body[2] = needs_you_section(folder, body[2], needs_you, resolved)

    if _is_empty(body[6]):
        body[6] = _decisions_list(folder) or "None recorded."

    for i in (3, 4, 5, 7):
        if _is_empty(body[i]):
            body[i] = "_(the agent did not fill this in)_"

    body[8] = usage_section(summarize_usage(attempts, usage_rows), spent, paid_credits, stuck)
    body[9] = logs_section(folder)

    parts = [head, ""]
    for i, heading in enumerate(SECTIONS, start=1):
        parts += [f"## {heading}", "", body[i], ""]
    if github:
        block = GITHUB_HEADING + "\n\n" + "\n".join(github)
        appendix = f"{appendix.strip()}\n\n{block}" if appendix else block
    # Unnumbered, so a later finalize reads it as part of section 9 (which
    # Nightshift rewrites) and it is never duplicated.
    if appendix:
        parts += [APPENDIX_HEADING, "", appendix.strip(), ""]
    text = redact("\n".join(parts))
    path.write_text(text)
    return text
