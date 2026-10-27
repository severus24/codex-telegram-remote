# Telegram 远程操作 Codex 完整实现指南

本文档对应当前 `codex-telegram-remote` 仓库中的
`src/codex_telegram_remote/` 实现，目标是让 Telegram
私聊成为本机 Codex 的远程控制入口，同时提供：

- 项目仓库切换和项目简介
- 最近任务/对话浏览与选择
- 在指定 Codex 线程下继续对话
- Telegram 与 Codex App 任务状态监控
- 所有任务完成/失败通知及最终结果
- Markdown 内容渲染和纯文本降级
- Telegram 命令菜单、内联按钮和 callback 交互
- 私聊白名单、仓库白名单和受限沙箱

本文不包含真实 Bot Token、Telegram User ID 或其他本地密钥。

---

## 1. 当前系统定位

当前实现采用“Telegram 控制层 + 本机 Codex CLI”的组合：

```text
Telegram 私聊
    |
    | Long Polling
    v
Codex Remote Service
    |
    +-- Telegram User ID 白名单
    +-- 仓库白名单
    +-- SQLite 任务/会话队列
    +-- 单 worker 执行 codex exec / codex exec resume
    +-- Codex App 状态与历史只读监控
    |
    v
Codex CLI 子进程
    |
    v
Telegram 结果通知
```

这套实现不需要公网 IP，不需要 webhook，也不需要在 Telegram 侧部署服务。

需要明确两个 Codex 入口的区别：

1. Telegram Bot 启动的普通任务使用 `codex exec`。
2. Codex App 自己的对话由 App 的 app-server 管理。

当前实现通过只读方式读取 Codex App 的线程历史，用于：

- 显示 App 内运行中的任务
- 列出最近任务/对话
- 在任务完成后读取最终 agent 回复
- 将选中的线程 ID 交给 `codex exec resume`

当前不会主动向 Codex App 的 app-server 写入新线程。选择已有 App 线程后，
后续消息会通过 `codex exec resume <thread_id>` 追加到该线程；Codex App
可能需要重新进入会话或刷新后才能看到外部追加的新 turn。

还可以直接使用官方 ChatGPT 手机的 Codex Remote 功能作为替代方案：

- <https://developers.openai.com/blog/mastering-codex-remote-for-engineering>

---

## 2. 目录结构

```text
codex-telegram-remote/
├── src/codex_telegram_remote/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── config.py
│   ├── config.example.env
│   ├── external_tasks.py
│   ├── http_client.py
│   ├── project_summary.py
│   ├── runner.py
│   ├── service.py
│   ├── store.py
│   └── telegram.py
├── config/example.env
├── docs/
│   ├── architecture.md
│   └── implementation-guide.zh-CN.md
├── launchd/
├── systemd/
├── scripts/
├── tests/
├── pyproject.toml
├── README.md
├── SECURITY.md
└── LICENSE
```

核心模块职责：

| 模块 | 职责 |
| --- | --- |
| `config.py` | 读取独立环境文件、校验配置、自动发现 Codex 数据库 |
| `telegram.py` | Telegram Bot API、长轮询、HTML 渲染、reaction、callback |
| `store.py` | SQLite 状态、任务队列、Session、完成通知订阅 |
| `runner.py` | 执行 `codex exec`、JSONL 解析、取消和超时 |
| `external_tasks.py` | 读取 Codex App 运行任务、最终结果和最近历史 |
| `project_summary.py` | 从 README、PRD、HTML 和项目结构生成简介 |
| `service.py` | 命令路由、任务 worker、通知 watcher、内联菜单 |
| `cli.py` | CLI 入口、配置初始化、检查和 User ID 发现 |

---

## 3. 前置条件

### 3.1 系统环境

- macOS 或 Linux
- Python 3.11 或更高版本
- Codex CLI 已安装
- Codex CLI 已完成登录
- Telegram 可以访问 `api.telegram.org`
- 可选：本地代理或分流工具

验证：

```bash
python3 --version
codex --version
codex login status
```

当前实现使用 Python 标准库，不要求额外安装 Telegram SDK。

### 3.2 项目虚拟环境

