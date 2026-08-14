#!/usr/bin/env python3
"""Validate repository documentation without network access."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_DOCUMENTS = (
    Path("README.md"),
    Path("CONTRIBUTING.md"),
    Path("MAINTAINING.md"),
    Path("SECURITY.md"),
    Path("CODE_OF_CONDUCT.md"),
    Path("DATA_POLICY.md"),
    Path("ATTRIBUTION.md"),
    Path("NOTICE.md"),
    Path("LICENSE.txt"),
    Path(".env.example"),
    Path("content/index.md"),
    Path("content/ATTRIBUTION.md"),
    Path(".github/pull_request_template.md"),
)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]\n]*\]\(([^)\n]+)\)")
PERSONAL_PATH = re.compile(r"(?:/home/[^/\s]+/|[A-Za-z]:\\Users\\[^\\\s]+\\)")


def _link_destination(raw: str) -> str:
    """Remove an optional Markdown title while preserving angle-bracket paths."""
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split(maxsplit=1)[0]


def _check_link(source: Path, raw: str) -> str | None:
    target = unquote(_link_destination(raw))
    parsed = urlsplit(target)
    if parsed.scheme or target.startswith(("#", "/")):
        return None
    relative = parsed.path
    if not relative:
        return None
    candidate = (source.parent / relative).resolve()
    if not candidate.is_relative_to(ROOT):
        return f"链接越出仓库: {target}"
    if not candidate.exists():
        return f"无效本地链接: {target}"
    return None


def check_document(relative: Path) -> list[str]:
    path = ROOT / relative
    if not path.is_file():
        return ["缺少必需文档"]
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"无法读取 UTF-8 文档: {exc}"]

    errors: list[str] = []
    if PERSONAL_PATH.search(text):
        errors.append("包含个人主目录绝对路径")
    for raw in MARKDOWN_LINK.findall(text):
        if error := _check_link(path, raw):
            errors.append(error)
    return errors


def main() -> int:
    failures: list[str] = []
    for relative in REQUIRED_DOCUMENTS:
        for error in check_document(relative):
            failures.append(f"{relative}: {error}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"documentation audit passed ({len(REQUIRED_DOCUMENTS)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
