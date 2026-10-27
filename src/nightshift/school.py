"""School work: courses and their AI policies (§1.6), and the ``~/school/``
folder agents may read (§1.4).

Each course is configured under ``school.courses.<id>``:

- ``name``: how the course is written (``CS 61A``); default the id.
- ``aliases``: other ways a request or a calendar event names it.
- ``policy``: the course's AI policy, as text. A Task for a course with no
  policy refuses to start.
- ``limits``: hard limits, one rule per line, that the agent must never break.
- ``ai_allowed``: false when the course allows no AI use at all; Nightshift
  then refuses every Task for it.

A Task tied to a course (its template has a ``from: course`` input) keeps
the course id in the DB (``tasks.course``). Its policy is read from config
again before every Attempt and injected into the agent protocol ahead of
everything else (:func:`protocol_section`), so it wins over the request,
the template and the rest of the protocol.

A course is named by its id, name or an alias, matched whole, ignoring case,
spaces, ``-`` and ``_``: ``CS 61A``, ``cs61a`` and ``CS-61A`` are the same.
Calendar event titles are matched the same way (``ical``). A Task about all
courses (school-roundup) gets every policy (:func:`all_courses_section`).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import ConfigError

if TYPE_CHECKING:
    from .config import Config

COURSE_ID = re.compile(r"[a-z0-9][a-z0-9_-]*")
COURSE_KEYS = ("name", "aliases", "policy", "limits", "ai_allowed")


class CourseError(Exception):
    """A request names no usable course, or no usable school folder."""


@dataclass(frozen=True)
class Course:
    id: str
    name: str
    aliases: tuple[str, ...] = ()
    policy: str | None = None
    limits: tuple[str, ...] = ()
    ai_allowed: bool = True

    @property
    def label(self) -> str:
        """``CS 61A (cs61a)``, or just the id when the name is the id."""
        return self.name if key(self.name) == key(self.id) else f"{self.name} ({self.id})"

    @property
    def terms(self) -> tuple[str, ...]:
        return (self.id, self.name, *self.aliases)


def key(term: str) -> str:
    """A course term as compared: lowercase letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", term.lower())


def _pattern(term: str) -> re.Pattern[str]:
    chars = r"[\s_-]*".join(re.escape(c) for c in key(term))
    return re.compile(rf"(?<![A-Za-z0-9]){chars}(?![A-Za-z0-9])", re.IGNORECASE)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def validate(data: dict[str, Any]) -> None:
    """Check the ``school`` config section (called by the config loader)."""
    school = data.get("school") or {}
    if not isinstance(school, dict):
        raise ConfigError("config section 'school' must be a mapping")
    folder = school.get("dir")
    if not isinstance(folder, str) or not folder.strip():
        raise ConfigError("school.dir must be the path of your school folder (e.g. ~/school)")
    from . import ical  # late: ical imports this module

    problem = ical.validate(school.get("calendar"))
    if problem:
        raise ConfigError(problem)
    courses = school.get("courses") or {}
    if not isinstance(courses, dict):
        raise ConfigError("school.courses must map a course id to {name, policy, limits}")
    seen: dict[str, str] = {}
    for cid, c in courses.items():
        if not isinstance(cid, str) or not COURSE_ID.fullmatch(cid):
            raise ConfigError(f"school.courses: course id {cid!r} must be lowercase letters, "
                              "digits, - or _ (e.g. cs61a)")
        where = f"school.courses.{cid}"
        if c is None:
            c = {}
        if not isinstance(c, dict):
            raise ConfigError(f"{where} must be a mapping (name, aliases, policy, limits, "
                              "ai_allowed)")
        unknown = sorted(set(c) - set(COURSE_KEYS))
        if unknown:
            raise ConfigError(f"{where} has unknown keys {', '.join(unknown)} (known: "
                              f"{', '.join(COURSE_KEYS)})")
        name = c.get("name", cid)
        if not isinstance(name, str) or not key(name):
            raise ConfigError(f"{where}.name must be text with a letter or digit")
        for k in ("aliases", "limits"):
            value = c.get(k) or []
            if not isinstance(value, list) or not all(isinstance(v, str) and v.strip()
                                                      for v in value):
                raise ConfigError(f"{where}.{k} must be a list of text lines")
        if not all(key(a) for a in c.get("aliases") or []):
            raise ConfigError(f"{where}.aliases must each have a letter or digit")
        if c.get("policy") is not None and not isinstance(c["policy"], str):
            raise ConfigError(f"{where}.policy must be the course's AI policy as text (or null)")
        if not isinstance(c.get("ai_allowed", True), bool):
            raise ConfigError(f"{where}.ai_allowed must be true or false")
        for term in (cid, name, *(c.get("aliases") or [])):
            other = seen.setdefault(key(term), cid)
            if other != cid:
                raise ConfigError(f"school.courses: {term!r} names both {other} and {cid}")