```bash
cd /path/to/codex-telegram-remote
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

如果项目已有 `.venv`，直接使用：

```bash
cd /path/to/codex-telegram-remote
source .venv/bin/activate
```

---

## 4. 创建 Telegram Bot

1. 在 Telegram 中打开 `@BotFather`。
2. 发送 `/newbot`。
3. 设置 Bot 名称和用户名。
4. 保存 BotFather 返回的 Token。

建议为远程 Codex 使用独立 Bot，不要与其他通知或公开自动化共用。

### 4.1 获取 Telegram User ID

先给 Bot 发送任意一条私聊消息，然后执行：

```bash
codex-telegram-remote discover-user-id
```

输出示例：

```json
[
  {
    "user_id": "123456789",
    "chat_id": "123456789",
    "chat_type": "private",
    "username": "your_name"
  }
]
```

只使用数字 `user_id` 作为白名单，不使用 `@username`。

---

## 5. 配置文件

### 5.1 使用独立环境文件

远程控制使用独立的用户配置文件：

```text
~/.config/codex-telegram-remote/config.env
```

首次创建：

```bash
codex-telegram-remote init
```

命令会创建权限为 `0600` 的配置文件。不要复用其他通知 Bot 的 Token，
也不要将 Token 写入代码、日志、README 或 Git。

### 5.2 最小配置

```dotenv
CODEX_REMOTE_BOT_TOKEN=YOUR_BOT_TOKEN
CODEX_REMOTE_ALLOWED_USER_IDS=YOUR_TELEGRAM_USER_ID
CODEX_REMOTE_REPOS=twitter=/path/to/project
CODEX_REMOTE_DEFAULT_REPO=twitter
CODEX_REMOTE_SANDBOX=workspace-write
```

### 5.3 多仓库配置

仓库格式：

```text
alias=/absolute/path;alias2=/absolute/path
```

示例：

```dotenv
CODEX_REMOTE_REPOS=twitter=/path/to/project;buildflow=/path/to/buildflow;clipflow=/path/to/clipflow
```

仓库别名不能包含空格，且 `repo:<alias>` 必须能放入 Telegram callback 的
64 字节限制内。

### 5.4 完整环境变量

| 变量 | 默认值 | 作用 |
| --- | --- | --- |
| `CODEX_REMOTE_BOT_TOKEN` | 空 | 专用 Bot Token，必填 |
| `CODEX_REMOTE_ALLOWED_USER_IDS` | 空 | 数字 User ID 白名单，逗号分隔 |
| `CODEX_REMOTE_REPOS` | 当前项目 | 仓库别名和绝对路径 |
| `CODEX_REMOTE_DEFAULT_REPO` | 第一个仓库 | 默认仓库 |
| `CODEX_REMOTE_SANDBOX` | `workspace-write` | `read-only` 或 `workspace-write` |
| `CODEX_REMOTE_MODEL` | 空 | 覆盖 Codex 默认模型 |
| `CODEX_REMOTE_CODEX_BIN` | `codex` | Codex CLI 可执行文件 |
| `CODEX_REMOTE_DATA_DIR` | `~/.local/state/codex-telegram-remote` | 状态、日志和临时文件目录 |
| `CODEX_REMOTE_TIMEOUT_SECONDS` | `1800` | 单任务最大运行时间 |
| `CODEX_REMOTE_CANCEL_GRACE_SECONDS` | `5` | 取消后等待进程退出的时间 |
| `CODEX_REMOTE_POLL_TIMEOUT_SECONDS` | `25` | Telegram 长轮询时间 |
| `CODEX_REMOTE_MAX_REPLY_CHARS` | `18000` | Telegram 任务结果最大保存长度 |
| `CODEX_REMOTE_MAX_PROMPT_CHARS` | `12000` | 单条 Telegram 任务最大长度 |
| `CODEX_REMOTE_RECENT_TASK_LIMIT` | `5` | 项目最近任务数量，范围 1-10 |
| `CODEX_REMOTE_REQUIRE_PRIVATE_CHAT` | `true` | 是否禁止群聊调用 |
| `CODEX_REMOTE_REDACT_VALUES` | 空 | 额外从回复中脱敏的字符串 |
| `CODEX_REMOTE_CODEX_STATE_DB` | 自动发现 | Codex `state_*.sqlite` 路径 |
| `CODEX_REMOTE_CODEX_THREAD_HISTORY_DB` | 自动发现 | Codex `thread_history_*.sqlite` 路径 |
| `CODEX_REMOTE_EXTERNAL_ACTIVE_WINDOW_SECONDS` | `21600` | App 任务活跃判定窗口 |
| `CODEX_REMOTE_WATCH_POLL_SECONDS` | `20` | 完成通知轮询间隔 |

Codex 数据库默认从以下位置自动选择最新文件：

```text
~/.codex/state_*.sqlite
~/.codex/thread_history_*.sqlite
```

Codex 版本升级后，如果数据库文件名改变，重启服务即可重新自动发现。

---

## 6. 配置检查

```bash
cd /path/to/codex-telegram-remote
codex-telegram-remote check
```

检查内容：

- Bot Token
- Telegram User ID 白名单
- 仓库路径
- 仓库 callback 长度
- 沙箱模式
- 超时和轮询参数
- Codex CLI 是否存在
- Codex 是否已登录

预期输出：

```text
配置检查通过
数据库：.../~/.local/state/codex-telegram-remote/state.db
仓库：...
codex-cli ...
Logged in using ChatGPT
```

---

## 7. 前台运行

```bash
cd /path/to/codex-telegram-remote
codex-telegram-remote
```

服务启动时会：

1. 初始化 SQLite。
2. 启动单任务 worker。
3. 启动外部任务通知 watcher。
4. 后台注册 Telegram 命令菜单。
5. 开始 Telegram Long Polling。

可用调试模式：

```bash
codex-telegram-remote run --debug
```

只执行一轮长轮询：

```bash
codex-telegram-remote run --once
```

---

## 8. 安装为 launchd 常驻服务

前台验证成功后：

```bash
./scripts/install-launchd.sh
```

安装脚本会：

1. 运行 `codex-telegram-remote check`。
2. 生成 `~/Library/LaunchAgents/com.codex-telegram-remote.plist`。
3. 注入已安装的 CLI、配置文件和工具路径。
4. 加载并启动 `com.codex-telegram-remote`。

查看状态：

```bash
launchctl print gui/$(id -u)/com.codex-telegram-remote
```

查看日志：

```bash
tail -f "$HOME/Library/Application Support/codex-telegram-remote/codex-telegram-remote.log"
tail -f "$HOME/Library/Application Support/codex-telegram-remote/stderr.log"
```

重启：

```bash
launchctl kickstart -k gui/$(id -u)/com.codex-telegram-remote
```

卸载：

```bash
./scripts/uninstall-launchd.sh
```

`launchd` 仅在当前 macOS 用户登录后运行。电脑关机、休眠或 Codex 登录失效
时，远程任务不会执行。

### 8.1 Linux systemd 用户服务

```bash
./scripts/install-systemd-user.sh
```

查看状态和日志：

```bash
systemctl --user status codex-telegram-remote.service
journalctl --user -u codex-telegram-remote.service -f
```

卸载：

```bash
./scripts/uninstall-systemd-user.sh
```

---

## 9. Telegram 菜单和命令

服务启动后会自动注册：

- 左下角 `Menu`
- Bot 命令列表
- 消息下方面板按钮

命令：

| 命令 | 作用 |
| --- | --- |
| `/start` | 打开控制面板 |
| `/status` | 查看当前任务、App 任务、队列和对话 |
| `/repos` | 查看仓库列表 |
| `/repo <alias>` | 切换仓库 |
| `/recent` | 查看当前项目最近任务 |
| `/watch` | 手动订阅当前 App 任务 |
| `/new` | 清除当前仓库的 Codex 会话 |
| `/cancel` | 取消当前 Telegram 任务和排队任务 |
| `/id` | 查看 Telegram User ID |
| `/help` | 查看帮助 |

控制面板按钮：

| 按钮 | 作用 |
| --- | --- |
| 刷新状态 | 重新读取 Codex App 和 Telegram 任务状态 |
| 切换仓库 | 显示所有项目 |
| 最近任务 | 显示最近对话和序号按钮 |
| 新建会话 | 清除当前仓库 Session |
| 取消任务 | 取消 Telegram 任务 |
| 完成时通知 | 手动订阅当前 App 任务 |
| 使用说明 | 显示帮助 |

---

## 10. 消息处理流程

### 10.1 鉴权

每条 Telegram 消息和 callback 都会检查：

1. 是否来自私聊。
2. Telegram User ID 是否在白名单。
3. 仓库别名是否在白名单。
4. callback 是否仍然有效。

未授权用户不会启动 Codex。

### 10.2 收到确认

普通文字消息进入处理流程后：

1. 后台尝试给原消息添加 `👀` reaction。
2. 返回 `✅ 收到任务`。
3. 回复中包含仓库、对话标题、对话 ID 和排队位置。

Reaction 失败不会阻止任务执行，文字确认仍会发送。

### 10.3 任务队列

任务写入本地 SQLite：

- 同一 `update_id` 只入队一次
- 单 worker 串行执行
- 同一时间只运行一个 Telegram `codex exec`
- 运行中再次发消息会排队

### 10.4 Codex 执行

新对话：

```bash
codex exec --json \
  --sandbox workspace-write \
  -c 'approval_policy="never"' \
  --cd /path/to/repo \
  --output-last-message /path/to/temp \
  "<prompt>"
