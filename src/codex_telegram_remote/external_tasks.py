from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


LOGGER = logging.getLogger("codex_telegram_remote.external_tasks")


@dataclass(frozen=True, slots=True)
class ExternalTask:
    thread_id: str
    title: str
    cwd: str
    started_at: int
    updated_at: int

    def elapsed_text(self, now: int) -> str:
        elapsed = max(0, now - self.started_at)
        if elapsed < 60:
            return f"{elapsed} 秒"
        if elapsed < 3600:
            return f"{elapsed // 60} 分钟"
        return f"{elapsed // 3600} 小时 {(elapsed % 3600) // 60} 分钟"


@dataclass(frozen=True, slots=True)
class ExternalTaskState:
    tasks: tuple[ExternalTask, ...]
    available: bool
    error: str = ""


@dataclass(frozen=True, slots=True)
class TurnState:
    thread_id: str
    turn_id: str
    status: str
    started_at: int | None
    completed_at: int | None
    user_text: str = ""
    result_text: str = ""
    error_text: str = ""


@dataclass(frozen=True, slots=True)
class RecentTask:
    thread_id: str
    title: str
    prompt: str
    result: str
    status: str
    updated_at: int


@dataclass(frozen=True, slots=True)
class RecentTaskState:
    tasks: tuple[RecentTask, ...]
    available: bool
    error: str = ""


