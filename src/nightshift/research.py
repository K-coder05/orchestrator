"""The research-report rules (§2.11 Research rule, §5 item 3).

A research template's agent writes its result as Markdown (``research-report``:
``output/research.md``, ``company-brief``: ``output/brief.md``). Nightshift,
not the agent, checks it after the Attempt (the ``research_rules`` post-hook);
any miss is a Failed Attempt:

- **every claim cites a URL**: each sentence of the body that makes a claim
  carries an http(s) URL (bare or as a Markdown link) or a ``[n]`` marker
  naming an entry of the Sources section, or is labelled ``*unverified*``;
- **at least 5 distinct sources**: distinct cited URLs (normalized as the
  Tracker normalizes posting URLs, so ``www.``, ``http``, trailing slashes and
  tracking parameters don't make a second source). A Sources entry nothing
  cites doesn't count;
- **never under one page**: at least ``min_words`` words of body text
  (default 500, about one single-spaced page), not counting the Sources
  section, headings or URLs;
- no URL on a denied domain (LinkedIn, §1.6), and no ``[n]`` marker without a
  Sources entry.

What counts as a claim: a sentence of a paragraph, list item, table row or
quote with at least ``CLAIM_MIN_WORDS`` words that doesn't end in ``:`` (a
lead-in to a list) or ``?`` (a question). Headings, fenced code, HTML
comments and the Sources section are not claims. A citation marker standing
alone after a sentence's full stop (``... in 2019. [3]``) belongs to that
sentence, and a bold lead-in (``**Single host only.** Every process ...``)
to the sentence after it.

The Sources section is a heading named Sources, References, Bibliography or
Works cited; it runs to the next heading of the same or a higher level. Each
entry is a line with one URL, numbered ``1.``, ``[1]``, ``[1]:`` or ``[^1]:``
(the number is what ``[1]`` / ``[^1]`` markers name). Reference definitions
(``[1]: https://...``) anywhere in the file are entries too.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .tracker import normalize_url

#: The defaults of the ``research_rules`` hook (§2.11).
MIN_SOURCES = 5
MIN_WORDS = 500
#: A shorter sentence ("Yes.", a table cell) isn't treated as a claim.
CLAIM_MIN_WORDS = 4

_URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+(?:\([^\s()]*\)[^\s<>()\[\]\"'`]*)*")
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MARKER = re.compile(r"\[\^?(\d{1,3}(?:\s*[,;–-]\s*\^?\d{1,3})*)\]")
_UNVERIFIED = re.compile(r"(?<![A-Za-z0-9])(\*{1,2}|_{1,2})\s*unverified\s*\1(?![A-Za-z0-9])",
                         re.IGNORECASE)
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SOURCES_TITLE = re.compile(
    r"^(?:\d+[.)]\s*)?(?:sources|references|bibliography|works cited|citations)\s*:?$",
    re.IGNORECASE)
_REF_DEF = re.compile(r"^\s{0,3}\[\^?(\d{1,3})\]:\s*(.*)$")
_ENTRY_NUM = re.compile(r"^\s*(?:[-*+]\s+)?(?:\[\^?(\d{1,3})\][.):]?|(\d{1,3})[.)])\s*")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
#: A bold lead-in (``**Single host only.** Every process ...``): a label for
#: the sentences after it, cited with them.
_LEAD_LABEL = re.compile(r"(\*\*|__)[^*_]+\1:?")
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’.%$-]*")
#: Words a full stop doesn't end a sentence after.
_ABBREV = frozenset({
    "inc", "corp", "co", "ltd", "llc", "plc", "dept", "univ", "e.g", "i.e", "etc", "vs", "al",
    "mr", "mrs", "ms", "dr", "prof", "jr", "sr", "st", "no", "nos", "approx", "est", "fig",
    "u.s", "u.k", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov",
    "dec", "mt", "ft", "vol", "ed", "eds", "rev", "gen", "gov", "sen", "rep", "ph.d", "b.s",
    "m.s", "b.a", "m.a"})


@dataclass
class Source:
    url: str
    title: str = ""
    number: int | None = None
    line: int = 0


@dataclass
class Claim:
    line: int
    text: str
    cited: bool
    unverified: bool


@dataclass
class Result:
    #: Distinct cited sources, in order of first citation.
    sources: list[Source] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    #: Sources section entries nothing cites (not counted).
    unused: list[Source] = field(default_factory=list)
    words: int = 0
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    @property
    def uncited(self) -> list[Claim]:
        return [c for c in self.claims if not c.cited and not c.unverified]

    @property
    def unverified(self) -> list[Claim]:
        return [c for c in self.claims if c.unverified and not c.cited]

    def summary(self, name: str) -> str:
        return (f"research rules: {name}: {self.words} words, {len(self.claims)} claim(s), "
                f"{len(self.sources)} distinct source(s) cited, {len(self.unverified)} labelled "
                f"unverified, {len(self.uncited)} uncited")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:!?*_'\"’”")


def _urls(text: str) -> list[str]:
    return [_clean_url(m.group(0)) for m in _URL.finditer(text)]


def _numbers(group: str) -> list[int]:
    """``1``, ``1, 3``, ``2-4`` -> the source numbers."""
    out: list[int] = []
    for part in re.split(r"\s*[,;]\s*", group):
        part = part.replace("^", "")
        m = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.extend(range(a, b + 1) if 0 < b - a < 50 else (a, b))
        elif part.isdigit():
            out.append(int(part))
    return out


def _title(text: str, url: str) -> str:
    """An entry's title: its text without the number, URL and punctuation."""
    m = _MD_LINK.search(text)
    if m and m.group(1).strip() and not _URL.fullmatch(m.group(1).strip()):
        t = text.replace(m.group(0), m.group(1))
    else:
        t = _MD_LINK.sub("", text)
    t = _ENTRY_NUM.sub("", t, count=1).replace(url, "")
    t = re.sub(r"<\s*>|\(\s*\)|\[\s*\]", "", t)
    t = re.sub(r"[*_`]", "", t)
    return t.strip(" \t-–—:|,.<>").strip()


