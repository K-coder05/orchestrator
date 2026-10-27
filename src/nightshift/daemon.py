"""The background daemon (§3.1 item 5): queue runner, launchd plist, lock.

``launchd`` keeps the daemon alive (KeepAlive). Inside it the queue runner
keeps a set of running Attempts, each on its own thread with its own control
handle and DB connection: at most one per lane (§5 item 5: one coding/writing
Task on Claude, or Codex when it has taken over for Claude, and one Gemini
Task), and at most ``daemon.max_running`` in all. A Task's lane is its
agent's (``adapters.lane_of``), read at every pick, so a Task that fails over
to another agent is picked by that agent's lane. It holds the machine awake
through the host seam while any Task is ``running`` and releases it once
none is (§1.1). Before every pick the scheduler applies each Schedule's
Catch-up and Overlap rules (``nightshift.schedules``); it also does so right
at start and right after the machine wakes from sleep (§2.5).

One Task per lane is guaranteed twice over: the runner starts an Attempt
only in a lane with nothing running, and only the process holding the
daemon lock (an exclusive ``flock`` on ``<data>/daemon.lock``) may run Tasks
at all.
"""

from __future__ import annotations

import fcntl
import json
import os
import plistlib
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

import nightshift

from . import failover, failures, paid, pauses, schedules, spend, usage
from .alerts import AlertService
from .adapters import get_adapter, lane_of
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


def running_cap(cfg: Config) -> int:
    """How many Attempts the queue runner may run at once, across all lanes
    (one per lane at most either way)."""
    return int((cfg.data.get("daemon") or {}).get("max_running", 2))


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