```

已有对话：

```bash
codex exec resume --json \
  -c 'approval_policy="never"' \
  --output-last-message /path/to/temp \
  "<thread_id>" \
  "<prompt>"
```

Runner 会：

- 解析 JSONL 事件
- 获取 thread ID
- 获取最终 agent message
- 写入临时结果
- 支持超时、取消和进程组终止

---

## 11. 项目切换与项目简介

切换仓库时，Bot 会先读取项目简介。

简介来源优先级：

1. `README.md`
2. `README.zh-CN.md`
3. `README.zh.md`
4. `overview.md`
5. `PRD.md`
6. `doc/PRD.md`
7. `PROJECT_STRUCTURE.md`
8. HTML `<meta name="description">`

没有文档时，会根据以下信息生成简介：

- `package.json`
- `Package.swift`
- 微信小程序 `app.json`
- LaTeX 文件
- Python 脚本目录
- HTML 入口
- 顶层目录结构

简介只做本地文本分析，不调用模型。

---

## 12. 最近任务与继续对话

切换项目或发送 `/recent` 后：

1. 从 Codex `state_*.sqlite` 读取当前项目最近线程。
2. 从 `thread_history_*.sqlite` 读取最新 turn。
3. 显示最近 5 条，最多可配置为 10 条。
4. 每条显示：
   - 序号
   - 标题
   - 任务主要内容
   - 最新状态
5. 生成 `1. 标题`、`2. 标题` 等按钮。

点击按钮后：

1. 校验线程仍属于当前项目。
2. 保存该 thread ID 为当前 Telegram Chat 在该仓库的 Session。
3. 后续普通消息通过 `codex exec resume <thread_id>` 继续。

选择已有 App 线程时，Codex App 可能需要刷新后才能看到外部 CLI 追加的
新 turn。

---

## 13. 状态显示

`/status` 会分别显示：

```text
仓库：clipflow
运行中：App 1 个，Telegram 0 个
Telegram 任务：无
对话：优化 SwiftUI 界面匹配 materials.html
对话 ID：01a17939-...
排队：0
会话：...
沙箱：workspace-write
App 运行中：优化 SwiftUI 界面匹配 materials.html（已运行 12 分钟）
```

App 任务识别规则：

1. 读取 `thread_history_*.sqlite`。
2. 每个线程只查看最新 turn。
3. 最新状态必须为 `inProgress`。
4. turn 起始时间必须在活跃窗口内。
5. 线程 `cwd` 必须与当前仓库完全一致。
6. 旧线程中的残留 `inProgress` 不会计入。

---

## 14. 完成通知与结果

### 14.1 Telegram 任务

Telegram 任务完成后直接发送：

- 任务编号
- 仓库
- 对话标题
- 对话 ID
- 成功结果或失败结果
- 耗时

### 14.2 Codex App 任务

Watcher 每 20 秒扫描所有白名单仓库：

1. 自动发现运行中的 App 任务。
2. 写入 `watched_tasks`。
3. 检查最新 turn。
4. `completed`、`failed`、`interrupted` 时通知。
5. 从 `final_agent_item_id` 提取最终回复。
6. 失败时读取 `error_json`。
7. Telegram 发送成功后才标记为已通知。

订阅保存在 SQLite，服务重启不会丢失。通知发送失败时会在下一轮重试。

限制：

- 服务完全离线期间开始并结束的任务，重启后不会自动视为新完成通知。
- 通知可能有最多一个轮询周期的延迟。
- Codex 数据库 schema 变化时需要同步更新读取逻辑。

---

## 15. Markdown 渲染

Telegram 不支持任意 Markdown。当前实现会转换为 Telegram HTML。

支持：

- `# 标题` -> `<b>标题</b>`
- `**粗体**`
- `*斜体*`
- `~~删除线~~`
- `[文字](https://example.com)` -> 可点击链接
- `` `行内代码` `` -> `<code>`
- 三反引号代码块 -> `<pre><code>`
- `- 项目`
- `- [x] 完成`
- 水平分隔线
- 简单 Markdown 表格 -> `│` 分隔文本

