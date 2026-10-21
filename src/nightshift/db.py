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
]

TASK_COLUMNS = (
    "id title slug prompt template task_type agent agent_override priority deadline run_at "
    "internet long estimated_minutes max_runtime_minutes approved approved_at schedule_name "
    "created_at folder"
).split()


class Database:
    def __init__(self, path: Path | str, *, tz: str = "America/Los_Angeles"):
        self.path = Path(path)
        self.tz = tz
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
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
        self, task_id: str, to_state: TaskState, reason: str, *, run_id: int | None = None
    ) -> None:
        """Move a Task to ``to_state``, recording timestamp and reason."""
        if not reason:
            raise ValueError("every transition needs a reason")
        with self.tx() as c:
            current = TaskState(
                c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()["state"]
            )
            check_transition(current, to_state)
            at = self._now()
            c.execute(
                "UPDATE tasks SET state = ?, state_changed_at = ? WHERE id = ?",
                (str(to_state), at, task_id),
            )
            c.execute(
                "INSERT INTO transitions (task_id, from_state, to_state, at, reason, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, str(current), str(to_state), at, reason, run_id),
            )

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
    def record_usage(self, run_id: int, task_id: str, agent: str, usage: list[dict]) -> None:
        at = self._now()
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

    def usage_for_task(self, task_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM usage_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
