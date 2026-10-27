from __future__ import annotations

import argparse
import fcntl
import importlib.resources
import json
import logging
import shutil
import signal
import subprocess
import sys
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from codex_telegram_remote import __version__
from codex_telegram_remote.config import (
    REMOTE_ENV_PATH,
    RemoteConfig,
    load_config,
)
from codex_telegram_remote.runner import CodexRunner
from codex_telegram_remote.service import CodexRemoteService
from codex_telegram_remote.store import RemoteStore
from codex_telegram_remote.telegram import TelegramBotAPI, discover_user_ids


LOGGER = logging.getLogger("codex_telegram_remote")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-telegram-remote",
        description="通过 Telegram 远程控制本机 Codex",
    )
    parser.add_argument("--env", type=Path, help="自定义 .env 路径")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--init", action="store_true", help="创建默认配置文件")
    parser.add_argument("--force", action="store_true", help="覆盖已有配置文件")
    parser.add_argument("--check", action="store_true", help="只检查配置与 Codex 环境")
    parser.add_argument(
        "--discover-user-id",
        action="store_true",
        help="读取最近 Telegram 消息并显示 User ID",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只执行一轮 Telegram 长轮询后退出",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(_normalize_command(argv))
    try:
        if args.init:
            return _initialize_config(args.env, force=args.force)
        config = load_config(env_path=args.env)
        _prepare_runtime(config, debug=args.debug)

        if args.discover_user_id:
            api = TelegramBotAPI(config.bot_token)
            if not config.bot_token:
                raise RuntimeError("未配置 Telegram Bot Token")
            users = discover_user_ids(api)
            print(json.dumps(users, ensure_ascii=False, indent=2))
            return 0

        errors = config.validate()
        errors.extend(_codex_environment_errors(config))
        if errors:
            for error in errors:
                print(f"配置错误：{error}", file=sys.stderr)
            return 2
        if args.check:
            print("配置检查通过")
            print(f"数据库：{config.database_path}")
            print(
                "仓库："
                + "；".join(
                    f"{alias}={path}"
                    for alias, path in sorted(config.repositories.items())
                )
            )
            _print_codex_status(config)
            return 0

        telegram = TelegramBotAPI(config.bot_token)
        store = RemoteStore(config.database_path)
        runner = CodexRunner(
            codex_bin=config.codex_bin,
            sandbox=config.sandbox,
            model=config.model,
            timeout_seconds=config.timeout_seconds,
            cancel_grace_seconds=config.cancel_grace_seconds,
            temp_dir=config.data_dir / "tmp",
        )
        service = CodexRemoteService(
            config=config,
            telegram=telegram,
            store=store,
            runner=runner,
        )

        def stop_service(signum: int, _frame: object) -> None:
            LOGGER.info("received signal=%s", signum)
            service.stop()

        signal.signal(signal.SIGTERM, stop_service)
        signal.signal(signal.SIGINT, stop_service)
        with _service_lock(config):
            service.run(once=args.once)
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        LOGGER.exception("startup failed")
        print(f"错误：{error}", file=sys.stderr)
        return 1


def _normalize_command(argv: list[str] | None) -> list[str] | None:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        return list(argv)
    aliases = {
        "run": None,
        "check": "--check",
        "discover-user-id": "--discover-user-id",
        "init": "--init",
    }
    command = argv[0]
    if command not in aliases:
        return list(argv)
    translated = list(argv[1:])
    flag = aliases[command]
    return ([flag] + translated) if flag else translated


def _initialize_config(env_path: Path | None, *, force: bool) -> int:
    target = (env_path or REMOTE_ENV_PATH).expanduser()
    if target.exists() and not force:
        print(f"配置文件已存在：{target}", file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    template = importlib.resources.files(
        "codex_telegram_remote"
    ).joinpath("config.example.env")
    target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    target.chmod(0o600)
    print(f"已创建配置文件：{target}")
    print("请填写 Bot Token、User ID 和仓库路径，然后运行 check。")
    return 0


def _prepare_runtime(config: RemoteConfig, *, debug: bool) -> None:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / "tmp").mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            config.log_path,
            maxBytes=2_000_000,
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(),
    ]
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _codex_environment_errors(config: RemoteConfig) -> list[str]:
    errors: list[str] = []
    binary = shutil.which(config.codex_bin)
    if not binary:
        errors.append(f"找不到 Codex 命令: {config.codex_bin}")
        return errors
    version = subprocess.run(
        [binary, "--version"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if version.returncode != 0:
        errors.append(f"Codex --version 失败: {version.stderr.strip()}")
    login = subprocess.run(
        [binary, "login", "status"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if login.returncode != 0:
        errors.append(f"Codex 未登录: {login.stderr.strip() or login.stdout.strip()}")
    return errors


def _print_codex_status(config: RemoteConfig) -> None:
    binary = shutil.which(config.codex_bin) or config.codex_bin
    for arguments in (["--version"], ["login", "status"]):
        result = subprocess.run(
            [binary, *arguments],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        print((result.stdout or result.stderr).strip())


@contextmanager
def _service_lock(config: RemoteConfig) -> Iterator[None]:
    lock_path = config.data_dir / ".service.lock"
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("已有另一个 Codex Telegram 服务在运行") from error
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


if __name__ == "__main__":
    raise SystemExit(main())