安全处理：

- 普通 HTML 标签会被转义
- 代码块内容会被转义
- 只允许 `http`、`https`、`tg`、`mailto` 链接
- 本地文件路径只显示文字，不生成无效链接

降级：

如果 Telegram 返回 HTML 解析错误，客户端会自动移除 `parse_mode` 并重试
纯文本，同时移除 Markdown 符号。

长消息按约 3000 字符切分，降低 HTML 转义后超过 Telegram 4096 字符限制
的风险。

---

## 16. SQLite 数据设计

默认数据库：

```text
~/.local/state/codex-telegram-remote/state.db
```

核心表：

| 表 | 作用 |
| --- | --- |
| `state` | Telegram `last_update_id` 等键值状态 |
| `chat_settings` | 当前 Chat 选择的仓库 |
| `sessions` | Chat + 仓库对应的 Codex Session/Thread ID |
| `jobs` | Telegram 任务、状态、结果和错误 |
| `watched_tasks` | Codex App 完成通知订阅和去重状态 |

任务状态：

```text
queued
running
succeeded
failed
canceled
```

通知状态：

```text
notified_at = ''
```

表示尚未成功通知。

---

## 17. 安全边界

必须遵守：

- 仅私聊
- 仅数字 Telegram User ID 白名单
- 仅仓库白名单
- 仅 `read-only` 或 `workspace-write`
- 不使用 `--dangerously-bypass-approvals-and-sandbox`
- 不把 `.env` 或 Token 提交到 Git
- 不将主目录、密钥目录或生产凭据目录加入 `CODEX_REMOTE_REPOS`

