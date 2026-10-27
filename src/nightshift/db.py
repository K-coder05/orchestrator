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
    # 6: Schedules and their fires (§2.5).
    """
    -- A named recurring rule that creates pre-approved Occurrences from a
    -- template. last_fire_at: every fire up to and including this time has
    -- been handled (created, skipped or collapsed); the daemon compares it
    -- with the time now on every start, wake and poll.
    CREATE TABLE schedules (
        name            TEXT PRIMARY KEY,
        template        TEXT NOT NULL,
        request         TEXT NOT NULL DEFAULT '',
        cron            TEXT NOT NULL,
        catch_up        TEXT NOT NULL CHECK (catch_up IN ('within', 'next_wake')),
        catch_up_hours  REAL,
        overlap         TEXT NOT NULL DEFAULT 'skip' CHECK (overlap IN ('skip', 'replace')),
        priority        TEXT,
        agent           TEXT,
        enabled         INTEGER NOT NULL DEFAULT 1,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        last_fire_at    TEXT NOT NULL,
        last_checked_at TEXT
    );

    -- One row per decision about a Schedule's fire(s): an Occurrence was
    -- created (or replaced an unfinished one), or the fire was skipped by the
    -- Catch-up rule or the Overlap rule. `fires` > 1: several missed fires
    -- collapsed into this one decision. No foreign key to schedules, so the
    -- history outlives `orch schedule remove`.
    CREATE TABLE schedule_runs (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_name  TEXT NOT NULL,
        trigger        TEXT NOT NULL CHECK (trigger IN ('start', 'wake', 'poll', 'run_now')),
        fire_at        TEXT NOT NULL,
        fires          INTEGER NOT NULL DEFAULT 1,
        decided_at     TEXT NOT NULL,
        outcome        TEXT NOT NULL CHECK (outcome IN
                         ('created', 'replaced', 'skipped_overlap', 'skipped_catch_up', 'error')),
        task_id        TEXT REFERENCES tasks(id),
        detail         TEXT
    );
    CREATE INDEX schedule_runs_name ON schedule_runs(schedule_name, id);
    """,
    # 7: Tracker merges (§1.7).
    """
    -- One row per run of a posting-scan Task's merge_tracker post-hook: what
    -- Nightshift appended to the Tracker (applications.csv) from the agent's
    -- Proposed rows. `result` is the JSON MergeResult (appended rows with
    -- their Tracker ids, duplicates, rejected rows); the report's New
    -- postings section is written from these rows only.
    CREATE TABLE tracker_merges (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id       TEXT NOT NULL REFERENCES tasks(id),
        run_id        INTEGER REFERENCES runs(id),
        at            TEXT NOT NULL,
        tracker_path  TEXT NOT NULL,
        proposed      INTEGER NOT NULL,
        appended      INTEGER NOT NULL,
        duplicates    INTEGER NOT NULL,
        invalid       INTEGER NOT NULL,
        result        TEXT NOT NULL
    );
    CREATE INDEX tracker_merges_task ON tracker_merges(task_id, id);
    """,
    # 8: Failovers (§2.7, §2.8): a Task moved from one agent to another.
    """
    -- One row per agent switch of a Task. trigger: usage_limit (the agent
    -- hit a real limit), simulated (--simulate-rate-limit-after), reset (a
    -- Task waiting for every agent resumed on the one that reset first),
    -- retry (the second retry of a Failed Attempt). deadline_driven: only
    -- allowed because the deadline is near (flagged in the report).
    CREATE TABLE failovers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id          TEXT NOT NULL REFERENCES tasks(id),
        run_id           INTEGER REFERENCES runs(id),
        at               TEXT NOT NULL,
        from_agent       TEXT NOT NULL,
        to_agent         TEXT NOT NULL,
        trigger          TEXT NOT NULL,
        deadline_driven  INTEGER NOT NULL DEFAULT 0,
        reason           TEXT NOT NULL
    );
    CREATE INDEX failovers_task ON failovers(task_id, id);
    """,
    # 9: Telegram alerts (§2.12).
    """
    -- One row per Telegram alert, made from the transition that caused it.
    -- sent_at stays NULL until the Bot API accepted it; dropped_at is set
    -- when it was never delivered and grew too old. `text` is already
    -- redacted, and a comedy Task's is a status line only.
    CREATE TABLE alerts (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        transition_id  INTEGER NOT NULL UNIQUE REFERENCES transitions(id),
        task_id        TEXT NOT NULL REFERENCES tasks(id),
        kind           TEXT NOT NULL,
        text           TEXT NOT NULL,
        created_at     TEXT NOT NULL,
        sent_at        TEXT,
        tries          INTEGER NOT NULL DEFAULT 0,
        last_error     TEXT,
        dropped_at     TEXT
    );
    CREATE INDEX alerts_pending ON alerts(sent_at, dropped_at, id);

    -- Small named values Nightshift keeps between daemon runs (e.g. the
    -- last transition alerts were made for, the Telegram update offset).
    CREATE TABLE meta (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );
    """,
    # 10: paid-per-use spend (§1.5).
    """
    -- One row per paid model call (the Gemini API path), priced by
    -- Nightshift from its tokens. Subscription agents never write here.
    CREATE TABLE spend_events (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id            TEXT NOT NULL REFERENCES tasks(id),
        run_id             INTEGER REFERENCES runs(id),
        agent              TEXT NOT NULL,
        model              TEXT,
        at                 TEXT NOT NULL,
        input_tokens       INTEGER NOT NULL,
        cached_tokens      INTEGER NOT NULL,
        output_tokens      INTEGER NOT NULL,
        thoughts_tokens    INTEGER NOT NULL,
        cost_usd           REAL NOT NULL,
        raw                TEXT
    );
    CREATE INDEX spend_events_task ON spend_events(task_id, id);
    CREATE INDEX spend_events_at ON spend_events(at);
    """,
    # 11: paid-credit decisions (§1.5).
    """
    -- One row per decision of the paid-credit rule about a Task whose own
    -- subscription (agent) is out of usage: use_paid 1 moved it to the paid
    -- path (to_agent), 0 kept it waiting. rule: a short slug of why
    -- (urgent, after_window, failed_over, ...). task_changed_at: the Task's
    -- state_changed_at then; a waiting Task's decision is recorded again
    -- only when its verdict or rule changes, or the Task changed state.
    CREATE TABLE paid_credit_decisions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id    TEXT NOT NULL REFERENCES tasks(id),
        at         TEXT NOT NULL,
        agent      TEXT NOT NULL,
        to_agent   TEXT,
        use_paid   INTEGER NOT NULL,
        rule       TEXT NOT NULL,
        reason     TEXT NOT NULL,
        task_changed_at TEXT NOT NULL
    );
    CREATE INDEX paid_credit_decisions_task ON paid_credit_decisions(task_id, id);
    """,
    # 12: the 8am Telegram digest (§2.12).
    """
    -- One row per morning digest; digest_date (the local date of the 8am it
    -- belongs to) is unique, so a digest is never made twice. It covers the
    -- transitions after the previous digest's through_transition_id up to
    -- its own. Delivery columns as in alerts; `text` is already redacted.
    CREATE TABLE digests (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        digest_date            TEXT NOT NULL UNIQUE,
        since                  TEXT NOT NULL,
        through_transition_id  INTEGER NOT NULL,
        text                   TEXT NOT NULL,
        created_at             TEXT NOT NULL,
        sent_at                TEXT,
        tries                  INTEGER NOT NULL DEFAULT 0,
        last_error             TEXT,
        dropped_at             TEXT
    );
    """,
    # 13: manual interventions with no other record (Gate 2's clean week).
    """
    -- Commands that change things by hand and leave no transition, approval
    -- or Schedule decision behind: orch priority, orch schedule
    -- enable|disable|remove, orch raise-cap. The clean-week check lists them.
    CREATE TABLE interventions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        at             TEXT NOT NULL,
        command        TEXT NOT NULL,
        task_id        TEXT,
        schedule_name  TEXT,
        detail         TEXT NOT NULL
    );
    CREATE INDEX interventions_at ON interventions(at);
    """,
    # 14: the surface a pending cancel or pause came from (nightshift.control).
    """
    -- `orch cancel`, `telegram` or `dashboard`: named in the transition the
    -- daemon writes when it stops or pauses the running Attempt.
    ALTER TABLE tasks ADD COLUMN cancel_requested_via TEXT;
    ALTER TABLE tasks ADD COLUMN pause_requested_via TEXT;
    """,
    # 15: free-form Tasks proposed from Telegram, waiting for a button (§5 item 1).
    """
    -- The estimate and Task type a Telegram `add` proposed, held until you
    -- tap Create or Don't create. `draft` is control.Draft as JSON; decision
    -- (created, declined, failed) is set once, so a second or stale tap
    -- only reports it. task_id is the Task a `created` draft made.
    CREATE TABLE telegram_drafts (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at  TEXT NOT NULL,
        draft       TEXT NOT NULL,
        decision    TEXT,
        decided_at  TEXT,
        task_id     TEXT REFERENCES tasks(id)
    );
    """,
    # 16: decisions on an agent's "needs you" request (§1.6, §2.10).
    """
    -- What an approvals row decides: a Task's plan (awaiting_approval) or
    -- an agent's request (needs_you.json). A request row keeps the
    -- request's kind, the SHA-256 of needs_you.json as it was decided, the
    -- copy kept in the Task's logs (relative to its folder) and the Attempt
    -- that raised it. Approved or denied, the Task is requeued.
    ALTER TABLE approvals ADD COLUMN subject TEXT NOT NULL DEFAULT 'plan'
        CHECK (subject IN ('plan', 'request'));
    ALTER TABLE approvals ADD COLUMN request_kind TEXT;
    ALTER TABLE approvals ADD COLUMN request_sha256 TEXT;
    ALTER TABLE approvals ADD COLUMN request_file TEXT;
    ALTER TABLE approvals ADD COLUMN run_id INTEGER REFERENCES runs(id);
    -- The request decision the Task's next Attempt is told (approvals.id);
    -- cleared when that Attempt starts.
    ALTER TABLE tasks ADD COLUMN request_decision_id INTEGER REFERENCES approvals(id);
    """,
]

