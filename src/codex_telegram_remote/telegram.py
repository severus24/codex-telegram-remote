from __future__ import annotations

import html
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from codex_telegram_remote.store import utc_now
from codex_telegram_remote.http_client import HttpRequestError, request_body


TELEGRAM_SAFE_LIMIT = 3000


class TelegramError(RuntimeError):
    pass


class TelegramBotAPI:
    def __init__(self, bot_token: str, *, timeout: int = 35):
        self.bot_token = bot_token
        self.timeout = timeout

    def get_updates(self, *, offset: int, timeout_seconds: int) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                "offset": offset,
                "timeout": timeout_seconds,
                "allowed_updates": json.dumps(["message", "callback_query"]),
            }
        )
        payload = self._request("getUpdates", query.encode("utf-8"))
        result = payload.get("result")
        if not isinstance(result, list):
            raise TelegramError("Telegram getUpdates 返回格式错误")
        return [item for item in result if isinstance(item, dict)]

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        render_markdown: bool = True,
    ) -> None:
        rendered_text = (
            markdown_to_telegram_html(text)
            if render_markdown
            else text
        )
        values = {
            "chat_id": str(chat_id),
            "text": rendered_text[:4096],
            "disable_web_page_preview": "true",
        }
        if render_markdown:
            values["parse_mode"] = "HTML"
        if reply_to_message_id:
            values["reply_to_message_id"] = str(reply_to_message_id)
        if reply_markup:
            values["reply_markup"] = json.dumps(
                reply_markup,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        try:
            self._request(
                "sendMessage",
                urllib.parse.urlencode(values).encode("utf-8"),
            )
        except TelegramError as error:
            if not render_markdown or not _is_parse_error(error):
                raise
            values.pop("parse_mode", None)
            values["text"] = _telegram_html_to_plain(rendered_text)[:4096]
            self._request(
                "sendMessage",
                urllib.parse.urlencode(values).encode("utf-8"),
            )

    def send_long_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
        render_markdown: bool = True,
    ) -> None:
        chunks = split_message(text)
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            label = f"[{index}/{total}]\n" if total > 1 else ""
            self.send_message(
                chat_id,
                label + chunk,
                reply_to_message_id=reply_to_message_id if index == 1 else None,
                reply_markup=reply_markup if index == total else None,
                render_markdown=render_markdown,
            )
            if index < total:
                time.sleep(0.35)

    def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        values = {
            "commands": json.dumps(
                commands,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        }
        self._request(
            "setMyCommands",
            urllib.parse.urlencode(values).encode("utf-8"),
        )

    def set_chat_menu_button(self, menu_button: dict[str, str]) -> None:
        values = {
            "menu_button": json.dumps(
                menu_button,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        }
        self._request(
            "setChatMenuButton",
            urllib.parse.urlencode(values).encode("utf-8"),
        )

    def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        values = {"callback_query_id": callback_query_id}
        if text:
            values["text"] = text[:200]
        if show_alert:
            values["show_alert"] = "true"
        self._request(
            "answerCallbackQuery",
            urllib.parse.urlencode(values).encode("utf-8"),
        )

    def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        *,
        emoji: str = "👀",
    ) -> None:
        values = {
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "reaction": json.dumps(
                [{"type": "emoji", "emoji": emoji}],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        }
        self._request(
            "setMessageReaction",
            urllib.parse.urlencode(values).encode("utf-8"),
        )

    def _request(self, method: str, data: bytes) -> dict[str, Any]:
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "codex-telegram-remote/0.1",
            },
        )
        try:
            body = request_body(
                request,
                timeout=self.timeout,
                attempts=3,
            )
        except HttpRequestError as error:
            raise TelegramError(self._redact(f"Telegram API {error}")) from error
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise TelegramError("Telegram API 返回了无效 JSON") from error
        if not payload.get("ok"):
            raise TelegramError(
                self._redact(f"Telegram API 调用失败: {payload}")
            )
        return payload

    def _redact(self, text: str) -> str:
        return text.replace(self.bot_token, "<redacted>")