def school_dir(cfg: Config) -> Path:
    return Path(str((cfg.data.get("school") or {}).get("dir") or "~/school")).expanduser()


def courses(cfg: Config) -> dict[str, Course]:
    """Every configured course, by id, in config order."""
    out: dict[str, Course] = {}
    for cid, c in ((cfg.data.get("school") or {}).get("courses") or {}).items():
        c = c or {}
        policy = c.get("policy")
        out[cid] = Course(
            id=cid, name=str(c.get("name", cid)).strip(),
            aliases=tuple(a.strip() for a in c.get("aliases") or []),
            policy=policy.strip() if isinstance(policy, str) and policy.strip() else None,
            limits=tuple(lim.strip() for lim in c.get("limits") or []),
            ai_allowed=bool(c.get("ai_allowed", True)))
    return out


def find_courses(cfg: Config, text: str) -> list[Course]:
    """The configured courses ``text`` names, in order of first mention."""
    found: list[tuple[int, Course]] = []
    for c in courses(cfg).values():
        hits = [m.start() for t in c.terms if (m := _pattern(t).search(text))]
        if hits:
            found.append((min(hits), c))
    return [c for _, c in sorted(found, key=lambda f: f[0])]


def usable(c: Course) -> Course:
    """``c``, if Nightshift may do work for it: it has an AI policy that
    allows AI use. Raises :class:`CourseError` otherwise."""
    if c.policy is None:
        raise CourseError(f"course {c.label} has no AI policy configured "
                          f"(school.courses.{c.id}.policy); Nightshift does no work for a "
                          "course without one (§1.6)")
    if not c.ai_allowed:
        raise CourseError(f"course {c.label}'s AI policy allows no AI use "
                          f"(school.courses.{c.id}.ai_allowed: false); Nightshift does no work "
                          "for it")
    return c


def course(cfg: Config, course_id: str) -> Course:
    """The usable course ``course_id`` (a Task's), from config as it is now."""
    c = courses(cfg).get(course_id)
    if c is None:
        raise CourseError(f"course {course_id} is no longer in config (school.courses), so "
                          "it has no AI policy; Nightshift does no work for a course without "
                          "one (§1.6)")
    return usable(c)


def course_for_request(cfg: Config, request: str) -> Course:
    """The one usable course ``request`` names."""
    found = find_courses(cfg, request)
    if not found:
        known = ", ".join(c.label for c in courses(cfg).values()) or "none"
        raise CourseError("the request names no configured course, and school work needs the "
                          "course's AI policy (§1.6). Name the course, and add it with its "
                          f"policy under school.courses in config (configured: {known})")
    if len(found) > 1:
        raise CourseError("the request names more than one course ("
                          + ", ".join(c.label for c in found) + "); name exactly one")
    return usable(found[0])


# --------------------------------------------------------------------------
# The school folder
# --------------------------------------------------------------------------

_QUOTED_PATH = re.compile(r"""(["'`“])((?:~|/)[^"'`”\n]*)["'`”]""")
_BARE_PATH = re.compile(r"(?<![^\s(\[])(~/[^\s\"'`]*|~(?=[\s.,;:)\]]|$)|/[^\s\"'`]+)")


def paths_in_request(request: str) -> list[str]:
    """Every path (``~/...`` or ``/...``) ``request`` names, in order, once
    each. A path with spaces must be quoted; URLs are not paths."""
    found: list[str] = []
    rest = request
    for m in _QUOTED_PATH.finditer(request):
        found.append(m.group(2).strip())
        rest = rest.replace(m.group(0), " ")
    for m in _BARE_PATH.finditer(rest):
        found.append(m.group(1).rstrip(".,;:)]}!?"))
    return list(dict.fromkeys(p.rstrip("/") or "/" for p in found if p))