Codex 子进程仍能读取所选仓库内的文件。若仓库包含敏感 `.env`，应改用：

- 单独的 Git worktree
- 不含生产密钥的专用克隆
- 只读仓库副本
- 独立 macOS 用户

`CODEX_REMOTE_REDACT_VALUES` 只能做字符串脱敏，不能替代目录隔离。

---

## 18. 测试

完整检查：

```bash
cd /path/to/codex-telegram-remote
make check
```

或：

```bash
python3 -m compileall -q app tests
python3 -m unittest discover -s tests -v
```

重点测试覆盖：

- 配置白名单和独立 Token
- Telegram HTML Markdown 渲染与纯文本回退
- command menu、inline keyboard、callback
- reaction
- 双数据库 external task 识别
- 最近任务列表和结果提取
- 选择线程后的 resume
- 自动订阅所有 App 任务
- 完成和失败结果通知
- 消息顺序、队列和取消
- 现有业务功能回归

---

## 19. 常见故障排查

### 19.1 Telegram 没反应

检查服务：

```bash
launchctl print gui/$(id -u)/com.codex-telegram-remote
```

检查日志：

```bash
tail -n 100 ~/.local/state/codex-telegram-remote/codex-telegram-remote.log
```

检查网络：

```bash
curl -sS --connect-timeout 10 --max-time 20 \
  -o /dev/null -w '%{http_code}\n' \
  https://api.telegram.org/
```

如果出现 `SSL EOF`、`RemoteDisconnected` 或 timeout：

- 检查本机代理是否运行
- 检查 Telegram 分流规则
- 检查代理节点是否可用
- 服务自身会退避重试，不会因为单个连接错误退出

### 19.2 Menu 不显示

- 重新进入 Bot 聊天
- 退出 Telegram 聊天后再次打开
- 检查日志中的 `telegram command menu configured`
- 检查 Bot Token 是否属于正确 Bot

### 19.3 点击按钮没反应

- 检查日志中是否出现 `callback action`
- 检查 User ID 是否仍在白名单
- 检查仓库路径是否仍然存在
- 检查服务是否正在运行

### 19.4 最近任务为空

检查：

```bash
ls -lh ~/.codex/state_*.sqlite
ls -lh ~/.codex/thread_history_*.sqlite
```

如果自动发现失败，可以显式配置：

```dotenv
CODEX_REMOTE_CODEX_STATE_DB=~/.codex/state_5.sqlite
CODEX_REMOTE_CODEX_THREAD_HISTORY_DB=~/.codex/thread_history_1.sqlite
```

### 19.5 选择任务后执行失败

