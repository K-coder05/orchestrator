"""The template engine (§3.1 item 9).

A template is a YAML file in the repo's ``templates/`` directory (or
``templates.dir`` in config). It defines a Task's ``task_type``, ``agent``,
``estimated_minutes``, ``max_runtime_minutes``, ``long``, ``internet``, its
``prompt`` (with ``{{variables}}``), where its deadline comes from and its
post-hooks. ``orch add "<request>" --template <name>`` turns the request into
a Task: no planning call (the estimate and type come from the template), the
rendered prompt is what the agent gets.

Variables in ``prompt``:

- ``{{request}}``: the ``orch add`` text, verbatim.
- each of ``inputs``: a value taken from the request (``from: url`` = the
  first http(s) URL in it; ``from: course`` = the one configured course it
  names, which must have an AI policy, and ties the Task to that course
  (``nightshift.school``); ``from: school_folder`` = the one folder path it
  names, which must be inside ``school.dir``; ``from: comedy_path`` = the
  one file or folder path it names inside ``comedy.dir``, or that folder
  itself when it names none (``nightshift.comedy``)).
- each of ``files``: a file named by a config key, copied into the Task
  folder (``to:``) when the Task is created; the variable is that relative
  path. A pristine copy is kept in ``<agents_root>/data/inputs/<task-id>/``,
  where the agent can't reach it, and the ``inputs_unchanged`` hook checks
  the agent left its copy alone.

- each of ``values``: a config value (``config: <dotted key>``), rendered
  into the prompt: a list becomes one ``- item`` line per entry (``(none
  configured)`` when empty), anything else its text.

``deadline: {from_output: <file>, field: <key>}``: after every Attempt,
Nightshift reads that JSON file the agent wrote and, if the Task has no
deadline yet (``--deadline`` always wins), sets it from the field (§2.3).

Post-hooks run after an Attempt the agent finished ``done``/``partial``; a
failing one is a Failed Attempt (§2.7). Hook names map to ``HOOKS``.

``report_diffs: [{file: <output>, against: <input file>}]``: Nightshift adds
a unified diff of each output against the pristine input to ``report.md``.
A template with a ``merge_tracker`` post-hook (posting-scan) also gets a New
postings section there, written from the Tracker merge result (§3.1 item 9).
One with a ``snapshot_pages`` post-hook (company-rescrape) gets a Career pages
snapshot section: the diff against the previous Task's snapshot as counts
(``snapshots``). One with a ``research_rules`` post-hook (research-report,
company-brief) gets a Sources section: the distinct sources its result cites,
read by Nightshift from the result file (``research``). One with a
``require_section`` post-hook (punch-up) gets that section of the output
copied in, read from the file. A course Task
(study-guide) gets a Course AI policy section naming the policy it ran under.

``pre_hooks``: Nightshift's own steps before each Attempt (``PRE_HOOKS``).
``school_calendar`` (school-roundup) fetches the calendar feeds (``ical``) and
writes the coming events to ``to``; one that fails is a Failed Attempt and the
agent doesn't start. ``courses: all``: the Task is about every course, so every
course's AI policy heads its protocol (and its report says which).
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import yaml

from . import comedy, ical, latex, research, school, snapshots, trace, tracker
from .config import Config
from .db import Database
from .failures import PostHook, PostHookFailed
from .failures import log_line as _log_line
from .host import Host
from .timeutil import iso, now
from .workspace import read_task_yaml, write_task_yaml


def log_line(folder: Path, line: str, redact: Callable[[str], str], tz: str) -> None:
    """A timestamped line in the Task's ``logs/nightshift.log``."""
    _log_line(folder, f"{iso(now(tz))} {line}", redact)


class TemplateError(Exception):
    """A template is missing or invalid, or a request doesn't fit it."""


#: The repo's built-in templates (src/nightshift/../../templates).
BUILTIN_DIR = Path(__file__).resolve().parents[2] / "templates"

REQUIRED = ("task_type", "agent", "estimated_minutes", "max_runtime_minutes", "long",
            "internet", "prompt")
OPTIONAL = ("name", "description", "inputs", "files", "values", "deadline", "post_hooks",
            "report_diffs", "schedule", "pre_hooks", "courses")
#: Keys of a template's ``schedule:`` block (its standard Schedule's rules).
SCHEDULE_KEYS = ("cron", "catch_up", "overlap", "request")
#: What an ``inputs`` entry takes from the request (``from:``).
INPUT_KINDS = ("url", "course", "school_folder", "comedy_path")
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_URL = re.compile(r"https?://[^\s<>\"'`]+")


def templates_dir(cfg: Config) -> Path:
    custom = (cfg.data.get("templates") or {}).get("dir")
    return Path(str(custom)).expanduser() if custom else BUILTIN_DIR


def available(cfg: Config) -> list[str]:
    d = templates_dir(cfg)
    return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []


@dataclass
class Template:
    name: str
    path: Path
    task_type: str
    agent: str
    estimated_minutes: int
    max_runtime_minutes: int
    long: bool
    internet: str
    prompt: str
    description: str = ""
    inputs: dict[str, dict] = field(default_factory=dict)
    files: dict[str, dict] = field(default_factory=dict)
    values: dict[str, dict] = field(default_factory=dict)
    deadline: dict | None = None
    post_hooks: list[dict] = field(default_factory=list)
    report_diffs: list[dict] = field(default_factory=list)
    #: The template's standard Schedule rules (``orch schedule add NAME``
    #: uses them when no flag is given): {cron, catch_up, overlap}.
    schedule: dict | None = None
    #: Steps Nightshift runs before each Attempt (``PRE_HOOKS``).
    pre_hooks: list[dict] = field(default_factory=list)
    #: ``all``: the Task is about all my courses; every course's AI policy
    #: heads its agent protocol (``school.all_courses_section``).
    courses: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """What ``planning.from_template`` reads."""
        return {"name": self.name, "estimated_minutes": self.estimated_minutes,
                "task_type": self.task_type}


def _rel_path(name: str, where: str, value: Any) -> str:
    """A path relative to the Task folder that can't leave it."""
    if not isinstance(value, str) or not value.strip():
        raise TemplateError(f"template {name!r}: {where} must be a relative path")
    p = Path(value)
    if p.is_absolute() or ".." in p.parts:
        raise TemplateError(f"template {name!r}: {where} {value!r} must stay inside the Task folder")
    return value


