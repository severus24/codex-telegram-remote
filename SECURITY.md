# Security Policy

## Supported Versions

Only the latest release receives security fixes.

## Reporting a Vulnerability

Do not open a public issue for a vulnerability involving:

- credential exposure
- sandbox escape
- authorization bypass
- arbitrary command execution
- Telegram callback authorization

Contact the maintainer privately and include a minimal reproduction.

## Threat Model

This project is a remote control plane for a local coding agent. A successful
caller can run Codex tasks in allowlisted repositories and may cause Codex to
modify files.

Required boundaries:

- private chats only
- numeric Telegram User ID allowlist
- repository allowlist
- `read-only` or `workspace-write` sandbox
- no sandbox bypass flag
- scoped Telegram Bot Token
- no public webhook

Do not run the service with a Telegram account or bot that is shared with
untrusted users. Do not add home directories, production secret stores, or
credential directories to the repository allowlist.

The Codex thread database adapter is read-only, but it depends on local Codex
data formats that may change. Keep the adapter isolated from execution paths.