#: Task columns ``transition(set_fields=...)`` may change with the state.
TRANSITION_FIELDS = frozenset({
    "retries_used", "retry_at", "cancel_requested_at", "approved", "approved_at",
    "simulated_limit_at", "pause_kind", "pause_limit_type", "pause_reset_at", "resume_at",
    "resume_pending", "pause_requested_at", "agent", "request_decision_id",
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

    def request_cancel(self, task_id: str, via: str = "orch cancel") -> None:
        """Ask the daemon to cancel a running Task (see ``nightshift.failures``).
        The first request's time and surface (``via``) stand."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET cancel_requested_via = CASE WHEN cancel_requested_at IS NULL "
                "THEN ? ELSE cancel_requested_via END, "
                "cancel_requested_at = COALESCE(cancel_requested_at, ?) WHERE id = ?",
                (via, self._now(), task_id),
            )

    def request_pause(self, task_id: str, via: str = "orch pause") -> None:
        """Ask the daemon to pause a running Task after its current Step."""
        with self.tx() as c:
            c.execute(
                "UPDATE tasks SET pause_requested_via = CASE WHEN pause_requested_at IS NULL "
                "THEN ? ELSE pause_requested_via END, "
                "pause_requested_at = COALESCE(pause_requested_at, ?) WHERE id = ?",
                (via, self._now(), task_id),
            )

    def clear_pause_request(self, task_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE tasks SET pause_requested_at = NULL, pause_requested_via = NULL "
                      "WHERE id = ?", (task_id,))

    def request_via(self, task_id: str, kind: str) -> str:
        """The surface a pending ``cancel`` or ``pause`` request came from."""
        row = self.conn.execute(
            f"SELECT {kind}_requested_via FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return (row[0] if row is not None else None) or f"orch {kind}"

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

    def decide_request(
        self,
        task_id: str,
        *,
        approved: bool,
        via: str,
        reason: str,
        run_id: int,
        kind: str,
        request_sha256: str,
        request_file: str,
    ) -> int:
        """Record a decision on an agent's request and requeue the Task, all
        in one transaction: the Task must still be in ``needs_you`` on that
        request (its last Attempt is ``run_id``), else :class:`StateChanged`.
        The next Attempt continues the work (``resume_pending``) and is told
        the decision (``request_decision_id``). Returns the approvals id."""
        with self.tx() as c:
            row = c.execute("SELECT state FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            current = TaskState(row["state"])
            last = c.execute("SELECT MAX(id) FROM runs WHERE task_id = ?", (task_id,)).fetchone()[0]
            if current != TaskState.NEEDS_YOU or last != run_id:
                raise StateChanged(f"Task {task_id} is {current}; its request was already decided")
            check_transition(current, TaskState.QUEUED)
            at = self._now()
            cur = c.execute(
                "INSERT INTO approvals (task_id, decision, at, via, subject, request_kind, "
                "request_sha256, request_file, run_id) VALUES (?, ?, ?, ?, 'request', ?, ?, ?, ?)",
                (task_id, "approved" if approved else "denied", at, via, kind, request_sha256,
                 request_file, run_id),
            )
            decision_id = int(cur.lastrowid)
            c.execute(
                "UPDATE tasks SET state = ?, state_changed_at = ?, resume_pending = 1, "
                "request_decision_id = ? WHERE id = ?",
                (str(TaskState.QUEUED), at, decision_id, task_id),
            )
            c.execute(
                "INSERT INTO transitions (task_id, from_state, to_state, at, reason, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (task_id, str(current), str(TaskState.QUEUED), at, reason, run_id),
            )
        return decision_id

    def approval(self, approval_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()

    def approvals(self, task_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM approvals WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    def usage_for_task(self, task_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM usage_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()

    def attempt_costs(self) -> list[sqlite3.Row]:
        """Each finished Attempt with a reported cost: its agent, its Task's
        template and Task type, when it started and ended, and the reported
        cost-equivalent it used (for the tonight's-queue estimate)."""
        return self.conn.execute(
            "SELECT r.agent, t.template, t.task_type, r.started_at, r.ended_at, "
            "SUM(u.cost_usd) AS cost_usd FROM runs r "
            "JOIN tasks t ON t.id = r.task_id JOIN usage_events u ON u.run_id = r.id "
            "WHERE r.ended_at IS NOT NULL AND u.cost_usd IS NOT NULL "
            "GROUP BY r.id ORDER BY r.id"
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

    # -- Schedules (§2.5) -------------------------------------------------------
    SCHEDULE_COLUMNS = ("name template request cron catch_up catch_up_hours overlap priority "
                        "agent enabled created_at updated_at last_fire_at").split()

    def insert_schedule(self, fields: dict[str, Any]) -> None:
        row = {k: fields.get(k) for k in self.SCHEDULE_COLUMNS}
        row["enabled"] = int(bool(row["enabled"]))
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.tx() as c:
            c.execute(f"INSERT INTO schedules ({cols}) VALUES ({marks})", list(row.values()))

    def get_schedule(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM schedules WHERE name = ?", (name,)).fetchone()

    def schedules(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        sql = "SELECT * FROM schedules" + (" WHERE enabled = 1" if enabled_only else "")
        return self.conn.execute(sql + " ORDER BY name").fetchall()

    def delete_schedule(self, name: str) -> bool:
        with self.tx() as c:
            return c.execute("DELETE FROM schedules WHERE name = ?", (name,)).rowcount == 1

    def set_schedule_enabled(self, name: str, enabled: bool, *, at: str,
                             last_fire_at: str | None = None) -> None:
        with self.tx() as c:
            cur = c.execute(
                "UPDATE schedules SET enabled = ?, updated_at = ?, "
                "last_fire_at = COALESCE(?, last_fire_at) WHERE name = ?",
                (int(enabled), at, last_fire_at, name))
            if cur.rowcount != 1:
                raise KeyError(name)

    def claim_schedule_fires(self, name: str, *, expect_last: str, through: str,
                             checked_at: str) -> bool:
        """Mark every fire up to ``through`` handled, if no one else did
        meanwhile (``last_fire_at`` still ``expect_last``). Done *before* the
        Occurrence is created, so a crash can lose a fire but never repeat it."""
        with self.tx() as c:
            return c.execute(
                "UPDATE schedules SET last_fire_at = ?, last_checked_at = ? "
                "WHERE name = ? AND last_fire_at = ?",
                (through, checked_at, name, expect_last)).rowcount == 1

    def touch_schedule(self, name: str, checked_at: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE schedules SET last_checked_at = ? WHERE name = ?",
                      (checked_at, name))

    def record_schedule_run(self, *, schedule_name: str, trigger: str, fire_at: str, fires: int,
                            decided_at: str, outcome: str, task_id: str | None,
                            detail: str | None) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO schedule_runs (schedule_name, trigger, fire_at, fires, decided_at, "
                "outcome, task_id, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (schedule_name, trigger, fire_at, fires, decided_at, outcome, task_id, detail))
            return int(cur.lastrowid)

    def occurrence_fire_at(self, task_id: str) -> str | None:
        """The fire time of the Schedule decision that created this Occurrence."""
        row = self.conn.execute(
            "SELECT fire_at FROM schedule_runs WHERE task_id = ? AND outcome IN "
            "('created', 'replaced') ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        return row["fire_at"] if row else None

    def schedule_runs(self, name: str | None = None) -> list[sqlite3.Row]:
        if name is None:
            return self.conn.execute("SELECT * FROM schedule_runs ORDER BY id").fetchall()
        return self.conn.execute(
            "SELECT * FROM schedule_runs WHERE schedule_name = ? ORDER BY id", (name,)).fetchall()

    def occurrences(self, schedule_name: str) -> list[sqlite3.Row]:
        """Tasks a Schedule created, oldest first."""
        return self.conn.execute(
            "SELECT * FROM tasks WHERE schedule_name = ? ORDER BY created_at, rowid",
            (schedule_name,)).fetchall()

    # -- Tracker merges (§1.7) ---------------------------------------------------
    def record_tracker_merge(self, task_id: str, *, run_id: int | None, at: str,
                             tracker_path: str, proposed: int, appended: int, duplicates: int,
                             invalid: int, result: dict[str, Any]) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO tracker_merges (task_id, run_id, at, tracker_path, proposed, "
                "appended, duplicates, invalid, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, run_id, at, tracker_path, proposed, appended, duplicates, invalid,
                 json.dumps(result)))
            return int(cur.lastrowid)

    def tracker_merges(self, task_id: str) -> list[sqlite3.Row]:
        """A Task's Tracker merges, oldest first."""
        return self.conn.execute("SELECT * FROM tracker_merges WHERE task_id = ? ORDER BY id",
                                 (task_id,)).fetchall()

    # -- Failovers (§2.7, §2.8) -------------------------------------------------
    def record_failover(self, task_id: str, *, run_id: int | None, at: str, from_agent: str,
                        to_agent: str, trigger: str, deadline_driven: bool, reason: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO failovers (task_id, run_id, at, from_agent, to_agent, trigger, "
                "deadline_driven, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, run_id, at, from_agent, to_agent, trigger, int(deadline_driven), reason))
            return int(cur.lastrowid)

    def set_agent(self, task_id: str, agent: str, *, expect: TaskState) -> bool:
        """Move a Task that stays in ``expect`` (e.g. queued) to another agent."""
        with self.tx() as c:
            return c.execute("UPDATE tasks SET agent = ? WHERE id = ? AND state = ?",
                             (agent, task_id, str(expect))).rowcount == 1

    def failovers(self, task_id: str) -> list[sqlite3.Row]:
        """A Task's agent switches, oldest first."""
        return self.conn.execute("SELECT * FROM failovers WHERE task_id = ? ORDER BY id",
                                 (task_id,)).fetchall()

    def record_paid_credit_decision(self, task_id: str, *, at: str, agent: str,
                                    to_agent: str | None, use_paid: bool, rule: str,
                                    reason: str, task_changed_at: str) -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO paid_credit_decisions (task_id, at, agent, to_agent, use_paid, "
                "rule, reason, task_changed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, at, agent, to_agent, int(use_paid), rule, reason, task_changed_at))
            return int(cur.lastrowid)

    def paid_credit_decisions(self, task_id: str) -> list[sqlite3.Row]:
        """A Task's paid-credit decisions (§1.5), oldest first."""
        return self.conn.execute(
            "SELECT * FROM paid_credit_decisions WHERE task_id = ? ORDER BY id",
            (task_id,)).fetchall()

    # -- Spend (§1.5) -------------------------------------------------------------
    def record_spend(self, task_id: str, *, run_id: int | None, agent: str, model: str | None,
                     at: str, input_tokens: int, cached_tokens: int, output_tokens: int,
                     thoughts_tokens: int, cost_usd: float, raw: dict[str, Any] | None) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT INTO spend_events (task_id, run_id, agent, model, at, input_tokens, "
                "cached_tokens, output_tokens, thoughts_tokens, cost_usd, raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, run_id, agent, model, at, input_tokens, cached_tokens, output_tokens,
                 thoughts_tokens, cost_usd, json.dumps(raw) if raw is not None else None))

    def spend_events(self, task_id: str | None = None) -> list[sqlite3.Row]:
        if task_id is None:
            return self.conn.execute("SELECT * FROM spend_events ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM spend_events WHERE task_id = ? ORDER BY id",
                                 (task_id,)).fetchall()

    def task_spend(self, task_id: str) -> float:
        """Dollars a Task has spent on paid calls, all its Attempts."""
        return float(self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) FROM spend_events WHERE task_id = ?",
            (task_id,)).fetchone()[0])

    def task_spend_by_agent(self, task_id: str) -> dict[str, float]:
        """Dollars a Task has spent on paid calls, per paid agent."""
        return {r[0]: float(r[1]) for r in self.conn.execute(
            "SELECT agent, SUM(cost_usd) FROM spend_events WHERE task_id = ? GROUP BY agent",
            (task_id,))}

    # -- Telegram alerts (§2.12) --------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def transitions_after(self, transition_id: int) -> list[sqlite3.Row]:
        """Every Task's transitions with an id above ``transition_id``, oldest first."""
        return self.conn.execute("SELECT * FROM transitions WHERE id > ? ORDER BY id",
                                 (transition_id,)).fetchall()

    def last_transition_id(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(id), 0) FROM transitions").fetchone()[0])

    def insert_alert(self, *, transition_id: int, task_id: str, kind: str, text: str,
                     at: str) -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO alerts (transition_id, task_id, kind, text, "
                      "created_at) VALUES (?, ?, ?, ?, ?)",
                      (transition_id, task_id, kind, text, at))

    def pending_alerts(self) -> list[sqlite3.Row]:
        """Alerts not yet sent or dropped, oldest first."""
        return self.conn.execute("SELECT * FROM alerts WHERE sent_at IS NULL AND dropped_at "
                                 "IS NULL ORDER BY id").fetchall()

    def alerts(self, task_id: str | None = None) -> list[sqlite3.Row]:
        if task_id is None:
            return self.conn.execute("SELECT * FROM alerts ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM alerts WHERE task_id = ? ORDER BY id",
                                 (task_id,)).fetchall()

    def mark_alert(self, alert_id: int, *, sent_at: str | None = None,
                   error: str | None = None, dropped_at: str | None = None) -> None:
        """Record one delivery try: sent, failed with ``error``, or dropped."""
        with self.tx() as c:
            c.execute("UPDATE alerts SET tries = tries + ?, sent_at = COALESCE(?, sent_at), "
                      "last_error = COALESCE(?, last_error), "
                      "dropped_at = COALESCE(?, dropped_at) WHERE id = ?",
                      (0 if dropped_at else 1, sent_at, error, dropped_at, alert_id))

    # -- Telegram digest (§2.12) ---------------------------------------------------
    def insert_digest(self, *, digest_date: str, since: str, through_transition_id: int,
                      text: str, at: str) -> bool:
        """Store the digest for ``digest_date``; False if it already exists."""
        with self.tx() as c:
            cur = c.execute("INSERT OR IGNORE INTO digests (digest_date, since, "
                            "through_transition_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
                            (digest_date, since, through_transition_id, text, at))
            return cur.rowcount == 1

    def last_digest(self) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM digests ORDER BY digest_date DESC LIMIT 1").fetchone()

    def digests(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM digests ORDER BY digest_date").fetchall()

    def pending_digests(self) -> list[sqlite3.Row]:
        """Digests not yet sent or dropped, oldest first."""
        return self.conn.execute("SELECT * FROM digests WHERE sent_at IS NULL AND dropped_at "
                                 "IS NULL ORDER BY digest_date").fetchall()

    def mark_digest(self, digest_id: int, *, sent_at: str | None = None,
                    error: str | None = None, dropped_at: str | None = None) -> None:
        """Record one delivery try: sent, failed with ``error``, or dropped."""
        with self.tx() as c:
            c.execute("UPDATE digests SET tries = tries + ?, sent_at = COALESCE(?, sent_at), "
                      "last_error = COALESCE(?, last_error), "
                      "dropped_at = COALESCE(?, dropped_at) WHERE id = ?",
                      (0 if dropped_at else 1, sent_at, error, dropped_at, digest_id))

    # -- Telegram drafts (§5 item 1) ------------------------------------------------
    def insert_telegram_draft(self, draft: str, *, at: str) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO telegram_drafts (created_at, draft) VALUES (?, ?)",
                            (at, draft))
            return int(cur.lastrowid)

    def telegram_draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM telegram_drafts WHERE id = ?",
                                 (draft_id,)).fetchone()

    def decide_telegram_draft(self, draft_id: int, decision: str, *, at: str) -> bool:
        """Set the draft's decision; False if it already had one (a second tap)."""
        with self.tx() as c:
            cur = c.execute("UPDATE telegram_drafts SET decision = ?, decided_at = ? "
                            "WHERE id = ? AND decision IS NULL", (decision, at, draft_id))
            return cur.rowcount == 1

    def finish_telegram_draft(self, draft_id: int, *, decision: str,
                              task_id: str | None = None) -> None:
        """After a claimed decision: the Task it made, or that making it failed."""
        with self.tx() as c:
            c.execute("UPDATE telegram_drafts SET decision = ?, task_id = ? WHERE id = ?",
                      (decision, task_id, draft_id))

    # -- Interventions (Gate 2) -----------------------------------------------------
    def record_intervention(self, *, at: str, command: str, detail: str,
                            task_id: str | None = None,
                            schedule_name: str | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO interventions (at, command, task_id, schedule_name, detail) "
                      "VALUES (?, ?, ?, ?, ?)", (at, command, task_id, schedule_name, detail))

    def interventions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM interventions ORDER BY id").fetchall()

    def all_transitions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM transitions ORDER BY id").fetchall()

    def all_attempts(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY id").fetchall()
