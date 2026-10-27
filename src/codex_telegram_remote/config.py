from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path.cwd().resolve()
CONFIG_HOME = Path(
    os.environ.get(
        "CODEX_REMOTE_CONFIG_DIR",
        str(Path.home() / ".config" / "codex-telegram-remote"),
    )
).expanduser()
REMOTE_ENV_PATH = CONFIG_HOME / "config.env"
VALID_SANDBOXES = {"read-only", "workspace-write"}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    value = _env(name, "")
    return int(value) if value else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name, "")
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class RemoteConfig:
    project_root: Path
    data_dir: Path
    database_path: Path
    log_path: Path
    bot_token: str
    allowed_user_ids: frozenset[int]
    repositories: dict[str, Path]
    default_repo: str
    codex_bin: str
    sandbox: str
    model: str
    timeout_seconds: int
    cancel_grace_seconds: int
    poll_timeout_seconds: int
    max_reply_chars: int
    max_prompt_chars: int
    recent_task_limit: int
    require_private_chat: bool
    redact_values: tuple[str, ...]
    codex_state_db: Path
    codex_thread_history_db: Path
    external_active_window_seconds: int
    watch_poll_seconds: int

    def validate(self, *, require_users: bool = True) -> list[str]:
        errors: list[str] = []
        if not self.bot_token:
            errors.append("未配置 CODEX_REMOTE_BOT_TOKEN")
        if require_users and not self.allowed_user_ids:
            errors.append("未配置 CODEX_REMOTE_ALLOWED_USER_IDS")
        if not self.repositories:
            errors.append("CODEX_REMOTE_REPOS 中没有可用仓库")
        if self.default_repo not in self.repositories:
            errors.append(
                f"默认仓库 {self.default_repo!r} 不在 CODEX_REMOTE_REPOS 中"
            )
        for alias, path in self.repositories.items():
            if len(f"repo:{alias}".encode("utf-8")) > 64:
                errors.append(
                    f"仓库别名 {alias!r} 过长，无法用于 Telegram 按钮"
                )
            if not path.exists():
                errors.append(f"仓库 {alias!r} 不存在: {path}")
            elif not path.is_dir():
                errors.append(f"仓库 {alias!r} 不是目录: {path}")
        if self.sandbox not in VALID_SANDBOXES:
            errors.append(
                "CODEX_REMOTE_SANDBOX 只能是 read-only 或 workspace-write"
            )
        if self.timeout_seconds <= 0:
            errors.append("CODEX_REMOTE_TIMEOUT_SECONDS 必须大于 0")
        if self.max_reply_chars < 500:
            errors.append("CODEX_REMOTE_MAX_REPLY_CHARS 不能小于 500")
        if not 1 <= self.recent_task_limit <= 10:
            errors.append("CODEX_REMOTE_RECENT_TASK_LIMIT 必须在 1 到 10 之间")
        if self.external_active_window_seconds <= 0:
            errors.append(
                "CODEX_REMOTE_EXTERNAL_ACTIVE_WINDOW_SECONDS 必须大于 0"
            )
        if self.watch_poll_seconds <= 0:
            errors.append("CODEX_REMOTE_WATCH_POLL_SECONDS 必须大于 0")
        return errors

    def repository_for(self, chat_id: int, override: str = "") -> str:
        return override or self.default_repo


