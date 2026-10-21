"""Resume traceability (§2.11 Resume rule, §3.1 item 9).

Every change in a tailored ``resume.tex`` must trace to ``master.tex``.
Nightshift checks that itself, after the Attempt, against its pristine copy
of ``master.tex`` (the agent can't reach it):

- every entry in ``changes.md`` cites at least one ``master.tex`` line
  (``master.tex:L<n>`` or ``L<n>-L<m>``) in its ``Source:`` field, every
  cited line exists (and they aren't all blank), and it gives a ``Reason:``;
- every changed line of the resume (a line of the document body, with text,
  that isn't a line of ``master.tex``) is covered by an entry whose
  ``New text:`` contains it: a changed bullet with no entry is a changed
  bullet without a citation;
- every number in the resume's document body (percentages, counts,
  amounts, years) appears in ``master.tex``. LaTeX lengths (``10pt``,
  ``0.5in``, ``0.9\\textwidth``) are layout, not claims, and are skipped.

Moved bullets are lines of ``master.tex`` verbatim, so they need no entry
(an entry for one is still checked). Removed bullets add nothing, likewise.

``changes.md`` format (the template prompt asks for exactly this; the parser
also takes ``**Source:**``-style bold labels and numbered lists)::

    ### 1. <short label>
    - Kind: reworded | moved | removed | condensed | merged
    - New text: <the line as it now reads in resume.tex>
    - Source: master.tex:L14, master.tex:L23-L24
    - Reason: <one line tied to the posting>

An entry is a heading or top-level list item whose block has any of those
fields. Text before the first entry (a title, notes) is ignored.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# LaTeX text
# --------------------------------------------------------------------------

_COMMENT = re.compile(r"(?<!\\)%.*")
_DIMEN = re.compile(
    r"[-+]?(?:\d+\.?\d*|\.\d+)(?:(?:pt|em|ex|in|cm|mm|bp|pc|sp|dd|cc|mu)(?![A-Za-z])"
    r"|\s*\\(?:textwidth|linewidth|columnwidth|paperwidth|textheight|paperheight|baselineskip"
    r"|parskip|parindent|tabcolsep)\b)")
_ESCAPES = {r"\%": "%", r"\&": "&", r"\$": "$", r"\#": "#", r"\_": "_", r"\{": "{", r"\}": "}"}
_NUMBER = re.compile(r"\d+(?:,\d{3})*(?:\.\d+)?")


def strip_comment(line: str) -> str:
    return _COMMENT.sub("", line)


def latex_text(line: str, *, comments: bool = True) -> str:
    """The visible text of one LaTeX line, roughly: comments, environment
    markers, command names, line breaks and braces removed. ``comments=False``
    is for prose that may quote LaTeX (a bare ``%`` there is a percent)."""
    s = strip_comment(line) if comments else line
    s = re.sub(r"\\\\(\*?\[[^\]]*\])?", " ", s)          # \\ and \\[2pt]
    s = re.sub(r"\\(?:begin|end)\s*\{[^}]*\}", " ", s)
    for k, v in _ESCAPES.items():
        s = s.replace(k, v)
    s = re.sub(r"\\[A-Za-z@]+\*?", " ", s)               # command names (args stay)
    s = re.sub(r"\\.", " ", s)                           # \, \; \- ...
    s = s.replace("~", " ").replace("---", "-").replace("--", "-")
    s = re.sub(r"[{}\[\]]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm(text: str) -> str:
    """Comparison form: lowercase words and numbers (``%`` kept)."""
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z%]+", " ", text.lower())).strip()


def _line_key(line: str) -> str:
    return re.sub(r"\s+", " ", strip_comment(line)).strip()


def body_range(lines: list[str]) -> tuple[int, int]:
    """0-based [start, end) of the lines inside ``\\begin{document}``."""
    start, end = 0, len(lines)
    for i, ln in enumerate(lines):
        if re.search(r"\\begin\s*\{document\}", strip_comment(ln)):
            start = i + 1
            break
    for i in range(len(lines) - 1, start - 1, -1):
        if re.search(r"\\end\s*\{document\}", strip_comment(lines[i])):
            end = i
            break
    return start, end


def numbers(text: str) -> list[str]:
    """The numbers in LaTeX text, lengths skipped, thousands commas dropped."""
    out = []
    for ln in text.splitlines():
        s = _DIMEN.sub(" ", strip_comment(ln))
        for m in _NUMBER.findall(latex_text(s)):
            out.append(m.replace(",", ""))
    return out


# --------------------------------------------------------------------------
# changes.md
# --------------------------------------------------------------------------

_FIELD = re.compile(
    r"^\s*(?:[-*+]|\d+[.)])?\s*(new text|new|text|sources?|cites?|citations?|reason|kind|change)"
    r"\s*:\s*(.*)$", re.I)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_TOP_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s+(.*)$")
_CITE = re.compile(r"\bL(\d+)(?:\s*[-\u2013]\s*L?(\d+))?\b")
_FIELD_NAMES = {"new text": "new", "new": "new", "text": "new", "source": "source",
                "sources": "source", "cite": "source", "cites": "source", "citation": "source",
                "citations": "source", "reason": "reason", "kind": "kind", "change": "kind"}


@dataclass
class Entry:
    label: str
    line: int  # 1-based line of changes.md where it starts
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def cites(self) -> list[tuple[int, int]]:
        return [(int(a), int(b or a)) for a, b in _CITE.findall(self.fields.get("source", ""))]

    @property
    def removed(self) -> bool:
        kind = self.fields.get("kind", "").lower()
        new = norm(self.fields.get("new", ""))
        return "remov" in kind or "delet" in kind or new in ("removed", "none", "n a", "")


def _clean(line: str) -> str:
    return line.replace("**", "").replace("__", "").replace("`", "")


def parse_changes(text: str) -> list[Entry]:
    blocks: list[tuple[str, int, list[str]]] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        line = _clean(raw)
        h = _HEADING.match(line)
        item = _TOP_ITEM.match(line) if not h and not _FIELD.match(line) else None
        if h or item:
            blocks.append(((h or item).group(1).strip(), n, []))
        elif blocks:
            blocks[-1][2].append(line)
        else:
            blocks.append(("", n, [line]))
    entries = []
    for label, n, body in blocks:
        e = Entry(label=label, line=n)
        current = None
        for line in body:
            m = _FIELD.match(line)
            if m:
                current = _FIELD_NAMES[m.group(1).lower()]
                value = m.group(2).strip()
                e.fields[current] = (e.fields.get(current, "") + " " + value).strip()
            elif not line.strip():
                current = None
            elif current is not None:
                e.fields[current] += " " + line.strip()
        # A field line in a title or notes block ("Source: input/master.tex")
        # doesn't make it an entry; any entry field, or a real citation, does.
        if {"new", "kind", "reason"} & set(e.fields) or e.cites:
            entries.append(e)
    return entries


# --------------------------------------------------------------------------
# The check
# --------------------------------------------------------------------------


@dataclass
class Changed:
    line: int  # 1-based in resume.tex
    tex: str
    text: str


@dataclass
class TraceResult:
    problems: list[str]
    changed: list[Changed]
    entries: list[Entry]
    numbers_checked: int

    @property
    def ok(self) -> bool:
        return not self.problems


def changed_lines(master: str, resume: str) -> list[Changed]:
    """Body lines of the resume with text that aren't lines of master.tex."""
    known = {_line_key(ln) for ln in master.splitlines()}
    lines = resume.splitlines()
    start, end = body_range(lines)
    out = []
    for i in range(start, end):
        ln = lines[i]
        if _line_key(ln) in known:
            continue
        text = latex_text(ln)
        if not norm(latex_text(_DIMEN.sub(" ", strip_comment(ln)))):
            continue  # layout only (\begin{itemize}, \vspace{-2pt}, blank)
        out.append(Changed(line=i + 1, tex=ln.strip(), text=text))
    return out