def _words(text: str) -> list[str]:
    return _WORD.findall(_URL.sub(" ", _MD_LINK.sub(r"\1", text)))


def _blocks(lines: list[str]) -> tuple[list[tuple[int, str]], list[Source], bool]:
    """The body as (first line number, text) blocks, the Sources entries,
    and whether a Sources section was found."""
    blocks: list[tuple[int, str]] = []
    entries: list[Source] = []
    found = False
    in_fence = in_comment = False
    sources_level = 0  # >0 while inside the Sources section
    current: list[str] = []
    start = 0

    def flush() -> None:
        nonlocal current
        if current:
            blocks.append((start, " ".join(s.strip() for s in current)))
        current = []

    for i, raw in enumerate(lines, start=1):
        line = raw.rstrip("\n")
        if _FENCE.match(line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if in_comment or "<!--" in line:
            before, _, rest = line.partition("<!--") if not in_comment else ("", "", line)
            if "-->" in rest:
                line = before + rest.split("-->", 1)[1]
                in_comment = False
            else:
                in_comment = True
                line = before
            if not line.strip():
                continue
        h = _HEADING.match(line)
        if h:
            flush()
            level = len(h.group(1))
            if sources_level and level <= sources_level:
                sources_level = 0
            if not sources_level and _SOURCES_TITLE.match(re.sub(r"[*_`]", "", h.group(2))):
                sources_level, found = level, True
            continue
        ref = _REF_DEF.match(line)
        if sources_level or ref:
            flush()
            urls = _urls(line)
            if urls:
                num = None
                if ref:
                    num = int(ref.group(1))
                else:
                    m = _ENTRY_NUM.match(line)
                    if m:
                        num = int(m.group(1) or m.group(2))
                entries.append(Source(url=urls[0], title=_title(line, urls[0]), number=num,
                                      line=i))
            continue
        if not line.strip() or re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", line):
            flush()
            continue
        if _TABLE_SEP.match(line):
            # The line before was the header row: not a claim.
            if blocks and blocks[-1][0] == i - 1 and "|" in blocks[-1][1]:
                blocks.pop()
            continue
        text = line.strip()
        if text.startswith("|"):
            flush()
            blocks.append((i, text.strip("|").replace("|", "; ")))
            continue
        text = re.sub(r"^>\s?", "", text)
        if _LIST_ITEM.match(line):
            flush()
            text = _LIST_ITEM.sub("", line, count=1)
            text = re.sub(r"^\[[ xX]\]\s+", "", text)  # task list box
        if not current:
            start = i
        current.append(text)
    flush()
    return blocks, entries, found


def _sentences(text: str) -> list[str]:
    """Split a block into sentences, keeping abbreviations, initials and URLs
    whole, and a lone trailing marker (``. [3]``) with its sentence."""
    protected = _URL.sub(lambda m: m.group(0).replace(".", "\0").replace("?", "\1")
                         .replace("!", "\2"), text)
    parts: list[str] = []
    start = 0
    for m in re.finditer(r"[.!?]+[\"'”’)\]*_]*\s+", protected):
        end = m.start()
        word = re.search(r"([A-Za-z.]+)$", protected[start:end])
        w = (word.group(1).lower().rstrip(".") if word else "")
        if protected[end] == "." and (w in _ABBREV or re.fullmatch(r"[a-z]", w)):
            continue
        parts.append(protected[start:m.end()])
        start = m.end()
    parts.append(protected[start:])
    out: list[str] = []
    for p in parts:
        p = p.replace("\0", ".").replace("\1", "?").replace("\2", "!").strip()
        if not p:
            continue
        lead = re.match(r"(?:\[\^?\d[^\]]*\]\s*)+", p)
        if out and lead and _MARKER.fullmatch(lead.group(0).split("]")[0] + "]"):
            # ``... insurers. [2] It moved ...``: [2] is the sentence before's.
            out[-1] = f"{out[-1]} {lead.group(0).strip()}"
            p = p[lead.end():].strip()
            if not p:
                continue
        bare = _UNVERIFIED.sub("", _MARKER.sub("", _URL.sub("", p))).strip(" ()[].,;*_-")
        if out and not bare:
            out[-1] = f"{out[-1]} {p}"  # a marker, URL or label alone: the sentence before's
        elif out and _LEAD_LABEL.fullmatch(out[-1]):
            out[-1] = f"{out[-1]} {p}"  # ``**Page size is fixed.** You cannot ... [1].``
        else:
            out.append(p)
    return out


def _host_denied(url: str, denied: Iterable[str]) -> str | None:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    for dom in denied:
        if dom and (host == dom or host.endswith("." + dom)):
            return dom
    return None


def check(text: str, *, name: str = "the result", min_sources: int = MIN_SOURCES,
          min_words: int = MIN_WORDS, denied_domains: Iterable[str] = ()) -> Result:
    """Check one research result against the §2.11 rules."""
    denied = [d.lower().strip() for d in denied_domains if d and d.strip()]
    blocks, entries, _found = _blocks(text.splitlines())
    by_number: dict[int, Source] = {}
    for e in entries:
        if e.number is not None:
            by_number.setdefault(e.number, e)
    res = Result()
    cited: dict[str, Source] = {}
    used_entries: set[int] = set()
    missing: dict[int, int] = {}  # marker number -> first line

    def cite(url: str, *, title: str = "", number: int | None = None, line: int = 0) -> None:
        key = normalize_url(url)
        if key and key not in cited:
            cited[key] = Source(url=url, title=title, number=number, line=line)

    for line, block in blocks:
        res.words += len(_words(_MARKER.sub(" ", block)))
        for sentence in _sentences(block):
            urls = _urls(sentence)
            numbers = [n for g in _MARKER.findall(_MD_LINK.sub(r"\1", sentence))
                       for n in _numbers(g)]
            good = [n for n in numbers if n in by_number]
            for n in numbers:
                if n not in by_number:
                    missing.setdefault(n, line)
            for u in urls:
                m = next((md for md in _MD_LINK.finditer(sentence) if md.group(2) == u
                          or _clean_url(md.group(2)) == u), None)
                cite(u, title=m.group(1).strip() if m else "", line=line)
            for n in good:
                used_entries.add(n)
                e = by_number[n]
                cite(e.url, title=e.title, number=n, line=e.line)
            claim_words = _words(_UNVERIFIED.sub("", _MARKER.sub("", sentence)))
            stripped = re.sub(r"[\s*_\"'”’)\]]+$", "", _MARKER.sub("", _URL.sub("", sentence)))
            if len(claim_words) < CLAIM_MIN_WORDS or stripped.endswith((":", "?")):
                continue
            res.claims.append(Claim(line=line, text=sentence, cited=bool(urls or good),
                                    unverified=bool(_UNVERIFIED.search(sentence))))
    # Entries nothing cites by number, but whose URL is cited inline, are used.
    for e in entries:
        if e.number not in used_entries and normalize_url(e.url) not in cited:
            res.unused.append(e)
    res.sources = list(cited.values())

    if len(res.sources) < min_sources:
        res.problems.append(
            f"{name} cites {len(res.sources)} distinct source URL(s); at least {min_sources} "
            "are required" + (f" ({len(res.unused)} Sources entr"
                              f"{'y is' if len(res.unused) == 1 else 'ies are'} never cited)"
                              if res.unused else ""))
    for n, line in sorted(missing.items()):
        res.problems.append(f"{name} line {line}: [{n}] is cited but there is no Sources entry {n}")
    uncited = res.uncited
    if uncited:
        shown = "; ".join(f"line {c.line}: \"{_short(c.text)}\"" for c in uncited[:3])
        more = f"; and {len(uncited) - 3} more" if len(uncited) > 3 else ""
        res.problems.append(
            f"{len(uncited)} claim(s) in {name} cite no URL and aren't labelled *unverified*: "
            f"{shown}{more}")
    if res.words < min_words:
        res.problems.append(f"{name} is {res.words} words, under one page (at least {min_words} "
                            "words of body text, not counting Sources)")
    seen: set[str] = set()
    for url in [*(s.url for s in entries), *(s.url for s in res.sources)]:
        dom = _host_denied(url, denied)
        if dom and url not in seen:
            seen.add(url)
            res.problems.append(f"{name} cites {url}, on a denied domain ({dom}); research uses "
                                "public sources only, never it (§1.6)")
    return res


def _short(text: str, limit: int = 90) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def sources_section(res: Result, name: str) -> str:
    """The report's Sources section, written by Nightshift from the result
    file: every distinct cited source, then the unverified count."""
    lines = ["### Sources", ""]
    if not res.sources:
        lines.append(f"`{name}` cites no sources.")
    else:
        n = len(res.sources)
        lines.append(f"{n} distinct source{'s' if n != 1 else ''} cited in `{name}`:")
        lines.append("")
        numbered = sorted((s for s in res.sources if s.number is not None),
                          key=lambda s: s.number)
        for s in numbered + [s for s in res.sources if s.number is None]:
            title = s.title.replace("[", "(").replace("]", ")")
            mark = f"[{s.number}] " if s.number is not None else ""
            lines.append(f"- {mark}{title} - <{s.url}>" if title else f"- {mark}<{s.url}>")
    k = len(res.unverified)
    lines += ["", f"{k} claim{'s' if k != 1 else ''} labelled *unverified*."
              + (f" {len(res.uncited)} uncited and unlabelled (a failed check)."
                 if res.uncited else "")]
    return "\n".join(lines)
