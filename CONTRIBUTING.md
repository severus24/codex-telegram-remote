# Contributing

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
make check
```

## Guidelines

- Keep runtime dependencies at zero unless a dependency removes substantial
  complexity.
- Add tests for Telegram payload changes and persistence behavior.
- Never include real tokens, chat IDs, or local Codex databases in fixtures.
- Treat Codex SQLite schemas as version-sensitive adapters.
- Preserve plain-text fallback behavior for Telegram rendering.
- Document security implications for any execution-related feature.

## Pull Requests

Include:

- a concise behavior summary
- tests
- documentation updates
- migration notes for config or SQLite schema changes
