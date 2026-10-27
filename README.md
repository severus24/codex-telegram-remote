# codex-telegram-remote

简体中文 | [English](README.en.md)

通过 Telegram 私聊远程控制本机 Codex CLI 的自托管控制面板。

[完整实现指南](docs/implementation-guide.zh-CN.md) |
[架构说明](docs/architecture.md) |
[安全策略](SECURITY.md) |
[贡献指南](CONTRIBUTING.md)

> [!WARNING]
> 这是一个本地远程代码执行工具。必须使用专用 Telegram Bot、严格的 User ID
> 白名单、仓库白名单，以及权限最小的 Codex 沙箱。不要把主目录、生产密钥
> 目录或不受信任的仓库加入白名单。

## 功能

- Telegram 私聊 Long Polling，无需公网 IP 或 webhook
- 数字 Telegram User ID 白名单
- 多项目仓库白名单和项目简介
- SQLite 持久化任务队列、Session 和通知状态
- `codex exec` 单任务串行执行
- `codex exec resume` 继续指定历史任务
- 最近任务/对话列表与序号按钮
- Codex App 运行任务状态、完成结果和失败结果通知
- Telegram 命令菜单、内联按钮和 callback 交互
- Markdown 转 Telegram HTML，解析失败自动降级为纯文本
- `read-only` 和 `workspace-write` 沙箱
- macOS `launchd` 与 Linux `systemd --user` 服务模板

## 工作方式

```text
Telegram 私聊
      |
      | Long Polling
      v
codex-telegram-remote
      |
      +-- Telegram User ID / 仓库白名单
      +-- SQLite 任务队列与会话
      +-- codex exec / codex exec resume
      +-- 只读 Codex 线程历史适配器
      |
      v
Telegram 结果与完成通知
```

默认执行层使用 `codex exec`，适合一次性和后台任务。选择历史任务后，使用
`codex exec resume <thread_id>` 将后续消息追加到对应 Codex 线程。

当前版本通过 Codex 本地 SQLite 数据读取线程状态和历史结果。这部分属于
版本敏感适配器，Codex 数据结构变化时可能需要同步更新。

## 环境要求

- Python 3.11 或更高版本
- 已安装 Codex CLI
- Codex CLI 已完成登录
- Telegram Bot Token
- 可以访问 `api.telegram.org`

```bash
codex --version
codex login status
```

运行时只使用 Python 标准库，不需要 Telegram SDK、Docker 或公网服务。

## 安装

```bash
git clone https://github.com/severus24/codex-telegram-remote.git
cd codex-telegram-remote

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

## 初始化配置

在 `@BotFather` 创建一个独立 Bot，然后执行：

```bash
codex-telegram-remote init
```

配置文件默认位于：

```text
~/.config/codex-telegram-remote/config.env
```

文件权限会自动设置为 `0600`。

填写：

```dotenv
CODEX_REMOTE_BOT_TOKEN=YOUR_BOT_TOKEN
CODEX_REMOTE_ALLOWED_USER_IDS=YOUR_TELEGRAM_USER_ID
CODEX_REMOTE_REPOS=project=/absolute/path/to/project
CODEX_REMOTE_DEFAULT_REPO=project
CODEX_REMOTE_SANDBOX=workspace-write
```

多仓库格式：

```dotenv
CODEX_REMOTE_REPOS=project=/path/to/project;another=/path/to/another
```

## 获取 Telegram User ID

先给 Bot 发送一条私聊消息，然后执行：

```bash
codex-telegram-remote discover-user-id
```

把输出中的数字 `user_id` 写入
`CODEX_REMOTE_ALLOWED_USER_IDS`，不要使用 `@username`。

## 检查与运行

```bash
codex-telegram-remote check
codex-telegram-remote run
```

调试模式：

```bash
codex-telegram-remote run --debug
```

只执行一轮 Telegram 长轮询：

```bash
codex-telegram-remote run --once
```

## 安装系统服务

macOS：

```bash
./scripts/install-launchd.sh
./scripts/uninstall-launchd.sh
```

Linux：

```bash
./scripts/install-systemd-user.sh
./scripts/uninstall-systemd-user.sh
```

安装脚本会先运行配置检查，再加载用户级常驻服务。

## Telegram 命令

| 命令 | 作用 |
| --- | --- |
| `/start` | 打开控制面板 |
| `/status` | 查看 Telegram 与 Codex App 任务状态 |
| `/repos` | 查看所有项目仓库 |
| `/repo <alias>` | 切换仓库 |
| `/recent` | 查看最近任务和序号按钮 |
| `/watch` | 手动订阅当前运行任务 |
| `/new` | 清除当前仓库会话 |
| `/cancel` | 取消运行和排队中的 Telegram 任务 |
| `/id` | 查看 Telegram User ID |
| `/help` | 查看帮助 |

切换项目后，Bot 会显示：

- 项目简介
- 当前任务状态
- 最近 5 条任务/对话
- 带序号的继续会话按钮

选择序号后，后续普通消息会在所选任务下执行。可通过
`CODEX_REMOTE_RECENT_TASK_LIMIT` 将最近任务数量调整为 1 到 10 条。

## 任务通知

任务完成和失败都会推送 Telegram 通知，并附上：

- 任务标题
- 项目仓库
- 对话 ID
- 最终结果或错误原因
- 运行耗时（Telegram 任务）

所有扫描到的 Codex App 运行任务会自动加入通知订阅。通知去重状态保存在
SQLite，服务重启后不会重复发送已成功通知的任务。

## Markdown 渲染

常见 Markdown 会转换为 Telegram HTML：

- 标题、粗体、斜体、删除线
- 链接、列表、任务勾选框
- 行内代码和围栏代码块
- 简单表格转换为 `│` 分隔文本

如果 Telegram 拒绝 HTML，客户端会自动去掉 Markdown 符号并以纯文本重试。

## 数据目录

macOS：

```text
~/Library/Application Support/codex-telegram-remote
```

Linux：

```text
$XDG_STATE_HOME/codex-telegram-remote
```

未设置 `XDG_STATE_HOME` 时使用：

```text
~/.local/state/codex-telegram-remote
```

主要文件：

```text
state.db
codex-telegram-remote.log
stdout.log
stderr.log
tmp/
```

## 安全

阅读 [SECURITY.md](SECURITY.md)。

不要提交或公开：

- Telegram Bot Token
- Telegram User ID 和私聊 ID
- `config.env` 或 `.env`
- Codex 本地数据库
- 包含敏感信息的仓库路径

## 开发

```bash
python3 -m pip install -e .
make check
```

测试使用模拟的 Telegram 和 Codex 组件，不会调用外部 API。

## 文档

- [完整实现指南（中文）](docs/implementation-guide.zh-CN.md)
- [English README](README.en.md)
- [架构说明](docs/architecture.md)
- [安全策略](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)

## 许可证

MIT
