"""The background daemon (§3.1 item 5): queue runner, launchd plist, lock.

``launchd`` keeps the daemon alive (KeepAlive). Inside it the queue runner
takes queued Tasks one at a time, holding the machine awake through the host
seam only while a Task is ``running`` (§1.1). The scheduler joins it in a
later issue.

Exactly one Task at a time is guaranteed twice over: the runner is a single
serial loop, and only the process holding the daemon lock (an exclusive
``flock`` on ``<data>/daemon.lock``) may run Tasks at all.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

import nightshift

from .adapters import get_adapter
from .config import Config
from .db import Database
from .host import Host
from .runner import AttemptControl, fail_orphaned, fail_unstartable, run_attempt
from .timeutil import iso, now

LOCK_FILENAME = "daemon.lock"
STATE_FILENAME = "daemon.json"
LOG_FILENAME = "daemon.log"


def lock_path(cfg: Config) -> Path:
    return cfg.data_dir / LOCK_FILENAME


def state_path(cfg: Config) -> Path:
    return cfg.data_dir / STATE_FILENAME


def log_path(cfg: Config) -> Path:
    return cfg.data_dir / LOG_FILENAME


def label(cfg: Config) -> str:
    return str((cfg.data.get("daemon") or {}).get("label", "com.nightshift.daemon"))


def poll_seconds(cfg: Config) -> float:
    return float((cfg.data.get("daemon") or {}).get("poll_seconds", 5))


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------


class DaemonLock:
    """Exclusive, non-blocking ``flock``. The kernel drops it if the process
    dies, so a crashed daemon never leaves a stale lock."""

    def __init__(self, path: Path):
        self.path = path
        self._fh: IO[str] | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        self._fh = fh
        return True

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def daemon_running(cfg: Config) -> bool:
    """Whether some process holds the daemon lock (i.e. a daemon is up)."""
    if not lock_path(cfg).exists():
        return False
    probe = DaemonLock(lock_path(cfg))
    if probe.acquire():
        probe.release()
        return False
    return True


def read_state(cfg: Config) -> dict[str, Any] | None:
    try:
        return json.loads(state_path(cfg).read_text())
    except (OSError, ValueError):
        return None


# --------------------------------------------------------------------------
# launchd plist
# --------------------------------------------------------------------------


def build_plist(cfg: Config, *, python: str | None = None, path_env: str | None = None) -> bytes:
    """The LaunchAgent plist that runs ``orch daemon run`` under KeepAlive.

    ``PYTHONPATH`` points at Nightshift's source so the daemon starts even
    when macOS hides the editable-install ``.pth`` (docs/decisions.md).
    ``PATH`` is captured at install time: launchd's default PATH would not
    find agent CLIs such as ``claude`` in ``~/.local/bin``.
    """
    python = python or sys.executable
    src_dir = str(Path(nightshift.__file__).resolve().parent.parent)
    path_env = path_env if path_env is not None else os.environ.get("PATH", "/usr/bin:/bin")
    entries = [str(Path(python).parent), *path_env.split(":")]
    path_env = ":".join(dict.fromkeys(e for e in entries if e))  # dedupe, keep order
    log = str(log_path(cfg))
    data = {
        "Label": label(cfg),
        "ProgramArguments": [
            python,
            "-m",
            "nightshift",
            "--config-dir",
            str(cfg.config_dir),
            "daemon",
            "run",
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        # Give a running agent time to stop cleanly on SIGTERM before SIGKILL.
        "ExitTimeOut": 30,
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {
            "PATH": path_env,
            "PYTHONPATH": src_dir,
            "NIGHTSHIFT_CONFIG_DIR": str(cfg.config_dir),
        },
    }
    return plistlib.dumps(data)


# --------------------------------------------------------------------------
# Queue runner
# --------------------------------------------------------------------------


class QueueRunner:
    """Runs queued Tasks one at a time.

    ``run_attempt_fn`` is injectable so tests can drive the runner with a
    fake Attempt; production uses :func:`nightshift.runner.run_attempt`.
    """

    def __init__(
        self,
        cfg: Config,
        db: Database,
        host: Host,
        *,
        run_attempt_fn: Callable[..., Any] = run_attempt,
        adapter_provider: Callable = get_adapter,
        log: Callable[[str], None] | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.host = host
        self.run_attempt_fn = run_attempt_fn
        self.adapter_provider = adapter_provider
        self._log = log or (lambda msg: None)
        self.current: str | None = None
        self.control: AttemptControl | None = None
        self.started_at = iso(now(cfg.timezone))
        self._stop = threading.Event()

    # -- logging / state ------------------------------------------------------
    def log(self, msg: str) -> None:
        self._log(self.cfg.secrets.redact(f"{iso(now(self.cfg.timezone))} {msg}"))

    def write_state(self) -> None:
        state = {
            "pid": os.getpid(),
            "started_at": self.started_at,
            "updated_at": iso(now(self.cfg.timezone)),
            "current_task": self.current,
            "awake_held": self.host.awake_held(),
        }
        path = state_path(self.cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n")
        tmp.replace(path)

    # -- control ----------------------------------------------------------------
    @property
    def stopping(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        """Stop after (or by interrupting) the current Attempt. Signal-safe."""
        self._stop.set()
        if self.control is not None:
            self.control.request_stop()

    # -- the loop -------------------------------------------------------------
    def recover(self) -> list[str]:
        """Fail Tasks a dead daemon left ``running``; call once at startup."""
        failed = fail_orphaned(self.cfg, self.db)
        for tid in failed:
            self.log(f"Task {tid} was left running by a stopped daemon; marked failed")
        return failed

    def run_next(self) -> str | None:
        """Run one Attempt of the next queued Task; None if the queue is empty."""
        if self.stopping:
            return None
        row = self.db.next_queued_task()
        if row is None:
            return None
        task_id = row["id"]
        self.current = task_id
        self.control = AttemptControl()
        # Held only while this Task runs; released in `finally` whatever happens.
        self.host.hold_awake()
        self.write_state()
        self.log(f"Task {task_id} starting ({row['priority']}, {row['agent']}): {row['title']}")
        try:
            result = self.run_attempt_fn(
                self.cfg,
                self.db,
                task_id,
                adapter_provider=self.adapter_provider,
                control=self.control,
            )
            self.log(f"Task {task_id} -> {result.state}")
        except Exception as exc:  # never let one Task kill the daemon
            self.log(f"Task {task_id} crashed Nightshift: {type(exc).__name__}: {exc}")
            fail_unstartable(self.cfg, self.db, task_id, f"{type(exc).__name__}: {exc}")
        finally:
            self.host.release_awake()
            self.current = None
            self.control = None
            self.write_state()
        return task_id

    def drain(self) -> list[str]:
        """Run queued Tasks until the queue is empty or a stop is requested."""
        ran: list[str] = []
        while not self.stopping:
            tid = self.run_next()
            if tid is None:
                break
            ran.append(tid)
        return ran

    def serve(self, *, poll: float | None = None, exit_when_idle: bool = False) -> None:
        """Recover, then run the queue until stopped (or idle, if asked)."""
        poll = poll_seconds(self.cfg) if poll is None else poll
        self.recover()
        self.write_state()
        self.log(f"daemon started (pid {os.getpid()}), polling every {poll:g}s")
        try:
            while not self.stopping:
                self.drain()
                if exit_when_idle or self.stopping:
                    break
                self._stop.wait(poll)
        finally:
            self.host.release_awake()
            self.log("daemon stopped")
            try:
                state_path(self.cfg).unlink()
            except OSError:
                pass
