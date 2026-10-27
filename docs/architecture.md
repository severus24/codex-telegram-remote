# Architecture

```text
Telegram Bot API
        |
        | long polling
        v
CodexRemoteService
        |
        +-- Telegram authorization
        +-- command and callback routing
        +-- task worker
        +-- completion watcher
        |
        +---------------------+
        |                     |
        v                     v
SQLiteStore          Codex adapters
        |                     |
        |                     +-- codex exec runner
        |                     +-- read-only thread history
        |
        v
Telegram responses and notifications
```

## Integration Layers

The upstream OpenAI documentation identifies three Codex integration layers:

1. `codex exec` for bounded non-interactive jobs
2. Codex SDK for programmatic task execution
3. Codex app-server for persistent threads, streaming events and approvals

This release uses `codex exec` as the execution adapter. The thread history
reader is a version-sensitive local adapter and must not become the only
long-term integration path.

Future adapters should implement a common execution interface so the Telegram
transport and SQLite state layer remain independent of Codex internals.
