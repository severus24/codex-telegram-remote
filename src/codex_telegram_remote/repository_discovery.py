from __future__ import annotations

import re
from pathlib import Path


def discover_repositories(
    scan_root: Path,
    configured: dict[str, Path],
    *,
    exclude: tuple[str, ...] = (),
) -> dict[str, Path]:
    root = scan_root.expanduser().resolve(strict=False)
    if not root.is_dir():
        return dict(configured)
    excluded = {
        "node_modules",
        ".venv",
        "__pycache__",
        *exclude,
    }
    existing_by_path = {
        path.expanduser().resolve(strict=False): alias
        for alias, path in configured.items()
    }
    discovered: dict[str, Path] = {}
    for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if (
            not child.is_dir()
            or child.name.startswith(".")
            or child.name.startswith("_")
            or child.name in excluded
        ):
            continue
        resolved = child.resolve(strict=False)
        alias = existing_by_path.get(resolved) or _unique_alias(
            _slug(child.name),
            discovered,
        )
        discovered[alias] = resolved

    # Preserve nested configured repositories such as Twitter/TwitterFavorites.
    for alias, path in configured.items():
        resolved = path.expanduser().resolve(strict=False)
        if not resolved.is_dir() or resolved in discovered.values():
            continue
        discovered[_unique_alias(_safe_alias(alias), discovered)] = resolved
    return discovered


def seconds_until_daily_time(
    hour: int,
    minute: int,
    now_seconds: int,
) -> int:
    current = now_seconds % 86400
    target = hour * 3600 + minute * 60
    delay = target - current
    return delay if delay > 0 else delay + 86400


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "", value.lower())
    return (slug or "project")[:50]


def _safe_alias(value: str) -> str:
    alias = re.sub(r"\s+", "", value).strip() or "project"
    return alias[:50]


def _unique_alias(alias: str, existing: dict[str, Path]) -> str:
    if alias not in existing:
        return alias
    index = 2
    while f"{alias}-{index}" in existing:
        index += 1
    return f"{alias}-{index}"