def split_message(text: str, limit: int = TELEGRAM_SAFE_LIMIT) -> list[str]:
    text = text.strip() or "任务完成，但没有返回文本。"
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def markdown_to_telegram_html(text: str) -> str:
    code_blocks: dict[str, str] = {}

    def stash_code_block(match: re.Match[str]) -> str:
        language = match.group(1).strip()
        code = html.escape(match.group(2).strip("\n"), quote=False)
        class_attribute = (
            f' class="language-{language}"'
            if re.fullmatch(r"[A-Za-z0-9_+.-]+", language)
            else ""
        )
        token = f"\x00CODE{len(code_blocks)}\x00"
        code_blocks[token] = (
            f"<pre><code{class_attribute}>{code}</code></pre>"
        )
        return token

    text = re.sub(
        r"```([^\n`]*)\n(.*?)```",
        stash_code_block,
        text,
        flags=re.DOTALL,
    )
    output: list[str] = []
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped in code_blocks:
            output.append(stripped)
            continue
        if not stripped:
            output.append("")
            continue
        if re.fullmatch(r"\s*[-*_]{3,}\s*", raw_line):
            output.append("────────")
            continue
        if _is_table_separator(stripped):
            continue
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            output.append(f"<b>{_format_inline(heading)}</b>")
            continue
        if stripped.startswith(">"):
            quote = stripped.lstrip(">").strip()
            output.append(f"<blockquote>{_format_inline(quote)}</blockquote>")
            continue
        if "|" in stripped:
            cells = [
                _format_inline(cell.strip())
                for cell in stripped.strip("|").split("|")
            ]
            output.append(" │ ".join(cells))
            continue
        checkbox = re.match(r"^\s*[-*+]\s+\[([ xX])]\s+(.*)$", raw_line)
        if checkbox:
            mark = "☑" if checkbox.group(1).lower() == "x" else "☐"
            output.append(f"{mark} {_format_inline(checkbox.group(2))}")
            continue
        bullet = re.match(r"^\s*[-*+]\s+(.*)$", raw_line)
        if bullet:
            output.append(f"• {_format_inline(bullet.group(1))}")
            continue
        output.append(_format_inline(raw_line))
    rendered = "\n".join(output)
    for token, code_html in code_blocks.items():
        rendered = rendered.replace(token, code_html)
    return rendered


def plain_text_without_markdown(text: str) -> str:
    return _telegram_html_to_plain(markdown_to_telegram_html(text))


def _format_inline(value: str) -> str:
    inline_tokens: dict[str, str] = {}

    def stash_inline_code(match: re.Match[str]) -> str:
        token = f"\x00INLINE{len(inline_tokens)}\x00"
        inline_tokens[token] = (
            f"<code>{html.escape(match.group(1), quote=False)}</code>"
        )
        return token

    value = re.sub(r"`([^`\n]+)`", stash_inline_code, value)

    def replace_link(match: re.Match[str]) -> str:
        label = html.escape(match.group(1), quote=True)
        target = match.group(2).strip()
        parsed = urllib.parse.urlparse(target)
        if parsed.scheme not in {"http", "https", "tg", "mailto"}:
            return label
        token = f"\x00LINK{len(inline_tokens)}\x00"
        inline_tokens[token] = (
            f'<a href="{html.escape(target, quote=True)}">{label}</a>'
        )
        return token

    value = re.sub(
        r"!?\[([^\]]+)]\(([^)\s]+)\)",
        replace_link,
        value,
    )
    value = html.escape(value, quote=False)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"__(.+?)__", r"<b>\1</b>", value)
    value = re.sub(r"~~(.+?)~~", r"<s>\1</s>", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", value)
    value = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<i>\1</i>", value)
    for token, rendered in inline_tokens.items():
        value = value.replace(token, rendered)
    return value


def _is_table_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell) for cell in cells
    )


def _telegram_html_to_plain(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value))


def _is_parse_error(error: TelegramError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "can't parse entities",
            "unsupported start tag",
            "can't find end tag",
            "bad request",
        )
    )


def discover_user_ids(api: TelegramBotAPI) -> list[dict[str, str]]:
    updates = api.get_updates(offset=0, timeout_seconds=0)
    found: dict[int, dict[str, str]] = {}
    for update in updates:
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        sender = message.get("from")
        chat = message.get("chat")
        if not isinstance(sender, dict) or not isinstance(chat, dict):
            continue
        user_id = sender.get("id")
        chat_id = chat.get("id")
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            continue
        found[user_id] = {
            "user_id": str(user_id),
            "chat_id": str(chat_id),
            "chat_type": str(chat.get("type", "")),
            "username": str(sender.get("username", "")),
            "detected_at": utc_now(),
        }
    return list(found.values())
