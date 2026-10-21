"""The host seam (ADR 0001).

Every OS-specific call goes through a ``Host``. ``MacHost`` is the only real
implementation; ``FakeHost`` keeps core logic testable on any OS.

Probes are read-only. The only state-changing calls are the daemon's own:
installing/removing its LaunchAgent (``orch daemon install|uninstall``) and
holding ``caffeinate`` while a Task runs. ``run_confined`` runs Nightshift's
own post-hook tools (LaTeX) under a Seatbelt profile. System settings are never changed:
``pmset`` in particular is never run (§1.9); ``orch doctor`` only prints it.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ConfinedRun:
    """The result of ``Host.run_confined``."""

    exit_code: int | None  # None: could not start, or timed out
    output: str  # stdout and stderr together
    timed_out: bool = False


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

    def launch_agents_dir(self) -> Path:
        """Where per-user LaunchAgent plists live (``~/Library/LaunchAgents``)."""
        ...

    def launchd_install(self, label: str, plist: bytes) -> str | None:
        """Write ``<label>.plist`` and load it (replacing a loaded copy).
        Returns None on success, else an error message."""
        ...

    def launchd_uninstall(self, label: str) -> str | None:
        """Unload the agent if loaded and delete its plist. None on success."""
        ...

    def protected_folders(self) -> list[Path]:
        """Folders macOS privacy protection (TCC) keeps from a launchd agent
        unless its program was granted access: a daemon whose Python,
        source or data lives in one hangs or gets ``Operation not
        permitted`` (docs/decisions.md #3, real launchd)."""
        ...

    def hold_awake(self) -> None:
        """Start holding the machine awake (idempotent)."""
        ...

    def release_awake(self) -> None:
        """Stop holding the machine awake (idempotent)."""
        ...

    def awake_held(self) -> bool:
        """Whether this process is currently holding the machine awake."""
        ...

    def awake_seconds(self) -> float:
        """A clock that does not advance while the machine sleeps; compared
        with wall time it tells the daemon the machine just woke up."""
        ...

    def run_confined(
        self,
        argv: list[str],
        *,
        cwd: Path,
        writable: list[Path],
        readable: list[Path],
        private_roots: list[Path],
        env: dict[str, str] | None = None,
        timeout: float = 120,
    ) -> ConfinedRun:
        """Run a Nightshift-side tool (e.g. the LaTeX compile of a post-hook)
        on agent-written input: no network, no reads under ``private_roots``
        except ``readable`` and ``writable``, writes only to ``writable``
        (and the system temp dir)."""
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
    ``system_profiler``, ``sw_vers`` and ``launchctl print``; the daemon's
    LaunchAgent via ``launchctl bootstrap|bootout``; wakefulness via
    ``caffeinate``."""

    def __init__(self) -> None:
        self._caffeinate: subprocess.Popen[bytes] | None = None

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

    def launch_agents_dir(self) -> Path:
        return Path("~/Library/LaunchAgents").expanduser()

    def protected_folders(self) -> list[Path]:
        home = Path.home()
        return [home / "Desktop", home / "Documents", home / "Downloads",
                home / "Library" / "Mobile Documents", Path("/Volumes")]

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def launchd_install(self, label: str, plist: bytes) -> str | None:
        path = self.launch_agents_dir() / f"{label}.plist"
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.launchd_status(label).loaded:
            _run(["launchctl", "bootout", f"{self._domain()}/{label}"], timeout=30)
        path.write_bytes(plist)
        proc = _run(["launchctl", "bootstrap", self._domain(), str(path)], timeout=30)
        if proc is None or proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip() if proc else "launchctl did not run"
            return f"launchctl bootstrap failed: {detail}"
        return None

    def launchd_uninstall(self, label: str) -> str | None:
        path = self.launch_agents_dir() / f"{label}.plist"
        if self.launchd_status(label).loaded:
            proc = _run(["launchctl", "bootout", f"{self._domain()}/{label}"], timeout=30)
            if proc is None or proc.returncode != 0:
                detail = (proc.stderr or proc.stdout).strip() if proc else "launchctl did not run"
                return f"launchctl bootout failed: {detail}"
        path.unlink(missing_ok=True)
        return None

    def hold_awake(self) -> None:
        if self._caffeinate is not None and self._caffeinate.poll() is None:
            return
        # -i: no idle sleep; -s: no system sleep on AC power; -w: exit with
        # this process, so a crashed daemon never leaves the Mac held awake.
        self._caffeinate = subprocess.Popen(
            ["caffeinate", "-i", "-s", "-w", str(os.getpid())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def release_awake(self) -> None:
        proc, self._caffeinate = self._caffeinate, None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def awake_held(self) -> bool:
        return self._caffeinate is not None and self._caffeinate.poll() is None

    def awake_seconds(self) -> float:
        # CLOCK_UPTIME_RAW (mach_absolute_time) stops while the Mac sleeps,
        # unlike wall time; Python doesn't promise that for time.monotonic().
        return time.clock_gettime(time.CLOCK_UPTIME_RAW)

    def run_confined(self, argv, *, cwd, writable, readable, private_roots, env=None,
                     timeout=120) -> ConfinedRun:
        # macOS Seatbelt through sandbox-exec (deprecated but still shipped,
        # and what Claude Code's own sandbox uses underneath).
        profile = sandbox_profile(writable=writable, readable=readable,
                                  private_roots=private_roots)
        return _run_capture(["/usr/bin/sandbox-exec", "-p", profile, *argv], cwd=cwd, env=env,
                            timeout=timeout)

    def launchd_status(self, label: str) -> LaunchdStatus:
        plist = self.launch_agents_dir() / f"{label}.plist"
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
    #: Installed plists by label (``launchd_install`` writes here).
    plists: dict[str, bytes] = field(default_factory=dict)
    #: Error message the next launchd install/uninstall returns, if any.
    launchd_error: str | None = None
    #: What ``protected_folders`` returns (none by default, so tests running
    #: from a repo under ~/Desktop can still install).
    protected: list[Path] = field(default_factory=list)
    awake: bool = False
    #: ``"hold"``/``"release"`` in call order (only state changes are recorded).
    awake_log: list[str] = field(default_factory=list)
    #: What ``awake_seconds`` returns (None: real monotonic time). Tests
    #: simulate sleep by moving their fake clock without moving this.
    awake_clock: float | None = None
    #: ``run_confined`` calls (kwargs plus argv); FakeHost runs them unconfined.
    confined_runs: list[dict] = field(default_factory=list)

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
        return LaunchdStatus(label, self.launch_agents_dir() / f"{label}.plist", exists, loaded, pid)

    def launch_agents_dir(self) -> Path:
        return Path("/fake/LaunchAgents")

    def protected_folders(self) -> list[Path]:
        return list(self.protected)

    def launchd_install(self, label: str, plist: bytes) -> str | None:
        if self.launchd_error:
            return self.launchd_error
        self.plists[label] = plist
        self.launchd[label] = (True, True, 4242)
        return None

    def launchd_uninstall(self, label: str) -> str | None:
        if self.launchd_error:
            return self.launchd_error
        self.plists.pop(label, None)
        self.launchd.pop(label, None)
        return None

    def hold_awake(self) -> None:
        if not self.awake:
            self.awake = True
            self.awake_log.append("hold")

    def release_awake(self) -> None:
        if self.awake:
            self.awake = False
            self.awake_log.append("release")

    def awake_held(self) -> bool:
        return self.awake

    def awake_seconds(self) -> float:
        return time.monotonic() if self.awake_clock is None else self.awake_clock

    def run_confined(self, argv, *, cwd, writable, readable, private_roots, env=None,
                     timeout=120) -> ConfinedRun:
        self.confined_runs.append({"argv": list(argv), "cwd": Path(cwd), "writable": list(writable),
                                   "readable": list(readable),
                                   "private_roots": list(private_roots)})
        return _run_capture(list(argv), cwd=cwd, env=env, timeout=timeout)


def _sb_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_profile(*, writable: list[Path], readable: list[Path],
                    private_roots: list[Path]) -> str:
    """A Seatbelt profile: no network; reads under ``private_roots`` only in
    ``readable``/``writable``; writes only to ``writable`` and the per-user temp dir (/private/var/folders).
    Paths are resolved, since Seatbelt matches real paths (/tmp is
    /private/tmp)."""
    def real(paths):
        return [Path(p).expanduser().resolve() for p in paths]

    lines = ["(version 1)", "(allow default)", "(deny network*)"]
    for root in real(private_roots):
        lines.append(f"(deny file-read* (subpath {_sb_quote(root)}))")
    allowed = real(readable) + real(writable)
    if allowed:
        lines.append("(allow file-read* " + " ".join(f"(subpath {_sb_quote(p)})" for p in allowed) + ")")
    lines.append("(deny file-write*)")
    targets = [f"(subpath {_sb_quote(p)})" for p in real(writable)]
    targets += ['(subpath "/private/var/folders")',
                '(literal "/dev/null")', '(literal "/dev/tty")']
    lines.append("(allow file-write* " + " ".join(targets) + ")")
    return "\n".join(lines) + "\n"


def _run_capture(argv: list[str], *, cwd: Path, env: dict[str, str] | None,
                 timeout: float) -> ConfinedRun:
    try:
        proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return ConfinedRun(exit_code=None, output=out, timed_out=True)
    except OSError as exc:
        return ConfinedRun(exit_code=None, output=f"could not start {argv[0]}: {exc}")
    return ConfinedRun(exit_code=proc.returncode, output=proc.stdout)
