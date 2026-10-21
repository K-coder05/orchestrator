"""Config loader (§1.3, §3.1 item 2).

User config lives in ``~/.config/nightshift/config.yaml``; secrets live in
``~/.config/nightshift/.env`` (mode 0600) or the macOS Keychain. Every value
has a default in ``defaults.yaml`` (identical to the repo's
``config.example.yaml``), so placeholder facts are never hard-coded in Python.

The config directory can be moved with ``NIGHTSHIFT_CONFIG_DIR`` (used by
tests and dry runs); the Task data root with ``paths.agents_root``.
"""

from __future__ import annotations

import copy
import os
import re
import stat
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from .host import Host, MacHost

CONFIG_DIR_ENV = "NIGHTSHIFT_CONFIG_DIR"
DEFAULT_CONFIG_DIR = "~/.config/nightshift"
CONFIG_FILENAME = "config.yaml"
ENV_FILENAME = ".env"


class ConfigError(Exception):
    """Raised for unreadable or invalid config. Messages never contain secrets."""


def load_defaults() -> dict[str, Any]:
    text = resources.files("nightshift").joinpath("defaults.yaml").read_text()
    return yaml.safe_load(text) or {}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def resolve_config_dir(config_dir: str | os.PathLike[str] | None = None) -> Path:
    raw = config_dir or os.environ.get(CONFIG_DIR_ENV) or DEFAULT_CONFIG_DIR
    return Path(raw).expanduser()


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """Parse a minimal ``.env``: ``KEY=value`` lines, ``#`` comments,
    optional ``export`` prefix and optional matching quotes."""
    values: dict[str, str] = {}
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            # Report the line number only: the line itself may be a secret.
            raise ConfigError(f".env line {lineno} is not KEY=value")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            raise ConfigError(f".env line {lineno} has an empty key")
        values[key] = value
    return values


class Secrets:
    """Secret lookup: ``.env`` first, then the Keychain.

    Values are never included in ``repr``/``str``. Every value that has been
    loaded is registered with :meth:`redact`, which Nightshift applies to
    anything it writes to logs or reports.
    """

    def __init__(
        self,
        env_values: dict[str, str] | None = None,
        *,
        host: Host | None = None,
        keychain_service: str = "nightshift",
    ):
        self._env = dict(env_values or {})
        self._host = host
        self._service = keychain_service
        self._cache: dict[str, str | None] = {}

    @classmethod
    def from_env_file(
        cls, path: Path, *, host: Host | None = None, keychain_service: str = "nightshift"
    ) -> "Secrets":
        env: dict[str, str] = {}
        if path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o077:
                raise ConfigError(
                    f"{path} must be mode 0600 (it is {mode:04o}); run: chmod 600 {path}"
                )
            env = parse_env_file(path.read_text())
        return cls(env, host=host, keychain_service=keychain_service)

    def get(self, name: str) -> str | None:
        if name in self._env:
            return self._env[name]
        if name not in self._cache:
            self._cache[name] = (
                self._host.keychain_get(self._service, name) if self._host else None
            )
        return self._cache[name]

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise ConfigError(
                f"secret {name!r} not found in .env or Keychain service {self._service!r}"
            )
        return value

    def known_values(self) -> list[str]:
        vals = list(self._env.values()) + [v for v in self._cache.values() if v]
        return [v for v in vals if v]

    def redact(self, text: str) -> str:
        # Longest first so a secret containing another is fully masked.
        for value in sorted(set(self.known_values()), key=len, reverse=True):
            if len(value) >= 4:
                text = text.replace(value, "[REDACTED]")
        return text

    def __repr__(self) -> str:
        names = sorted(set(self._env) | {k for k, v in self._cache.items() if v})
        return f"Secrets(names={names})"

    __str__ = __repr__


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