def running_tasks(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The running Tasks a ``daemon.json`` lists, oldest first: each a dict
    with ``id`` (and ``agent``, ``lane`` and ``started_at`` when known). A file from a
    daemon older than the list names at most one, as ``current_task``."""
    listed = state.get("running_tasks")
    if isinstance(listed, list):
        return [t for t in listed if isinstance(t, dict) and t.get("id")]
    current = state.get("current_task")
    return [{"id": current}] if current else []


# --------------------------------------------------------------------------
# launchd plist
# --------------------------------------------------------------------------


def build_plist(cfg: Config, *, python: str | None = None, path_env: str | None = None) -> bytes:
    """The LaunchAgent plist that runs ``orch daemon run`` under KeepAlive."""
    return launch_agent_plist(cfg, label=label(cfg), command=["daemon", "run"],
                              log=log_path(cfg), python=python, path_env=path_env)


def launch_agent_plist(cfg: Config, *, label: str, command: list[str], log: Path,
                       python: str | None = None, path_env: str | None = None) -> bytes:
    """A LaunchAgent plist that runs ``orch <command>`` under KeepAlive.

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
    data = {
        "Label": label,
        "ProgramArguments": [
            python,
            "-m",
            "nightshift",
            "--config-dir",
            str(cfg.config_dir),
            *command,
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        # Give a running agent time to stop cleanly on SIGTERM before SIGKILL.
        "ExitTimeOut": 30,
        "WorkingDirectory": str(Path.home()),
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "EnvironmentVariables": {
            "PATH": path_env,
            "PYTHONPATH": src_dir,
            "NIGHTSHIFT_CONFIG_DIR": str(cfg.config_dir),
            # stdout is a file under launchd: without this, log lines sit in
            # Python's block buffer and daemon.log looks empty.
            "PYTHONUNBUFFERED": "1",
        },
    }
    return plistlib.dumps(data)


@dataclass(frozen=True)
class AccessProblem:
    """A path the launchd daemon needs that sits in a privacy-protected
    folder. ``blocking``: the daemon can't start or run Tasks at all
    (otherwise only some Tasks lose an input or a readable folder)."""

    what: str
    path: Path
    folder: Path
    blocking: bool


def _inside(path: Path, folder: Path) -> bool:
    try:
        return path.is_relative_to(folder.resolve())
    except OSError:
        return False


def launchd_access_problems(cfg: Config, host: Host, *, python: str | None = None) -> list[AccessProblem]:
    """Paths the launchd agent reads that macOS privacy protection (TCC)
    keeps from it. Verified on this Mac: a LaunchAgent whose Python lives
    under ~/Desktop hangs in dyld before Python starts, and a launchd job
    reading a file there gets ``Operation not permitted``."""
    from .templates import templates_dir  # lazy: templates imports a lot

    folders = host.protected_folders()
    if not folders:
        return []
    python = python or sys.executable
    needed: list[tuple[str, Path, bool]] = [
        ("Python", Path(python), True),
        ("Nightshift source", Path(nightshift.__file__).parent, True),
        ("config dir", cfg.config_dir, True),
        ("agents_root", cfg.agents_root, True),
        ("templates dir", templates_dir(cfg), True),
        ("Tracker", cfg.tracker_path, True),
    ]
    for tname, section in ((cfg.data.get("templates") or {}).items()):
        if isinstance(section, dict):
            for key, value in section.items():
                if isinstance(value, str) and value.startswith(("~", "/")):
                    needed.append((f"templates.{tname}.{key}", Path(value), False))
    for d in (cfg.data.get("permissions") or {}).get("read_only_dirs") or []:
        needed.append(("permissions.read_only_dirs", Path(str(d)), False))
    out: list[AccessProblem] = []
    for what, path, blocking in needed:
        real = path.expanduser().resolve()
        for folder in folders:
            if _inside(real, folder):
                out.append(AccessProblem(what, real, folder, blocking))
                break
    return out


def format_access_problems(problems: list[AccessProblem], python: str | None = None,
                           service: str = "daemon") -> list[str]:
    """Human lines for ``orch daemon|dashboard install`` and ``orch doctor``."""
    if not problems:
        return []
    real_python = Path(python or sys.executable).resolve()
    lines = []
    for p in problems:
        kind = f"{service} can't run" if p.blocking else "a launchd-run Task touching it waits on a privacy prompt"
        lines.append(f"{p.what} {p.path} is in {p.folder} ({kind})")
    lines.append(
        "macOS privacy protection (TCC) keeps launchd agents out of Desktop, Documents, "
        "Downloads, iCloud Drive and external volumes: the access either fails or waits, "
        "unattended, on a \"python3.12 would like to access ...\" dialog. Move these "
        "outside them (the repo with its .python/.venv, then re-run `orch "
        f"{service} install`), or allow {real_python} that folder (answer the dialog once at the "
        "Mac, or give it Full Disk Access in System Settings > Privacy & Security) and "
        "install with --allow-protected."
    )
    return lines


# --------------------------------------------------------------------------
# Queue runner
# --------------------------------------------------------------------------


@dataclass
class RunningAttempt:
    """One Attempt the queue runner has running: its Task, the lane it
    holds, control handle and thread."""

    task_id: str
    agent: str
    lane: str
    started_at: str
    control: AttemptControl
    thread: threading.Thread | None = None


class QueueRunner:
    """Runs queued Tasks, one per lane and at most ``max_running`` at a time.

    Each Attempt runs on its own thread with its own control handle and DB
    connection (SQLite connections stay in their thread); the runner's own
    thread picks Tasks and waits. ``run_attempt_fn`` is injectable so tests
    can drive the runner with a fake Attempt; production uses
    :func:`nightshift.runner.run_attempt`. ``max_running`` overrides
    ``daemon.max_running`` (tests).
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
        alerts: AlertService | None = None,
        max_running: int | None = None,
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
        # Telegram alerts (§2.12); kicked after each Attempt so they go out at once.
        self.alerts = alerts
        self.max_running = max_running if max_running is not None else running_cap(cfg)
        #: The running Attempts by Task id, in the order they started.
        self.running: dict[str, RunningAttempt] = {}
        # Guards `running`, the awake hold and the state file; `_changed` is
        # notified whenever an Attempt ends.
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._ended = 0  # Attempts ended so far
        self._wake_mark: tuple[datetime, float] | None = None
        self.started_at = iso(now(cfg.timezone))
        self._stop = threading.Event()

    # -- logging / state ------------------------------------------------------
    def log(self, msg: str) -> None:
        self._log(self.cfg.secrets.redact(f"{iso(now(self.cfg.timezone))} {msg}"))

    @property
    def current(self) -> str | None:
        """The oldest running Task, or None."""
        first = self._oldest()
        return first.task_id if first else None

    @property
    def control(self) -> AttemptControl | None:
        """The control handle of the oldest running Attempt, or None."""
        first = self._oldest()
        return first.control if first else None

    def _oldest(self) -> RunningAttempt | None:
        return next(iter(list(self.running.values())), None)

    def write_state(self) -> None:
        with self._lock:
            running = list(self.running.values())
            state = {
                "pid": os.getpid(),
                "started_at": self.started_at,
                "updated_at": iso(now(self.cfg.timezone)),
                "max_running": self.max_running,
                "running_tasks": [{"id": r.task_id, "agent": r.agent, "lane": r.lane,
                                   "started_at": r.started_at} for r in running],
                # The oldest running Task, as daemons before the list wrote it.
                "current_task": running[0].task_id if running else None,
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
        """Stop by interrupting every running Attempt (each Task goes back
        to ``queued``). Signal-safe: it takes no lock."""
        self._stop.set()
        for r in list(self.running.values()):
            r.control.request_stop()

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

    def apply_spend_caps(self) -> None:
        """Daily and monthly spend caps (§1.5): queue paused paid Tasks again
        once no cap holds; while one does, pause the queued ones."""
        at = self.clock()
        for tid in spend.promote_paused(self.cfg, self.db, at):
            self.log(f"Task {tid} spend pause over; {self.db.task_state(tid)}")
        pause = spend.spend_pause(self.cfg, self.db, at)
        for tid in spend.pause_paid_queued(self.cfg, self.db, at):
            self.log(f"Task {tid} paused: {pause.reason if pause else 'spend cap reached'}")

    def apply_schedules(self, trigger: str = schedules.TRIGGER_POLL) -> list[schedules.Decision]:
        """Apply every Schedule's Catch-up and Overlap rules now (§2.5).
        With an Attempt running in the other lane, replacing its Task's
        Occurrence asks that Attempt to stop, as for a separate daemon."""
        try:
            done = schedules.apply_catch_up(self.cfg, self.db, self.clock(), trigger=trigger,
                                            daemon_up=bool(self.running))
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

    def busy_lanes(self) -> set[str]:
        """The lanes with an Attempt running."""
        return {r.lane for r in list(self.running.values())}

    def start_next(self) -> str | None:
        """Start an Attempt of the next queued Task in a free lane, on its
        own thread; None if no slot is free, the runner is stopping or
        nothing may start.

        Returns once the Attempt has moved its Task to ``running`` (or
        ended): the pre-pick work of the next call (failover, the
        paid-credit rule, spend caps, the Overlap rule) changes ``queued``
        Tasks and must never touch one an Attempt is about to run."""
        if self.stopping or len(self.running) >= self.max_running:
            return None
        self.apply_schedules()
        self.promote_retries()
        self.promote_paused()
        self.apply_spend_caps()
        # Effective priority and time windows are re-evaluated on every pick;
        # an agent out of usage starts nothing until its reset.
        held = pauses.held_agents(self.cfg, self.db, self.clock())
        if held:
            # Tasks waiting on an agent that is out move to one that isn't (§2.8).
            for tid, src, dst in failover.reassign_held(self.cfg, self.db, held, self.clock()):
                self.log(f"Task {tid}: {src} is out of usage; failed over to {dst}")
        # Then the paid-credit rule (§1.5): a Task whose subscription is out
        # uses paid credits only if it can't wait for the reset.
        for d in paid.apply(self.cfg, self.db, self.clock()):
            self.log(f"Task {d.task_id}: paid-credit rule: {d.verdict.reason}")
        # Proactive thresholds (§1.5) hold back new Tasks only; nothing fails
        # over, since the agent is not out yet.
        thresholds = usage.threshold_holds(self.cfg, self.db, self.clock())
        # Each free lane may start its next Task (§5 item 5): the queue order
        # among the Tasks whose agent's lane has nothing running. The lane is
        # read now, after any failover above moved a Task to another agent.
        busy = self.busy_lanes()
        queued = [r for r in self.db.tasks_in_state(TaskState.QUEUED)
                  if r["id"] not in self.running and lane_of(r["agent"]) not in busy]
        row = pick_next(self.cfg, queued, self.clock(),
                        held_agents=thresholds.all | held,
                        held_background=thresholds.background)
        if row is None:
            return None
        task_id = row["id"]
        slot = RunningAttempt(task_id, row["agent"], lane_of(row["agent"]),
                              iso(now(self.cfg.timezone)), AttemptControl())
        with self._lock:
            self.running[task_id] = slot
            if self.stopping:
                # stop() came since the check above and may have missed this
                # slot: start nothing (the Task stays queued).
                del self.running[task_id]
                return None
            # Held while any Task runs; released when the last one ends.
            self.host.hold_awake()
            self.write_state()
        eff = evaluate(self.cfg, row, self.clock()).effective
        prio = row["priority"] if eff == row["priority"] else f"{row['priority']}, effective {eff}"
        self.log(f"Task {task_id} starting ({prio}, {row['agent']}, {slot.lane} lane): "
                 f"{row['title']}")
        slot.thread = threading.Thread(target=self._attempt, args=(slot, dict(row)),
                                       name=f"attempt-{task_id}", daemon=True)
        slot.thread.start()
        # Until its Attempt moves it to `running`, the Task is still `queued`
        # in the database: wait (moments) so no pick in between moves it to
        # another agent, pauses it for spend or replaces it.
        slot.control.started.wait()
        return task_id

    def _attempt(self, slot: RunningAttempt, row: dict[str, Any]) -> None:
        """One Attempt, on its own thread with its own DB connection."""
        task_id = slot.task_id
        db: Database | None = None
        try:
            db = Database(self.db.path, tz=self.db.tz)
            result = self.run_attempt_fn(
                self.cfg,
                db,
                task_id,
                adapter_provider=self.adapter_provider,
                control=slot.control,
                clock=self.clock,
                post_hooks=self.post_hooks_for(self.cfg, row),
            )
            self.log(f"Task {task_id} -> {result.state}")
        except Exception as exc:  # never let one Task kill the daemon
            self.log(f"Task {task_id} crashed Nightshift: {type(exc).__name__}: {exc}")
            try:
                db = db or Database(self.db.path, tz=self.db.tz)
                fail_unstartable(self.cfg, db, task_id, f"{type(exc).__name__}: {exc}")
            except Exception as again:
                self.log(f"Task {task_id} could not be failed: {type(again).__name__}: {again}")
        finally:
            slot.control.started.set()
            if db is not None:
                db.close()
            with self._lock:
                self.running.pop(task_id, None)
                if not self.running:
                    self.host.release_awake()
                self.write_state()
                self._ended += 1
                self._changed.notify_all()
            if self.alerts is not None:
                self.alerts.kick()

    def wait(self, task_id: str, timeout: float | None = None) -> None:
        """Wait until ``task_id``'s Attempt ends (at once if it isn't running)."""
        with self._changed:
            self._changed.wait_for(lambda: task_id not in self.running, timeout)

    def _wait_for_an_end(self, since: int, timeout: float | None = None) -> bool:
        """Wait until an Attempt has ended after the ``since``-th (or
        ``timeout`` seconds pass); whether one has."""
        with self._changed:
            return self._changed.wait_for(lambda: self._ended != since, timeout)

    def wait_all(self) -> None:
        """Wait until no Attempt is running."""
        with self._changed:
            self._changed.wait_for(lambda: not self.running)

    def run_next(self) -> str | None:
        """Run one Attempt of the next queued Task to its end; None if
        nothing may start."""
        task_id = self.start_next()
        if task_id is not None:
            self.wait(task_id)
        return task_id

    def drain(self, *, poll: float | None = None) -> list[str]:
        """Run queued Tasks until the queue is empty and nothing runs, or a
        stop is requested (then wait for the running Attempts to end).

        While Attempts run, free lanes are filled again whenever one ends
        and every ``poll`` seconds (``daemon.poll_seconds``): a Task queued
        for the Gemini lane starts while a long Claude Task runs, not after
        it. Each poll also checks for a wake from sleep."""
        poll = poll_seconds(self.cfg) if poll is None else poll
        ran: list[str] = []
        while True:
            since = self._ended
            while not self.stopping:
                tid = self.start_next()
                if tid is None:
                    break
                ran.append(tid)
            if not self.running:
                break
            if not self._wait_for_an_end(since, poll) and not self.stopping:
                self.check_wake()
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
                self.drain(poll=poll)
                if exit_when_idle or self.stopping:
                    break
                self._stop.wait(poll)
        finally:
            if self.running:  # an error in the loop: don't leave Attempts behind
                self.stop()
                self.wait_all()
            self.host.release_awake()
            self.log("daemon stopped")
            try:
                state_path(self.cfg).unlink()
            except OSError:
                pass
