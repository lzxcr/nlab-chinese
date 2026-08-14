#!/usr/bin/env python3
"""Fail CI when publishable content violates provenance or structure gates."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from nlab_pipeline.processor import _validate_page  # noqa: E402

CONTENT = ROOT / "content"
FRONTMATTER = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n(.*)$", re.DOTALL)
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^)\n]+)\)")
ENGLISH_SEPARATOR = "\n\n---\n\n## 英文原文 / Original English\n\n"
TRANSLATION_HEADING = "## 中文翻译 / Chinese Translation\n\n"
ORIGINAL_NOTE = (
    "> [!NOTE]\n"
    "> 本文由自动化管道机器翻译，未经人工校对；下方为原始学术文献快照。\n\n"
)


def _frontmatter_and_body(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER.match(text)
    if not match:
        raise ValueError("缺少或损坏 YAML frontmatter")
    front = yaml.safe_load(match.group(1)) or {}
    if not isinstance(front, dict):
        raise ValueError("frontmatter 不是映射")
    return front, match.group(2)


def _target_exists(source: Path, raw_target: str) -> bool:
    target = unquote(raw_target.split("#", 1)[0].split("?", 1)[0])
    if not target or "://" in target or target.startswith(("mailto:", "#")):
        return True
    candidate = CONTENT / target.lstrip("/") if target.startswith("/") else source.parent / target
    candidates = [candidate, candidate.with_suffix(".md"), candidate / "index.md"]
    content_root = CONTENT.resolve()
    return any(
        resolved.is_relative_to(content_root) and resolved.is_file()
        for item in candidates
        if (resolved := item.resolve())
    )


def check_file(path: Path) -> list[str]:
    errors: list[str] = []
    try:
        front, body = _frontmatter_and_body(path)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        return [str(exc)]

    if re.search(r"^# [^#]", body, re.MULTILINE):
        errors.append("正文含 H1，会与 Quartz ArticleTitle 产生重复标题")

    if front.get("draft") is True:
        if not front.get("quarantine_reason"):
            errors.append("隔离草稿缺少 quarantine_reason")
        return errors

    nlab = front.get("nlab")
    if isinstance(nlab, dict):
        required = (
            "page_id", "source_url", "source_revision", "source_content_md5",
            "fetched_at", "translated_at", "translator", "quality_status",
        )
        missing = [name for name in required if not nlab.get(name)]
        if missing:
            errors.append("发布页缺少溯源字段: " + ", ".join(missing))
        translator = nlab.get("translator")
        if not isinstance(translator, dict) or not all(
            translator.get(name) for name in ("model", "prompt_version")
        ):
            errors.append("发布页缺少翻译器模型/规则版本")
        if not body.startswith(TRANSLATION_HEADING) or ENGLISH_SEPARATOR not in body:
            errors.append("中英对照区块结构损坏")
        else:
            translated, original = body[len(TRANSLATION_HEADING) :].split(
                ENGLISH_SEPARATOR, 1
            )
            if original.startswith(ORIGINAL_NOTE):
                original = original[len(ORIGINAL_NOTE) :]
            errors.extend(_validate_page(str(front.get("title", "")), translated, original))

    for target in MARKDOWN_LINK.findall(body):
        if not _target_exists(path, target):
            errors.append(f"无效内部链接: {target}")
    return errors


def main() -> int:
    failures: list[str] = []
    for path in sorted(CONTENT.rglob("*.md")):
        for error in check_file(path):
            failures.append(f"{path.relative_to(ROOT)}: {error}")
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"content audit passed ({len(list(CONTENT.rglob('*.md')))} Markdown files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
