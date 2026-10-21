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
    def data_dir(self) -> Path:
        return self.agents_root / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "nightshift.db"

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

    def internet_default(self, task_type: str) -> str:
        types = self.data.get("task_types") or {}
        entry = types.get(task_type) or {}
        # Unknown Task types are conservative: internet off.
        return str(entry.get("internet", "off"))


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
