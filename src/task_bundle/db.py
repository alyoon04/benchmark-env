"""SQLite persistence: every CLI invocation, run, test result, and artifact path.

Thin repository layer over stdlib sqlite3 (DESIGN.md §6). Artifacts live on disk
under ``<artifacts-dir>/<command-id>/``; the DB stores paths, never blobs. Ids are
time-ordered (millisecond hex timestamp + random suffix) so default sort is
chronological.
"""

import secrets
import sqlite3
import time
from pathlib import Path

from task_bundle.grading import TestExecution

_SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS commands (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  argv        TEXT NOT NULL,
  bundle_path TEXT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  exit_code   INTEGER,
  log_path    TEXT
);

CREATE TABLE IF NOT EXISTS runs (
  id            TEXT PRIMARY KEY,
  command_id    TEXT NOT NULL REFERENCES commands(id),
  task_id       TEXT NOT NULL,
  solver        TEXT NOT NULL,
  model         TEXT,
  verdict       TEXT,
  started_at    TEXT NOT NULL,
  finished_at   TEXT,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  image_tag     TEXT,
  image_digest  TEXT,
  tool_versions TEXT
);

CREATE TABLE IF NOT EXISTS test_results (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id     TEXT REFERENCES runs(id),
  command_id TEXT NOT NULL REFERENCES commands(id),
  test_name  TEXT NOT NULL,
  bucket     TEXT NOT NULL CHECK (bucket IN ('fail2pass','pass2pass')),
  phase      TEXT NOT NULL CHECK (phase IN ('baseline','post_solver','post_gold')),
  attempt    INTEGER NOT NULL DEFAULT 1,
  status     TEXT NOT NULL CHECK (status IN ('passed','failed','error','timeout')),
  duration_seconds REAL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  command_id TEXT NOT NULL REFERENCES commands(id),
  run_id     TEXT REFERENCES runs(id),
  type       TEXT NOT NULL,
  path       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_test_results_command ON test_results(command_id);
CREATE INDEX IF NOT EXISTS idx_test_results_run ON test_results(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_command ON artifacts(command_id);
"""

Phase = str  # 'baseline' | 'post_solver' | 'post_gold'


def new_id(prefix: str) -> str:
    """Time-ordered unique id, e.g. cmd_018f3c2a1b4-9f2e6c1d."""
    return f"{prefix}_{int(time.time() * 1000):011x}-{secrets.token_hex(4)}"


class Database:
    """Repository over the task-bundle SQLite database (one connection per CLI run)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    # -- commands ------------------------------------------------------------

    def insert_command(
        self,
        command_id: str,
        name: str,
        argv: str,
        bundle_path: str | None,
        started_at: str,
        log_path: str | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO commands (id, name, argv, bundle_path, started_at, log_path)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (command_id, name, argv, bundle_path, started_at, log_path),
        )
        self._conn.commit()

    def finish_command(self, command_id: str, exit_code: int, finished_at: str) -> None:
        self._conn.execute(
            "UPDATE commands SET exit_code = ?, finished_at = ? WHERE id = ?",
            (exit_code, finished_at, command_id),
        )
        self._conn.commit()

    def get_command(self, command_id: str) -> sqlite3.Row | None:
        cur = self._conn.execute("SELECT * FROM commands WHERE id = ?", (command_id,))
        row: sqlite3.Row | None = cur.fetchone()
        return row

    def recent_commands(self, limit: int = 20) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM commands ORDER BY id DESC LIMIT ?", (limit,))
        return list(cur.fetchall())

    # -- runs ------------------------------------------------------------------

    def insert_run(
        self,
        run_id: str,
        command_id: str,
        task_id: str,
        solver: str,
        model: str | None,
        started_at: str,
        image_tag: str | None,
        image_digest: str | None,
        tool_versions: str,
    ) -> None:
        self._conn.execute(
            "INSERT INTO runs (id, command_id, task_id, solver, model, started_at,"
            " image_tag, image_digest, tool_versions) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                command_id,
                task_id,
                solver,
                model,
                started_at,
                image_tag,
                image_digest,
                tool_versions,
            ),
        )
        self._conn.commit()

    def finish_run(
        self,
        run_id: str,
        verdict: str,
        finished_at: str,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        self._conn.execute(
            "UPDATE runs SET verdict = ?, finished_at = ?, input_tokens = ?,"
            " output_tokens = ?, cost_usd = ? WHERE id = ?",
            (verdict, finished_at, input_tokens, output_tokens, cost_usd, run_id),
        )
        self._conn.commit()

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        cur = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row: sqlite3.Row | None = cur.fetchone()
        return row

    def list_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        return list(cur.fetchall())

    # -- test results ------------------------------------------------------------

    def record_test_results(
        self,
        command_id: str,
        phase: Phase,
        executions: list[TestExecution],
        run_id: str | None = None,
    ) -> None:
        self._conn.executemany(
            "INSERT INTO test_results (run_id, command_id, test_name, bucket, phase,"
            " attempt, status, duration_seconds) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id,
                    command_id,
                    e.test,
                    e.bucket,
                    phase,
                    e.attempt,
                    e.status,
                    e.duration_seconds,
                )
                for e in executions
            ],
        )
        self._conn.commit()

    def test_results_for(
        self, *, command_id: str | None = None, run_id: str | None = None
    ) -> list[sqlite3.Row]:
        if command_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM test_results WHERE command_id = ? ORDER BY test_name, attempt",
                (command_id,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM test_results WHERE run_id = ? ORDER BY phase, test_name, attempt",
                (run_id,),
            )
        return list(cur.fetchall())

    # -- artifacts ---------------------------------------------------------------

    def add_artifact(
        self, command_id: str, type_: str, path: str, run_id: str | None = None
    ) -> None:
        self._conn.execute(
            "INSERT INTO artifacts (command_id, run_id, type, path) VALUES (?, ?, ?, ?)",
            (command_id, run_id, type_, path),
        )
        self._conn.commit()

    def artifacts_for(self, command_id: str) -> list[sqlite3.Row]:
        cur = self._conn.execute(
            "SELECT * FROM artifacts WHERE command_id = ? ORDER BY id", (command_id,)
        )
        return list(cur.fetchall())