def _positive(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def load(cfg: Config, name: str) -> Template:
    """Load and validate the template ``name`` (its file stem)."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name or ""):
        raise TemplateError(f"bad template name {name!r}")
    path = templates_dir(cfg) / f"{name}.yaml"
    if not path.is_file():
        known = ", ".join(available(cfg)) or "none"
        raise TemplateError(f"no template {name!r} in {templates_dir(cfg)} (available: {known})")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise TemplateError(f"{path} is not valid YAML: {exc}") from None
    return parse(cfg, name, path, data)


def parse(cfg: Config, name: str, path: Path, data: Any) -> Template:
    if not isinstance(data, dict):
        raise TemplateError(f"template {name!r} must be a mapping")
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise TemplateError(f"template {name!r} is missing {', '.join(missing)}")
    unknown = sorted(set(data) - set(REQUIRED) - set(OPTIONAL))
    if unknown:
        raise TemplateError(f"template {name!r} has unknown keys: {', '.join(unknown)}")
    if data.get("name", name) != name:
        raise TemplateError(f"template {path.name} says its name is {data['name']!r}")
    if data["task_type"] not in cfg.task_types:
        raise TemplateError(f"template {name!r}: unknown task_type {data['task_type']!r}")
    if data["agent"] not in (cfg.data.get("agents") or {}):
        raise TemplateError(f"template {name!r}: agent {data['agent']!r} is not configured")
    for key in ("estimated_minutes", "max_runtime_minutes"):
        if not _positive(data[key]):
            raise TemplateError(f"template {name!r}: {key} must be a positive integer")
    if not isinstance(data["long"], bool):
        raise TemplateError(f"template {name!r}: long must be true or false")
    if data["internet"] not in ("on", "off"):
        raise TemplateError(f"template {name!r}: internet must be \"on\" or \"off\" (quoted)")
    if not isinstance(data["prompt"], str) or not data["prompt"].strip():
        raise TemplateError(f"template {name!r}: prompt must be text")

    inputs = data.get("inputs") or {}
    if not isinstance(inputs, dict):
        raise TemplateError(f"template {name!r}: inputs must be a mapping")
    for var, spec in inputs.items():
        if not isinstance(spec, dict) or set(spec) != {"from"} or spec["from"] not in INPUT_KINDS:
            raise TemplateError(f"template {name!r}: input {var!r} needs from: "
                                + " | ".join(INPUT_KINDS))
    kinds = [spec["from"] for spec in inputs.values()]
    for kind in ("course", "school_folder", "comedy_path"):
        if kinds.count(kind) > 1:
            raise TemplateError(f"template {name!r}: at most one input from: {kind}")
    if "school_folder" in kinds and "course" not in kinds:
        raise TemplateError(f"template {name!r}: an input from: school_folder needs one from: "
                            "course too (school work runs under its course's AI policy)")
    files = data.get("files") or {}
    if not isinstance(files, dict):
        raise TemplateError(f"template {name!r}: files must be a mapping")
    for var, spec in files.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("config"), str):
            raise TemplateError(f"template {name!r}: file {var!r} needs config: <dotted key>")
        _rel_path(name, f"file {var!r} to", spec.get("to"))
    values = data.get("values") or {}
    if not isinstance(values, dict):
        raise TemplateError(f"template {name!r}: values must be a mapping")
    for var, spec in values.items():
        if not isinstance(spec, dict) or set(spec) != {"config"} or not isinstance(
                spec["config"], str):
            raise TemplateError(f"template {name!r}: value {var!r} needs config: <dotted key>")
    clash = sorted((set(inputs) & set(files)) | (set(inputs) & set(values))
                   | (set(files) & set(values)) | ({"request"} & (set(inputs) | set(files) | set(values))))
    if clash:
        raise TemplateError(f"template {name!r}: variable(s) {', '.join(clash)} defined twice")
    deadline = data.get("deadline")
    if deadline is not None:
        if not isinstance(deadline, dict) or not isinstance(deadline.get("field"), str):
            raise TemplateError(f"template {name!r}: deadline needs from_output and field")
        _rel_path(name, "deadline from_output", deadline.get("from_output"))
    hooks = data.get("post_hooks") or []
    if not isinstance(hooks, list):
        raise TemplateError(f"template {name!r}: post_hooks must be a list")
    for h in hooks:
        if not isinstance(h, dict) or h.get("hook") not in HOOKS:
            raise TemplateError(
                f"template {name!r}: post-hook {h!r} must name one of {', '.join(sorted(HOOKS))}")
        HOOKS[h["hook"]].validate(name, h)
        for key in HOOKS[h["hook"]].file_keys:
            _input_file(name, files, f"{h['hook']} {key}", h.get(key))
    diffs = data.get("report_diffs") or []
    if not isinstance(diffs, list):
        raise TemplateError(f"template {name!r}: report_diffs must be a list")
    for d in diffs:
        if not isinstance(d, dict) or set(d) - {"file", "against"}:
            raise TemplateError(f"template {name!r}: each report_diffs item is "
                                "{file: <output>, against: <input file>}")
        _rel_path(name, "report_diffs file", d.get("file"))
        _input_file(name, files, "report_diffs against", d.get("against"))

    schedule = _validate_schedule(name, data.get("schedule"))
    pre = data.get("pre_hooks") or []
    if not isinstance(pre, list):
        raise TemplateError(f"template {name!r}: pre_hooks must be a list")
    for h in pre:
        if not isinstance(h, dict) or h.get("hook") not in PRE_HOOKS:
            raise TemplateError(f"template {name!r}: pre-hook {h!r} must name one of "
                                f"{', '.join(sorted(PRE_HOOKS))}")
        PRE_HOOKS[h["hook"]].validate(name, h)
    courses = data.get("courses")
    if courses is not None and courses != "all":
        raise TemplateError(f"template {name!r}: courses must be all (the Task is about every "
                            "configured course) or left out")
    if courses and "course" in [spec["from"] for spec in inputs.values()]:
        raise TemplateError(f"template {name!r}: courses: all and an input from: course "
                            "exclude each other")

    known = {"request", *inputs, *files, *values}
    used = set(_VAR.findall(data["prompt"]))
    if used - known:
        raise TemplateError(
            f"template {name!r}: prompt uses unknown variables {', '.join(sorted(used - known))}")
    return Template(
        name=name, path=path, task_type=data["task_type"], agent=data["agent"],
        estimated_minutes=data["estimated_minutes"],
        max_runtime_minutes=data["max_runtime_minutes"], long=data["long"],
        internet=data["internet"], prompt=data["prompt"],
        description=str(data.get("description") or ""), inputs=dict(inputs), files=dict(files),
        values=dict(values), deadline=deadline, post_hooks=list(hooks), report_diffs=list(diffs),
        schedule=schedule, pre_hooks=list(pre), courses=courses,
    )


def _validate_schedule(name: str, block: Any) -> dict | None:
    """A template's standard Schedule: ``schedule: {cron, catch_up,
    overlap, request}`` (overlap defaults to skip, request to empty), checked
    with the same rules as `orch schedule add`."""
    if block is None:
        return None
    from . import cron, schedules  # late: schedules imports this module

    if not isinstance(block, dict) or set(block) - set(SCHEDULE_KEYS) or not {
            "cron", "catch_up"} <= set(block):
        raise TemplateError(f"template {name!r}: schedule is {{cron: <rule>, catch_up: <rule>, "
                            "overlap: skip|replace, request: <text>}} (overlap and request "
                            "optional)")
    try:
        cron.parse(str(block["cron"]))
        schedules.parse_catch_up(str(block["catch_up"]))
    except (cron.CronError, schedules.ScheduleError) as exc:
        raise TemplateError(f"template {name!r}: schedule: {exc}") from None
    overlap = block.get("overlap", schedules.OVERLAP_SKIP)
    if overlap not in schedules.OVERLAPS:
        raise TemplateError(f"template {name!r}: schedule overlap must be one of "
                            f"{', '.join(schedules.OVERLAPS)}")
    request = block.get("request", "")
    if not isinstance(request, str):
        raise TemplateError(f"template {name!r}: schedule request must be text")
    return {"cron": str(block["cron"]), "catch_up": str(block["catch_up"]), "overlap": overlap,
            "request": request}


def _input_file(name: str, files: dict, where: str, value: Any) -> None:
    """``value`` must be where one of the template's ``files`` is copied
    (the check reads Nightshift's pristine copy, not the agent's)."""
    targets = sorted(spec["to"] for spec in files.values())
    if value not in targets:
        raise TemplateError(f"template {name!r}: {where} must be one of the template's input "
                            f"files ({', '.join(targets) or 'none'})")


# --------------------------------------------------------------------------
# Instantiating a template for one request
# --------------------------------------------------------------------------


@dataclass
class InputFile:
    var: str
    source: Path
    to: str  # relative to the Task folder
    content: bytes


@dataclass
class Instance:
    template: Template
    request: str
    prompt: str  # rendered: what the agent gets
    values: dict[str, str]
    files: list[InputFile]
    #: The ``school.courses`` id of a ``from: course`` input, else None.
    course: str | None = None


def config_value(cfg: Config, dotted: str) -> Any:
    node: Any = cfg.data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def render_value(value: Any) -> str:
    """A config value as prompt text: a list is one ``- item`` per line."""
    if isinstance(value, (list, tuple)):
        if not value:
            return "(none configured)"
        lines = []
        for item in value:
            if isinstance(item, dict):
                item = ", ".join(f"{k}: {v}" for k, v in item.items())
            lines.append(f"- {item}")
        return "\n".join(lines)
    if value is None or value == "":
        return "(none configured)"
    return str(value)


def denied_domains(cfg: Config) -> list[str]:
    return [str(d).lower().strip()
            for d in (cfg.data.get("permissions") or {}).get("denied_domains") or []
            if str(d).strip()]


def _denied_domain(cfg: Config, host: str) -> str | None:
    host = host.lower().rstrip(".")
    for dom in (cfg.data.get("permissions") or {}).get("denied_domains") or []:
        dom = str(dom).lower().strip()
        if dom and (host == dom or host.endswith("." + dom)):
            return dom
    return None


def instantiate(cfg: Config, tpl: Template, request: str) -> Instance:
    """Fill in the template for ``request``. Reads the input files now, so a
    missing one fails ``orch add`` before any Task exists."""
    values: dict[str, str] = {"request": request.strip()}
    course = None
    for var, spec in tpl.inputs.items():
        try:
            if spec["from"] == "course":
                c = school.course_for_request(cfg, request)
                values[var], course = c.label, c.id
                continue
            if spec["from"] == "school_folder":
                values[var] = str(school.school_folder(cfg, request))
                continue
            if spec["from"] == "comedy_path":
                values[var] = str(comedy.material(cfg, request))
                continue
        except (school.CourseError, comedy.ComedyError) as exc:
            raise TemplateError(f"template {tpl.name!r}: {exc}") from None
        found = _URL.search(request)
        if not found:
            raise TemplateError(f"template {tpl.name!r} needs a {var} (an http(s) URL) in the request")
        url = found.group(0).rstrip(".,;:)]}")
        host = urlparse(url).hostname or ""
        dom = _denied_domain(cfg, host)
        if dom:
            raise TemplateError(f"{var} {url} is on a denied domain ({dom}); "
                                "Nightshift never fetches it (§1.6)")
        values[var] = url
    files: list[InputFile] = []
    for var, spec in tpl.files.items():
        raw = config_value(cfg, spec["config"])
        if not raw:
            raise TemplateError(f"template {tpl.name!r} needs {spec['config']} set in config "
                                f"(the {var.replace('_', ' ')})")
        src = Path(str(raw)).expanduser()
        if not src.is_file():
            raise TemplateError(f"{spec['config']}: {src} does not exist")
        files.append(InputFile(var=var, source=src, to=spec["to"], content=src.read_bytes()))
        values[var] = spec["to"]
    for var, spec in tpl.values.items():
        values[var] = render_value(config_value(cfg, spec["config"]))
    prompt = _VAR.sub(lambda m: values[m.group(1)], tpl.prompt)
    return Instance(template=tpl, request=request, prompt=prompt, values=values, files=files,
                    course=course)


def pristine_dir(cfg: Config, task_id: str) -> Path:
    return cfg.data_dir / "inputs" / task_id


def install_inputs(cfg: Config, instance: Instance, folder: Path, task_id: str) -> None:
    """Copy the input files into the new Task folder, plus a pristine copy
    in the data dir (agents have no access there)."""
    keep = pristine_dir(cfg, task_id)
    for f in instance.files:
        dest = folder / f.to
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(f.content)
        (keep / f.to).parent.mkdir(parents=True, exist_ok=True)
        (keep / f.to).write_bytes(f.content)
    if instance.files:
        log_line(folder, "inputs copied: " + ", ".join(
            f"{f.source} -> {f.to}" for f in instance.files), cfg.secrets.redact, cfg.timezone)


# --------------------------------------------------------------------------
# Deadline source
# --------------------------------------------------------------------------


def parse_close_date(value: Any, tz_name: str) -> str | None:
    """A posting's close date: ``YYYY-MM-DD`` (end of that day, local) or an
    ISO 8601 timestamp; None for anything else. A passed date is kept: the
    deadline is a fact about the posting."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    tz = ZoneInfo(tz_name)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return iso(datetime.combine(datetime.fromisoformat(text).date(), time(23, 59, 59), tz))
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return iso(dt if dt.tzinfo else dt.replace(tzinfo=tz))


