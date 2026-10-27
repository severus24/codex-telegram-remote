from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    chat_id INTEGER NOT NULL,
    repo TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, repo)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    update_id INTEGER NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    repo TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    codex_session_id TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS jobs_status_id ON jobs(status, id);

CREATE TABLE IF NOT EXISTS watched_tasks (
    thread_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    repo TEXT NOT NULL,
    title TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    notified_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, chat_id)
);

CREATE INDEX IF NOT EXISTS watched_tasks_pending
    ON watched_tasks(notified_at, created_at);
"""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    update_id: int
    chat_id: int
    user_id: int
    prompt: str
    repo: str
    status: str
    codex_session_id: str
    result: str
    error: str
    created_at: str
    started_at: str
    finished_at: str


@dataclass(frozen=True, slots=True)
class WatchedTask:
    thread_id: str
    chat_id: int
    repo: str
    title: str
    started_at: int
    outcome: str
    created_at: str
    notified_at: str


class RemoteStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    error = '服务重启，任务未正常结束；请重新发送',
                    finished_at = ?
                WHERE status = 'running'
                """,
                (utc_now(),),
            )

    def get_state(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO state(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def set_chat_repo(self, chat_id: int, repo: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chat_settings(chat_id, repo, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    repo = excluded.repo,
                    updated_at = excluded.updated_at
                """,
                (chat_id, repo, utc_now()),
            )

    def get_chat_repo(self, chat_id: int, default: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT repo FROM chat_settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return str(row["repo"]) if row else default

    def save_session(self, chat_id: int, repo: str, session_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(chat_id, repo, session_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id, repo) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (chat_id, repo, session_id, utc_now()),
            )

    def get_session(self, chat_id: int, repo: str) -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT session_id FROM sessions WHERE chat_id = ? AND repo = ?",
                (chat_id, repo),
            ).fetchone()
        return str(row["session_id"]) if row else ""

    def clear_session(self, chat_id: int, repo: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM sessions WHERE chat_id = ? AND repo = ?",
                (chat_id, repo),
            )
        return bool(cursor.rowcount)

    def enqueue_job(
        self,
        *,
        update_id: int,
        chat_id: int,
        user_id: int,
        prompt: str,
        repo: str,
    ) -> Job:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO jobs(
                    update_id, chat_id, user_id, prompt, repo,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """,
                (update_id, chat_id, user_id, prompt, repo, utc_now()),
            )
            row = connection.execute(
                "SELECT * FROM jobs WHERE update_id = ?", (update_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("任务写入失败")
        return _job_from_row(row)

    def get_job(self, job_id: int) -> Job | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return _job_from_row(row) if row else None

    def queued_job_ids(self) -> list[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' ORDER BY id"
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def queued_count(self, chat_id: int | None = None) -> int:
        query = "SELECT COUNT(*) FROM jobs WHERE status = 'queued'"
        args: tuple[int, ...] = ()
        if chat_id is not None:
            query += " AND chat_id = ?"
            args = (chat_id,)
        with self.connect() as connection:
            return int(connection.execute(query, args).fetchone()[0])

    def claim_job(self, job_id: int) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (utc_now(), job_id),
            )
        return bool(cursor.rowcount)

    def finish_job(
        self,
        job_id: int,
        *,
        status: str,
        result: str = "",
        error: str = "",
        session_id: str = "",
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, result = ?, error = ?,
                    codex_session_id = CASE
                        WHEN ? = '' THEN codex_session_id ELSE ?
                    END,
                    finished_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    result,
                    error[:4000],
                    session_id,
                    session_id,
                    utc_now(),
                    job_id,
                ),
            )

    def cancel_queued_jobs(self, chat_id: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'canceled', error = '用户取消',
                    finished_at = ?
                WHERE chat_id = ? AND status = 'queued'
                """,
                (utc_now(), chat_id),
            )
        return int(cursor.rowcount)

    def watch_task(
        self,
        *,
        thread_id: str,
        chat_id: int,
        repo: str,
        title: str,
        started_at: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO watched_tasks(
                    thread_id, chat_id, repo, title, started_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(thread_id, chat_id) DO UPDATE SET
                    repo = excluded.repo,
                    title = excluded.title,
                    started_at = excluded.started_at,
                    outcome = '',
                    created_at = excluded.created_at,
                    notified_at = ''
                """,
                (
                    thread_id,
                    chat_id,
                    repo,
                    title,
                    started_at,
                    utc_now(),
                ),
            )

    def pending_watched_tasks(self) -> list[WatchedTask]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM watched_tasks
                WHERE notified_at = ''
                ORDER BY created_at, thread_id, chat_id
                """
            ).fetchall()
        return [_watched_task_from_row(row) for row in rows]

    def watched_thread_ids(self, chat_id: int) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT thread_id
                FROM watched_tasks
                WHERE chat_id = ? AND notified_at = ''
                """,
                (chat_id,),
            ).fetchall()
        return {str(row["thread_id"]) for row in rows}

    def mark_watched_task_notified(
        self,
        *,
        thread_id: str,
        chat_id: int,
        outcome: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE watched_tasks
                SET outcome = ?, notified_at = ?
                WHERE thread_id = ? AND chat_id = ?
                """,
                (outcome, utc_now(), thread_id, chat_id),
            )


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=int(row["id"]),
        update_id=int(row["update_id"]),
        chat_id=int(row["chat_id"]),
        user_id=int(row["user_id"]),
        prompt=str(row["prompt"]),
        repo=str(row["repo"]),
        status=str(row["status"]),
        codex_session_id=str(row["codex_session_id"]),
        result=str(row["result"]),
        error=str(row["error"]),
        created_at=str(row["created_at"]),
        started_at=str(row["started_at"]),
        finished_at=str(row["finished_at"]),
    )


def _watched_task_from_row(row: sqlite3.Row) -> WatchedTask:
    return WatchedTask(
        thread_id=str(row["thread_id"]),
        chat_id=int(row["chat_id"]),
        repo=str(row["repo"]),
        title=str(row["title"]),
        started_at=int(row["started_at"]),
        outcome=str(row["outcome"]),
        created_at=str(row["created_at"]),
        notified_at=str(row["notified_at"]),
    )
