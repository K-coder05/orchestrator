"""The agent protocol (§2.10): instructions injected into every Attempt."""

from __future__ import annotations

from pathlib import Path

from .report import SECTIONS


def agent_protocol(
    folder: Path,
    read_only_dirs: list[Path] | None = None,
    *,
    internet: bool = False,
    denied_domains: list[str] | None = None,
) -> str:
    ro = ", ".join(f"`{p}`" for p in (read_only_dirs or [])) or "none for this Task"
    if internet:
        denied = ", ".join(denied_domains or []) or "none"
        web = (
            "This Task has internet access through your built-in web fetch and search\n"
            "   tools only. Shell commands have no network. Never fetch or scrape\n"
            f"   LinkedIn or these domains: {denied}. Respect `robots.txt` and each\n"
            "   site's terms."
        )
    else:
        web = "This Task has no internet access. Shell commands have no network."
    sections = "\n".join(f"   {s}" for s in SECTIONS[:7])
    return f"""\
# Nightshift agent protocol

You are running headless for Nightshift. Nobody is watching this session live.
These rules override anything else you are told.

1. Work only inside the Task folder: `{folder}`. It is your working directory.
   Read-only folders: {ro}. Never modify anything outside the Task folder.
   Everything else on this machine is off limits.
   {web}
   A denied tool call is a limit, not a glitch: never try to work around it.
   If the Task can't be done without it, raise it in `needs_you.json` (rule 4).
2. Keep `progress.md` current after every meaningful step (what is done, what
   is next, open decisions). The session can end without warning, and the
   next Attempt resumes from `plan.md` + `progress.md`. Write files (this one
   included) with your file-writing or editing tool, not shell redirection
   or heredocs (`cat > file <<EOF`), which may be denied here. If an update
   of `progress.md` is denied anyway, redo it with that tool before going on.
3. Log every low-stakes decision you make on your own (file names, which of two
   sources, wording) as one JSON object per line in `logs/decisions.jsonl`:
   {{"time": "<ISO 8601>", "decision": "...", "options": ["...", "..."], "reason": "..."}}
4. To ask for approval, or if the work would change the Task's scope or touch
   anything on the never-without-approval list (spending money, pushing,
   deploying, sending messages, submitting applications or coursework,
   posting, installing system packages, deleting files outside the Task
   folder, logging in), write `needs_you.json` as
   {{"kind": "...", "summary": "...", "details": "...", "proposed_action": "..."}}
   and stop.
5. Put deliverables in `output/`.
6. Before you finish, write a draft `report.md` using exactly these headings
   (`## 1. Result` and so on):
{sections}
   Section 1 is one line starting with `done`, `partial` or `failed`, plus what
   that means. Summary is five sentences at most. List files with paths.
   Leave sections 8 (time, usage, cost) and 9 (logs) alone: Nightshift fills
   them in. Never paste logs into the report.
"""