def apply_output_deadline(cfg: Config, db: Database, task_id: str) -> str | None:
    """Set the Task's deadline from its template's deadline source, if it
    has none yet. Returns the new deadline, or None. Never raises for bad
    agent output (it is logged and ignored)."""
    row = db.get_task(task_id)
    if row is None or not row["template"] or row["deadline"]:
        return None
    try:
        tpl = load(cfg, row["template"])
    except TemplateError:
        return None
    if not tpl.deadline:
        return None
    folder = Path(row["folder"])
    path = folder / tpl.deadline["from_output"]
    if not path.is_file() or path.is_symlink():
        return None
    key = tpl.deadline["field"]
    try:
        value = json.loads(path.read_text()).get(key)
    except (ValueError, AttributeError, OSError):
        log_line(folder, f"deadline source {tpl.deadline['from_output']} is not a JSON object; "
                 "ignored", cfg.secrets.redact, cfg.timezone)
        return None
    if value in (None, ""):
        return None
    deadline = parse_close_date(value, cfg.timezone)
    if deadline is None:
        log_line(folder, f"deadline source {key}={value!r} is not a date; ignored",
                 cfg.secrets.redact, cfg.timezone)
        return None
    db.set_deadline(task_id, deadline)
    if (folder / "task.yaml").exists():
        spec = read_task_yaml(folder)
        spec.deadline = deadline
        write_task_yaml(folder, spec)
    log_line(folder, f"deadline set to {deadline} from {tpl.deadline['from_output']} ({key})",
             cfg.secrets.redact, cfg.timezone)
    return deadline


