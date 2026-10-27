from __future__ import annotations

import json
import re
from pathlib import Path


DOCUMENT_CANDIDATES = (
    "README.md",
    "README.zh-CN.md",
    "README.zh.md",
    "README.txt",
    "overview.md",
    "PRD.md",
    "doc/PRD.md",
    "PROJECT_STRUCTURE.md",
    "1/README.md",
)
IGNORED_TOP_LEVEL = {
    ".git",
    ".next",
    ".venv",
    ".build",
    ".mypy_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "temp",
}
GENERIC_LINES = {
    "目录",
    "产品概述",
    "本次工作概览",
    "完成内容",
}


def summarize_project(path: Path, *, max_chars: int = 320) -> str:
    path = path.expanduser()
    project_name = path.name
    title, description = _document_summary(path)
    project_type, tech = _detect_project_type(path)
    directories = _top_level_directories(path)

    if description:
        summary = description
    elif title:
        summary = f"{project_name} 是一个 {project_type}，项目标题为《{title}》。"
    else:
        summary = f"{project_name} 是一个 {project_type}。"

    missing_tech = [
        item for item in tech if item.lower() not in summary.lower()
    ]
    if missing_tech:
        summary += f"\n技术栈：{'、'.join(missing_tech)}。"
    if directories:
        summary += f"\n顶层包含：{'、'.join(directories)}。"
    return _truncate(summary, max_chars)


def _document_summary(path: Path) -> tuple[str, str]:
    for relative in DOCUMENT_CANDIDATES:
        document = path / relative
        if not document.is_file():
            continue
        title, paragraph = _extract_markdown_summary(document)
        if paragraph:
            return title, paragraph
    html_title, html_description = _html_summary(path)
    return html_title, html_description


def _extract_markdown_summary(path: Path) -> tuple[str, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:65536]
    except OSError:
        return "", ""
    lines = text.splitlines()
    title = ""
    title_index = -1
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if line.startswith("# "):
            title = _clean_markdown(line[2:].strip())
            title_index = index
            break
    start = title_index + 1 if title_index >= 0 else 0
    paragraph: list[str] = []
    in_code = False
    for raw_line in lines[start:]:
        line = raw_line.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            continue
        if not line:
            if paragraph:
                value = _clean_markdown(" ".join(paragraph)).strip()
                if _is_meaningful_paragraph(value):
                    return title, _truncate(value, 240)
                paragraph = []
            continue
        if _skip_markdown_line(line):
            if paragraph:
                value = _clean_markdown(" ".join(paragraph)).strip()
                if _is_meaningful_paragraph(value):
                    return title, _truncate(value, 240)
                paragraph = []
            continue
        paragraph.append(line)
    if paragraph:
        value = _clean_markdown(" ".join(paragraph)).strip()
        if _is_meaningful_paragraph(value):
            return title, _truncate(value, 240)
    return title, ""


def _skip_markdown_line(line: str) -> bool:
    if line.startswith(("#", ">", "![", "<", "|", "```", "---")):
        return True
    if re.match(r"^(?:[-*+] |\d+\.\s)", line):
        return True
    if line.startswith("**") and line.endswith("**") and len(line) < 80:
        return True
    if line.lower().startswith(("版本", "日期", "状态", "扫描路径", "扫描时间")):
        return True
    return False


def _is_meaningful_paragraph(value: str) -> bool:
    if len(value) < 20:
        return False
    if value in GENERIC_LINES:
        return False
    if re.fullmatch(r"[\W\d_]+", value):
        return False
    return True


def _clean_markdown(value: str) -> str:
    value = re.sub(r"!\[[^\]]*]\([^)]*\)", "", value)
    value = re.sub(r"\[([^\]]+)]\([^)]*\)", r"\1", value)
    value = re.sub(r"[*_`~]+", "", value)
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _html_summary(path: Path) -> tuple[str, str]:
    candidates = [path / "index.html"]
    try:
        candidates.extend(
            child / "index.html"
            for child in sorted(path.iterdir())
            if child.is_dir() and not child.name.startswith(".")
        )
    except OSError:
        pass
    for document in candidates:
        if not document.is_file():
            continue
        try:
            text = document.read_text(encoding="utf-8", errors="replace")[:32768]
        except OSError:
            continue
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        description_match = re.search(
            r"<meta[^>]+name=[\"']description[\"'][^>]+"
            r"content=[\"'](.*?)[\"']",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title = _clean_html(title_match.group(1)) if title_match else ""
        description = (
            _clean_html(description_match.group(1))
            if description_match
            else ""
        )
        if title or description:
            return title, _truncate(description, 240)
    return "", ""


def _clean_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", value)).strip()


def _detect_project_type(path: Path) -> tuple[str, list[str]]:
    tech: list[str] = []
    package_json = path / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            package = {}
        dependencies = {
            **package.get("dependencies", {}),
            **package.get("devDependencies", {}),
        }
        if "next" in dependencies:
            project_type = "Next.js Web 应用"
            tech.append("Next.js")
        elif "react" in dependencies:
            project_type = "React Web 应用"
            tech.append("React")
        elif "vue" in dependencies:
            project_type = "Vue Web 应用"
            tech.append("Vue")
        else:
            project_type = "JavaScript/TypeScript 项目"
        if "typescript" in dependencies:
            tech.append("TypeScript")
        if "tailwindcss" in dependencies or "@tailwindcss/postcss" in dependencies:
            tech.append("Tailwind CSS")
        return project_type, list(dict.fromkeys(tech))
    if (path / "Package.swift").is_file():
        return "Swift/SwiftUI macOS 应用", ["Swift", "SwiftUI"]
    if (path / "wechat" / "app.json").is_file() or (path / "app.json").is_file():
        return "微信小程序", ["WXML", "WXSS", "JavaScript"]
    if any(path.glob("*.tex")) or any(path.glob("*/latex")):
        return "LaTeX 教材与课程资料项目", ["LaTeX"]
    if (path / "index.html").is_file() or any(path.glob("*/index.html")):
        return "静态网页项目", ["HTML", "CSS", "JavaScript"]
    if (path / "work").is_dir() and any((path / "work").glob("*.py")):
        return "Python 自动化脚本项目", ["Python"]
    if (path / "doc").is_dir() and (path / "UI").is_dir():
        return "产品设计与原型项目", []
    return "本地项目", []


def _top_level_directories(path: Path) -> list[str]:
    try:
        names = [
            child.name
            for child in path.iterdir()
            if child.is_dir()
            and not child.name.startswith(".")
            and child.name not in IGNORED_TOP_LEVEL
        ]
    except OSError:
        return []
    names.sort(key=lambda item: (not item.isdigit(), item.lower()))
    return names[:6]


def _truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    cutoff = max(value.rfind(mark, 0, limit) for mark in "。！？.!?")
    if cutoff >= limit // 2:
        return value[: cutoff + 1]
    return value[:limit].rstrip() + "…"
