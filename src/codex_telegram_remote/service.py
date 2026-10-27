from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Any

from codex_telegram_remote.config import RemoteConfig
from codex_telegram_remote.external_tasks import (
    ExternalTaskMonitor,
    RecentTaskState,
)
from codex_telegram_remote.project_summary import summarize_project
from codex_telegram_remote.runner import CodexRunError, CodexRunner
from codex_telegram_remote.store import Job, RemoteStore
from codex_telegram_remote.telegram import TelegramBotAPI, TelegramError


LOGGER = logging.getLogger("codex_telegram_remote.service")


HELP_TEXT = """Codex 远程控制

直接发送普通文字：在当前仓库执行 Codex 任务
/repo <别名>：切换仓库
/repos：查看可用仓库
/new：清除当前仓库的 Codex 会话
/recent：选择当前仓库的最近任务并继续会话
/status：查看当前任务和队列
/watch：订阅当前 App 任务的完成通知
/cancel：取消当前任务及当前聊天的排队任务
/id：查看 Telegram User ID
/help：显示此帮助"""

BOT_COMMANDS = [
    {"command": "start", "description": "打开控制面板"},
    {"command": "status", "description": "查看任务与队列状态"},
    {"command": "repos", "description": "查看和切换仓库"},
    {"command": "recent", "description": "选择最近任务继续会话"},
    {"command": "watch", "description": "订阅当前任务完成通知"},
    {"command": "new", "description": "新建 Codex 会话"},
    {"command": "cancel", "description": "取消当前任务"},
    {"command": "help", "description": "查看使用说明"},
    {"command": "id", "description": "查看 Telegram ID"},
]


@dataclass(frozen=True, slots=True)
class ActiveJob:
    job_id: int
    chat_id: int
    repo: str