def load_config(
    *,
    env_path: Path | None = None,
    project_root: Path | None = None,
) -> RemoteConfig:
    load_dotenv(env_path or REMOTE_ENV_PATH)
    root = (project_root or PROJECT_ROOT).expanduser().resolve()
    data_dir = _path(
        _env("CODEX_REMOTE_DATA_DIR", str(default_data_dir())),
        root,
    )
    repositories = _parse_repositories(
        _env("CODEX_REMOTE_REPOS", f"twitter={root}"),
        root,
    )
    default_repo = _env(
        "CODEX_REMOTE_DEFAULT_REPO",
        next(iter(repositories), ""),
    )
    redact_values = tuple(
        dict.fromkeys(
            value.strip()
            for value in (
                _env("CODEX_REMOTE_REDACT_VALUES", "").split(",")
                + [_env("CODEX_REMOTE_BOT_TOKEN", "")]
            )
            if len(value.strip()) >= 6
        )
    )
    state_db_value = _env("CODEX_REMOTE_CODEX_STATE_DB", "")
    thread_history_value = _env("CODEX_REMOTE_CODEX_THREAD_HISTORY_DB", "")
    return RemoteConfig(
        project_root=root,
        data_dir=data_dir,
        database_path=data_dir / "state.db",
        log_path=data_dir / "codex-remote.log",
        bot_token=_env("CODEX_REMOTE_BOT_TOKEN", ""),
        allowed_user_ids=_parse_user_ids(
            _env("CODEX_REMOTE_ALLOWED_USER_IDS", "")
        ),
        repositories=repositories,
        default_repo=default_repo,
        codex_bin=_env("CODEX_REMOTE_CODEX_BIN", "codex"),
        sandbox=_env("CODEX_REMOTE_SANDBOX", "workspace-write"),
        model=_env("CODEX_REMOTE_MODEL", ""),
        timeout_seconds=_env_int("CODEX_REMOTE_TIMEOUT_SECONDS", 1800),
        cancel_grace_seconds=_env_int("CODEX_REMOTE_CANCEL_GRACE_SECONDS", 5),
        poll_timeout_seconds=_env_int("CODEX_REMOTE_POLL_TIMEOUT_SECONDS", 25),
        max_reply_chars=_env_int("CODEX_REMOTE_MAX_REPLY_CHARS", 18000),
        max_prompt_chars=_env_int("CODEX_REMOTE_MAX_PROMPT_CHARS", 12000),
        recent_task_limit=_env_int("CODEX_REMOTE_RECENT_TASK_LIMIT", 5),
        require_private_chat=_env_bool(
            "CODEX_REMOTE_REQUIRE_PRIVATE_CHAT",
            True,
        ),
        redact_values=redact_values,
        codex_state_db=(
            _path(state_db_value, root)
            if state_db_value
            else _latest_codex_database("state_*.sqlite")
        ),
        codex_thread_history_db=(
            _path(thread_history_value, root)
            if thread_history_value
            else _latest_codex_database("thread_history_*.sqlite")
        ),
        external_active_window_seconds=_env_int(
            "CODEX_REMOTE_EXTERNAL_ACTIVE_WINDOW_SECONDS",
            21600,
        ),
        watch_poll_seconds=_env_int("CODEX_REMOTE_WATCH_POLL_SECONDS", 20),
    )


def _path(value: str, root: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _parse_user_ids(value: str) -> frozenset[int]:
    result: set[int] = set()
    for item in value.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            result.add(int(item))
        except ValueError as error:
            raise ValueError(f"无效的 Telegram User ID: {item}") from error
    return frozenset(result)


def _parse_repositories(value: str, root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for item in value.replace("\n", ";").split(";"):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(
                "CODEX_REMOTE_REPOS 格式应为 alias=/absolute/path;alias2=/path2"
            )
        alias, path_text = item.split("=", 1)
        alias = alias.strip()
        if not alias or any(char.isspace() for char in alias):
            raise ValueError(f"无效的仓库别名: {alias!r}")
        result[alias] = _path(path_text.strip(), root)
    return result


def _latest_codex_database(pattern: str) -> Path:
    codex_home = Path.home() / ".codex"
    candidates = sorted(
        codex_home.glob(pattern),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else codex_home / pattern


def default_data_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(
            os.environ.get(
                "XDG_STATE_HOME",
                str(Path.home() / ".local" / "state"),
            )
        )
    return base / "codex-telegram-remote"