@dataclass
class Config:
    data: dict[str, Any]
    config_dir: Path
    secrets: Secrets = field(repr=False)
    source: Path | None = None

    # -- paths ---------------------------------------------------------------
    @property
    def agents_root(self) -> Path:
        return Path(str(self.data["paths"]["agents_root"])).expanduser()

    @property
    def tasks_dir(self) -> Path:
        return self.agents_root / "tasks"

    @property
    def recurring_dir(self) -> Path:
        """Occurrence folders: ``recurring/<schedule-name>/YYYY-MM-DD/`` (§1.3)."""
        return self.agents_root / "recurring"

    @property
    def data_dir(self) -> Path:
        return self.agents_root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nightshift.db"

    @property
    def tracker_path(self) -> Path:
        """The Tracker, ``applications.csv`` (§1.7): ``tracker.path``, else
        ``<agents_root>/data/applications.csv``."""
        custom = (self.data.get("tracker") or {}).get("path")
        return Path(str(custom)).expanduser() if custom else self.data_dir / "applications.csv"

    @property
    def env_path(self) -> Path:
        return self.config_dir / ENV_FILENAME

    # -- values --------------------------------------------------------------
    @property
    def timezone(self) -> str:
        return str(self.data["timezone"])

    def agent(self, name: str) -> dict[str, Any]:
        agents = self.data.get("agents") or {}
        if name not in agents:
            raise ConfigError(f"unknown agent {name!r}; configured: {sorted(agents)}")
        return agents[name]

    def default(self, key: str) -> Any:
        return self.data["defaults"][key]

    def planning(self, key: str) -> Any:
        return (self.data.get("planning") or {}).get(key)

    @property
    def task_types(self) -> list[str]:
        return list(self.data.get("task_types") or {})

    def internet_default(self, task_type: str) -> str:
        types = self.data.get("task_types") or {}
        entry = types.get(task_type) or {}
        # Unknown Task types are conservative: internet off.
        return str(entry.get("internet", "off"))

    def long_default(self, task_type: str) -> bool:
        """Whether Tasks of this type are long Tasks (§2.3); unknown: no."""
        entry = (self.data.get("task_types") or {}).get(task_type) or {}
        return bool(entry.get("long", False))

    def max_runtime_default(self, task_type: str) -> int:
        """Runtime cap in minutes for one Attempt of this Task type (§2.7):
        ``task_types.<type>.max_runtime_minutes``, else
        ``defaults.max_runtime_minutes``."""
        entry = (self.data.get("task_types") or {}).get(task_type) or {}
        return int(entry.get("max_runtime_minutes") or self.default("max_runtime_minutes"))

    def failover(self, task_type: str) -> tuple[list[str], str]:
        """``task_types.<type>.failover``: the agents Tasks of this type may
        run on, in order of preference, and when they fail over (``always``
        or ``deadline``). No entry: ``([], "always")`` (never fails over)."""
        entry = ((self.data.get("task_types") or {}).get(task_type) or {}).get("failover") or {}
        return [str(a) for a in entry.get("agents") or []], str(entry.get("when", "always"))

    @property
    def max_retries(self) -> int:
        return int((self.data.get("retries") or {}).get("max_retries", 2))

    @property
    def retry_wait_minutes(self) -> list[float]:
        return [float(m) for m in (self.data.get("retries") or {}).get("wait_minutes", [10, 30])]

    USAGE_DEFAULTS = {
        "resume_session_max_hours": 24,
        "reapproval_after_hours": 72,
        "probe_interval_minutes": 30,
        "step_grace_seconds": 120,
        "simulated_reset_minutes": 5,
        "probe_timeout_seconds": 120,
        "limit_exit_wait_seconds": 10,
    }

    def windows(self, agent: str) -> dict[str, float]:
        """``usage.windows.<agent>``: the agent's usage Windows and their
        length in hours (§1.5). ``orch usage`` always lists these."""
        table = ((self.data.get("usage") or {}).get("windows") or {}).get(agent) or {}
        return {str(k): float(v) for k, v in table.items()}

    def window_hours(self, agent: str, window: str | None) -> float:
        """Length of a Window. Unknown names: weekly-looking ones (``seven_day_*``)
        are 168h; anything else, and a hit that names no Window, counts as the
        agent's shortest Window (less recorded usage: a conservative Baseline)."""
        table = self.windows(agent)
        if window and window in table:
            return table[window]
        if window and window.startswith("seven_day"):
            return 168.0
        return min(table.values()) if table else 5.0

    def usage(self, key: str) -> float:
        """A ``usage:`` setting (pauses and resume, §2.6)."""
        value = (self.data.get("usage") or {}).get(key)
        return float(self.USAGE_DEFAULTS[key] if value is None else value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate(data: dict[str, Any]) -> None:
    for section in ("paths", "agents", "defaults"):
        if not isinstance(data.get(section), dict):
            raise ConfigError(f"config section {section!r} must be a mapping")
    if data["defaults"].get("priority") not in ("urgent", "normal", "background"):
        raise ConfigError("defaults.priority must be urgent, normal or background")
    if data["defaults"].get("agent") not in data["agents"]:
        raise ConfigError("defaults.agent must name a configured agent")
    for name, entry in (data.get("task_types") or {}).items():
        if (entry or {}).get("internet", "off") not in ("on", "off"):
            raise ConfigError(f"task_types.{name}.internet must be 'on' or 'off'")
        if not isinstance((entry or {}).get("long", False), bool):
            raise ConfigError(f"task_types.{name}.long must be true or false")
        fo = (entry or {}).get("failover")
        if fo is not None:
            if (not isinstance(fo, dict) or not isinstance(fo.get("agents"), list)
                    or not all(isinstance(a, str) for a in fo["agents"])):
                raise ConfigError(f"task_types.{name}.failover must be "
                                  "{agents: [agent, ...], when: always|deadline}")
            unknown = [a for a in fo["agents"] if a not in data["agents"]]
            if unknown:
                raise ConfigError(f"task_types.{name}.failover names unconfigured agents {unknown}")
            if fo.get("when", "always") not in ("always", "deadline"):
                raise ConfigError(f"task_types.{name}.failover.when must be always or deadline")
        cap = (entry or {}).get("max_runtime_minutes")
        if cap is not None and not _positive_int(cap):
            raise ConfigError(f"task_types.{name}.max_runtime_minutes must be a positive integer")
    if not _positive_int(data["defaults"].get("max_runtime_minutes")):
        raise ConfigError("defaults.max_runtime_minutes must be a positive integer")
    retries = data.get("retries") or {}
    if not isinstance(retries, dict):
        raise ConfigError("config section 'retries' must be a mapping")
    max_retries = retries.get("max_retries", 2)
    if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
        raise ConfigError("retries.max_retries must be an integer >= 0")
    waits = retries.get("wait_minutes", [10, 30])
    if (
        not isinstance(waits, list)
        or not waits
        or any(isinstance(w, bool) or not isinstance(w, (int, float)) or w < 0 for w in waits)
    ):
        raise ConfigError("retries.wait_minutes must be a non-empty list of minutes >= 0")
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        raise ConfigError("config section 'usage' must be a mapping")
    for key in Config.USAGE_DEFAULTS:
        value = usage.get(key, Config.USAGE_DEFAULTS[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"usage.{key} must be a number >= 0")
    windows = usage.get("windows") or {}
    if not isinstance(windows, dict) or not all(
        isinstance(t, dict) and all(
            isinstance(h, (int, float)) and not isinstance(h, bool) and h > 0 for h in t.values())
        for t in windows.values()
    ):
        raise ConfigError("usage.windows must map agent -> {window name: hours > 0}")
    queue = data.get("queue") or {}
    if not isinstance(queue, dict):
        raise ConfigError("config section 'queue' must be a mapping")
    for key, default in (("long_window_start", "00:00"), ("long_window_end", "07:00")):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(queue.get(key, default))):
            raise ConfigError(f"queue.{key} must be HH:MM (24-hour), e.g. 07:00")
    hours = queue.get("deadline_urgent_hours", 24)
    if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
        raise ConfigError("queue.deadline_urgent_hours must be a number >= 0")
    try:
        ZoneInfo(str(data.get("timezone")))
    except (ValueError, ZoneInfoNotFoundError):
        raise ConfigError(f"timezone {data.get('timezone')!r} is not a known IANA zone") from None
    perms = data.get("permissions") or {}
    if not isinstance(perms, dict):
        raise ConfigError("config section 'permissions' must be a mapping")
    for key in ("read_only_dirs", "no_access_dirs", "tool_dirs", "denied_domains", "denied_commands"):
        value = perms.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"permissions.{key} must be a list of strings")
    planning = data.get("planning") or {}
    if not isinstance(planning, dict):
        raise ConfigError("config section 'planning' must be a mapping")
    if planning.get("agent") is not None and planning["agent"] not in data["agents"]:
        raise ConfigError("planning.agent must name a configured agent (or be null)")
    for key, default in (("approval_threshold_minutes", 30), ("timeout_seconds", 180)):
        value = planning.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"planning.{key} must be a positive number")
    templates = data.get("templates") or {}
    if not isinstance(templates, dict):
        raise ConfigError("config section 'templates' must be a mapping")
    tex = templates.get("latex") or {}
    if not isinstance(tex, dict):
        raise ConfigError("templates.latex must be a mapping")
    if tex.get("engine", "pdflatex") not in ("pdflatex", "xelatex", "lualatex"):
        raise ConfigError("templates.latex.engine must be pdflatex, xelatex or lualatex")
    secs = tex.get("timeout_seconds", 120)
    if isinstance(secs, bool) or not isinstance(secs, (int, float)) or secs <= 0:
        raise ConfigError("templates.latex.timeout_seconds must be a positive number")
    dirs = tex.get("readable_dirs") or []
    if not isinstance(dirs, list) or not all(isinstance(d, str) for d in dirs):
        raise ConfigError("templates.latex.readable_dirs must be a list of paths")
    poll = (data.get("daemon") or {}).get("poll_seconds", 5)
    if isinstance(poll, bool) or not isinstance(poll, (int, float)) or poll <= 0:
        raise ConfigError("daemon.poll_seconds must be a positive number")
    wake = data.get("wake") or {}
    days = str(wake.get("days", "MTWRFSU"))
    if not days or any(c not in "MTWRFSU" for c in days):
        raise ConfigError("wake.days must be letters from MTWRFSU, e.g. MTWRF")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d:[0-5]\d", str(wake.get("time", "01:55:00"))):
        raise ConfigError("wake.time must be HH:MM:SS (24-hour), e.g. 01:55:00")


def load_config(
    config_dir: str | os.PathLike[str] | None = None,
    *,
    host: Host | None = None,
) -> Config:
    """Load config: packaged defaults, overlaid with the user's config.yaml."""
    cdir = resolve_config_dir(config_dir)
    path = cdir / CONFIG_FILENAME
    data = load_defaults()
    source: Path | None = None
    if path.exists():
        try:
            user = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from None
        if not isinstance(user, dict):
            raise ConfigError(f"{path} must contain a mapping at the top level")
        if "secrets" in user and any(k != "keychain_service" for k in user["secrets"] or {}):
            raise ConfigError(
                "secrets do not belong in config.yaml; put them in .env or the Keychain"
            )
        data = deep_merge(data, user)
        source = path
    _validate(data)
    service = str((data.get("secrets") or {}).get("keychain_service", "nightshift"))
    secrets = Secrets.from_env_file(
        cdir / ENV_FILENAME, host=host if host is not None else MacHost(), keychain_service=service
    )
    return Config(data=data, config_dir=cdir, secrets=secrets, source=source)