# --------------------------------------------------------------------------
# Post-hooks
# --------------------------------------------------------------------------


@dataclass
class HookKind:
    validate: Callable[[str, dict], None]
    build: Callable[[Config, Host, Template, dict], PostHook]
    #: Keys naming one of the template's input files (checked against ``files``).
    file_keys: tuple[str, ...] = ()


def _named(fn: PostHook, name: str) -> PostHook:
    fn.__name__ = name  # type: ignore[attr-defined]
    return fn


def _regular_file(folder: Path, rel: str) -> Path:
    p = folder / rel
    try:
        latex.inside(folder, p)
    except latex.LatexError as exc:
        raise PostHookFailed(str(exc)) from None
    if p.is_symlink() or not p.is_file():
        raise PostHookFailed(f"{rel} is missing")
    if p.stat().st_size == 0:
        raise PostHookFailed(f"{rel} is empty")
    return p


def _validate_require(name: str, h: dict) -> None:
    files = h.get("files")
    if not isinstance(files, list) or not files:
        raise TemplateError(f"template {name!r}: require_outputs needs a list of files")
    for f in files:
        _rel_path(name, "require_outputs file", f)


def _build_require(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        missing = []
        for rel in h["files"]:
            try:
                _regular_file(folder, rel)
            except PostHookFailed as exc:
                missing.append(str(exc))
        if missing:
            raise PostHookFailed("; ".join(missing))
    return _named(hook, "require_outputs")


def _validate_compile(name: str, h: dict) -> None:
    _rel_path(name, "compile_latex source", h.get("source"))
    _rel_path(name, "compile_latex pdf", h.get("pdf"))
    if not str(h["source"]).endswith(".tex") or not str(h["pdf"]).endswith(".pdf"):
        raise TemplateError(f"template {name!r}: compile_latex goes from a .tex to a .pdf")
    if "pages" in h and not _positive(h["pages"]):
        raise TemplateError(f"template {name!r}: compile_latex pages must be a positive integer")


def _build_compile(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        src = _regular_file(folder, h["source"])
        try:
            built = latex.compile_tex(cfg, host, folder, src, folder / h["pdf"])
        except latex.LatexError as exc:
            raise PostHookFailed(str(exc)) from None
        want = h.get("pages")
        log_line(folder, f"compiled {h['source']} -> {h['pdf']}: {built.pages} "
                 f"page{'s' if built.pages != 1 else ''}", cfg.secrets.redact, cfg.timezone)
        if want and built.pages != want:
            raise PostHookFailed(f"{h['pdf']} has {built.pages} pages; it must be exactly {want}")
    return _named(hook, f"compile_latex({h['source']})")


def _validate_nothing(name: str, h: dict) -> None:
    pass


def _build_unchanged(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        keep = pristine_dir(cfg, row["id"])
        changed = []
        for spec in tpl.files.values():
            original = keep / spec["to"]
            if not original.is_file():
                raise PostHookFailed(f"the pristine copy of {spec['to']} is gone")
            copy = folder / spec["to"]
            if copy.is_symlink() or not copy.is_file() or copy.read_bytes() != original.read_bytes():
                changed.append(spec["to"])
                # Put it back, so the next Attempt works from the real file.
                if copy.is_symlink() or copy.exists():
                    if copy.is_dir() and not copy.is_symlink():
                        shutil.rmtree(copy)
                    else:
                        copy.unlink()
                copy.parent.mkdir(parents=True, exist_ok=True)
                copy.write_bytes(original.read_bytes())
        if changed:
            raise PostHookFailed(f"the agent changed its input file(s) {', '.join(changed)} "
                                 "(restored); inputs are read-only")
    return _named(hook, "inputs_unchanged")


def _validate_trace(name: str, h: dict) -> None:
    unknown = sorted(set(h) - {"hook", "resume", "changes", "master"})
    if unknown:
        raise TemplateError(f"template {name!r}: trace_changes has unknown keys {', '.join(unknown)}")
    for key in ("resume", "changes", "master"):
        _rel_path(name, f"trace_changes {key}", h.get(key))


def _pristine(cfg: Config, row: Mapping[str, Any], rel: str) -> Path:
    p = pristine_dir(cfg, row["id"]) / rel
    if not p.is_file():
        raise PostHookFailed(f"the pristine copy of {rel} is gone")
    return p


def _read_text(p: Path, rel: str) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise PostHookFailed(f"can't read {rel}: {exc}") from None


def _build_trace(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    """Every change in the resume traces to master.tex (see ``trace``)."""
    master_name = Path(h["master"]).name

    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        master = _read_text(_pristine(cfg, row, h["master"]), h["master"])
        resume = _read_text(_regular_file(folder, h["resume"]), h["resume"])
        changes = _read_text(_regular_file(folder, h["changes"]), h["changes"])
        res = trace.check(master, resume, changes, master_name=master_name,
                          resume_name=h["resume"], changes_name=h["changes"])
        say = lambda line: log_line(folder, line, cfg.secrets.redact, cfg.timezone)  # noqa: E731
        summary = (f"traceability: {len(res.changed)} changed line(s) in {h['resume']}, "
                   f"{len(res.entries)} entr{'y' if len(res.entries) == 1 else 'ies'} in "
                   f"{h['changes']}, {res.numbers_checked} number(s) checked against {master_name}")
        if res.ok:
            say(f"{summary}: all trace")
            return
        say(f"{summary}: {len(res.problems)} problem(s)")
        for p in res.problems:
            say(f"  traceability: {p}")
        shown = res.problems[:5]
        more = len(res.problems) - len(shown)
        raise PostHookFailed("; ".join(shown)
                             + (f"; and {more} more (see logs/nightshift.log)" if more else ""))
    return _named(hook, "trace_changes")


def _validate_merge(name: str, h: dict) -> None:
    unknown = sorted(set(h) - {"hook", "proposed"})
    if unknown:
        raise TemplateError(f"template {name!r}: merge_tracker has unknown keys {', '.join(unknown)}")
    _rel_path(name, "merge_tracker proposed", h.get("proposed"))


def posting_season(cfg: Config) -> str:
    return str(config_value(cfg, "templates.posting_scan.season") or "Summer 2027")


def _build_merge(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    """Merge the agent's Proposed rows into the Tracker (§1.7). Nightshift
    code does it, never the agent. Rows this Task appended in an earlier
    Attempt are recognized (not appended twice, still its New postings)."""

    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        path = _regular_file(folder, h["proposed"])
        say = lambda line: log_line(folder, line, cfg.secrets.redact, cfg.timezone)  # noqa: E731
        db = Database(cfg.db_path, tz=cfg.timezone)  # hooks run in the runner's thread
        try:
            own = [r["id"] for m in db.tracker_merges(row["id"])
                   for r in json.loads(m["result"]).get("appended") or []]
            try:
                res = tracker.merge(cfg.tracker_path, _read_text(path, h["proposed"]),
                                    season=posting_season(cfg), today=now(cfg.timezone).date(),
                                    denied_domains=denied_domains(cfg), own_ids=own)
            except tracker.TrackerError as exc:
                raise PostHookFailed(str(exc)) from None
            attempts = db.attempts(row["id"])
            db.record_tracker_merge(
                row["id"], run_id=attempts[-1]["id"] if attempts else None,
                at=iso(now(cfg.timezone)), tracker_path=str(cfg.tracker_path),
                proposed=res.proposed, appended=len(res.appended),
                duplicates=res.dropped_duplicates, invalid=len(res.invalid),
                result=res.as_dict())
        finally:
            db.close()
        say(f"tracker merge: {res.proposed} proposed row(s): {len(res.appended)} appended to "
            f"{cfg.tracker_path}, {res.dropped_duplicates} duplicate(s) dropped, "
            f"{len(res.invalid)} rejected"
            + (f", {len(res.already_appended)} already appended by this Task"
               if res.already_appended else ""))
        for r in res.appended:
            say(f"  appended id {r['id']}: {r['company']} | {r['role']} | {r['posting_url']}")
        for d in res.duplicates:
            what = f"Tracker id {d.matches}" if d.matches else "an earlier row of the file"
            say(f"  duplicate ({d.by}) of {what}{' (this Task)' if d.own else ''}: "
                f"{d.row.get('company')} | {d.row.get('role')} | {d.row.get('posting_url')}")
        for r in res.invalid:
            say(f"  rejected line {r.line}: {r.reason}")
    return _named(hook, "merge_tracker")


def _validate_snapshot(name: str, h: dict) -> None:
    unknown = sorted(set(h) - {"hook", "pages"})
    if unknown:
        raise TemplateError(f"template {name!r}: snapshot_pages has unknown keys {', '.join(unknown)}")
    _rel_path(name, "snapshot_pages pages", h.get("pages"))


def snapshot_root(cfg: Config, template: str) -> Path:
    """Where a template's career page snapshots go (agents can't reach it)."""
    return cfg.data_dir / "snapshots" / template


def _build_snapshot(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    """Snapshot the agent's career page listing and diff it against the
    previous Task's (``snapshots``). Nightshift code writes ``data/snapshots/``."""

    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        path = _regular_file(folder, h["pages"])
        try:
            pages = snapshots.parse_pages(_read_text(path, h["pages"]))
        except snapshots.SnapshotError as exc:
            raise PostHookFailed(str(exc)) from None
        rec = snapshots.record(snapshot_root(cfg, tpl.name), row["id"], pages,
                               taken_at=now(cfg.timezone))
        log_line(folder, f"snapshot of {len(pages)} career page(s) in {rec.folder}: "
                 f"{rec.summary()}", cfg.secrets.redact, cfg.timezone)
    return _named(hook, "snapshot_pages")


def _validate_research(name: str, h: dict) -> None:
    unknown = sorted(set(h) - {"hook", "file", "min_sources", "min_words"})
    if unknown:
        raise TemplateError(f"template {name!r}: research_rules has unknown keys {', '.join(unknown)}")
    _rel_path(name, "research_rules file", h.get("file"))
    if not str(h["file"]).endswith(".md"):
        raise TemplateError(f"template {name!r}: research_rules checks a Markdown (.md) file")
    for key, floor in (("min_sources", research.MIN_SOURCES), ("min_words", 1)):
        if key in h and not (_positive(h[key]) and h[key] >= floor):
            raise TemplateError(f"template {name!r}: research_rules {key} must be an integer "
                                f"of at least {floor}")


def _research_check(cfg: Config, folder: Path, h: dict) -> research.Result:
    text = _read_text(_regular_file(folder, h["file"]), h["file"])
    return research.check(text, name=h["file"],
                          min_sources=h.get("min_sources", research.MIN_SOURCES),
                          min_words=h.get("min_words", research.MIN_WORDS),
                          denied_domains=denied_domains(cfg))


def _build_research(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    """The §2.11 Research rules, checked by Nightshift on the result file
    (see ``research``): every claim cites a URL or is labelled *unverified*,
    at least 5 distinct sources, never under one page."""

    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        res = _research_check(cfg, folder, h)
        say = lambda line: log_line(folder, line, cfg.secrets.redact, cfg.timezone)  # noqa: E731
        say(res.summary(h["file"]) + (": all pass" if res.ok else f": {len(res.problems)} "
                                      "problem(s)"))
        for c in res.uncited:
            say(f"  uncited claim, line {c.line}: {c.text}")
        for s in res.unused:
            say(f"  Sources entry never cited, line {s.line}: {s.url}")
        if not res.ok:
            for p in res.problems:
                say(f"  research rules: {p}")
            raise PostHookFailed("; ".join(res.problems))
    return _named(hook, "research_rules")


def _validate_section(name: str, h: dict) -> None:
    unknown = sorted(set(h) - {"hook", "file", "heading"})
    if unknown:
        raise TemplateError(f"template {name!r}: require_section has unknown keys "
                            f"{', '.join(unknown)}")
    _rel_path(name, "require_section file", h.get("file"))
    if not str(h["file"]).endswith(".md"):
        raise TemplateError(f"template {name!r}: require_section checks a Markdown (.md) file")
    if not isinstance(h.get("heading"), str) or not _heading_key(h["heading"]):
        raise TemplateError(f"template {name!r}: require_section needs a heading (text)")


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def _heading_key(text: str) -> str:
    """A heading compared loosely: case, ``*``/``_``/`` ` `` emphasis, a
    leading number (``3.``) and trailing punctuation don't count."""
    text = re.sub(r"[*_`]", "", text).strip()
    text = re.sub(r"^\d+[.)]?\s*", "", text)
    return re.sub(r"\s+", " ", text).rstrip(" :.!?").lower()


def markdown_section(text: str, heading: str) -> str | None:
    """The body of the first Markdown section headed ``heading`` (any level,
    matched by :func:`_heading_key`), up to the next heading of the same or
    a higher level; None when there is no such heading. Headings inside
    fenced code don't count."""
    want = _heading_key(heading)
    level, body, fence = 0, [], False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        m = None if fence else _MD_HEADING.match(line)
        if level:
            if m and len(m.group(1)) <= level:
                break
            body.append(line)
        elif m and _heading_key(m.group(2)) == want:
            level = len(m.group(1))
    return "\n".join(body).strip() if level else None


def _section_of(folder: Path, h: dict) -> str:
    """The required section's body; raises PostHookFailed if it is missing
    or empty."""
    text = _read_text(_regular_file(folder, h["file"]), h["file"])
    body = markdown_section(text, h["heading"])
    if body is None:
        raise PostHookFailed(f"{h['file']} has no \"{h['heading']}\" section (a heading "
                             "with that title)")
    if not body:
        raise PostHookFailed(f"the \"{h['heading']}\" section of {h['file']} is empty; "
                             "write \"None\" if there is nothing to put there")
    return body


def _build_section(cfg: Config, host: Host, tpl: Template, h: dict) -> PostHook:
    """The output has a non-empty section with a given heading (punch-up:
    "Close to a known bit", §2.11; it may say "None")."""

    def hook(folder: Path, row: Mapping[str, Any]) -> None:
        body = _section_of(folder, h)
        lines = len(body.splitlines())
        log_line(folder, f"section \"{h['heading']}\" of {h['file']}: present, {lines} "
                 f"line{'s' if lines != 1 else ''}", cfg.secrets.redact, cfg.timezone)
    return _named(hook, f"require_section({h['heading']})")


HOOKS: dict[str, HookKind] = {
    "require_outputs": HookKind(_validate_require, _build_require),
    "compile_latex": HookKind(_validate_compile, _build_compile),
    "inputs_unchanged": HookKind(_validate_nothing, _build_unchanged),
    "trace_changes": HookKind(_validate_trace, _build_trace, file_keys=("master",)),
    "merge_tracker": HookKind(_validate_merge, _build_merge),
    "snapshot_pages": HookKind(_validate_snapshot, _build_snapshot),
    "research_rules": HookKind(_validate_research, _build_research),
    "require_section": HookKind(_validate_section, _build_section),
}


def post_hooks(cfg: Config, row: Mapping[str, Any], host: Host | None = None) -> list[PostHook]:
    """The post-hooks of a template Task, in template order."""
    if not row["template"]:
        return []
    if host is None:
        from .host import MacHost
        host = MacHost()
    tpl = load(cfg, row["template"])
    return [HOOKS[h["hook"]].build(cfg, host, tpl, h) for h in tpl.post_hooks]


# --------------------------------------------------------------------------
# Pre-hooks: Nightshift's steps before each Attempt
# --------------------------------------------------------------------------


class PreHookFailed(Exception):
    """A pre-hook could not prepare the Attempt; it is a Failed Attempt."""


@dataclass
class PreHookKind:
    validate: Callable[[str, dict], None]
    #: run(cfg, tpl, hook, row, folder, at); raises PreHookFailed.
    run: Callable[[Config, Template, dict, Mapping[str, Any], Path, datetime], None]


def _validate_calendar(name: str, h: dict) -> None:
    unknown = sorted(set(h) - {"hook", "to"})
    if unknown:
        raise TemplateError(f"template {name!r}: school_calendar has unknown keys "
                            f"{', '.join(unknown)}")
    _rel_path(name, "school_calendar to", h.get("to"))


def calendar_record(cfg: Config, task_id: str) -> Path:
    """The calendar a Task's Attempts work from, as fetched (no URLs), in
    the data dir where the agent can't reach it."""
    return pristine_dir(cfg, task_id) / "calendar.json"


def _run_calendar(cfg: Config, tpl: Template, h: dict, row: Mapping[str, Any], folder: Path,
                  at: datetime) -> None:
    """Fetch the school calendar feeds (``ical``) and write the coming
    events into the Task folder (``to``). Once per Task: later Attempts
    (retries, resumes) work from the same events. A feed that fails, or no
    feed set up at all, fails the Attempt; nothing is written."""
    dest = folder / h["to"]
    record = calendar_record(cfg, row["id"])
    if record.is_file() and dest.is_file() and not dest.is_symlink():
        return
    if not any(ical.configured(cfg).values()):
        raise PreHookFailed(
            "no calendar feed is set up: put your bCourses and Google Calendar iCal URLs in "
            "~/.config/nightshift/.env or the Keychain as "
            + " and ".join(f.secret for f in ical.FEEDS) + " (`orch doctor` checks them)")
    snap = ical.fetch(cfg, at)
    ical.store(cfg, snap)
    if snap.errors:
        raise PreHookFailed("could not fetch the calendar: " + "; ".join(snap.errors))
    ical.save(record, snap)
    try:
        latex.inside(folder, dest)
    except latex.LatexError as exc:
        raise PreHookFailed(str(exc)) from None
    if dest.is_symlink():
        dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(ical.calendar_markdown(cfg, snap))
    due = sum(1 for e in snap.events if e.due)
    log_line(folder, "calendar fetched: " + ", ".join(
        f"{f.label} {f.events} event(s)" if f.configured else f"{f.label} not configured"
        for f in snap.feeds) + f"; {due} due date(s); written to {h['to']}",
        cfg.secrets.redact, cfg.timezone)


PRE_HOOKS: dict[str, PreHookKind] = {
    "school_calendar": PreHookKind(_validate_calendar, _run_calendar),
}


def run_pre_hooks(cfg: Config, row: Mapping[str, Any], folder: Path, at: datetime) -> str | None:
    """Run a template Task's pre-hooks before an Attempt. Returns why one
    failed (the Attempt is then a Failed Attempt, §2.7), or None."""
    if not row["template"]:
        return None
    try:
        tpl = load(cfg, row["template"])
    except TemplateError:
        return None  # the post-hooks report it
    for h in tpl.pre_hooks:
        try:
            PRE_HOOKS[h["hook"]].run(cfg, tpl, h, row, folder, at)
        except (PreHookFailed, OSError) as exc:
            why = cfg.secrets.redact(f"pre-hook {h['hook']} failed: {exc}")
            log_line(folder, why, cfg.secrets.redact, cfg.timezone)
            return why
    return None


def all_courses_policy(cfg: Config, row: Mapping[str, Any]) -> str | None:
    """The protocol section of a ``courses: all`` template Task: every
    course's AI policy (``school.all_courses_section``); else None."""
    if not row["template"]:
        return None
    try:
        tpl = load(cfg, row["template"])
    except TemplateError:
        return None
    return school.all_courses_section(cfg) if tpl.courses == "all" else None


# --------------------------------------------------------------------------
# Report diffs
# --------------------------------------------------------------------------

#: A longer diff is cut in the report (the files are in the Task folder).
REPORT_DIFF_MAX_LINES = 400


def new_postings(cfg: Config, row: Mapping[str, Any]) -> str:
    """The report's New postings section: every Tracker row this Task's
    merges appended (across its Attempts), and the latest merge's counts of
    dropped duplicates and rejected rows. Written from the DB, never from
    the agent's files."""
    db = Database(cfg.db_path, tz=cfg.timezone)
    try:
        merges = db.tracker_merges(row["id"])
    finally:
        db.close()
    if not merges:
        return ("### New postings\n\nThe Tracker merge did not run, so nothing was appended "
                "to the Tracker (see `logs/nightshift.log`).")
    appended = [r for m in merges for r in json.loads(m["result"]).get("appended") or []]
    last = merges[-1]
    return tracker.new_postings_section(
        appended, dropped_duplicates=int(last["duplicates"]), invalid=int(last["invalid"]),
        tracker_path=Path(last["tracker_path"]))


def snapshot_section(cfg: Config, row: Mapping[str, Any], template: str) -> str:
    """The report's Career pages snapshot section: the diff against the
    previous Task's snapshot as counts, with the path to its ``diff.md``.
    Postings are listed only in New postings."""
    title = "### Career pages snapshot"
    rec = snapshots.load(snapshot_root(cfg, template), row["id"])
    if rec is None:
        return f"{title}\n\nNo snapshot was taken (see `logs/nightshift.log`)."
    n = len(rec.pages)
    lines = [title, "", f"{n} career page{'s' if n != 1 else ''} snapshotted in `{rec.folder}`."]
    lines.append(rec.summary()[:1].upper() + rec.summary()[1:] + ".")
    if rec.previous is not None:
        lines.append(f"Counts are of any role or season. Full diff: "
                     f"`{rec.folder / snapshots.DIFF_MD}`.")
    first = [p for p in rec.pages if p.status == snapshots.FIRST]
    if rec.previous is not None and first:
        lines.append("First snapshot of: " + ", ".join(p.company or p.url for p in first) + ".")
    if rec.failed:
        lines.append("Failed to load: " + ", ".join(
            (p.company or p.url) + (f" ({p.error})" if p.error else "") for p in rec.failed)
            + ". Their postings are compared again the next time they load.")
    return "\n".join(lines)


def research_sources(cfg: Config, row: Mapping[str, Any], h: dict) -> str:
    """The report's Sources section for one ``research_rules`` result file,
    read from the file (never from the agent's report)."""
    try:
        res = _research_check(cfg, Path(row["folder"]), h)
    except PostHookFailed as exc:
        return f"### Sources\n\nNo sources to list: {exc}."
    return research.sources_section(res, h["file"])


def required_section(row: Mapping[str, Any], h: dict) -> str:
    """The report's copy of a ``require_section`` section, read from the
    output file by Nightshift (never from the agent's report)."""
    title = f"### {h['heading']}"
    try:
        body = _section_of(Path(row["folder"]), h)
    except PostHookFailed as exc:
        return f"{title}\n\nNot copied: {exc}."
    return f"{title}\n\nFrom `{h['file']}`:\n\n{body}"


def course_section(cfg: Config, course_id: str) -> str:
    """The report's Course AI policy section: which course's policy the
    agent ran under (the policy itself is in config)."""
    title = "### Course AI policy"
    c = school.courses(cfg).get(course_id)
    if c is None or c.policy is None:
        return (f"{title}\n\nCourse {course_id} has no AI policy in config now "
                f"(`school.courses.{course_id}.policy`); no Attempt starts without one.")
    limits = f", with {len(c.limits)} hard limit{'s' if len(c.limits) != 1 else ''}" if (
        c.limits) else ""
    return (f"{title}\n\n{c.label}: its AI policy (`school.courses.{c.id}` in config"
            f"{limits}) headed the agent protocol of every Attempt, ahead of everything else.")


def courses_section(cfg: Config) -> str:
    """The report's Course AI policies section of a ``courses: all`` Task."""
    found = school.courses(cfg).values()
    ok = [c for c in found if c.policy is not None and c.ai_allowed]
    none = [c.label + (" (no policy)" if c.policy is None else " (no AI use)")
            for c in found if c not in ok]
    lines = ["### Course AI policies", ""]
    if not ok:
        lines.append("No course has a usable AI policy in config (`school.courses`), so the "
                     "agent was told to list due dates only.")
    else:
        lines.append("Each course's AI policy (`school.courses` in config) headed the agent "
                     "protocol of every Attempt: " + ", ".join(
                         c.label + (f" ({len(c.limits)} hard limit"
                                    f"{'s' if len(c.limits) != 1 else ''})" if c.limits else "")
                         for c in ok) + ".")
    if none:
        lines.append("Due dates only, no study work: " + ", ".join(none) + ".")
    return "\n".join(lines)


def calendar_section(cfg: Config, row: Mapping[str, Any]) -> str:
    """The report's School calendar section: what Nightshift fetched (feeds
    by name, never their URLs) and the due dates per course, as counts."""
    title = "### School calendar"
    snap = ical.load(calendar_record(cfg, row["id"]))
    if snap is None:
        return f"{title}\n\nThe calendar was not fetched (see `logs/nightshift.log`)."
    tz = ZoneInfo(cfg.timezone)
    feeds = ", ".join(f"{f.label}: {f.events} event{'s' if f.events != 1 else ''}"
                      if f.configured else f"{f.label}: not configured" for f in snap.feeds)
    names = {c.id: c.label for c in school.courses(cfg).values()}
    per: dict[str, int] = {}
    for e in snap.events:
        if e.due and e.course:
            per[names.get(e.course, e.course)] = per.get(names.get(e.course, e.course), 0) + 1
    loose = sum(1 for e in snap.events if e.course is None)
    lines = [title, "",
             f"Fetched by Nightshift at {snap.fetched_at.astimezone(tz):%Y-%m-%d %H:%M}, covering "
             f"{snap.start.astimezone(tz):%b %d} to {snap.end.astimezone(tz):%b %d} ({feeds}).",
             "Due dates per course: " + (", ".join(f"{k} {v}" for k, v in per.items())
                                         or "none") + ".",
             f"{loose} event{'s' if loose != 1 else ''} named no configured course."]
    if snap.notes:
        lines.append("Not fully read: " + "; ".join(snap.notes) + ".")
    return "\n".join(lines)


def report_appendix(cfg: Config, row: Mapping[str, Any]) -> str | None:
    """The Nightshift-written part of a template Task's ``report.md``: each
    ``report_diffs`` item as a unified diff of an output against the pristine
    copy of an input file (resume-tailor: ``output/resume.tex`` against
    ``master.tex``). None when the template has none. Never raises."""
    if not row["template"]:
        return None
    try:
        tpl = load(cfg, row["template"])
    except TemplateError:
        return None
    parts = []
    if "course" in row.keys() and row["course"]:
        parts.append(course_section(cfg, row["course"]))
    if tpl.courses == "all":
        parts.append(courses_section(cfg))
    if any(h["hook"] == "school_calendar" for h in tpl.pre_hooks):
        parts.append(calendar_section(cfg, row))
    if any(h["hook"] == "merge_tracker" for h in tpl.post_hooks):
        parts.append(new_postings(cfg, row))
    if any(h["hook"] == "snapshot_pages" for h in tpl.post_hooks):
        parts.append(snapshot_section(cfg, row, tpl.name))
    for h in tpl.post_hooks:
        if h["hook"] == "research_rules":
            parts.append(research_sources(cfg, row, h))
        if h["hook"] == "require_section":
            parts.append(required_section(row, h))
    if not tpl.report_diffs:
        return "\n\n".join(parts) or None
    folder = Path(row["folder"])
    for d in tpl.report_diffs:
        against = Path(d["against"]).name
        title = f"### Diff: {d['file']} against {against}"
        original = pristine_dir(cfg, row["id"]) / d["against"]
        new = folder / d["file"]
        if not original.is_file():
            parts.append(f"{title}\n\nThe pristine copy of {d['against']} is gone.")
            continue
        if new.is_symlink() or not new.is_file():
            parts.append(f"{title}\n\n{d['file']} was not written.")
            continue
        try:
            latex.inside(folder, new)
            diff = trace.unified_diff(original.read_text(errors="replace"),
                                      new.read_text(errors="replace"),
                                      f"a/{against}", f"b/{d['file']}")
        except (OSError, latex.LatexError) as exc:
            parts.append(f"{title}\n\nCould not diff it: {exc}")
            continue
        if not diff:
            parts.append(f"{title}\n\n{d['file']} is identical to {against}.")
            continue
        lines = diff.splitlines()
        note = ""
        if len(lines) > REPORT_DIFF_MAX_LINES:
            note = (f"\n\n_(cut at {REPORT_DIFF_MAX_LINES} of {len(lines)} lines; run "
                    f"`diff -u {d['against']} {d['file']}` against the Task folder for the rest)_")
            lines = lines[:REPORT_DIFF_MAX_LINES]
        added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        parts.append(f"{title}\n\n{added} line(s) added, {removed} removed.\n\n"
                     + trace.fenced("\n".join(lines), "diff") + note)
    return "\n\n".join(parts)
