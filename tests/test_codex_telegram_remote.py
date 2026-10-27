from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from codex_telegram_remote.config import RemoteConfig, load_config
from codex_telegram_remote.external_tasks import (
    ExternalTask,
    ExternalTaskMonitor,
    ExternalTaskState,
    RecentTask,
    RecentTaskState,
    TurnState,
)
from codex_telegram_remote.project_summary import summarize_project
from codex_telegram_remote.runner import (
    CodexRunner,
    CodexRunError,
    CodexRunResult,
    ParsedEvent,
    parse_jsonl_event,
)
from codex_telegram_remote.service import CodexRemoteService
from codex_telegram_remote.store import RemoteStore
from codex_telegram_remote.telegram import (
    TelegramBotAPI,
    TelegramError,
    markdown_to_telegram_html,
    plain_text_without_markdown,
    split_message,
)


class FakeTelegram:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str]] = []
        self.markups: list[dict[str, object] | None] = []
        self.callback_answers: list[tuple[str, str, bool]] = []
        self.reactions: list[tuple[int, int, str]] = []
        self.on_send = None
        self.commands: list[dict[str, str]] = []
        self.menu_button: dict[str, str] | None = None

    def send_long_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        self.messages.append((chat_id, text))
        self.markups.append(reply_markup)
        if self.on_send:
            self.on_send(text)

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        self.commands = commands

    def set_chat_menu_button(self, menu_button: dict[str, str]) -> None:
        self.menu_button = menu_button

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        self.callback_answers.append((callback_query_id, text, show_alert))

    def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        *,
        emoji: str = "👀",
    ) -> None:
        self.reactions.append((chat_id, message_id, emoji))


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(self, **kwargs: object) -> CodexRunResult:
        self.calls.append(kwargs)
        return CodexRunResult(
            text="PONG",
            session_id=str(kwargs.get("session_id") or "session-1"),
            exit_code=0,
            elapsed_seconds=1.2,
        )


class FailingRunner:
    def run(self, **_kwargs: object) -> CodexRunResult:
        raise CodexRunError("测试失败结果：断言未通过")


