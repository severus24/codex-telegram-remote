from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO


LOGGER = logging.getLogger("codex_telegram_remote.runner")


class CodexRunError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CodexRunResult:
    text: str
    session_id: str
    exit_code: int
    elapsed_seconds: float
    canceled: bool = False
    timed_out: bool = False


@dataclass(frozen=True, slots=True)
class ParsedEvent:
    session_id: str = ""
    agent_text: str = ""
    progress: str = ""


class CodexRunner:
    def __init__(
        self,
        *,
        codex_bin: str,
        sandbox: str,
        model: str,
        timeout_seconds: int,
        cancel_grace_seconds: int,
        temp_dir: Path,
    ):
        self.codex_bin = codex_bin
        self.sandbox = sandbox
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.cancel_grace_seconds = cancel_grace_seconds
        self.temp_dir = temp_dir

    def build_command(
        self,
        *,
        prompt: str,
        output_path: Path,
        session_id: str = "",
    ) -> list[str]:
        if session_id:
            command = [
                self.codex_bin,
                "exec",
                "resume",
                "--json",
                "-c",
                'approval_policy="never"',
                "--output-last-message",
                str(output_path),
            ]
            if self.model:
                command.extend(["--model", self.model])
            command.extend([session_id, prompt])
            return command

        command = [
            self.codex_bin,
            "exec",
            "--json",
            "--sandbox",
            self.sandbox,
            "-c",
            'approval_policy="never"',
            "--cd",
            str(self.temp_dir.parent),
            "--output-last-message",
            str(output_path),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append(prompt)
        return command

    def run(
        self,
        *,
        prompt: str,
        cwd: Path,
        session_id: str = "",
        cancel_event: threading.Event | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> CodexRunResult:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        output_handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="last-message-",
            suffix=".txt",
            dir=self.temp_dir,
            delete=False,
        )
        output_path = Path(output_handle.name)
        output_handle.close()
        command = self._command_for_cwd(
            prompt=prompt,
            cwd=cwd,
            output_path=output_path,
            session_id=session_id,
        )
        LOGGER.info("starting codex job cwd=%s resume=%s", cwd, bool(session_id))
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
            env=self._environment(),
        )
        events: queue.Queue[str | None] = queue.Queue()
        assert process.stdout is not None
        reader = threading.Thread(
            target=_read_lines,
            args=(process.stdout, events),
            name="codex-output-reader",
            daemon=True,
        )
        reader.start()

        detected_session = session_id
        last_agent_text = ""
        log_tail: list[str] = []
        canceled = False
        timed_out = False
        deadline = started + self.timeout_seconds

        while True:
            if cancel_event and cancel_event.is_set() and process.poll() is None:
                canceled = True
                self._terminate(process)
                break
            if time.monotonic() >= deadline and process.poll() is None:
                timed_out = True
                self._terminate(process)
                break
            try:
                line = events.get(timeout=0.5)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            if line is None:
                break
            parsed = parse_jsonl_event(line)
            if parsed.session_id and not detected_session:
                detected_session = parsed.session_id
                if on_session:
                    on_session(detected_session)
            if parsed.agent_text:
                last_agent_text = parsed.agent_text
            if parsed.progress:
                log_tail.append(parsed.progress)
                log_tail = log_tail[-30:]
            elif line.strip() and not line.lstrip().startswith("{"):
                log_tail.append(line.strip())
                log_tail = log_tail[-30:]

        try:
            exit_code = process.wait(timeout=self.cancel_grace_seconds + 2)
        except subprocess.TimeoutExpired:
            self._kill(process)
            exit_code = process.wait(timeout=3)
        reader.join(timeout=1)
        elapsed = time.monotonic() - started
        final_text = _read_output(output_path) or last_agent_text
        try:
            output_path.unlink()
        except OSError:
            LOGGER.debug("failed to remove temp output path=%s", output_path)

        if canceled:
            return CodexRunResult(
                text="任务已取消。",
                session_id=detected_session,
                exit_code=exit_code,
                elapsed_seconds=elapsed,
                canceled=True,
            )
        if timed_out:
            return CodexRunResult(
                text=f"任务超过 {self.timeout_seconds} 秒，已终止。",
                session_id=detected_session,
                exit_code=exit_code,
                elapsed_seconds=elapsed,
                timed_out=True,
            )
        if exit_code != 0:
            detail = "\n".join(log_tail[-12:]).strip()
            raise CodexRunError(
                f"Codex 退出码 {exit_code}"
                + (f"\n{detail[:1800]}" if detail else "")
            )
        return CodexRunResult(
            text=final_text or "任务完成，但没有返回文本。",
            session_id=detected_session,
            exit_code=exit_code,
            elapsed_seconds=elapsed,
        )

    def _command_for_cwd(
        self,
        *,
        prompt: str,
        cwd: Path,
        output_path: Path,
        session_id: str,
    ) -> list[str]:
        command = self.build_command(
            prompt=prompt,
            output_path=output_path,
            session_id=session_id,
        )
        if not session_id:
            cd_index = command.index("--cd")
            command[cd_index + 1] = str(cwd)
        return command

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        environment.setdefault("NO_COLOR", "1")
        return environment

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=self.cancel_grace_seconds)
        except subprocess.TimeoutExpired:
            self._kill(process)

    @staticmethod
    def _kill(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


def parse_jsonl_event(line: str) -> ParsedEvent:
    line = line.strip()
    if not line or not line.startswith("{"):
        return ParsedEvent(
            progress=line if line and not line.startswith("Reading additional") else ""
        )
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return ParsedEvent(progress=line)
    if not isinstance(payload, dict):
        return ParsedEvent()
    event_type = str(payload.get("type", ""))
    session_id = ""
    if event_type == "thread.started":
        session_id = str(payload.get("thread_id", ""))
    agent_text = ""
    item = payload.get("item")
    if (
        event_type == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "agent_message"
    ):
        agent_text = str(item.get("text", ""))
    progress = _progress_label(event_type, item)
    return ParsedEvent(
        session_id=session_id,
        agent_text=agent_text,
        progress=progress,
    )


def _progress_label(event_type: str, item: Any) -> str:
    if event_type == "thread.started":
        return "Codex 会话已创建"
    if event_type == "turn.started":
        return "Codex 开始处理"
    if event_type == "turn.completed":
        return "Codex 处理完成"
    if event_type == "item.completed" and isinstance(item, dict):
        item_type = str(item.get("type", ""))
        if item_type == "command_execution":
            return "命令执行完成"
        if item_type == "file_change":
            return "文件修改完成"
        if item_type == "agent_message":
            return "生成最终回复"
    return ""


def _read_lines(stream: TextIO, events: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            events.put(line.rstrip("\n"))
    finally:
        events.put(None)
        stream.close()


def _read_output(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
