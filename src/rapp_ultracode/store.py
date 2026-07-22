from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .errors import ApprovalRequired, NotFound, StateConflict
from .models import Plan

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    digest TEXT NOT NULL UNIQUE,
    plan_json TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    plan_id TEXT PRIMARY KEY REFERENCES plans(id),
    digest TEXT NOT NULL,
    approved_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL REFERENCES plans(id),
    state TEXT NOT NULL,
    worktree TEXT,
    branch TEXT,
    rdw_run_id TEXT NOT NULL,
    current_task TEXT,
    failure TEXT,
    result_json TEXT,
    worker_pid INTEGER,
    worker_started_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_state (
    run_id TEXT NOT NULL REFERENCES runs(id),
    task_id TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    commit_sha TEXT,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id)
);
CREATE TABLE IF NOT EXISTS run_leases (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    token TEXT NOT NULL UNIQUE,
    pid INTEGER NOT NULL,
    acquired_at_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    type TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run_seq ON events(run_id, seq);
"""


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        if os.name == "posix":
            os.chmod(path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self.connection.executescript(_SCHEMA)
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(runs)").fetchall()
        }
        for name, kind in (
            ("result_json", "TEXT"),
            ("worker_pid", "INTEGER"),
            ("worker_started_at_ms", "INTEGER"),
        ):
            if name not in columns:
                self.connection.execute(f"ALTER TABLE runs ADD COLUMN {name} {kind}")

    def close(self) -> None:
        self.connection.close()

    def save_plan(self, plan: Plan) -> None:
        payload = json.dumps(
            plan.model_dump(mode="json", by_alias=True),
            separators=(",", ":"),
            sort_keys=True,
        )
        now = _now_ms()
        existing = self.connection.execute(
            "SELECT digest FROM plans WHERE id = ?", (plan.plan_id,)
        ).fetchone()
        if existing and existing["digest"] != plan.digest:
            raise StateConflict(f"plan id collision: {plan.plan_id}")
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO plans(id, digest, plan_json, created_at_ms)
                VALUES (?, ?, ?, ?)
                """,
                (plan.plan_id, plan.digest, payload, now),
            )

    def get_plan(self, plan_id: str) -> Plan:
        row = self.connection.execute(
            "SELECT plan_json FROM plans WHERE id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"plan not found: {plan_id}")
        return Plan.model_validate_json(row["plan_json"])

    def list_plans(self) -> list[Plan]:
        rows = self.connection.execute(
            "SELECT plan_json FROM plans ORDER BY created_at_ms DESC"
        ).fetchall()
        return [Plan.model_validate_json(row["plan_json"]) for row in rows]

    def approve(self, plan_id: str, expected_digest: str) -> None:
        plan = self.get_plan(plan_id)
        if plan.digest != expected_digest:
            raise StateConflict("approval digest does not match the stored plan")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO approvals(plan_id, digest, approved_at_ms)
                VALUES (?, ?, ?)
                ON CONFLICT(plan_id) DO UPDATE SET
                    digest=excluded.digest,
                    approved_at_ms=excluded.approved_at_ms
                """,
                (plan_id, expected_digest, _now_ms()),
            )

    def require_approval(self, plan: Plan) -> None:
        row = self.connection.execute(
            "SELECT digest FROM approvals WHERE plan_id = ?", (plan.plan_id,)
        ).fetchone()
        if row is None or row["digest"] != plan.digest:
            raise ApprovalRequired(
                f"plan {plan.plan_id} requires approval for digest {plan.digest}"
            )

    def create_run(self, plan: Plan) -> str:
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        now = _now_ms()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO runs(
                    id, plan_id, state, rdw_run_id, created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'queued', ?, ?, ?)
                """,
                (run_id, plan.plan_id, run_id, now, now),
            )
            for task in plan.tasks:
                self.connection.execute(
                    """
                    INSERT INTO task_state(
                        run_id, task_id, state, attempt, detail_json
                    ) VALUES (?, ?, 'pending', 0, '{}')
                    """,
                    (run_id, task.id),
                )
            self._event(run_id, "run.created", {"plan_id": plan.plan_id})
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFound(f"run not found: {run_id}")
        tasks = self.connection.execute(
            "SELECT * FROM task_state WHERE run_id = ? ORDER BY rowid", (run_id,)
        ).fetchall()
        return {
            **{key: value for key, value in dict(row).items() if key != "result_json"},
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "tasks": [
                {
                    **{key: value for key, value in dict(task).items() if key != "detail_json"},
                    "detail": json.loads(task["detail_json"]),
                }
                for task in tasks
            ],
        }

    def list_runs(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM runs ORDER BY created_at_ms DESC").fetchall()
        return [
            {
                **{key: value for key, value in dict(row).items() if key != "result_json"},
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
            }
            for row in rows
        ]

    def set_run_state(
        self,
        run_id: str,
        state: str,
        *,
        worktree: str | None = None,
        branch: str | None = None,
        current_task: str | None = None,
        failure: str | None = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE runs SET
                    state=?,
                    worktree=COALESCE(?, worktree),
                    branch=COALESCE(?, branch),
                    current_task=?,
                    failure=?,
                    updated_at_ms=?
                WHERE id=?
                """,
                (
                    state,
                    worktree,
                    branch,
                    current_task,
                    failure,
                    _now_ms(),
                    run_id,
                ),
            )
            self._event(run_id, f"run.{state}", {"current_task": current_task})

    def complete_run(self, run_id: str, result: dict[str, Any]) -> None:
        encoded = json.dumps(result, separators=(",", ":"), sort_keys=True)
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runs
                SET state='succeeded', result_json=?, current_task=NULL,
                    failure=NULL, updated_at_ms=?
                WHERE id=?
                """,
                (encoded, _now_ms(), run_id),
            )
            if updated.rowcount != 1:
                raise NotFound(f"run not found: {run_id}")
            self._event(run_id, "run.succeeded", {"result": result})

    def set_worker(self, run_id: str, pid: int) -> None:
        with self.connection:
            updated = self.connection.execute(
                """
                UPDATE runs
                SET worker_pid=?, worker_started_at_ms=?, updated_at_ms=?
                WHERE id=?
                """,
                (pid, _now_ms(), _now_ms(), run_id),
            )
            if updated.rowcount != 1:
                raise NotFound(f"run not found: {run_id}")
            self._event(run_id, "worker.started", {"pid": pid})

    def acquire_lease(self, run_id: str, token: str, pid: int) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT token, pid FROM run_leases WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if existing and existing["token"] != token:
                if _pid_alive(int(existing["pid"])):
                    raise StateConflict(
                        f"run {run_id} already has active worker pid {existing['pid']}"
                    )
                self.connection.execute(
                    "DELETE FROM run_leases WHERE run_id=?",
                    (run_id,),
                )
            self.connection.execute(
                """
                INSERT OR REPLACE INTO run_leases(run_id, token, pid, acquired_at_ms)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, token, pid, _now_ms()),
            )
            self._event(run_id, "lease.acquired", {"pid": pid})
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def transfer_lease(self, run_id: str, token: str, pid: int) -> None:
        with self.connection:
            updated = self.connection.execute(
                "UPDATE run_leases SET pid=? WHERE run_id=? AND token=?",
                (pid, run_id, token),
            )
            if updated.rowcount != 1:
                raise StateConflict("run lease was lost before worker start")

    def require_lease(self, run_id: str, token: str) -> None:
        row = self.connection.execute(
            "SELECT token FROM run_leases WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None or row["token"] != token:
            raise StateConflict("run lease is not owned by this worker")

    def release_lease(self, run_id: str, token: str) -> None:
        with self.connection:
            self.connection.execute(
                "DELETE FROM run_leases WHERE run_id=? AND token=?",
                (run_id, token),
            )
            self._event(run_id, "lease.released", {})

    def begin_task(self, run_id: str, task_id: str) -> int:
        row = self.connection.execute(
            "SELECT attempt FROM task_state WHERE run_id=? AND task_id=?",
            (run_id, task_id),
        ).fetchone()
        if row is None:
            raise NotFound(f"task not found: {task_id}")
        attempt = int(row["attempt"]) + 1
        with self.connection:
            self.connection.execute(
                """
                UPDATE task_state
                SET state='running', attempt=?, detail_json='{}'
                WHERE run_id=? AND task_id=?
                """,
                (attempt, run_id, task_id),
            )
            self._event(
                run_id,
                "task.started",
                {"task_id": task_id, "attempt": attempt},
            )
        return attempt

    def ensure_task(self, run_id: str, task_id: str) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT OR IGNORE INTO task_state(
                    run_id, task_id, state, attempt, detail_json
                ) VALUES (?, ?, 'pending', 0, '{}')
                """,
                (run_id, task_id),
            )

    def finish_task(
        self,
        run_id: str,
        task_id: str,
        *,
        state: str,
        commit_sha: str | None,
        detail: dict[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE task_state
                SET state=?, commit_sha=?, detail_json=?
                WHERE run_id=? AND task_id=?
                """,
                (
                    state,
                    commit_sha,
                    json.dumps(detail, separators=(",", ":"), sort_keys=True),
                    run_id,
                    task_id,
                ),
            )
            self._event(
                run_id,
                f"task.{state}",
                {"task_id": task_id, "commit_sha": commit_sha},
            )

    def events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq",
            (run_id, after),
        ).fetchall()
        return [
            {
                **{key: value for key, value in dict(row).items() if key != "data_json"},
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    def _event(self, run_id: str, event_type: str, data: dict[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO events(run_id, type, created_at_ms, data_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                run_id,
                event_type,
                _now_ms(),
                json.dumps(data, separators=(",", ":"), sort_keys=True),
            ),
        )


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