class ConfigTests(unittest.TestCase):
    def test_loads_whitelist_repositories_and_safe_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "CODEX_REMOTE_BOT_TOKEN=123456:abcdefghijklmnop",
                        "CODEX_REMOTE_ALLOWED_USER_IDS=123,456",
                        f"CODEX_REMOTE_REPOS=demo={repo}",
                        "CODEX_REMOTE_DEFAULT_REPO=demo",
                    )
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(env_path=env_file, project_root=root)
            errors = config.validate()

        self.assertEqual(frozenset({123, 456}), config.allowed_user_ids)
        self.assertEqual({"demo": repo.resolve()}, config.repositories)
        self.assertEqual("workspace-write", config.sandbox)
        self.assertEqual(5, config.recent_task_limit)
        self.assertEqual([], errors)

    def test_rejects_sandbox_bypass_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            env_file = root / ".env"
            env_file.write_text(
                "\n".join(
                    (
                        "CODEX_REMOTE_BOT_TOKEN=123456:abcdefghijklmnop",
                        "CODEX_REMOTE_ALLOWED_USER_IDS=123",
                        f"CODEX_REMOTE_REPOS=demo={repo}",
                        "CODEX_REMOTE_DEFAULT_REPO=demo",
                        "CODEX_REMOTE_SANDBOX=danger-full-access",
                    )
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                config = load_config(env_path=env_file, project_root=root)

        self.assertTrue(any("CODEX_REMOTE_SANDBOX" in item for item in config.validate()))

    def test_does_not_reuse_unrelated_telegram_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            env_file = root / "config.env"
            env_file.write_text(
                "\n".join(
                    (
                        "CODEX_REMOTE_ALLOWED_USER_IDS=123",
                        f"CODEX_REMOTE_REPOS=demo={repo}",
                        "CODEX_REMOTE_DEFAULT_REPO=demo",
                    )
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {"TELEGRAM_BOT_TOKEN": "x-archive-only-token"},
                clear=True,
            ):
                config = load_config(env_path=env_file, project_root=root)

        self.assertEqual("", config.bot_token)
        self.assertIn("未配置 CODEX_REMOTE_BOT_TOKEN", config.validate()[0])


class ProjectSummaryTests(unittest.TestCase):
    def test_reads_readme_title_and_description(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "demo"
            root.mkdir()
            (root / "README.md").write_text(
                "# Demo Tool\n\n"
                "A local command-line tool for organizing project notes.\n\n"
                "## Usage\n",
                encoding="utf-8",
            )
            summary = summarize_project(root)

        self.assertIn("organizing project notes", summary)

    def test_detects_nextjs_project_without_readme(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "web-app"
            root.mkdir()
            (root / "package.json").write_text(
                '{"dependencies":{"next":"16.0.0","react":"19.0.0"},'
                '"devDependencies":{"typescript":"5.0.0","tailwindcss":"4.0.0"}}',
                encoding="utf-8",
            )
            (root / "src").mkdir()
            summary = summarize_project(root)

        self.assertIn("Next.js Web 应用", summary)
        self.assertIn("TypeScript", summary)
        self.assertIn("Tailwind CSS", summary)

    def test_reads_html_meta_description(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "landing"
            version = root / "1"
            version.mkdir(parents=True)
            (version / "index.html").write_text(
                "<html><head><title>Demo Invitation</title>"
                '<meta name="description" content="A responsive invitation page.">'
                "</head></html>",
                encoding="utf-8",
            )
            summary = summarize_project(root)

        self.assertIn("responsive invitation page", summary)


class ExternalTaskTests(unittest.TestCase):
    def test_detects_recent_active_turn_for_exact_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clipflow = root / "ClipFlow"
            other = root / "Other"
            clipflow.mkdir()
            other.mkdir()
            state_db = root / "state.sqlite"
            history_db = root / "history.sqlite"
            _create_external_state_db(
                state_db,
                (
                    ("active-1", "ClipFlow task", str(clipflow), 970, 0),
                    ("active-other", "Other task", str(other), 975, 0),
                    ("stale-1", "Stale task", str(clipflow), 980, 0),
                ),
            )
            _create_external_history_db(
                history_db,
                (
                    ("active-1", "turn-complete", 1, "completed", 900),
                    ("active-1", "turn-active", 2, "inProgress", 950),
                    ("active-other", "turn-active", 1, "inProgress", 960),
                    ("stale-1", "turn-old", 1, "inProgress", 100),
                ),
            )
            monitor = ExternalTaskMonitor(
                state_db=state_db,
                thread_history_db=history_db,
                active_window_seconds=200,
                now=lambda: 1000,
            )
            result = monitor.running_for(clipflow)

        self.assertTrue(result.available)
        self.assertEqual(1, len(result.tasks))
        self.assertEqual("ClipFlow task", result.tasks[0].title)

    def test_ignores_thread_whose_latest_turn_completed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "Repo"
            repo.mkdir()
            state_db = root / "state.sqlite"
            history_db = root / "history.sqlite"
            _create_external_state_db(
                state_db,
                (("thread-1", "Old active row", str(repo), 990, 0),),
            )
            _create_external_history_db(
                history_db,
                (
                    ("thread-1", "turn-old", 1, "inProgress", 900),
                    ("thread-1", "turn-new", 2, "completed", 980),
                ),
            )
            result = ExternalTaskMonitor(
                state_db=state_db,
                thread_history_db=history_db,
                active_window_seconds=200,
                now=lambda: 1000,
            ).running_for(repo)

        self.assertTrue(result.available)
        self.assertEqual((), result.tasks)

    def test_lists_recent_project_tasks_with_prompt_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "Repo"
            other = root / "Other"
            repo.mkdir()
            other.mkdir()
            state_db = root / "state.sqlite"
            history_db = root / "history.sqlite"
            _create_external_state_db(
                state_db,
                (
                    ("recent-1", "完成任务 A", str(repo), 1000, 0),
                    ("recent-2", "失败任务 B", str(repo), 990, 0),
                    ("other-1", "其他项目任务", str(other), 980, 0),
                ),
            )
            _create_external_history_db(
                history_db,
                (
                    ("recent-1", "turn-1", 2, "completed", 900),
                    ("recent-2", "turn-2", 3, "failed", 910),
                    ("other-1", "turn-3", 4, "completed", 920),
                ),
            )
            _add_external_turn_details(
                history_db,
                thread_id="recent-1",
                turn_id="turn-1",
                user_text="实现最近任务列表",
                result_text="最近任务列表已经完成",
            )
            _add_external_turn_details(
                history_db,
                thread_id="recent-2",
                turn_id="turn-2",
                user_text="修复失败通知",
                error_text="失败的详细结果",
            )
            _add_external_turn_details(
                history_db,
                thread_id="other-1",
                turn_id="turn-3",
                user_text="其他项目",
                result_text="不应返回",
            )
            result = ExternalTaskMonitor(
                state_db=state_db,
                thread_history_db=history_db,
                active_window_seconds=200,
                now=lambda: 1000,
            ).recent_for(repo, limit=5)

        self.assertTrue(result.available)
        self.assertEqual(2, len(result.tasks))
        self.assertEqual("recent-1", result.tasks[0].thread_id)
        self.assertEqual("实现最近任务列表", result.tasks[0].prompt)
        self.assertEqual("最近任务列表已经完成", result.tasks[0].result)
        self.assertEqual("failed", result.tasks[1].status)
        self.assertEqual("失败的详细结果", result.tasks[1].result)


class StoreTests(unittest.TestCase):
    def test_jobs_sessions_and_restart_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RemoteStore(Path(temp_dir) / "state.db")
            store.initialize()
            first = store.enqueue_job(
                update_id=100,
                chat_id=1,
                user_id=2,
                prompt="do work",
                repo="demo",
            )
            duplicate = store.enqueue_job(
                update_id=100,
                chat_id=1,
                user_id=2,
                prompt="duplicate",
                repo="demo",
            )
            self.assertEqual(first.id, duplicate.id)
            self.assertTrue(store.claim_job(first.id))
            store.save_session(1, "demo", "session-1")
            store.clear_session(1, "demo")
            store.claim_job(
                store.enqueue_job(
                    update_id=101,
                    chat_id=1,
                    user_id=2,
                    prompt="second",
                    repo="demo",
                ).id
            )
            store.initialize()
            failed = store.get_job(first.id)
            duplicate_claim = store.claim_job(first.id)

        self.assertFalse(duplicate_claim)
        self.assertEqual("failed", failed.status if failed else "")
        self.assertIn("服务重启", failed.error if failed else "")

    def test_chat_repo_and_cancel_queued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RemoteStore(Path(temp_dir) / "state.db")
            store.initialize()
            store.set_chat_repo(10, "other")
            job = store.enqueue_job(
                update_id=200,
                chat_id=10,
                user_id=20,
                prompt="work",
                repo="other",
            )
            canceled = store.cancel_queued_jobs(10)
            updated = store.get_job(job.id)
            repo = store.get_chat_repo(10, "default")

        self.assertEqual("other", repo)
        self.assertEqual(1, canceled)
        self.assertEqual("canceled", updated.status if updated else "")

    def test_external_task_watch_is_persisted_and_notified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = RemoteStore(Path(temp_dir) / "state.db")
            store.initialize()
            store.watch_task(
                thread_id="thread-1",
                chat_id=10,
                repo="demo",
                title="Finish task",
                started_at=100,
            )
            pending = store.pending_watched_tasks()
            watched_ids = store.watched_thread_ids(10)
            store.mark_watched_task_notified(
                thread_id="thread-1",
                chat_id=10,
                outcome="completed",
            )
            remaining = store.pending_watched_tasks()

        self.assertEqual(1, len(pending))
        self.assertEqual({"thread-1"}, watched_ids)
        self.assertEqual([], remaining)


class RunnerTests(unittest.TestCase):
    def test_parses_thread_and_agent_message_events(self) -> None:
        started = parse_jsonl_event(
            '{"type":"thread.started","thread_id":"019abc"}'
        )
        completed = parse_jsonl_event(
            '{"type":"item.completed","item":'
            '{"id":"item_1","type":"agent_message","text":"done"}}'
        )

        self.assertEqual(ParsedEvent(session_id="019abc", progress="Codex 会话已创建"), started)
        self.assertEqual("done", completed.agent_text)

    def test_builds_safe_exec_and_resume_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            runner = CodexRunner(
                codex_bin="codex",
                sandbox="read-only",
                model="",
                timeout_seconds=60,
                cancel_grace_seconds=2,
                temp_dir=Path(temp_dir),
            )
            output = Path(temp_dir) / "answer.txt"
            new_command = runner.build_command(
                prompt="inspect",
                output_path=output,
            )
            resumed = runner.build_command(
                prompt="continue",
                output_path=output,
                session_id="session-1",
            )

        self.assertIn("--sandbox", new_command)
        self.assertIn("read-only", new_command)
        self.assertIn('approval_policy="never"', new_command)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", new_command)
        self.assertEqual(["exec", "resume"], resumed[1:3])
        self.assertIn("session-1", resumed)


class TelegramTests(unittest.TestCase):
    def test_splits_long_messages_without_losing_text(self) -> None:
        text = ("line one\n" * 1500).strip()
        chunks = split_message(text, limit=1000)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1000 for chunk in chunks))
        self.assertEqual("".join(text.split()), "".join("".join(chunks).split()))

    def test_configures_command_menu_and_inline_keyboard_payloads(self) -> None:
        api = TelegramBotAPI("123456:abcdefghijklmnop")
        commands = [{"command": "status", "description": "查看状态"}]
        keyboard = {"inline_keyboard": [[{"text": "刷新", "callback_data": "status"}]]}
        with patch.object(api, "_request", return_value={"ok": True}) as request:
            api.set_my_commands(commands)
            api.set_chat_menu_button({"type": "commands"})
            api.send_message(123, "menu", reply_markup=keyboard)
            api.answer_callback_query("callback-1", text="完成")
            api.set_message_reaction(123, 456)

        calls = request.call_args_list
        command_data = urllib.parse.parse_qs(calls[0].args[1].decode("utf-8"))
        menu_data = urllib.parse.parse_qs(calls[1].args[1].decode("utf-8"))
        message_data = urllib.parse.parse_qs(calls[2].args[1].decode("utf-8"))
        callback_data = urllib.parse.parse_qs(calls[3].args[1].decode("utf-8"))
        reaction_data = urllib.parse.parse_qs(calls[4].args[1].decode("utf-8"))
        self.assertEqual("setMyCommands", calls[0].args[0])
        self.assertIn("status", command_data["commands"][0])
        self.assertEqual("setChatMenuButton", calls[1].args[0])
        self.assertEqual('{"type":"commands"}', menu_data["menu_button"][0])
        self.assertIn("inline_keyboard", message_data["reply_markup"][0])
        self.assertEqual("answerCallbackQuery", calls[3].args[0])
        self.assertEqual("完成", callback_data["text"][0])
        self.assertEqual("setMessageReaction", calls[4].args[0])
        self.assertIn("👀", reaction_data["reaction"][0])
        self.assertEqual("HTML", message_data["parse_mode"][0])

    def test_markdown_is_rendered_as_telegram_html(self) -> None:
        rendered = markdown_to_telegram_html(
            "# 标题\n\n"
            "**重点** 和 [链接](https://example.com)\n\n"
            "- 项目一\n"
            "- [x] 已完成\n\n"
            "```python\nprint('<ok>')\n```\n\n"
            "`inline`"
        )

        self.assertIn("<b>标题</b>", rendered)
        self.assertIn("<b>重点</b>", rendered)
        self.assertIn('<a href="https://example.com">链接</a>', rendered)
        self.assertIn("• 项目一", rendered)
        self.assertIn("☑ 已完成", rendered)
        self.assertIn(
            '<pre><code class="language-python">print(\'&lt;ok&gt;\')</code></pre>',
            rendered,
        )
        self.assertIn("<code>inline</code>", rendered)
        self.assertNotIn("**", plain_text_without_markdown("**重点**"))

    def test_send_message_falls_back_to_plain_text_on_parse_error(self) -> None:
        api = TelegramBotAPI("123456:abcdefghijklmnop")
        with patch.object(
            api,
            "_request",
            side_effect=[
                TelegramError("Bad Request: can't parse entities"),
                {"ok": True},
            ],
        ) as request:
            api.send_message(123, "**plain fallback**")

        second_payload = urllib.parse.parse_qs(
            request.call_args_list[1].args[1].decode("utf-8")
        )
        self.assertNotIn("parse_mode", second_payload)
        self.assertEqual("plain fallback", second_payload["text"][0])


class ServiceTests(unittest.TestCase):
    def test_authorized_task_is_persisted_and_enqueued(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            telegram = FakeTelegram()
            store = RemoteStore(root / "state.db")
            store.initialize()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
            )
            service.handle_update(
                _message_update(
                    update_id=1,
                    user_id=123,
                    chat_id=123,
                    text="fix tests",
                )
            )
            queued = store.queued_job_ids()

        self.assertEqual(1, len(queued))
        self.assertEqual(1, service._task_queue.qsize())
        self.assertIn("已排队", telegram.messages[0][1])
        self.assertIn("收到任务", telegram.messages[0][1])
        self.assertIn("对话：fix tests", telegram.messages[0][1])
        self.assertIn(
            "对话 ID：新会话创建后生成",
            telegram.messages[0][1],
        )

    def test_queue_confirmation_is_sent_before_worker_can_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            telegram = FakeTelegram()
            observed: list[str] = []
            telegram.on_send = lambda text: observed.append(
                "queued" if "已排队" in text else "other"
            )
            store = RemoteStore(root / "state.db")
            store.initialize()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
            )
            original_put = service._task_queue.put

            def observe_put(job_id: int) -> None:
                observed.append(f"put:{job_id}")
                original_put(job_id)

            service._task_queue.put = observe_put  # type: ignore[method-assign]
            service.handle_update(
                _message_update(
                    update_id=5,
                    user_id=123,
                    chat_id=123,
                    text="order test",
                )
            )

        self.assertEqual(["queued", "put:1"], observed)

    def test_unauthorized_user_cannot_queue_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            telegram = FakeTelegram()
            store = RemoteStore(root / "state.db")
            store.initialize()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
            )
            service.handle_update(
                _message_update(
                    update_id=2,
                    user_id=999,
                    chat_id=999,
                    text="run anything",
                )
            )
            queued = store.queued_job_ids()

        self.assertEqual([], queued)
        self.assertIn("未授权", telegram.messages[0][1])

    def test_repo_command_switches_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            config = _config(
                root,
                first,
                repositories={"one": first, "two": second},
            )
            store = RemoteStore(root / "state.db")
            store.initialize()
            telegram = FakeTelegram()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
            )
            service.handle_update(
                _message_update(
                    update_id=3,
                    user_id=123,
                    chat_id=123,
                    text="/repo two",
                )
            )
            repo = store.get_chat_repo(123, "one")

        self.assertEqual("two", repo)
        self.assertIn("已切换", telegram.messages[0][1])

    def test_start_command_sends_inline_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            telegram = FakeTelegram()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
            )
            service.handle_update(
                _message_update(
                    update_id=6,
                    user_id=123,
                    chat_id=123,
                    text="/start",
                )
            )

        self.assertTrue(telegram.markups[0])
        rows = telegram.markups[0]["inline_keyboard"]  # type: ignore[index]
        callbacks = {
            button["callback_data"]
            for row in rows  # type: ignore[union-attr]
            for button in row
        }
        self.assertEqual(
            {"status", "repos", "recent", "new", "cancel", "watch", "help"},
            callbacks,
        )

    def test_callback_switches_repository_and_answers_query(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            config = _config(
                root,
                first,
                repositories={"one": first, "two": second},
            )
            store = RemoteStore(root / "state.db")
            store.initialize()
            telegram = FakeTelegram()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
            )
            service.handle_update(
                _callback_update(
                    update_id=7,
                    user_id=123,
                    chat_id=123,
                    data="repo:two",
                )
            )
            repo = store.get_chat_repo(123, "one")

        self.assertEqual("two", repo)
        self.assertEqual(("callback-7", "已切换：two", False), telegram.callback_answers[0])
        self.assertIn("已切换到仓库：two", telegram.messages[0][1])
        self.assertIn("项目简介", telegram.messages[0][1])

    def test_repo_selection_lists_recent_tasks_with_numbered_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "one"
            second = root / "two"
            first.mkdir()
            second.mkdir()
            config = _config(
                root,
                first,
                repositories={"one": first, "two": second},
            )
            store = RemoteStore(root / "state.db")
            store.initialize()
            telegram = FakeTelegram()
            external = FakeExternalTaskMonitor(
                ExternalTaskState(tasks=(), available=True),
                recent=RecentTaskState(
                    tasks=(
                        RecentTask(
                            thread_id="thread-1",
                            title="完善远程控制",
                            prompt="任务主要内容：增加最近任务选择",
                            result="完成",
                            status="completed",
                            updated_at=100,
                        ),
                    ),
                    available=True,
                ),
            )
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
                external_tasks=external,
            )
            service.handle_update(
                _callback_update(
                    update_id=9,
                    user_id=123,
                    chat_id=123,
                    data="repo:two",
                )
            )

        rows = telegram.markups[0]["inline_keyboard"]  # type: ignore[index]
        task_button = rows[0][0]  # type: ignore[index]
        self.assertTrue(task_button["text"].startswith("1."))
        self.assertEqual("task:thread-1", task_button["callback_data"])
        self.assertIn("任务主要内容", telegram.messages[0][1])
        self.assertIn("点击序号继续会话", telegram.messages[0][1])

    def test_selecting_recent_task_resumes_that_thread(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            telegram = FakeTelegram()
            runner = FakeRunner()
            external = FakeExternalTaskMonitor(
                ExternalTaskState(tasks=(), available=True),
                recent=RecentTaskState(
                    tasks=(
                        RecentTask(
                            thread_id="thread-history-1",
                            title="历史任务",
                            prompt="继续完成历史任务",
                            result="已有结果",
                            status="completed",
                            updated_at=100,
                        ),
                    ),
                    available=True,
                ),
            )
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=runner,
                external_tasks=external,
            )
            service.handle_update(
                _callback_update(
                    update_id=10,
                    user_id=123,
                    chat_id=123,
                    data="task:thread-history-1",
                )
            )
            selected = store.get_session(123, "demo")
            service.handle_update(
                _message_update(
                    update_id=11,
                    user_id=123,
                    chat_id=123,
                    text="继续补充测试",
                )
            )
            job = store.get_job(store.queued_job_ids()[0])
            assert job is not None
            service._run_job(job)

        self.assertEqual("thread-history-1", selected)
        self.assertEqual("thread-history-1", runner.calls[0]["session_id"])
        self.assertIn("后续普通消息将在这个任务下继续", telegram.messages[0][1])
        self.assertIn("对话：历史任务", telegram.messages[1][1])
        self.assertIn("对话 ID：thread-history-1", telegram.messages[1][1])
        self.assertTrue(
            any(
                "对话：历史任务" in text
                and "对话 ID：thread-history-1" in text
                for _, text in telegram.messages
            )
        )

    def test_unauthorized_callback_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            telegram = FakeTelegram()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
            )
            service.handle_update(
                _callback_update(
                    update_id=8,
                    user_id=999,
                    chat_id=999,
                    data="status",
                )
            )

        self.assertEqual([], telegram.messages)
        self.assertEqual(("callback-8", "未授权", True), telegram.callback_answers[0])

    def test_worker_runs_job_and_saves_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            job = store.enqueue_job(
                update_id=4,
                chat_id=123,
                user_id=123,
                prompt="say PONG",
                repo="demo",
            )
            runner = FakeRunner()
            telegram = FakeTelegram()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=runner,
            )
            service._run_job(job)
            finished = store.get_job(job.id)
            session = store.get_session(123, "demo")

        self.assertEqual("succeeded", finished.status if finished else "")
        self.assertEqual("session-1", session)
        self.assertEqual(1, len(runner.calls))
        self.assertTrue(any("任务 #1 完成" in text for _, text in telegram.messages))
        self.assertTrue(
            any(
                "对话：say PONG" in text
                and "对话 ID：session-1" in text
                for _, text in telegram.messages
            )
        )
        self.assertTrue(any("任务结果：\nPONG" in text for _, text in telegram.messages))

    def test_failed_job_notification_contains_failure_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            job = store.enqueue_job(
                update_id=41,
                chat_id=123,
                user_id=123,
                prompt="make it fail",
                repo="demo",
            )
            telegram = FakeTelegram()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FailingRunner(),
            )
            service._run_job(job)
            finished = store.get_job(job.id)

        self.assertEqual("failed", finished.status if finished else "")
        self.assertTrue(
            any("任务结果：\n测试失败结果" in text for _, text in telegram.messages)
        )
        self.assertTrue(
            any(
                "对话：make it fail" in text
                and "对话 ID：新会话创建后生成" in text
                for _, text in telegram.messages
            )
        )

    def test_status_includes_external_app_task(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            external = FakeExternalTaskMonitor(
                ExternalTaskState(
                    tasks=(
                        ExternalTask(
                            thread_id="thread-1",
                            title="优化 SwiftUI 界面",
                            cwd=str(repo),
                            started_at=930,
                            updated_at=990,
                        ),
                    ),
                    available=True,
                )
            )
            service = CodexRemoteService(
                config=config,
                telegram=FakeTelegram(),
                store=store,
                runner=FakeRunner(),
                external_tasks=external,
            )
            status = service._status_text(123)

        self.assertIn("App 1 个", status)
        self.assertIn("优化 SwiftUI 界面", status)

    def test_auto_watches_running_external_tasks_for_all_users(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            external = FakeExternalTaskMonitor(
                ExternalTaskState(
                    tasks=(
                        ExternalTask(
                            thread_id="thread-auto",
                            title="All-task notification",
                            cwd=str(repo),
                            started_at=900,
                            updated_at=990,
                        ),
                    ),
                    available=True,
                )
            )
            service = CodexRemoteService(
                config=config,
                telegram=FakeTelegram(),
                store=store,
                runner=FakeRunner(),
                external_tasks=external,
            )
            service._auto_watch_external_tasks()
            pending = store.pending_watched_tasks()

        self.assertEqual(1, len(pending))
        self.assertEqual("thread-auto", pending[0].thread_id)
        self.assertEqual(123, pending[0].chat_id)

    def test_message_reaction_confirms_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            telegram = FakeTelegram()
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
                external_tasks=FakeExternalTaskMonitor(
                    ExternalTaskState(tasks=(), available=True)
                ),
            )
            service._safe_react(123, 456)

        self.assertEqual([(123, 456, "👀")], telegram.reactions)

    def test_watched_external_task_sends_completion_notification(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            store.watch_task(
                thread_id="thread-1",
                chat_id=123,
                repo="demo",
                title="Optimize UI",
                started_at=100,
            )
            telegram = FakeTelegram()
            external = FakeExternalTaskMonitor(
                ExternalTaskState(tasks=(), available=True),
                turn=TurnState(
                    thread_id="thread-1",
                    turn_id="turn-1",
                    status="completed",
                    started_at=100,
                    completed_at=200,
                    result_text="界面优化已经完成，并补充了测试。",
                ),
            )
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
                external_tasks=external,
            )
            service._check_watched_tasks()
            pending = store.pending_watched_tasks()

        self.assertEqual([], pending)
        self.assertIn("Codex App 任务已完成", telegram.messages[0][1])
        self.assertIn("Optimize UI", telegram.messages[0][1])
        self.assertIn("对话 ID：thread-1", telegram.messages[0][1])
        self.assertIn("界面优化已经完成", telegram.messages[0][1])

    def test_failed_external_task_notification_contains_failure_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            config = _config(root, repo)
            store = RemoteStore(root / "state.db")
            store.initialize()
            store.watch_task(
                thread_id="thread-failed",
                chat_id=123,
                repo="demo",
                title="Failing task",
                started_at=100,
            )
            telegram = FakeTelegram()
            external = FakeExternalTaskMonitor(
                ExternalTaskState(tasks=(), available=True),
                turn=TurnState(
                    thread_id="thread-failed",
                    turn_id="turn-failed",
                    status="failed",
                    started_at=100,
                    completed_at=200,
                    error_text="失败结果：上游接口返回 403",
                ),
            )
            service = CodexRemoteService(
                config=config,
                telegram=telegram,
                store=store,
                runner=FakeRunner(),
                external_tasks=external,
            )
            service._check_watched_tasks()

        self.assertIn("失败结果：", telegram.messages[0][1])
        self.assertIn("上游接口返回 403", telegram.messages[0][1])
        self.assertIn("对话 ID：thread-failed", telegram.messages[0][1])


class FakeExternalTaskMonitor:
    def __init__(
        self,
        state: ExternalTaskState,
        *,
        turn: TurnState | None = None,
        recent: RecentTaskState | None = None,
    ):
        self.state = state
        self.turn = turn
        self.recent = recent or RecentTaskState(tasks=(), available=True)

    @staticmethod
    def now() -> float:
        return 1000

    def running_for(self, _path: Path) -> ExternalTaskState:
        return self.state

    def latest_turn(self, _thread_id: str) -> TurnState | None:
        return self.turn

    def recent_for(
        self,
        _path: Path,
        _limit: int,
    ) -> RecentTaskState:
        return self.recent

    def resolve_thread(
        self,
        _path: Path,
        thread_id: str,
    ) -> RecentTask | None:
        return next(
            (
                task
                for task in self.recent.tasks
                if task.thread_id == thread_id
            ),
            None,
        )


def _create_external_state_db(path: Path, rows: tuple[tuple, ...]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                name TEXT,
                title TEXT,
                preview TEXT,
                first_user_message TEXT NOT NULL DEFAULT '',
                cwd TEXT NOT NULL,
                updated_at INTEGER NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO threads(
                id, name, title, preview, first_user_message,
                cwd, updated_at, archived
            ) VALUES (?, ?, '', '', '', ?, ?, ?)
            """,
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _create_external_history_db(path: Path, rows: tuple[tuple, ...]) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE thread_turns (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                error_json TEXT,
                first_user_item_id TEXT,
                final_agent_item_id TEXT,
                PRIMARY KEY (thread_id, turn_id)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO thread_turns(
                thread_id, turn_id, rollout_ordinal, status, started_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
        connection.execute(
            """
            CREATE TABLE thread_items (
                thread_id TEXT NOT NULL,
                turn_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                rollout_ordinal INTEGER NOT NULL,
                created_at_ms INTEGER NOT NULL,
                item_json TEXT NOT NULL,
                item_type TEXT NOT NULL DEFAULT '',
                updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
                started_at_ms INTEGER,
                completed_at_ms INTEGER,
                PRIMARY KEY (thread_id, turn_id, item_id)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()


def _add_external_turn_details(
    path: Path,
    *,
    thread_id: str,
    turn_id: str,
    user_text: str,
    result_text: str = "",
    error_text: str = "",
) -> None:
    user_item_id = f"user-{turn_id}"
    agent_item_id = f"agent-{turn_id}"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            UPDATE thread_turns
            SET error_json = ?, first_user_item_id = ?,
                final_agent_item_id = ?, completed_at = started_at
            WHERE thread_id = ? AND turn_id = ?
            """,
            (
                json.dumps({"message": error_text}, ensure_ascii=False)
                if error_text
                else None,
                user_item_id,
                agent_item_id if result_text else None,
                thread_id,
                turn_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO thread_items(
                thread_id, turn_id, item_id, rollout_ordinal,
                created_at_ms, item_json, item_type
            ) VALUES (?, ?, ?, 1, 0, ?, 'userMessage')
            """,
            (
                thread_id,
                turn_id,
                user_item_id,
                json.dumps(
                    {
                        "type": "userMessage",
                        "content": [{"type": "text", "text": user_text}],
                    },
                    ensure_ascii=False,
                ),
            ),
        )
        if result_text:
            connection.execute(
                """
                INSERT INTO thread_items(
                    thread_id, turn_id, item_id, rollout_ordinal,
                    created_at_ms, item_json, item_type
                ) VALUES (?, ?, ?, 2, 0, ?, 'agentMessage')
                """,
                (
                    thread_id,
                    turn_id,
                    agent_item_id,
                    json.dumps(
                        {"type": "agentMessage", "text": result_text},
                        ensure_ascii=False,
                    ),
                ),
            )
        connection.commit()
    finally:
        connection.close()


def _config(
    root: Path,
    repo: Path,
    *,
    repositories: dict[str, Path] | None = None,
) -> RemoteConfig:
    return RemoteConfig(
        project_root=root,
        data_dir=root / "data",
        database_path=root / "data" / "state.db",
        log_path=root / "data" / "test.log",
        bot_token="123456:abcdefghijklmnop",
        allowed_user_ids=frozenset({123}),
        repositories=repositories or {"demo": repo},
        default_repo="demo",
        codex_bin="codex",
        sandbox="workspace-write",
        model="",
        timeout_seconds=60,
        cancel_grace_seconds=2,
        poll_timeout_seconds=1,
        max_reply_chars=5000,
        max_prompt_chars=10000,
        recent_task_limit=5,
        require_private_chat=True,
        redact_values=("123456:abcdefghijklmnop",),
        codex_state_db=root / "missing-state.sqlite",
        codex_thread_history_db=root / "missing-thread-history.sqlite",
        external_active_window_seconds=21600,
        watch_poll_seconds=20,
    )


def _message_update(
    *,
    update_id: int,
    user_id: int,
    chat_id: int,
    text: str,
    chat_type: str = "private",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 10,
            "from": {"id": user_id, "username": "tester"},
            "chat": {"id": chat_id, "type": chat_type},
            "text": text,
        },
    }


def _callback_update(
    *,
    update_id: int,
    user_id: int,
    chat_id: int,
    data: str,
    chat_type: str = "private",
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id, "username": "tester"},
            "message": {
                "message_id": update_id + 20,
                "chat": {"id": chat_id, "type": chat_type},
            },
            "data": data,
        },
    }


if __name__ == "__main__":
    unittest.main()