class CodexRemoteService:
    def __init__(
        self,
        *,
        config: RemoteConfig,
        telegram: TelegramBotAPI,
        store: RemoteStore,
        runner: CodexRunner,
        external_tasks: ExternalTaskMonitor | None = None,
    ):
        self.config = config
        self.telegram = telegram
        self.store = store
        self.runner = runner
        self.external_tasks = external_tasks or ExternalTaskMonitor(
            state_db=config.codex_state_db,
            thread_history_db=config.codex_thread_history_db,
            active_window_seconds=config.external_active_window_seconds,
        )
        self._task_queue: queue.Queue[int] = queue.Queue()
        self._stop_event = threading.Event()
        self._cancel_event = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="codex-remote-worker",
            daemon=True,
        )
        self._watcher = threading.Thread(
            target=self._watch_loop,
            name="codex-remote-watcher",
            daemon=True,
        )
        self._active_lock = threading.Lock()
        self._active: ActiveJob | None = None

    def run(self, *, once: bool = False) -> None:
        self.store.initialize()
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._worker.start()
        self._watcher.start()
        threading.Thread(
            target=self._configure_telegram_ui,
            name="telegram-ui-config",
            daemon=True,
        ).start()
        for job_id in self.store.queued_job_ids():
            self._task_queue.put(job_id)
        LOGGER.info("service started repos=%s", ",".join(self.config.repositories))

        failures = 0
        try:
            while not self._stop_event.is_set():
                try:
                    offset = int(self.store.get_state("last_update_id", "0")) + 1
                    updates = self.telegram.get_updates(
                        offset=offset,
                        timeout_seconds=self.config.poll_timeout_seconds,
                    )
                    failures = 0
                    for update in updates:
                        try:
                            self.handle_update(update)
                        except Exception:
                            LOGGER.exception(
                                "failed to handle telegram update=%s",
                                update.get("update_id"),
                            )
                            update_id = update.get("update_id")
                            if isinstance(update_id, int):
                                self.store.set_state(
                                    "last_update_id",
                                    str(update_id),
                                )
                    if once:
                        break
                except TelegramError as error:
                    failures += 1
                    wait = min(30, 2**min(failures, 4))
                    LOGGER.warning("telegram polling failed: %s; retry=%ss", error, wait)
                    self._stop_event.wait(wait)
                except Exception as error:
                    failures += 1
                    wait = min(30, 2**min(failures, 4))
                    LOGGER.exception(
                        "unexpected telegram polling failure: %s; retry=%ss",
                        error,
                        wait,
                    )
                    self._stop_event.wait(wait)
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_event.set()
        if self._worker.is_alive():
            self._task_queue.put(-1)
            self._worker.join(timeout=10)
        if self._watcher.is_alive():
            self._watcher.join(timeout=10)

    def handle_update(self, update: dict[str, Any]) -> None:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            return
        callback_query = update.get("callback_query")
        if isinstance(callback_query, dict):
            self._handle_callback_query(update_id, callback_query)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            return
        user_id = sender.get("id")
        chat_id = chat.get("id")
        chat_type = str(chat.get("type", ""))
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return

        if self.config.require_private_chat and chat_type != "private":
            LOGGER.warning(
                "rejected non-private update user_id=%s chat_id=%s type=%s",
                user_id,
                chat_id,
                chat_type,
            )
            return
        if user_id not in self.config.allowed_user_ids:
            LOGGER.warning("rejected unauthorized user_id=%s chat_id=%s", user_id, chat_id)
            self._safe_send(
                chat_id,
                f"未授权。你的 Telegram User ID 是 {user_id}。\n"
                "请把该 ID 写入 "
                "~/.config/codex-telegram-remote/config.env 的 "
                "CODEX_REMOTE_ALLOWED_USER_IDS。",
            )
            self.store.set_state("last_update_id", str(update_id))
            return

        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self.store.set_state("last_update_id", str(update_id))
            return
        text = text.strip()
        message_id = message.get("message_id")
        reply_to = message_id if isinstance(message_id, int) else None
        if reply_to is not None:
            threading.Thread(
                target=self._safe_react,
                args=(chat_id, reply_to),
                name=f"telegram-reaction-{update_id}",
                daemon=True,
            ).start()
        try:
            if text.startswith("/"):
                self._handle_command(
                    chat_id=chat_id,
                    user_id=user_id,
                    text=text,
                    reply_to=reply_to,
                )
            else:
                self._handle_task(
                    update_id=update_id,
                    chat_id=chat_id,
                    user_id=user_id,
                    text=text,
                    reply_to=reply_to,
                )
        finally:
            # The job has a UNIQUE update_id even if the process dies before this write.
            self.store.set_state("last_update_id", str(update_id))

    def _handle_command(
        self,
        *,
        chat_id: int,
        user_id: int,
        text: str,
        reply_to: int | None,
    ) -> None:
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()
        reply_markup = self._panel_markup()

        if command in {"/start", "/help"}:
            if command == "/start":
                response = (
                    "Codex 控制面板\n\n"
                    f"当前仓库：{self._chat_repo(chat_id)}\n"
                    "直接发送普通文字即可创建 Codex 任务。\n\n"
                    + HELP_TEXT
                )
            else:
                response = HELP_TEXT
        elif command == "/id":
            response = f"User ID：{user_id}\nChat ID：{chat_id}"
        elif command == "/repos":
            current = self._chat_repo(chat_id)
            response = self._repos_text(current)
            reply_markup = self._repos_markup(current)
        elif command == "/repo":
            if argument not in self.config.repositories:
                response = (
                    "仓库不存在。可用值："
                    + "、".join(sorted(self.config.repositories))
                )
                reply_markup = self._repos_markup(self._chat_repo(chat_id))
            else:
                self.store.set_chat_repo(chat_id, argument)
                response, reply_markup = self._switch_repo_view(
                    chat_id,
                    argument,
                )
        elif command == "/recent":
            response, reply_markup = self._recent_tasks_view(chat_id)
        elif command == "/new":
            repo = self._chat_repo(chat_id)
            removed = self.store.clear_session(chat_id, repo)
            response = (
                f"已清除仓库 {repo} 的会话。下一条任务将新建会话。"
                if removed
                else f"仓库 {repo} 当前没有可清除的会话。"
            )
        elif command == "/status":
            response = self._status_text(chat_id)
        elif command == "/watch":
            response = self._watch_current_external_tasks(chat_id)
        elif command == "/cancel":
            response = self._cancel(chat_id)
        else:
            response = "未知命令。\n\n" + HELP_TEXT
        self._safe_send(
            chat_id,
            response,
            reply_to_message_id=reply_to,
            reply_markup=reply_markup,
        )

    def _handle_callback_query(
        self,
        update_id: int,
        callback_query: dict[str, Any],
    ) -> None:
        query_id = callback_query.get("id")
        sender = callback_query.get("from")
        message = callback_query.get("message")
        if (
            not isinstance(query_id, str)
            or not isinstance(sender, dict)
            or not isinstance(message, dict)
        ):
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        user_id = sender.get("id")
        chat_id = chat.get("id")
        chat_type = str(chat.get("type", ""))
        message_id = message.get("message_id")
        reply_to = message_id if isinstance(message_id, int) else None
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            self._safe_answer_callback(query_id, "无效请求", show_alert=True)
            self.store.set_state("last_update_id", str(update_id))
            return
        if self.config.require_private_chat and chat_type != "private":
            self._safe_answer_callback(query_id, "仅支持私聊", show_alert=True)
            self.store.set_state("last_update_id", str(update_id))
            return
        if user_id not in self.config.allowed_user_ids:
            self._safe_answer_callback(query_id, "未授权", show_alert=True)
            self.store.set_state("last_update_id", str(update_id))
            return

        data = callback_query.get("data")
        action = str(data) if isinstance(data, str) else ""
        LOGGER.info(
            "callback action user_id=%s chat_id=%s action=%s",
            user_id,
            chat_id,
            action,
        )
        try:
            if action == "status":
                response = self._status_text(chat_id)
                markup = self._panel_markup()
                toast = "状态已刷新"
            elif action == "repos":
                current = self._chat_repo(chat_id)
                response = self._repos_text(current)
                markup = self._repos_markup(current)
                toast = "请选择仓库"
            elif action == "recent":
                response, markup = self._recent_tasks_view(chat_id)
                toast = "最近任务"
            elif action == "new":
                repo = self._chat_repo(chat_id)
                removed = self.store.clear_session(chat_id, repo)
                response = (
                    f"已清除仓库 {repo} 的会话。下一条任务将新建会话。"
                    if removed
                    else f"仓库 {repo} 当前没有可清除的会话。"
                )
                markup = self._panel_markup()
                toast = "会话已处理"
            elif action == "cancel":
                response = self._cancel(chat_id)
                markup = self._panel_markup()
                toast = "取消请求已处理"
            elif action == "watch":
                response = self._watch_current_external_tasks(chat_id)
                markup = self._panel_markup()
                toast = "完成通知已处理"
            elif action == "help":
                response = HELP_TEXT
                markup = self._panel_markup()
                toast = "使用说明"
            elif action == "panel":
                response = (
                    f"Codex 控制面板\n\n当前仓库：{self._chat_repo(chat_id)}\n"
                    "直接发送普通文字即可创建 Codex 任务。"
                )
                markup = self._panel_markup()
                toast = "控制面板"
            elif action.startswith("repo:"):
                repo = action.partition(":")[2]
                if repo not in self.config.repositories:
                    self._safe_answer_callback(
                        query_id,
                        "仓库不存在",
                        show_alert=True,
                    )
                    return
                self.store.set_chat_repo(chat_id, repo)
                response, markup = self._switch_repo_view(chat_id, repo)
                toast = f"已切换：{repo}"
            elif action.startswith("task:"):
                thread_id = action.partition(":")[2]
                repo = self._chat_repo(chat_id)
                task = self.external_tasks.resolve_thread(
                    self.config.repositories[repo],
                    thread_id,
                )
                if task is None:
                    self._safe_answer_callback(
                        query_id,
                        "该任务不属于当前项目或已失效",
                        show_alert=True,
                    )
                    return
                self.store.save_session(chat_id, repo, task.thread_id)
                response = "\n".join(
                    (
                        f"已选择任务：{task.title}",
                        f"对话 ID：{task.thread_id}",
                        f"任务内容：{task.prompt or '无可用内容'}",
                        "",
                        "后续普通消息将在这个任务下继续。",
                    )
                )
                markup = self._panel_markup()
                toast = "已选择该任务"
            else:
                self._safe_answer_callback(
                    query_id,
                    "操作已失效，请重新打开菜单",
                    show_alert=True,
                )
                return
            self._safe_answer_callback(query_id, toast)
            self._safe_send(
                chat_id,
                response,
                reply_to_message_id=reply_to,
                reply_markup=markup,
            )
        finally:
            self.store.set_state("last_update_id", str(update_id))

    def _handle_task(
        self,
        *,
        update_id: int,
        chat_id: int,
        user_id: int,
        text: str,
        reply_to: int | None,
    ) -> None:
        if len(text) > self.config.max_prompt_chars:
            self._safe_send(
                chat_id,
                f"任务过长：{len(text)} 字符，上限为 "
                f"{self.config.max_prompt_chars} 字符。",
                reply_to_message_id=reply_to,
            )
            return
        repo = self._chat_repo(chat_id)
        job = self.store.enqueue_job(
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            prompt=text,
            repo=repo,
        )
        position = self.store.queued_count(chat_id)
        session_id = self.store.get_session(chat_id, repo)
        conversation = "\n".join(
            self._conversation_lines(repo, session_id, text)
        )
        self._safe_send(
            chat_id,
            f"✅ 收到任务\n任务 #{job.id} 已排队\n仓库：{repo}\n"
            f"{conversation}\n当前排队：{position}",
            reply_to_message_id=reply_to,
        )
        self._task_queue.put(job.id)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            job_id = self._task_queue.get()
            if job_id < 0:
                return
            if not self.store.claim_job(job_id):
                continue
            job = self.store.get_job(job_id)
            if job is None:
                continue
            self._run_job(job)

    def _watch_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._auto_watch_external_tasks()
                self._check_watched_tasks()
            except Exception:
                LOGGER.exception("failed to check watched external tasks")
            self._stop_event.wait(self.config.watch_poll_seconds)

    def _auto_watch_external_tasks(self) -> None:
        for repo, repo_path in self.config.repositories.items():
            state = self.external_tasks.running_for(repo_path)
            if not state.available:
                continue
            for task in state.tasks:
                for chat_id in self.config.allowed_user_ids:
                    self.store.watch_task(
                        thread_id=task.thread_id,
                        chat_id=chat_id,
                        repo=repo,
                        title=task.title,
                        started_at=task.started_at,
                    )

    def _check_watched_tasks(self) -> None:
        for watched in self.store.pending_watched_tasks():
            turn = self.external_tasks.latest_turn(watched.thread_id)
            if turn is None or turn.status == "inProgress":
                continue
            outcome = {
                "completed": "已完成",
                "failed": "失败",
                "interrupted": "已中断",
            }.get(turn.status, turn.status)
            icon = "✅" if turn.status == "completed" else "⚠️"
            message = "\n".join(
                (
                    f"{icon} Codex App 任务{outcome}",
                    f"项目：{watched.repo}",
                    f"任务：{watched.title}",
                    f"对话 ID：{watched.thread_id}",
                    f"状态：{turn.status}",
                    "",
                    "任务结果：" if turn.status == "completed" else "失败结果：",
                    (
                        turn.result_text
                        if turn.status == "completed"
                        else turn.error_text or turn.result_text
                    )
                    or "没有返回可读结果。",
                )
            )
            if self._safe_send(watched.chat_id, message):
                self.store.mark_watched_task_notified(
                    thread_id=watched.thread_id,
                    chat_id=watched.chat_id,
                    outcome=turn.status,
                )

    def _run_job(self, job: Job) -> None:
        repo_path = self.config.repositories.get(job.repo)
        if repo_path is None:
            error = f"仓库 {job.repo} 已不在配置中"
            self.store.finish_job(
                job.id,
                status="failed",
                error=error,
            )
            self._safe_send(
                job.chat_id,
                self._job_result_message(
                    job.id,
                    job.repo,
                    self.store.get_session(job.chat_id, job.repo),
                    job.prompt,
                    "失败",
                    error,
                ),
            )
            return

        self._cancel_event.clear()
        with self._active_lock:
            self._active = ActiveJob(job.id, job.chat_id, job.repo)
        session_id = self.store.get_session(job.chat_id, job.repo)
        conversation = "\n".join(
            self._conversation_lines(job.repo, session_id, job.prompt)
        )
        self._safe_send(
            job.chat_id,
            f"任务 #{job.id} 开始执行\n仓库：{job.repo}\n"
            f"{conversation}\n"
            f"沙箱：{self.config.sandbox}",
        )

        def on_session(detected: str) -> None:
            self.store.save_session(job.chat_id, job.repo, detected)

        try:
            result = self.runner.run(
                prompt=job.prompt,
                cwd=repo_path,
                session_id=session_id,
                cancel_event=self._cancel_event,
                on_session=on_session,
            )
            if result.session_id:
                self.store.save_session(job.chat_id, job.repo, result.session_id)
            if result.canceled:
                self.store.finish_job(
                    job.id,
                    status="canceled",
                    result=result.text,
                    session_id=result.session_id,
                )
                self._safe_send(
                    job.chat_id,
                    self._job_result_message(
                        job.id,
                        job.repo,
                        result.session_id or session_id,
                        job.prompt,
                        "已取消",
                        result.text,
                    ),
                )
                return
            if result.timed_out:
                self.store.finish_job(
                    job.id,
                    status="failed",
                    error=result.text,
                    session_id=result.session_id,
                )
                self._safe_send(
                    job.chat_id,
                    self._job_result_message(
                        job.id,
                        job.repo,
                        result.session_id or session_id,
                        job.prompt,
                        "失败",
                        result.text,
                    ),
                )
                return
            output = self._redact(result.text)
            if len(output) > self.config.max_reply_chars:
                output = (
                    output[: self.config.max_reply_chars]
                    + "\n\n...(回复过长，已截断)"
                )
            self.store.finish_job(
                job.id,
                status="succeeded",
                result=output,
                session_id=result.session_id,
            )
            self._safe_send(
                job.chat_id,
                self._job_result_message(
                    job.id,
                    job.repo,
                    result.session_id or session_id,
                    job.prompt,
                    "完成",
                    output,
                    elapsed_seconds=result.elapsed_seconds,
                ),
            )
        except CodexRunError as error:
            message = self._redact(str(error))
            self.store.finish_job(
                job.id,
                status="failed",
                error=message,
            )
            self._safe_send(
                job.chat_id,
                self._job_result_message(
                    job.id,
                    job.repo,
                    self.store.get_session(job.chat_id, job.repo)
                    or session_id,
                    job.prompt,
                    "失败",
                    message,
                ),
            )
        except Exception as error:
            LOGGER.exception("job %s failed", job.id)
            message = self._redact(f"{type(error).__name__}: {error}")
            self.store.finish_job(job.id, status="failed", error=message)
            self._safe_send(
                job.chat_id,
                self._job_result_message(
                    job.id,
                    job.repo,
                    self.store.get_session(job.chat_id, job.repo)
                    or session_id,
                    job.prompt,
                    "失败",
                    message,
                ),
            )
        finally:
            with self._active_lock:
                self._active = None

    def _cancel(self, chat_id: int) -> str:
        canceled_queued = self.store.cancel_queued_jobs(chat_id)
        with self._active_lock:
            active = self._active
        if active and active.chat_id == chat_id:
            self._cancel_event.set()
            return (
                f"正在取消任务 #{active.job_id}；"
                f"同时取消排队任务 {canceled_queued} 个。"
            )
        if canceled_queued:
            return f"已取消排队任务 {canceled_queued} 个。"
        return "当前没有属于此聊天的运行中或排队任务。"

    def _status_text(self, chat_id: int) -> str:
        repo = self._chat_repo(chat_id)
        session_id = self.store.get_session(chat_id, repo)
        queued = self.store.queued_count(chat_id)
        with self._active_lock:
            active = self._active
        if active and active.chat_id == chat_id:
            telegram_running = f"任务 #{active.job_id}（{active.repo}）"
        else:
            telegram_running = "无"
        external = self.external_tasks.running_for(
            self.config.repositories[repo]
        )
        app_count = len(external.tasks) if external.available else 0
        lines = [
            f"仓库：{repo}",
            f"运行中：App {app_count if external.available else '?'} 个，"
            f"Telegram {1 if active and active.chat_id == chat_id else 0} 个",
            f"Telegram 任务：{telegram_running}",
            f"排队：{queued}",
            f"沙箱：{self.config.sandbox}",
        ]
        lines[4:4] = self._conversation_lines(repo, session_id)
        if not external.available:
            lines.append(f"App 任务状态：{external.error or '不可用'}")
        elif external.tasks:
            now = int(self.external_tasks.now())
            watched_ids = self.store.watched_thread_ids(chat_id)
            for task in external.tasks[:3]:
                watch_label = (
                    " · 已订阅完成通知"
                    if task.thread_id in watched_ids
                    else ""
                )
                lines.append(
                    f"App 运行中：{task.title}（已运行 "
                    f"{task.elapsed_text(now)}）{watch_label}"
                )
            if len(external.tasks) > 3:
                lines.append(f"App 另有 {len(external.tasks) - 3} 个运行任务")
        else:
            lines.append("App 运行中：无")
        return "\n".join(lines)

    def _watch_current_external_tasks(self, chat_id: int) -> str:
        repo = self._chat_repo(chat_id)
        state = self.external_tasks.running_for(self.config.repositories[repo])
        if not state.available:
            return f"无法读取 App 任务状态：{state.error}"
        if not state.tasks:
            return f"当前仓库 {repo} 没有正在运行的 App 任务。"
        for task in state.tasks:
            self.store.watch_task(
                thread_id=task.thread_id,
                chat_id=chat_id,
                repo=repo,
                title=task.title,
                started_at=task.started_at,
            )
        titles = "\n".join(
            f"• {task.title}\n  对话 ID：{task.thread_id}"
            for task in state.tasks
        )
        return (
            f"已订阅 {len(state.tasks)} 个 App 任务的完成通知：\n"
            f"{titles}\n\n任务结束后会通过 Telegram 通知。"
        )

    def _switch_repo_view(
        self,
        chat_id: int,
        repo: str,
    ) -> tuple[str, dict[str, Any]]:
        repo_path = self.config.repositories[repo]
        summary = summarize_project(repo_path)
        recent = self._recent_tasks_state(repo)
        text = "\n".join(
            (
                f"已切换到仓库：{repo}",
                "",
                "项目简介：",
                summary,
                "",
                self._status_text(chat_id),
                "",
                self._recent_tasks_text(chat_id, repo, recent),
            )
        )
        return text, self._recent_tasks_markup(chat_id, repo, recent)

    def _recent_tasks_view(
        self,
        chat_id: int,
    ) -> tuple[str, dict[str, Any]]:
        repo = self._chat_repo(chat_id)
        recent = self._recent_tasks_state(repo)
        return (
            self._recent_tasks_text(chat_id, repo, recent),
            self._recent_tasks_markup(chat_id, repo, recent),
        )

    def _recent_tasks_state(self, repo: str) -> RecentTaskState:
        return self.external_tasks.recent_for(
            self.config.repositories[repo],
            self.config.recent_task_limit,
        )

    def _recent_tasks_text(
        self,
        chat_id: int,
        repo: str,
        recent: RecentTaskState,
    ) -> str:
        if not recent.available:
            return f"最近任务：读取失败\n{recent.error or 'Codex 历史不可用'}"
        if not recent.tasks:
            return f"仓库 {repo} 最近没有可继续的任务。"
        selected = self.store.get_session(chat_id, repo)
        lines = ["最近任务（点击序号继续会话）："]
        for index, task in enumerate(recent.tasks, start=1):
            selected_mark = " [当前]" if task.thread_id == selected else ""
            status = f" · {task.status}" if task.status else ""
            lines.extend(
                (
                    f"{index}. {task.title}{selected_mark}{status}",
                    f"   内容：{task.prompt or '无可用内容'}",
                )
            )
        return "\n".join(lines)

    def _recent_tasks_markup(
        self,
        chat_id: int,
        repo: str,
        recent: RecentTaskState,
    ) -> dict[str, Any]:
        selected = self.store.get_session(chat_id, repo)
        rows: list[list[dict[str, str]]] = []
        for index, task in enumerate(recent.tasks, start=1):
            mark = "✓ " if task.thread_id == selected else ""
            rows.append(
                [
                    {
                        "text": f"{index}. {mark}{_button_title(task.title)}",
                        "callback_data": f"task:{task.thread_id}",
                    }
                ]
            )
        rows.extend(
            (
                [
                    {"text": "新建会话", "callback_data": "new"},
                    {"text": "刷新最近任务", "callback_data": "recent"},
                ],
                [
                    {"text": "切换仓库", "callback_data": "repos"},
                    {"text": "返回控制面板", "callback_data": "panel"},
                ],
            )
        )
        return {"inline_keyboard": rows}

    @staticmethod
    def _panel_markup() -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "刷新状态", "callback_data": "status"},
                    {"text": "切换仓库", "callback_data": "repos"},
                ],
                [
                    {"text": "新建会话", "callback_data": "new"},
                    {"text": "取消任务", "callback_data": "cancel"},
                ],
                [
                    {"text": "最近任务", "callback_data": "recent"},
                    {"text": "完成时通知", "callback_data": "watch"},
                ],
                [
                    {"text": "使用说明", "callback_data": "help"},
                ],
            ]
        }

    def _repos_markup(self, current: str) -> dict[str, Any]:
        rows = [
            [
                {
                    "text": f"{'✓ ' if alias == current else ''}{alias}",
                    "callback_data": f"repo:{alias}",
                }
            ]
            for alias in sorted(self.config.repositories)
        ]
        rows.append([{"text": "返回控制面板", "callback_data": "panel"}])
        return {"inline_keyboard": rows}

    def _repos_text(self, current: str) -> str:
        lines = [
            f"{'*' if alias == current else '-'} {alias}: {path}"
            for alias, path in sorted(self.config.repositories.items())
        ]
        prefix = (
            "当前只配置了一个仓库：\n"
            if len(self.config.repositories) == 1
            else "仓库列表（* 为当前仓库）：\n"
        )
        return prefix + "\n".join(lines)

    def _configure_telegram_ui(self) -> None:
        try:
            self.telegram.set_my_commands(BOT_COMMANDS)
            self.telegram.set_chat_menu_button({"type": "commands"})
            LOGGER.info("telegram command menu configured")
        except TelegramError:
            LOGGER.exception("failed to configure telegram command menu")

    def _chat_repo(self, chat_id: int) -> str:
        return self.store.get_chat_repo(chat_id, self.config.default_repo)

    def _safe_send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        safe_text = self._redact(text)
        try:
            self.telegram.send_long_message(
                chat_id,
                safe_text,
                reply_to_message_id=reply_to_message_id,
                reply_markup=reply_markup,
            )
            return True
        except TelegramError:
            LOGGER.exception("failed to send telegram message chat_id=%s", chat_id)
            return False

    def _safe_answer_callback(
        self,
        callback_query_id: str,
        text: str,
        *,
        show_alert: bool = False,
    ) -> None:
        try:
            self.telegram.answer_callback_query(
                callback_query_id,
                text=self._redact(text),
                show_alert=show_alert,
            )
        except TelegramError:
            LOGGER.exception("failed to answer telegram callback")

    def _safe_react(self, chat_id: int, message_id: int) -> None:
        try:
            self.telegram.set_message_reaction(chat_id, message_id)
        except TelegramError:
            LOGGER.warning(
                "failed to react to telegram message chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )

    def _redact(self, text: str) -> str:
        result = text
        for value in self.config.redact_values:
            result = result.replace(value, "<redacted>")
        return result

    def _job_result_message(
        self,
        job_id: int,
        repo: str,
        session_id: str,
        prompt: str,
        outcome: str,
        result: str,
        *,
        elapsed_seconds: float | None = None,
    ) -> str:
        elapsed = (
            f" · {elapsed_seconds:.1f}s"
            if elapsed_seconds is not None
            else ""
        )
        conversation = "\n".join(
            self._conversation_lines(repo, session_id, prompt)
        )
        return (
            f"任务 #{job_id} {outcome} · 仓库 {repo}{elapsed}\n"
            f"{conversation}\n\n"
            f"任务结果：\n{result or '没有返回可读结果。'}"
        )

    def _conversation_lines(
        self,
        repo: str,
        session_id: str,
        prompt: str = "",
    ) -> tuple[str, str]:
        title = ""
        repo_path = self.config.repositories.get(repo)
        if session_id and repo_path is not None:
            try:
                task = self.external_tasks.resolve_thread(repo_path, session_id)
            except Exception:
                LOGGER.warning(
                    "failed to resolve conversation title repo=%s session=%s",
                    repo,
                    session_id,
                    exc_info=True,
                )
                task = None
            if task is not None:
                title = task.title
        if not title:
            title = _button_title(prompt, 80) if prompt else "未命名任务"
        if session_id:
            return f"对话：{title}", f"对话 ID：{session_id}"
        return f"对话：{title}", "对话 ID：新会话创建后生成"


def _button_title(value: str, limit: int = 40) -> str:
    normalized = " ".join(value.split()) or "未命名任务"
    return (
        normalized
        if len(normalized) <= limit
        else normalized[: limit - 1] + "…"
    )