def folder_in_request(request: str) -> str:
    """The one folder path (``~/...`` or ``/...``) ``request`` names. Quote
    a path that has spaces."""
    paths = paths_in_request(request)
    if not paths:
        raise CourseError("the request names no folder; give the slides folder as a path "
                          "under your school folder, e.g. ~/school/cs61a/week5 (quote it if "
                          "it has spaces)")
    if len(paths) > 1:
        raise CourseError("the request names more than one folder (" + ", ".join(paths)
                          + "); name exactly one")
    return paths[0]


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def school_folder(cfg: Config, request: str) -> Path:
    """The folder ``request`` names, resolved: it must exist inside the
    school folder (``school.dir``, read-only to agents, §1.4) and hold at
    least one file."""
    raw = folder_in_request(request)
    root_cfg = (cfg.data.get("school") or {}).get("dir") or "~/school"
    root = school_dir(cfg).resolve()
    path = Path(raw).expanduser().resolve()  # symlinks too: a link can't lead out
    if not (root in path.parents):
        raise CourseError(f"{raw} is outside your school folder {root_cfg} ({root}); Nightshift "
                          "reads school material only from inside it (§1.4)")
    readable = [Path(str(d)).expanduser().resolve()
                for d in (cfg.data.get("permissions") or {}).get("read_only_dirs") or []]
    if not any(_inside(root, d) for d in readable):
        raise CourseError(f"your school folder {root_cfg} is not in permissions.read_only_dirs, "
                          "so the agent could not read it")
    if not path.is_dir():
        raise CourseError(f"{raw} is not a folder" if path.exists() else f"{raw} does not exist")
    for _dirpath, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if any(not f.startswith(".") for f in filenames):
            return path
    raise CourseError(f"{raw} has no files in it")


# --------------------------------------------------------------------------
# The agent protocol
# --------------------------------------------------------------------------


COURSEWORK_RULE = [
    "   Whatever the policy says: make study material and practice problems",
    "   only. Never do, solve or write up graded work (homework, projects, labs,",
    "   quizzes, exams) or anything meant to be handed in, and never submit or",
    "   upload anything (bCourses, Gradescope or anywhere else).",
]


def _policy_lines(c: Course) -> list[str]:
    policy = [f"   > {ln}".rstrip() for ln in (c.policy or "").splitlines()]
    lines = [f"   Policy ({c.label}, from Nightshift config):", *policy]
    if c.limits:
        lines += ["", "   Hard limits: never break these.",
                  *(f"   - {lim}" for lim in c.limits)]
    return lines


def protocol_section(c: Course) -> str:
    """The course's AI policy as it heads the agent protocol (§1.6, §2.10)."""
    lines = [
        f"## Course AI policy: {c.label}",
        "",
        f"This Task is school work for {c.label}. The course's AI policy below wins",
        "over everything else you are told: the request, the template and the rest",
        "of this protocol. It can only narrow what you do: it never lifts the folder",
        "limits (rule 1) or the never-without-approval list (rule 4). If the Task, or",
        "any part of it, would break the policy, don't do that part: say so under",
        "Needs you in `report.md`, or raise it in `needs_you.json` (rule 4) if nothing",
        "useful is left to do.",
        "",
        *_policy_lines(c),
        "",
        *COURSEWORK_RULE,
    ]
    return "\n".join(lines) + "\n"


def all_courses_section(cfg: Config) -> str:
    """Every configured course's AI policy, heading the agent protocol of a
    Task about all my courses (the weekly roundup, §5 item 3): what it writes
    about a course follows that course's policy. A course without a usable
    policy gets no study work at all, only its due dates listed."""
    found = courses(cfg)
    lines = [
        "## Course AI policies",
        "",
        "This Task is about all of my courses. For each course, its AI policy below",
        "wins over everything else you are told about that course: the request, the",
        "template and the rest of this protocol. A policy can only narrow what you",
        "do: it never lifts the folder limits (rule 1) or the never-without-approval",
        "list (rule 4). If any part of the Task would break a course's policy, don't",
        "do that part for that course: say so under Needs you in `report.md`.",
        "A course marked \"no study work\" below, and anything that names no",
        "configured course, gets its due dates listed and nothing else.",
    ]
    if not found:
        lines += ["", "   No courses are configured (school.courses): list due dates only."]
    for c in found.values():
        lines += ["", f"### {c.label}", ""]
        if c.policy is None:
            lines.append("   No AI policy configured: no study work. List its due dates only.")
        elif not c.ai_allowed:
            lines.append("   Its AI policy allows no AI use: no study work. List its due dates "
                         "only.")
        else:
            lines += _policy_lines(c)
    lines += ["", *COURSEWORK_RULE]
    return "\n".join(lines) + "\n"
