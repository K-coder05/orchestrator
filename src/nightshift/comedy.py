"""YouTube comedy material (§0): the ``~/Documents/comedy/`` folder agents
may read but never write (§1.4).

``comedy.dir`` in config is that folder. A comedy template (punch-up) takes
its material with an input ``from: comedy_path``: the one file or folder
under ``comedy.dir`` the request names, or, when it names none, the whole
folder (the agent finds the script or idea the request describes there).

Unreleased comedy material must never be posted or shared outside the model
providers, Telegram included (§1.6). Telegram, the digest and alerts show a
comedy Task by id and state only (``alerts.STATUS_ONLY_TYPES``). The
refusals here may reach Telegram (``add punch-up ...``), so they never echo
the path the request named: a file name can carry a bit's premise.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import ConfigError
from .school import paths_in_request  # the same path rule as a slides folder

if TYPE_CHECKING:
    from .config import Config


class ComedyError(Exception):
    """A request names no usable comedy material."""


def validate(data: dict[str, Any]) -> None:
    """Check the ``comedy`` config section (called by the config loader)."""
    comedy = data.get("comedy") or {}
    if not isinstance(comedy, dict):
        raise ConfigError("config section 'comedy' must be a mapping")
    unknown = sorted(set(comedy) - {"dir"})
    if unknown:
        raise ConfigError(f"comedy has unknown keys {', '.join(unknown)} (known: dir)")
    folder = comedy.get("dir")
    if not isinstance(folder, str) or not folder.strip():
        raise ConfigError("comedy.dir must be the path of your comedy folder "
                          "(e.g. ~/Documents/comedy)")


def _configured(cfg: Config) -> str:
    return str((cfg.data.get("comedy") or {}).get("dir") or "~/Documents/comedy")


def comedy_dir(cfg: Config) -> Path:
    return Path(_configured(cfg)).expanduser()


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _has_files(folder: Path) -> bool:
    for _dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if any(not f.startswith(".") for f in filenames):
            return True
    return False


def material(cfg: Config, request: str) -> Path:
    """The comedy material ``request`` names, resolved: the one file or
    folder path in it, which must exist inside ``comedy.dir``; or
    ``comedy.dir`` itself when it names no path. The folder must be
    read-only to agents (``permissions.read_only_dirs``)."""
    where = _configured(cfg)
    root = comedy_dir(cfg).resolve()
    readable = [Path(str(d)).expanduser().resolve()
                for d in (cfg.data.get("permissions") or {}).get("read_only_dirs") or []]
    if not any(_inside(root, d) for d in readable):
        raise ComedyError(f"your comedy folder {where} is not in permissions.read_only_dirs, "
                          "so the agent could not read it")
    if not root.is_dir() or not _has_files(root):
        raise ComedyError(f"your comedy folder {where} ({root}) has no files in it; put the "
                          "script or idea there first")
    paths = paths_in_request(request)
    if len(paths) > 1:
        raise ComedyError(f"the request names {len(paths)} paths; name at most one script "
                          f"or idea (a file or folder under {where})")
    if not paths:
        return root
    path = Path(paths[0]).expanduser().resolve()  # symlinks too: a link can't lead out
    if not _inside(path, root):
        raise ComedyError(f"the path in the request is outside your comedy folder {where} "
                          f"({root}); Nightshift reads comedy material only from inside it "
                          "(§1.4)")
    if path.is_file():
        return path
    if not path.is_dir():
        raise ComedyError(f"the path in the request does not exist under {where}")
    if not _has_files(path):
        raise ComedyError(f"the folder in the request has no files in it (under {where})")
    return path