可能原因：

- 原线程所属模型与当前配置不同
- 原线程仍在运行
- 线程不属于当前仓库
- Codex Session 文件损坏

查看：

```bash
codex login status
codex --version
tail -n 100 ~/.local/state/codex-telegram-remote/codex-telegram-remote.log
```

### 19.6 收不到完成通知

- 检查 `CODEX_REMOTE_WATCH_POLL_SECONDS`
- 检查 `watched_tasks.notified_at`
- 检查 Codex App 最新 turn 是否真正结束
- 检查 Telegram 网络
- 检查是否已经发送成功并去重

查询：

```bash
sqlite3 -header -column ~/.local/state/codex-telegram-remote/state.db \
  "SELECT repo,title,outcome,notified_at FROM watched_tasks;"
```

### 19.7 launchd 找不到 codex

检查：

```bash
command -v codex
launchctl print gui/$(id -u)/com.codex-telegram-remote | rg PATH
```

当前安装脚本会加入：

```text
/opt/homebrew/bin
/usr/local/bin
```

仍找不到时，显式配置：

```dotenv
CODEX_REMOTE_CODEX_BIN=/opt/homebrew/bin/codex
```

然后重启服务。

---

## 20. 数据备份与恢复

建议备份：

```text
~/.config/codex-telegram-remote/config.env
~/.local/state/codex-telegram-remote/state.db
```

其中 `~/.config/codex-telegram-remote/config.env` 包含 Token，必须保持 `600` 权限，并放到安全
位置备份。

不要备份或提交：

- `~/.local/state/codex-telegram-remote/tmp/`
- `__pycache__/`
- 包含 Token 的日志

恢复后：

```bash
chmod 600 ~/.config/codex-telegram-remote/config.env
codex-telegram-remote check
launchctl kickstart -k gui/$(id -u)/com.codex-telegram-remote
```

---

## 21. 已知限制

- Telegram 不是 Codex App 的原生前端。
- Telegram 选择的任务通过 `codex exec resume` 追加，不是直接调用 app-server。
- Codex App 可能需要刷新才能显示 CLI 追加的新 turn。
- 当前外部任务监控依赖 Codex SQLite schema。
- Telegram 网络必须可访问 `api.telegram.org`。
- 自动完成通知只对服务运行期间被扫描到的活跃任务可靠。
- Telegram Markdown 只支持常见子集，不支持完整 Markdown 表格和 HTML。
- 同一时间只运行一个 Telegram Codex 子进程。

---

## 22. 验收清单

### 基础

- [ ] `codex-telegram-remote check` 通过
- [ ] `make check` 通过
- [ ] launchd 状态为 `running`
- [ ] Telegram API 可连接
- [ ] Bot 菜单显示

### 交互

- [ ] 发送普通消息后出现 `👀` reaction
- [ ] 收到 `✅ 收到任务`
- [ ] 回复显示仓库、对话标题和对话 ID
- [ ] 项目切换后显示最近任务
- [ ] 点击序号按钮后 Session 被切换
- [ ] 后续消息写回所选线程
- [ ] Telegram 结果包含最终回复
- [ ] 失败结果包含错误信息
- [ ] Codex App 任务结束后收到通知
- [ ] Markdown 标题、粗体、代码块和链接显示为渲染内容
- [ ] Telegram 拒绝 HTML 时可降级为纯文本

---

## 23. 日常运维命令

```bash
cd /path/to/codex-telegram-remote

# 配置检查
codex-telegram-remote check

# 获取 Telegram User ID
codex-telegram-remote discover-user-id

# 前台运行
codex-telegram-remote run

# 全量测试
make check

# 服务状态
launchctl print gui/$(id -u)/com.codex-telegram-remote

# 重启服务
launchctl kickstart -k gui/$(id -u)/com.codex-telegram-remote

# 应用日志
tail -f ~/.local/state/codex-telegram-remote/codex-telegram-remote.log

# 标准错误
tail -f ~/.local/state/codex-telegram-remote/stderr.log

# 查看待通知任务
sqlite3 -header -column ~/.local/state/codex-telegram-remote/state.db \
  "SELECT repo,title,outcome,notified_at FROM watched_tasks;"

# 查看 Telegram 任务
sqlite3 -header -column ~/.local/state/codex-telegram-remote/state.db \
  "SELECT id,repo,status,result,error FROM jobs ORDER BY id DESC LIMIT 20;"
```
