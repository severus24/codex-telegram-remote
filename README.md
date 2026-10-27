# codex-telegram-remote

Self-hosted Telegram control plane for Codex CLI.

Control a local Codex installation from a Telegram private chat with:

- allowlisted Telegram users and repositories
- persistent SQLite task queues
- repository switching and project summaries
- recent Codex thread discovery and numbered selection
- continuation of a selected thread with `codex exec resume`
- completion and failure notifications with results
- Telegram command menus, inline buttons and callbacks
- Markdown-to-Telegram HTML rendering
- `read-only` or `workspace-write` sandboxing
- macOS `launchd` and Linux `systemd --user` templates

The project uses the Python standard library at runtime. It does not require a
public webhook server, Docker, or a Telegram SDK.

> [!WARNING]
> This is remote code execution for your local development environment.
> Use a dedicated Telegram bot, a strict user allowlist, a repository
> allowlist, and the least-privileged sandbox available.

## How it works

```text
Telegram private chat
        |
        | long polling
        v
codex-telegram-remote
        |
        +-- user and repository allowlists
        +-- SQLite task queue and sessions
        +-- codex exec / codex exec resume
        +-- read-only Codex thread history adapter
        |
        v
Telegram result and completion notifications
```

The default execution adapter uses `codex exec`, which is appropriate for
bounded non-interactive jobs. Codex SDK and app-server integration are future
adapter targets.

## Requirements

- Python 3.11+
- Codex CLI installed and authenticated
- Telegram Bot Token
- network access to `api.telegram.org`

```bash
codex --version
codex login status
```

## Install

From a checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Initialize configuration:

```bash
codex-telegram-remote init
```

This creates:

```text
~/.config/codex-telegram-remote/config.env
```

The file is created with mode `0600`.

Fill in:

```dotenv
CODEX_REMOTE_BOT_TOKEN=YOUR_BOT_TOKEN
CODEX_REMOTE_ALLOWED_USER_IDS=YOUR_TELEGRAM_USER_ID
CODEX_REMOTE_REPOS=project=/absolute/path/to/project
CODEX_REMOTE_DEFAULT_REPO=project
CODEX_REMOTE_SANDBOX=workspace-write
```

Discover your Telegram User ID:

```bash
codex-telegram-remote discover-user-id
```

Check configuration:

```bash
codex-telegram-remote check
```

Run in the foreground:

```bash
codex-telegram-remote run
```

## Commands

```text
codex-telegram-remote init
codex-telegram-remote check
codex-telegram-remote discover-user-id
codex-telegram-remote run
codex-telegram-remote run --debug
codex-telegram-remote run --once
```

Compatibility flags such as `--check`, `--init` and `--discover-user-id` are
also accepted.

## Telegram commands

| Command | Purpose |
| --- | --- |
| `/start` | Open the control panel |
| `/status` | Show Telegram and Codex App task status |
| `/repos` | List repositories |
| `/repos` button flow | Switch repository |
| `/recent` | List recent threads with numbered buttons |
| `/watch` | Subscribe to currently running App tasks |
| `/new` | Clear the current repository session |
| `/cancel` | Cancel the running Telegram task and queue |
| `/help` | Show help |

Selecting a recent thread stores its ID as the active session. Subsequent
Telegram prompts are executed with:

```bash
codex exec resume <thread_id> "<prompt>"
```

## Services

macOS:

```bash
./scripts/install-launchd.sh
./scripts/uninstall-launchd.sh
```

Linux user service:

```bash
./scripts/install-systemd-user.sh
./scripts/uninstall-systemd-user.sh
```

## Data

Default data directory:

- macOS: `~/Library/Application Support/codex-telegram-remote`
- Linux: `$XDG_STATE_HOME/codex-telegram-remote` or
  `~/.local/state/codex-telegram-remote`

Contents:

```text
state.db
codex-telegram-remote.log
stdout.log
stderr.log
tmp/
```

## Markdown

Common Markdown is converted to Telegram HTML:

- headings, bold, italic, strikethrough
- links, lists, checkboxes
- inline code and fenced code blocks
- simple tables converted to `│`-separated text

If Telegram rejects the HTML, the message is retried as plain text with
Markdown syntax removed.

## Security

Read [SECURITY.md](SECURITY.md) before exposing this service.

Never commit:

- Telegram Bot Tokens
- User IDs or private chat IDs
- `.env` or `config.env`
- local Codex databases
- repository paths containing confidential information

## Development

```bash
python3 -m pip install -e .
make check
```

The tests use fake Telegram and Codex components and do not call external APIs.

## Documentation

- [Chinese implementation guide](docs/implementation-guide.zh-CN.md)
- [Architecture](docs/architecture.md)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)

## License

MIT
