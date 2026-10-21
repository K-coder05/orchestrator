"""The background daemon (§3.1 item 5): queue runner, launchd plist, lock.

``launchd`` keeps the daemon alive (KeepAlive). Inside it the queue runner
takes queued Tasks one at a time, holding the machine awake through the host
seam only while a Task is ``running`` (§1.1). Before every pick the
scheduler applies each Schedule's Catch-up and Overlap rules
(``nightshift.schedules``); it also does so right at start and right after
the machine wakes from sleep (§2.5).

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
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import nightshift

from . import failover, failures, pauses, schedules, usage
from .adapters import get_adapter
from .config import Config
from .db import Database
from .host import Host
from .priority import evaluate, pick_next
from .runner import AttemptControl, fail_orphaned, fail_unstartable, run_attempt
from .states import TaskState
from .timeutil import iso, now

LOCK_FILENAME = "daemon.lock"
STATE_FILENAME = "daemon.json"
LOG_FILENAME = "daemon.log"

#: Wall time running this far ahead of awake time between two checks means
#: the machine slept (and has just woken).
WAKE_GAP_SECONDS = 60.0


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
        clock: Callable[[], datetime] | None = None,
        post_hooks_for: Callable[[Config, Any], Any] | None = None,
        prober: Callable[[str], Any] | None = None,
    ):
        self.cfg = cfg
        self.db = db
        self.host = host
        self.run_attempt_fn = run_attempt_fn
        self.adapter_provider = adapter_provider
        self._log = log or (lambda msg: None)
        # The queue's notion of "now"; tests pass a fake clock.
        self.clock = clock or (lambda: now(cfg.timezone))
        # Template post-hooks; confined tools (the LaTeX compile) run through `host`.
        self.post_hooks_for = post_hooks_for or (
            lambda cfg, row: failures.post_hooks_for(cfg, row, self.host))
        # Minimal probe requests for agents out of usage with no reset time.
        self.prober = prober or (lambda agent: usage.probe(
            self.cfg, self.db, agent, adapter_provider=self.adapter_provider, clock=self.clock))
        self.current: str | None = None
        self.control: AttemptControl | None = None
        self._wake_mark: tuple[datetime, float] | None = None
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
        settled = fail_orphaned(self.cfg, self.db, clock=self.clock)
        for tid in settled:
            self.log(f"Task {tid} was left running by a stopped daemon; now {self.db.task_state(tid)}")
        return settled

    def promote_retries(self) -> list[str]:
        """Requeue Tasks whose ``retry_wait`` is over (§2.7)."""
        moved = failures.promote_due(self.cfg, self.db, self.clock())
        for tid in moved:
            self.log(f"Task {tid} retry wait over; queued")
        return moved

    def promote_paused(self) -> pauses.Promoted:
        """Expire paused Tasks or requeue them once their reset time has come
        (§2.6), in resume order."""
        done = pauses.promote_due(self.cfg, self.db, self.clock(), prober=self.prober)
        for agent, found in done.probes.items():
            what = ("available" if found.available else "still out of usage"
                    if found.limit is not None else f"probe failed: {found.error}")
            self.log(f"probed {agent}: {what}")
        for tid in done.cancelled:
            self.log(f"Task {tid} deadline passed while paused; cancelled")
        for tid in done.reapproval:
            self.log(f"Task {tid} paused too long; awaiting re-approval")
        for tid in done.resumed:
            self.log(f"Task {tid} usage pause over; queued to resume")
        return done

    def apply_schedules(self, trigger: str = schedules.TRIGGER_POLL) -> list[schedules.Decision]:
        """Apply every Schedule's Catch-up and Overlap rules now (§2.5)."""
        try:
            done = schedules.apply_catch_up(self.cfg, self.db, self.clock(), trigger=trigger)
        except Exception as exc:  # a broken Schedule must not stop the queue
            self.log(f"scheduler error ({trigger}): {type(exc).__name__}: {exc}")
            return []
        for d in done:
            self.log(("on daemon start: " if trigger == schedules.TRIGGER_START else
                      "after wake: " if trigger == schedules.TRIGGER_WAKE else "") + d.describe())
        return done

    def check_wake(self) -> bool:
        """Whether the machine slept since the last check (wall time ran
        ahead of awake time); if so, apply the Catch-up rules now."""
        mark = (self.clock(), self.host.awake_seconds())
        last, self._wake_mark = self._wake_mark, mark
        if last is None:
            return False
        asleep = (mark[0] - last[0]).total_seconds() - (mark[1] - last[1])
        if asleep < WAKE_GAP_SECONDS:
            return False
        self.log(f"woke from sleep (asleep about {asleep / 60:.0f} min); applying catch-up")
        self.apply_schedules(schedules.TRIGGER_WAKE)
        return True

    def run_next(self) -> str | None:
        """Run one Attempt of the next queued Task; None if the queue is empty."""
        if self.stopping:
            return None
        self.apply_schedules()
        self.promote_retries()
        self.promote_paused()
        # Effective priority and time windows are re-evaluated on every pick;
        # an agent out of usage starts nothing until its reset.
        held = pauses.held_agents(self.cfg, self.db, self.clock())
        if held:
            # Tasks waiting on an agent that is out move to one that isn't (§2.8).
            for tid, src, dst in failover.reassign_held(self.cfg, self.db, held, self.clock()):
                self.log(f"Task {tid}: {src} is out of usage; failed over to {dst}")
        row = pick_next(self.cfg, self.db.tasks_in_state(TaskState.QUEUED), self.clock(),
                        held_agents=held)
        if row is None:
            return None
        task_id = row["id"]
        self.current = task_id
        self.control = AttemptControl()
        # Held only while this Task runs; released in `finally` whatever happens.
        self.host.hold_awake()
        self.write_state()
        eff = evaluate(self.cfg, row, self.clock()).effective
        prio = row["priority"] if eff == row["priority"] else f"{row['priority']}, effective {eff}"
        self.log(f"Task {task_id} starting ({prio}, {row['agent']}): {row['title']}")
        try:
            result = self.run_attempt_fn(
                self.cfg,
                self.db,
                task_id,
                adapter_provider=self.adapter_provider,
                control=self.control,
                clock=self.clock,
                post_hooks=self.post_hooks_for(self.cfg, row),
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
            self.apply_schedules(schedules.TRIGGER_START)
            self.check_wake()
            while not self.stopping:
                self.check_wake()
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