class ExternalTaskMonitor:
    def __init__(
        self,
        *,
        state_db: Path,
        thread_history_db: Path,
        active_window_seconds: int,
        now: Callable[[], float] = time.time,
    ):
        self.state_db = state_db
        self.thread_history_db = thread_history_db
        self.active_window_seconds = active_window_seconds
        self.now = now

    def running_for(self, repo_path: Path) -> ExternalTaskState:
        if not self.state_db.is_file() or not self.thread_history_db.is_file():
            return ExternalTaskState(
                tasks=(),
                available=False,
                error="Codex App 状态数据库不可用",
            )
        now = int(self.now())
        cutoff = now - self.active_window_seconds
        try:
            active_turns = self._latest_active_turns(cutoff)
            if not active_turns:
                return ExternalTaskState(tasks=(), available=True)
            tasks = self._threads_for_cwd(repo_path, active_turns, cutoff)
        except sqlite3.Error as error:
            LOGGER.warning("failed to read external Codex task state: %s", error)
            return ExternalTaskState(
                tasks=(),
                available=False,
                error=f"Codex App 状态读取失败: {error}",
            )
        return ExternalTaskState(tasks=tuple(tasks), available=True)

    def latest_turn(self, thread_id: str) -> TurnState | None:
        if not self.thread_history_db.is_file():
            return None
        connection = _readonly_connection(self.thread_history_db)
        try:
            row = connection.execute(
                """
                SELECT
                    t.thread_id,
                    t.turn_id,
                    t.status,
                    t.started_at,
                    t.completed_at,
                    t.error_json,
                    u.item_json AS user_item_json,
                    COALESCE(
                        a.item_json,
                        (
                            SELECT latest_agent.item_json
                            FROM thread_items latest_agent
                            WHERE latest_agent.thread_id = t.thread_id
                              AND latest_agent.turn_id = t.turn_id
                              AND latest_agent.item_type = 'agentMessage'
                            ORDER BY latest_agent.rollout_ordinal DESC
                            LIMIT 1
                        )
                    ) AS result_item_json
                FROM thread_turns t
                LEFT JOIN thread_items u
                  ON u.thread_id = t.thread_id
                 AND u.turn_id = t.turn_id
                 AND u.item_id = t.first_user_item_id
                LEFT JOIN thread_items a
                  ON a.thread_id = t.thread_id
                 AND a.turn_id = t.turn_id
                 AND a.item_id = t.final_agent_item_id
                WHERE t.thread_id = ?
                ORDER BY t.rollout_ordinal DESC
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return TurnState(
            thread_id=str(row["thread_id"]),
            turn_id=str(row["turn_id"]),
            status=str(row["status"]),
            started_at=(
                int(row["started_at"])
                if isinstance(row["started_at"], int)
                else None
            ),
            completed_at=(
                int(row["completed_at"])
                if isinstance(row["completed_at"], int)
                else None
            ),
            user_text=_item_text(row["user_item_json"], "userMessage"),
            result_text=_item_text(
                row["result_item_json"],
                "agentMessage",
            ),
            error_text=_error_text(row["error_json"]),
        )

    def recent_for(self, repo_path: Path, limit: int) -> RecentTaskState:
        if not self.state_db.is_file():
            return RecentTaskState(
                tasks=(),
                available=False,
                error="Codex App 状态数据库不可用",
            )
        try:
            connection = _readonly_connection(self.state_db)
            try:
                rows = connection.execute(
                    """
                    SELECT
                        id, name, title, preview, first_user_message,
                        cwd, updated_at
                    FROM threads
                    WHERE archived = 0
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (max(limit * 10, 50),),
                ).fetchall()
            finally:
                connection.close()
            tasks = self._recent_tasks_from_rows(rows, repo_path, limit)
        except sqlite3.Error as error:
            LOGGER.warning("failed to read recent Codex tasks: %s", error)
            return RecentTaskState(
                tasks=(),
                available=False,
                error=f"Codex App 会话读取失败: {error}",
            )
        return RecentTaskState(tasks=tuple(tasks), available=True)

    def resolve_thread(
        self,
        repo_path: Path,
        thread_id: str,
    ) -> RecentTask | None:
        if not self.state_db.is_file():
            return None
        connection = _readonly_connection(self.state_db)
        try:
            row = connection.execute(
                """
                SELECT
                    id, name, title, preview, first_user_message,
                    cwd, updated_at
                FROM threads
                WHERE id = ? AND archived = 0
                LIMIT 1
                """,
                (thread_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or _normal_cwd(Path(str(row["cwd"]))) != _normal_cwd(
            repo_path
        ):
            return None
        tasks = self._recent_tasks_from_rows((row,), repo_path, 1)
        return tasks[0] if tasks else None

    def _recent_tasks_from_rows(
        self,
        rows: Any,
        repo_path: Path,
        limit: int,
    ) -> list[RecentTask]:
        expected_cwd = _normal_cwd(repo_path)
        tasks: list[RecentTask] = []
        for row in rows:
            if _normal_cwd(Path(str(row["cwd"]))) != expected_cwd:
                continue
            thread_id = str(row["id"])
            title = str(
                row["name"]
                or row["title"]
                or row["first_user_message"]
                or row["preview"]
                or thread_id
            )
            prompt = str(
                row["first_user_message"]
                or row["preview"]
                or title
            )
            status = ""
            result = ""
            try:
                turn = self.latest_turn(thread_id)
            except sqlite3.Error as error:
                LOGGER.warning(
                    "failed to read latest turn thread_id=%s: %s",
                    thread_id,
                    error,
                )
                turn = None
            if turn is not None:
                status = turn.status
                prompt = turn.user_text or prompt
                result = turn.result_text
                if turn.status != "completed":
                    result = turn.error_text or result
            tasks.append(
                RecentTask(
                    thread_id=thread_id,
                    title=_shorten(title, 90),
                    prompt=_shorten(prompt, 220),
                    result=result,
                    status=status,
                    updated_at=int(row["updated_at"]),
                )
            )
            if len(tasks) >= limit:
                break
        return tasks

    def _latest_active_turns(self, cutoff: int) -> dict[str, int]:
        connection = _readonly_connection(self.thread_history_db)
        try:
            rows = connection.execute(
                """
                WITH latest AS (
                    SELECT thread_id, MAX(rollout_ordinal) AS max_ordinal
                    FROM thread_turns
                    GROUP BY thread_id
                )
                SELECT t.thread_id, t.started_at
                FROM thread_turns t
                JOIN latest l
                  ON l.thread_id = t.thread_id
                 AND l.max_ordinal = t.rollout_ordinal
                WHERE t.status = 'inProgress'
                """
            ).fetchall()
        finally:
            connection.close()
        result: dict[str, int] = {}
        for row in rows:
            started_at = row["started_at"]
            if isinstance(started_at, int) and started_at >= cutoff:
                result[str(row["thread_id"])] = started_at
        return result

    def _threads_for_cwd(
        self,
        repo_path: Path,
        active_turns: dict[str, int],
        cutoff: int,
    ) -> list[ExternalTask]:
        connection = _readonly_connection(self.state_db)
        placeholders = ",".join("?" for _ in active_turns)
        try:
            rows = connection.execute(
                f"""
                SELECT id, name, title, preview, cwd, updated_at
                FROM threads
                WHERE id IN ({placeholders})
                  AND archived = 0
                  AND updated_at >= ?
                """,
                (*active_turns.keys(), cutoff),
            ).fetchall()
        finally:
            connection.close()

        expected_cwd = _normal_cwd(repo_path)
        tasks: list[ExternalTask] = []
        for row in rows:
            if _normal_cwd(Path(str(row["cwd"]))) != expected_cwd:
                continue
            title = str(
                row["name"]
                or row["title"]
                or row["preview"]
                or row["id"]
            )
            tasks.append(
                ExternalTask(
                    thread_id=str(row["id"]),
                    title=_shorten(title, 70),
                    cwd=str(row["cwd"]),
                    started_at=active_turns[str(row["id"])],
                    updated_at=int(row["updated_at"]),
                )
            )
        tasks.sort(key=lambda task: task.updated_at, reverse=True)
        return tasks


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri_path = urllib.parse.quote(str(path), safe="/")
    connection = sqlite3.connect(
        f"file:{uri_path}?mode=ro",
        uri=True,
        timeout=2,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _normal_cwd(path: Path) -> str:
    return str(path.expanduser().resolve(strict=False))


def _item_text(value: Any, item_type: str) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict) or payload.get("type") != item_type:
        return ""
    if item_type == "agentMessage":
        return _truncate_text(str(payload.get("text", "")), 10000)
    content = payload.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = str(item.get("text", "")).strip()
            if text:
                parts.append(text)
    return _truncate_text("\n".join(parts), 10000)


def _error_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return _truncate_text(value, 10000)
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message:
            return _truncate_text(message, 10000)
    return _truncate_text(value, 10000)


def _truncate_text(value: str, limit: int) -> str:
    value = value.strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _shorten(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: limit - 1] + "…"
