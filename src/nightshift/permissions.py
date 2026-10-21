"""The per-Task access policy (§1.4, §1.6), independent of any agent.

``build_policy`` turns config plus one Task into a ``PermissionPolicy``; the
Task's adapter turns that into its agent's own permission, sandbox and
allowlist settings (``AgentAdapter.write_permissions``). What each limit
really enforces is documented in ``docs/safety.md``.
"""

from __future__ import annotations

from pathlib import Path

from .adapters.base import PermissionPolicy
from .config import Config, ConfigError


def _path(raw: str | Path) -> Path:
    # Resolved so macOS symlinks (/tmp -> /private/tmp) can't dodge a rule.
    return Path(str(raw)).expanduser().resolve()


def _paths(raw: list | None) -> list[Path]:
    out: list[Path] = []
    for p in raw or []:
        path = _path(p)
        if path not in out:
            out.append(path)
    return out


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def build_policy(cfg: Config, *, task_id: str, workspace: Path, internet: str) -> PermissionPolicy:
    """The access policy for one Task's Attempt.

    The Task folder is the only writable place; ``permissions.read_only_dirs``
    are readable; the data dir (``<agents_root>/data``), the configured
    no-access dirs and everything else are off limits.
    """
    perms = cfg.data.get("permissions") or {}
    ws = _path(workspace)
    data_dir = _path(cfg.data_dir)
    read_only = [p for p in _paths(perms.get("read_only_dirs")) if p != ws]
    # The data dir is never agent-accessible, whatever the config says (§1.4).
    no_access = _paths([data_dir, *(perms.get("no_access_dirs") or [])])

    for ro in [ws, *read_only]:
        for na in no_access:
            if _inside(ro, na) or _inside(na, ro):
                what = "the Task folder" if ro == ws else f"read-only dir {ro}"
                raise ConfigError(f"{what} overlaps no-access dir {na}; fix permissions in config")

    return PermissionPolicy(
        workspace=ws,
        internet=internet == "on",
        read_only_dirs=read_only,
        no_access_dirs=no_access,
        private_roots=_paths([Path.home(), cfg.agents_root]),
        tool_dirs=_paths(perms.get("tool_dirs")),
        denied_domains=[str(d).lower().strip() for d in perms.get("denied_domains") or [] if str(d).strip()],
        denied_commands=[str(c).strip() for c in perms.get("denied_commands") or [] if str(c).strip()],
        settings_dir=data_dir / "permissions" / task_id,
    )
