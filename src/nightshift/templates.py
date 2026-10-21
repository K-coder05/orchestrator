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
  first http(s) URL in it).
- each of ``files``: a file named by a config key, copied into the Task
  folder (``to:``) when the Task is created; the variable is that relative
  path. A pristine copy is kept in ``<agents_root>/data/inputs/<task-id>/``,
  where the agent can't reach it, and the ``inputs_unchanged`` hook checks
  the agent left its copy alone.

``deadline: {from_output: <file>, field: <key>}``: after every Attempt,
Nightshift reads that JSON file the agent wrote and, if the Task has no
deadline yet (``--deadline`` always wins), sets it from the field (§2.3).

Post-hooks run after an Attempt the agent finished ``done``/``partial``; a
failing one is a Failed Attempt (§2.7). Hook names map to ``HOOKS``.

``report_diffs: [{file: <output>, against: <input file>}]``: Nightshift adds
a unified diff of each output against the pristine input to ``report.md``.
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

from . import latex, trace
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
OPTIONAL = ("name", "description", "inputs", "files", "deadline", "post_hooks", "report_diffs")
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
    deadline: dict | None = None
    post_hooks: list[dict] = field(default_factory=list)
    report_diffs: list[dict] = field(default_factory=list)

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
        if not isinstance(spec, dict) or spec.get("from") != "url":
            raise TemplateError(f"template {name!r}: input {var!r} needs from: url")
    files = data.get("files") or {}
    if not isinstance(files, dict):
        raise TemplateError(f"template {name!r}: files must be a mapping")
    for var, spec in files.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("config"), str):
            raise TemplateError(f"template {name!r}: file {var!r} needs config: <dotted key>")
        _rel_path(name, f"file {var!r} to", spec.get("to"))
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

    known = {"request", *inputs, *files}
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
        deadline=deadline, post_hooks=list(hooks), report_diffs=list(diffs),
    )


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


def config_value(cfg: Config, dotted: str) -> Any:
    node: Any = cfg.data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


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
    for var, spec in tpl.inputs.items():
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
    prompt = _VAR.sub(lambda m: values[m.group(1)], tpl.prompt)
    return Instance(template=tpl, request=request, prompt=prompt, values=values, files=files)


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


HOOKS: dict[str, HookKind] = {
    "require_outputs": HookKind(_validate_require, _build_require),
    "compile_latex": HookKind(_validate_compile, _build_compile),
    "inputs_unchanged": HookKind(_validate_nothing, _build_unchanged),
    "trace_changes": HookKind(_validate_trace, _build_trace, file_keys=("master",)),
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
# Report diffs
# --------------------------------------------------------------------------

#: A longer diff is cut in the report (the files are in the Task folder).
REPORT_DIFF_MAX_LINES = 400


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
    if not tpl.report_diffs:
        return None
    folder = Path(row["folder"])
    parts = []
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
