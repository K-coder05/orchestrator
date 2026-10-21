"""The host seam (ADR 0001).

Every OS-specific call goes through a ``Host``. ``MacHost`` is the only real
implementation; ``FakeHost`` keeps core logic testable on any OS.

All probes are read-only. Nothing here changes system settings: ``pmset`` in
particular is never run (§1.9); ``orch doctor`` only prints the command.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class LaunchdStatus:
    """What ``launchd`` knows about one LaunchAgent label."""

    label: str
    plist_path: Path
    plist_exists: bool
    loaded: bool
    pid: int | None = None


@dataclass(frozen=True)
class HardwareInfo:
    """Hardware facts read from the machine. ``None`` means unknown."""

    model_name: str | None = None
    model_id: str | None = None
    chip: str | None = None
    ram_bytes: int | None = None


class Host(Protocol):
    def keychain_get(self, service: str, account: str) -> str | None:
        """Return a generic-password secret from the Keychain, or None."""
        ...

    def hardware(self) -> HardwareInfo:
        """Model, chip and RAM of this machine."""
        ...

    def os_version(self) -> str | None:
        """The macOS product version, e.g. ``26.6.2``."""
        ...

    def free_disk_bytes(self, path: Path) -> int | None:
        """Free bytes on the volume holding ``path`` (or its nearest existing parent)."""
        ...

    def which(self, name: str) -> str | None:
        """Full path of an executable on PATH, or None."""
        ...

    def tool_version(self, argv: list[str]) -> str | None:
        """First non-empty output line of a version command, or None if it fails."""
        ...

    def python_version(self) -> str:
        """Version of the Python interpreter running Nightshift."""
        ...

    def python_package_version(self, name: str) -> str | None:
        """Installed version of a package in Nightshift's environment, or None."""
        ...

    def launchd_status(self, label: str) -> LaunchdStatus:
        """Whether a per-user LaunchAgent is installed and loaded."""
        ...


def _run(argv: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _existing_ancestor(path: Path) -> Path:
    p = path.expanduser()
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


class MacHost:
    """Real macOS host. Read-only probes via ``security``, ``sysctl``,
    ``system_profiler``, ``sw_vers`` and ``launchctl print``."""

    def keychain_get(self, service: str, account: str) -> str | None:
        proc = _run(["security", "find-generic-password", "-s", service, "-a", account, "-w"])
        if proc is None or proc.returncode != 0:
            return None
        value = proc.stdout.rstrip("\n")
        return value or None

    def _sysctl(self, name: str) -> str | None:
        proc = _run(["sysctl", "-n", name])
        if proc is None or proc.returncode != 0:
            return None
        return proc.stdout.strip() or None

    def hardware(self) -> HardwareInfo:
        model_name = chip = None
        proc = _run(["system_profiler", "SPHardwareDataType", "-json"], timeout=30)
        if proc is not None and proc.returncode == 0:
            try:
                items = json.loads(proc.stdout).get("SPHardwareDataType") or [{}]
                # Only the fields doctor needs; serial numbers and UUIDs are dropped.
                model_name = items[0].get("machine_name")
                chip = items[0].get("chip_type") or items[0].get("cpu_type")
            except (ValueError, AttributeError, IndexError):
                pass
        chip = chip or self._sysctl("machdep.cpu.brand_string")
        mem = self._sysctl("hw.memsize")
        return HardwareInfo(
            model_name=model_name,
            model_id=self._sysctl("hw.model"),
            chip=chip,
            ram_bytes=int(mem) if mem and mem.isdigit() else None,
        )

    def os_version(self) -> str | None:
        proc = _run(["sw_vers", "-productVersion"])
        if proc is not None and proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
        return platform.mac_ver()[0] or None

    def free_disk_bytes(self, path: Path) -> int | None:
        try:
            return shutil.disk_usage(_existing_ancestor(path)).free
        except OSError:
            return None

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def tool_version(self, argv: list[str]) -> str | None:
        proc = _run(argv)
        if proc is None:
            return None
        # Some tools (pdfinfo, latexmk) print their version to stderr.
        for line in (proc.stdout + "\n" + proc.stderr).splitlines():
            if line.strip():
                return line.strip()
        return None

    def python_version(self) -> str:
        return platform.python_version()

    def python_package_version(self, name: str) -> str | None:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    def launchd_status(self, label: str) -> LaunchdStatus:
        plist = Path("~/Library/LaunchAgents").expanduser() / f"{label}.plist"
        proc = _run(["launchctl", "print", f"gui/{os.getuid()}/{label}"])
        loaded = proc is not None and proc.returncode == 0
        pid = None
        if loaded:
            for line in proc.stdout.splitlines():
                key, _, value = line.strip().partition("=")
                if key.strip() == "pid" and value.strip().isdigit():
                    pid = int(value.strip())
                    break
        return LaunchdStatus(label, plist, plist.exists(), loaded, pid)


@dataclass
class FakeHost:
    """In-memory host for tests.

    ``tools`` maps an executable name to its version line; a name that is
    absent is not installed.
    """

    keychain: dict[tuple[str, str], str] = field(default_factory=dict)
    hw: HardwareInfo = field(default_factory=HardwareInfo)
    os_ver: str | None = None
    free_disk: int | None = None
    tools: dict[str, str] = field(default_factory=dict)
    python: str = "3.12.0"
    packages: dict[str, str] = field(default_factory=dict)
    launchd: dict[str, tuple[bool, bool, int | None]] = field(default_factory=dict)
    keychain_reads: list[tuple[str, str]] = field(default_factory=list)
    commands_run: list[list[str]] = field(default_factory=list)

    def keychain_get(self, service: str, account: str) -> str | None:
        self.keychain_reads.append((service, account))
        return self.keychain.get((service, account))

    def hardware(self) -> HardwareInfo:
        return self.hw

    def os_version(self) -> str | None:
        return self.os_ver

    def free_disk_bytes(self, path: Path) -> int | None:
        return self.free_disk

    def which(self, name: str) -> str | None:
        return f"/fake/bin/{name}" if name in self.tools else None

    def tool_version(self, argv: list[str]) -> str | None:
        self.commands_run.append(list(argv))
        return self.tools.get(Path(argv[0]).name)

    def python_version(self) -> str:
        return self.python

    def python_package_version(self, name: str) -> str | None:
        return self.packages.get(name)

    def launchd_status(self, label: str) -> LaunchdStatus:
        exists, loaded, pid = self.launchd.get(label, (False, False, None))
        return LaunchdStatus(label, Path(f"/fake/LaunchAgents/{label}.plist"), exists, loaded, pid)