def _covers(entry_text: str, line_text: str) -> bool:
    a, b = norm(latex_text(entry_text, comments=False)), norm(line_text)
    if not a or not b:
        return False
    if b in a:
        return True
    return difflib.SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.85


def _short(s: str, n: int = 100) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "\u2026"


def check(master: str, resume: str, changes: str, *, master_name: str = "master.tex",
          resume_name: str = "output/resume.tex", changes_name: str = "output/changes.md"
          ) -> TraceResult:
    problems: list[str] = []
    mlines = master.splitlines()
    entries = parse_changes(changes)

    for e in entries:
        name = f"{changes_name} entry {_short(e.label, 40)!r} (line {e.line})" if e.label else (
            f"{changes_name} line {e.line}")
        cites = e.cites
        if not cites:
            problems.append(f"{name} cites no {master_name} line (want Source: {master_name}:L<n>)")
        else:
            bad = [(a, b) for a, b in cites if a < 1 or b < a or b > len(mlines)]
            for a, b in bad:
                where = f"L{a}" if a == b else f"L{a}-L{b}"
                problems.append(f"{name} cites {master_name}:{where}, but {master_name} has "
                                f"{len(mlines)} lines")
            if not bad and not any(norm(latex_text(mlines[i - 1]))
                                   for a, b in cites for i in range(a, b + 1)):
                problems.append(f"{name} cites only blank or comment lines of {master_name}")
        if not e.fields.get("reason", "").strip():
            problems.append(f"{name} gives no Reason")

    changed = changed_lines(master, resume)
    covering = [e.fields.get("new", "") for e in entries if not e.removed]
    for c in changed:
        if not any(_covers(t, c.text) for t in covering):
            problems.append(f"{resume_name} line {c.line} changed but no {changes_name} entry "
                            f"cites it: {_short(c.text)!r}")

    have = set(numbers(master))
    rlines = resume.splitlines()
    start, end = body_range(rlines)
    checked = 0
    for i in range(start, end):
        for num in numbers(rlines[i]):
            checked += 1
            if num not in have:
                problems.append(f"{resume_name} line {i + 1} has the number {num}, which is not in "
                                f"{master_name}: {_short(latex_text(rlines[i]))!r}")
    return TraceResult(problems=problems, changed=changed, entries=entries, numbers_checked=checked)


# --------------------------------------------------------------------------
# The diff for the report
# --------------------------------------------------------------------------


def unified_diff(old: str, new: str, old_name: str, new_name: str) -> str:
    return "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=old_name, tofile=new_name)).rstrip("\n")


def fenced(text: str, lang: str = "") -> str:
    """A Markdown code fence longer than any backtick run inside."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{lang}\n{text}\n{fence}"
