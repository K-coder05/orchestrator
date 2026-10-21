"""SQLite storage: Tasks, Attempts (table ``runs``), transitions, usage.

The schema is built by an ordered list of migrations; later work appends to
``MIGRATIONS`` (never edits an applied one). ``PRAGMA user_version`` records
how many have been applied.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .states import TaskState, check_transition
from .timeutil import iso, now

MIGRATIONS: list[str] = [
    # 1: Tasks, Attempts, transitions, usage events.
    """
    CREATE TABLE tasks (
        id                  TEXT PRIMARY KEY,
        title               TEXT NOT NULL,
        slug                TEXT NOT NULL,
        prompt              TEXT NOT NULL,
        template            TEXT,
        task_type           TEXT NOT NULL,
        agent               TEXT NOT NULL,
        agent_override      INTEGER NOT NULL DEFAULT 0,
        priority            TEXT NOT NULL,
        deadline            TEXT,
        run_at              TEXT,
        internet            TEXT NOT NULL,
        long                INTEGER NOT NULL DEFAULT 0,
        estimated_minutes   INTEGER,
        max_runtime_minutes INTEGER,
        approved            INTEGER NOT NULL DEFAULT 0,
        approved_at         TEXT,
        schedule_name       TEXT,
        created_at          TEXT NOT NULL,
        state               TEXT NOT NULL,
        state_changed_at    TEXT NOT NULL,
        folder              TEXT NOT NULL
    );

    -- One row per Attempt (one agent session working on a Task).
    CREATE TABLE runs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(id),
        attempt_no  INTEGER NOT NULL,
        agent       TEXT NOT NULL,
        session_id  TEXT,
        started_at  TEXT NOT NULL,
        ended_at    TEXT,
        exit_code   INTEGER,
        outcome     TEXT,
        end_reason  TEXT,
        command     TEXT,
        log_path    TEXT,
        UNIQUE (task_id, attempt_no)
    );

    CREATE TABLE transitions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id     TEXT NOT NULL REFERENCES tasks(id),
        from_state  TEXT,
        to_state    TEXT NOT NULL,
        at          TEXT NOT NULL,
        reason      TEXT NOT NULL,
        run_id      INTEGER REFERENCES runs(id)
    );
    CREATE INDEX transitions_task ON transitions(task_id, id);

    -- Every usage field an adapter reports, per Attempt and model (§1.5).
    CREATE TABLE usage_events (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id                 INTEGER NOT NULL REFERENCES runs(id),
        task_id                TEXT NOT NULL REFERENCES tasks(id),
        agent                  TEXT NOT NULL,
        model                  TEXT,
        at                     TEXT NOT NULL,
        input_tokens           INTEGER,
        output_tokens          INTEGER,
        cache_creation_tokens  INTEGER,
        cache_read_tokens      INTEGER,
        requests               INTEGER,
        cost_usd               REAL,
        raw                    TEXT
    );
    CREATE INDEX usage_events_run ON usage_events(run_id);
    """,
    # 2: plan approvals and the usage of planning calls (§2.4).
    """
    -- One row per decision on a Task that was awaiting approval.
    CREATE TABLE approvals (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id            TEXT NOT NULL REFERENCES tasks(id),
        decision           TEXT NOT NULL CHECK (decision IN ('approved', 'denied')),
        at                 TEXT NOT NULL,
        via                TEXT NOT NULL,
        estimated_minutes  INTEGER,
        plan_sha256        TEXT
    );
    CREATE INDEX approvals_task ON approvals(task_id, id);

    -- Planning calls are not Attempts (no session, no Task state change), so
    -- their usage has its own table. task_id is NULL when the estimate was
    -- declined and no Task was created.
    CREATE TABLE planning_usage (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id                TEXT REFERENCES tasks(id),
        kind                   TEXT NOT NULL,
        agent                  TEXT NOT NULL,
        model                  TEXT,
        at                     TEXT NOT NULL,
        input_tokens           INTEGER,
        output_tokens          INTEGER,
        cache_creation_tokens  INTEGER,
        cache_read_tokens      INTEGER,
        requests               INTEGER,
        cost_usd               REAL,
        raw                    TEXT
    );
    """,
    # 3: retries and cancel requests (§2.7).
    """
    -- Failed Attempts charged to the retry budget since the Task was created
    -- or last retried by hand.
    ALTER TABLE tasks ADD COLUMN retries_used INTEGER NOT NULL DEFAULT 0;
    -- When a Task in retry_wait goes back in the queue.
    ALTER TABLE tasks ADD COLUMN retry_at TEXT;
    -- Set by `orch cancel` for a running Task; the daemon stops the Attempt.
    ALTER TABLE tasks ADD COLUMN cancel_requested_at TEXT;
    """,
    # 4: usage pauses, resume and fault injection (§2.6, §3.1 item 15).
    """
    -- orch add --simulate-rate-limit-after N: a limit is simulated once, after
    -- N Steps of an Attempt; simulated_limit_at records that it fired.
    ALTER TABLE tasks ADD COLUMN simulate_rate_limit_after INTEGER;
    ALTER TABLE tasks ADD COLUMN simulated_limit_at TEXT;
    -- Why a paused_usage Task is paused: usage_limit, simulated or by_hand.
    ALTER TABLE tasks ADD COLUMN pause_kind TEXT;
    -- The agent's limit type (e.g. five_hour) and reported reset time, if any.
    ALTER TABLE tasks ADD COLUMN pause_limit_type TEXT;
    ALTER TABLE tasks ADD COLUMN pause_reset_at TEXT;
    -- When the daemon puts a paused Task back in the queue (NULL: only by hand).
    ALTER TABLE tasks ADD COLUMN resume_at TEXT;
    -- The next Attempt continues paused work: same session if it can.
    ALTER TABLE tasks ADD COLUMN resume_pending INTEGER NOT NULL DEFAULT 0;
    -- Set by `orch pause` for a running Task; the daemon pauses it after the
    -- current Step.
    ALTER TABLE tasks ADD COLUMN pause_requested_at TEXT;
    """,
    # 5: Limit observations and Window status (§1.5, §3.1 item 13).
    """
    -- One row per actual usage-limit hit in a Window, with the usage
    -- Nightshift had recorded in that Window up to the hit. Baselines are
    -- learned from these (lowest cost of the last 3 per agent and Window).
    CREATE TABLE limit_observations (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        agent                  TEXT NOT NULL,
        window                 TEXT NOT NULL,
        limit_type             TEXT,
        hit_at                 TEXT NOT NULL,
        source                 TEXT NOT NULL,
        task_id                TEXT REFERENCES tasks(id),
        run_id                 INTEGER REFERENCES runs(id),
        resets_at              TEXT,
        window_start           TEXT NOT NULL,
        cost_usd               REAL,
        events_without_cost    INTEGER NOT NULL DEFAULT 0,
        input_tokens           INTEGER NOT NULL DEFAULT 0,
        output_tokens          INTEGER NOT NULL DEFAULT 0,
        cache_creation_tokens  INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens      INTEGER NOT NULL DEFAULT 0,
        requests               INTEGER NOT NULL DEFAULT 0,
        utilization            REAL
    );
    CREATE INDEX limit_observations_window ON limit_observations(agent, window, id);

    -- The latest status an agent reported for each of its Windows (reset
    -- time, plan-wide utilization); tells where the current Window started.
    CREATE TABLE window_status (
        agent        TEXT NOT NULL,
        window       TEXT NOT NULL,
        resets_at    TEXT,
        utilization  REAL,
        status       TEXT,
        seen_at      TEXT NOT NULL,
        PRIMARY KEY (agent, window)
    );

    -- When the next probe of an agent out of usage is due (no reset time
    -- known), and what the last probe found.
    CREATE TABLE probes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        agent      TEXT NOT NULL,
        at         TEXT NOT NULL,
        outcome    TEXT NOT NULL CHECK (outcome IN ('available', 'limited', 'error')),
        resets_at  TEXT,
        detail     TEXT
    );
    """,
]

#: Task columns ``transition(set_fields=...)`` may change with the state.
TRANSITION_FIELDS = frozenset({
    "retries_used", "retry_at", "cancel_requested_at", "approved", "approved_at",
    "simulated_limit_at", "pause_kind", "pause_limit_type", "pause_reset_at", "resume_at",
    "resume_pending", "pause_requested_at",
})

TASK_COLUMNS = (
    "id title slug prompt template task_type agent agent_override priority deadline run_at "
    "internet long estimated_minutes max_runtime_minutes approved approved_at schedule_name "
    "created_at folder simulate_rate_limit_after"
).split()


class StateChanged(Exception):
    """A conditional transition found the Task in another state."""


class Database:
    def __init__(self, path: Path | str, *, tz: str = "America/Los_Angeles"):
        self.path = Path(path)
        self.tz = tz
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            # The daemon and CLI commands share the DB; WAL lets readers
            # (`orch list`) proceed while the daemon writes.
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 10000")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    def migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
            with self.conn:
                self.conn.executescript(sql)
                self.conn.execute(f"PRAGMA user_version = {i}")

    def _now(self) -> str:
        return iso(now(self.tz))

    # -- Tasks ----------------------------------------------------------------
    def task_id_exists(self, task_id: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (task_id,)).fetchone()
            is not None
        )

    def insert_task(self, fields: dict[str, Any], initial_state: TaskState, reason: str) -> None:
        check_transition(None, initial_state)
        row = {k: fields.get(k) for k in TASK_COLUMNS}
        for key in ("agent_override", "long", "approved"):
            row[key] = int(bool(row[key]))
        at = self._now()
        cols = ", ".join(row) + ", state, state_changed_at"
        marks = ", ".join("?" for _ in row) + ", ?, ?"
        with self.tx() as c:
            c.execute(
                f"INSERT INTO tasks ({cols}) VALUES ({marks})",
                [*row.values(), str(initial_state), at],
            )
            c.execute(
                "INSERT INTO transitions (task_id, from_state, to_state, at, reason) "
                "VALUES (?, NULL, ?, ?, ?)",
                (row["id"], str(initial_state), at, reason),
            )

    def get_task(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()

    def task_state(self, task_id: str) -> TaskState:
        row = self.conn.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskState(row["state"])

    def transition(
        self,
        task_id: str,
        to_state: TaskState,
        reason: str,
        *,
        run_id: int | None = None,
        expect: TaskState | None = None,
        set_fields: dict[str, Any] | None = None,
    ) -> None:
        """Move a Task to ``to_state``, recording timestamp and reason.

        ``expect`` makes it conditional: if the Task is no longer in that
        state (another process moved it), :class:`StateChanged` is raised and
        nothing is written. ``set_fields`` updates columns from
        ``TRANSITION_FIELDS`` in the same transaction.
        """
        if not reason:
            raise ValueError("every transition needs a reason")
        fields = dict(set_fields or {})
        if bad := set(fields) - TRANSITION_FIELDS:
            raise ValueError(f"transition cannot set {sorted(bad)}")
        with self.tx() as c:
            current = TaskState(
                c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"]
            )
            if expect is not None and current != expect:
                raise StateChanged(f"Task {task_id} is {current}, no longer {expect}")
            check_transition(current, to_state)
            at = self._now()
            extra = "".join(f", {k} = ?" for k in fields)
            c.execute(
                f"UPDATE tasks SET state = ?, state_changed_at = ?{extra} WHERE id = ?",
                (str(to_state), at, *fields.values(), task_id),
            )
            c.execute(
                "INSERT INTO transitions (task_id, from_state, to_state, at, reason, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, str(current), str(to_state), at, reason, run_id),
            )

    def list_tasks(self, states: list[TaskState] | None = None) -> list[sqlite3.Row]:
        """Tasks, newest first, optionally only those in ``states``."""
        if states:
            marks = ", ".join("?" for _ in states)
            return self.conn.execute(
                f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY created_at DESC, rowid DESC",
                [str(s) for s in states],
            ).fetchall()
        return self.conn.execute("SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC").fetchall()

    def tasks_in_state(self, state: TaskState) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM tasks WHERE state = ? ORDER BY created_at, rowid", (str(state),)
        ).fetchall()

    def set_priority(self, task_id: str, priority: str) -> None:
        """Change a Task's assigned Priority (not a state change)."""
        if priority not in ("urgent", "normal", "background"):
            raise ValueError(f"unknown priority {priority!r}")
        with self.tx() as c:
            if c.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, task_id)).rowcount != 1:
                raise KeyError(task_id)

    def set_deadline(self, task_id: str, deadline: str | None) -> None:
        """Set a Task's deadline (a template's deadline source; not a state change)."""
        with self.tx() as c:
            if c.execute("UPDATE tasks SET deadline = ? WHERE id = ?", (deadline, task_id)).rowcount != 1:
                raise KeyError(task_id)

    def request_cancel(self, task_id: str) -> None:
        """Ask the daemon to cancel a running Task (see ``nightshift.failures``)."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET cancel_requested_at = COALESCE(cancel_requested_at, ?) "
                "WHERE id = ?",
                (self._now(), task_id),
            )

    def request_pause(self, task_id: str) -> None:
        """Ask the daemon to pause a running Task after its current Step."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pause_requested_at = COALESCE(pause_requested_at, ?) "
                "WHERE id = ?",
                (self._now(), task_id),
            )

    def clear_pause_request(self, task_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE tasks SET pause_requested_at = NULL WHERE id = ?", (task_id,))

    def cancel_requested(self, task_id: str) -> bool:
        row = self.conn.execute(
            "SELECT cancel_requested_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return row is not None and row["cancel_requested_at"] is not None

    def transitions(self, task_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM transitions WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    # -- Attempts -------------------------------------------------------------
    def start_attempt(self, task_id: str, agent: str, command: list[str], log_path: str) -> int:
        with self.tx() as c:
            n = c.execute(
                "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM runs WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            cur = c.execute(
                "INSERT INTO runs (task_id, attempt_no, agent, started_at, command, log_path) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, n, agent, self._now(), json.dumps(command), log_path),
            )
            return int(cur.lastrowid)

    def set_attempt_session(self, run_id: int, session_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET session_id = ? WHERE id = ?", (session_id, run_id))

    def finish_attempt(
        self,
        run_id: int,
        *,
        exit_code: int | None,
        outcome: str,
        end_reason: str,
        session_id: str | None,
    ) -> None:
        with self.tx() as c:
            c.execute(
                "UPDATE runs SET ended_at = ?, exit_code = ?, outcome = ?, end_reason = ?, "
                "session_id = COALESCE(?, session_id) WHERE id = ?",
                (self._now(), exit_code, outcome, end_reason, session_id, run_id),
            )

    def attempts(self, task_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY attempt_no", (task_id,)
        ).fetchall()

    # -- Usage ----------------------------------------------------------------
    def record_usage(self, run_id: int, task_id: str, agent: str, usage: list[dict],
                     *, at: str | None = None) -> None:
        at = at or self._now()
        with self.tx() as c:
            for u in usage:
                c.execute(
                    "INSERT INTO usage_events (run_id, task_id, agent, model, at, input_tokens, "
                    "output_tokens, cache_creation_tokens, cache_read_tokens, requests, cost_usd, "
                    "raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        task_id,
                        agent,
                        u.get("model"),
                        at,
                        u.get("input_tokens"),
                        u.get("output_tokens"),
                        u.get("cache_creation_tokens"),
                        u.get("cache_read_tokens"),
                        u.get("requests"),
                        u.get("cost_usd"),
                        json.dumps(u.get("raw")) if u.get("raw") is not None else None,
                    ),
                )

    def record_planning_usage(
        self, task_id: str | None, kind: str, agent: str, usage: list[dict], *, at: str | None = None
    ) -> None:
        at = at or self._now()
        with self.tx() as c:
            for u in usage:
                c.execute(
                    "INSERT INTO planning_usage (task_id, kind, agent, model, at, input_tokens, "
                    "output_tokens, cache_creation_tokens, cache_read_tokens, requests, cost_usd, "
                    "raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id, kind, agent, u.get("model"), at,
                        u.get("input_tokens"), u.get("output_tokens"),
                        u.get("cache_creation_tokens"), u.get("cache_read_tokens"),
                        u.get("requests"), u.get("cost_usd"),
                        json.dumps(u.get("raw")) if u.get("raw") is not None else None,
                    ),
                )

    def planning_usage(self, task_id: str | None = None) -> list[sqlite3.Row]:
        if task_id is None:
            return self.conn.execute("SELECT * FROM planning_usage ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM planning_usage WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    # -- Approvals ------------------------------------------------------------
    def decide_approval(
        self,
        task_id: str,
        *,
        approved: bool,
        via: str,
        to_state: TaskState,
        reason: str,
        plan_sha256: str | None,
    ) -> str:
        """Record an approval decision, set ``approved``/``approved_at`` and
        move the Task out of ``awaiting_approval``, all in one transaction.
        Returns the decision time."""
        with self.tx() as c:
            row = c.execute(
                "SELECT state, estimated_minutes FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            if current != TaskState.AWAITING_APPROVAL:
                raise ValueError(f"Task {task_id} is {current}, not awaiting approval")
            check_transition(current, to_state)
            at = self._now()
            c.execute(
                "UPDATE tasks SET approved = ?, approved_at = ?, state = ?, state_changed_at = ? "
                "WHERE id = ?",
                (int(approved), at if approved else None, str(to_state), at, task_id),
            )
            c.execute(
                "INSERT INTO approvals (task_id, decision, at, via, estimated_minutes, plan_sha256) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, "approved" if approved else "denied", at, via,
                 row["estimated_minutes"], plan_sha256),
            )
            c.execute(
                "INSERT INTO transitions (task_id, from_state, to_state, at, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, str(current), str(to_state), at, reason),
            )
        return at

    def approvals(self, task_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    def usage_for_task(self, task_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM usage_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    # -- Windows, Limit observations, probes (§1.5) -----------------------------
    def agent_usage(self, agent: str) -> list[sqlite3.Row]:
        """Every usage row of ``agent``: Attempts (``usage_events``) and
        calls outside Attempts (planning calls, probes: ``planning_usage``)."""
        cols = ("at, model, input_tokens, output_tokens, cache_creation_tokens, "
                "cache_read_tokens, requests, cost_usd")
        return self.conn.execute(
            f"SELECT {cols}, 'attempt' AS kind FROM usage_events WHERE agent = ? "
            f"UNION ALL SELECT {cols}, kind FROM planning_usage WHERE agent = ?",
            (agent, agent),
        ).fetchall()

    def usage_agents(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT agent FROM usage_events UNION SELECT agent FROM planning_usage "
            "UNION SELECT agent FROM limit_observations UNION SELECT agent FROM window_status"
        ).fetchall()]

    def insert_limit_observation(self, fields: dict[str, Any]) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.tx() as c:
            cur = c.execute(f"INSERT INTO limit_observations ({cols}) VALUES ({marks})",
                            list(fields.values()))
            return int(cur.lastrowid)

    def limit_observations(self, agent: str | None = None,
                           window: str | None = None) -> list[sqlite3.Row]:
        """Limit observations, oldest first."""
        sql, args = "SELECT * FROM limit_observations WHERE 1 = 1", []
        if agent is not None:
            sql, args = sql + " AND agent = ?", [*args, agent]
        if window is not None:
            sql, args = sql + " AND window = ?", [*args, window]
        return self.conn.execute(sql + " ORDER BY id", args).fetchall()

    def set_window_status(self, agent: str, window: str, *, resets_at: str | None,
                          utilization: float | None, status: str | None, seen_at: str) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO window_status (agent, window, resets_at, utilization, status, seen_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (agent, window) DO UPDATE SET "
                "resets_at = COALESCE(excluded.resets_at, resets_at), "
                "utilization = COALESCE(excluded.utilization, utilization), "
                "status = COALESCE(excluded.status, status), seen_at = excluded.seen_at",
                (agent, window, resets_at, utilization, status, seen_at),
            )

    def window_status(self, agent: str) -> dict[str, sqlite3.Row]:
        return {r["window"]: r for r in self.conn.execute(
            "SELECT * FROM window_status WHERE agent = ?", (agent,)).fetchall()}

    def record_probe(self, agent: str, *, at: str, outcome: str, resets_at: str | None,
                     detail: str | None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO probes (agent, at, outcome, resets_at, detail) "
                      "VALUES (?, ?, ?, ?, ?)", (agent, at, outcome, resets_at, detail))

    def probes(self, agent: str | None = None) -> list[sqlite3.Row]:
        if agent is None:
            return self.conn.execute("SELECT * FROM probes ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM probes WHERE agent = ? ORDER BY id",
                                 (agent,)).fetchall()
