"""Re-render pages published by the old pipeline into the new format.

The old pipeline wrote an H1 into the body (double H1), hand-built the
frontmatter (broken YAML for titles containing quotes), kept no provenance and
left the chinese_translation field empty for the two published pages.  This
migration extracts the Chinese section from the existing files, matches them
to state entries (by title/alias), and rewrites them with the new
YAML-serialised frontmatter, provenance block, and no body H1 - all without
calling the LLM.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .config import logger, settings
from .processor import _bilingual_body, build_frontmatter
from .state import StateStore

_SECTION = re.compile(
    r"##\s*中文翻译.*?\n(.*?)(?:\n---\s*\n##\s*英文原文.*?\n(.*))?$",
    re.DOTALL,
)


def _parse_legacy_page(path: Path) -> dict | None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return None
    match = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not match:
        return None
    try:
        front = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    body = match.group(2)
    section = _SECTION.search(body)
    if not section:
        return None
    translated = section.group(1).strip()
    original = (section.group(2) or "").strip()
    return {
        "frontmatter": front,
        "translated": translated,
        "original": original,
    }


def migrate_legacy_pages(store: StateStore, *, dry_run: bool = False) -> int:
    """Match legacy content files to state pages and rewrite them.

    Returns the number of migrated pages.
    """
    published = store.pages_in_status("published")
    if not published:
        logger.info("没有已发布词条需要迁移。")
        return 0

    by_title = {p["true_title"].lower(): p for p in published}
    by_alias: dict[str, dict] = {}
    lookup = store.get_lookup()
    for alias_lower, candidates in lookup.items():
        for candidate in candidates:
            if candidate["status"] == "published":
                by_alias[alias_lower] = candidate

    migrated = 0
    legacy_files = [
        p for p in settings.content_dir.rglob("*.md")
        if p.name != "index.md" and p.is_file()
    ]
    for path in legacy_files:
        parsed = _parse_legacy_page(path)
        if parsed is None or not parsed["translated"]:
            continue
        title = str(parsed["frontmatter"].get("title", "")).strip()
        aliases = [str(a) for a in parsed["frontmatter"].get("aliases", [])]
        page = (
            by_title.get(title.lower())
            or by_title.get(path.stem.lower())
            or next((by_alias.get(a.lower()) for a in [title] + aliases if a.lower() in by_alias), None)
        )
        if page is None:
            continue
        page = store.get_page(page["page_id"])
        if page is None or page["status"] != "published":
            continue

        if dry_run:
            logger.info("[dry-run] 将迁移: %s (%s)", page["true_title"], path)
            continue

        new_path = (
            settings.content_dir / page["category_primary"] / page["category_secondary"]
            / (page["slug"] + ".md")
        )
        new_path.parent.mkdir(parents=True, exist_ok=True)
        content = build_frontmatter(page) + "\n" + _bilingual_body(
            page["true_title"], parsed["translated"], parsed["original"] or parsed["translated"]
        )
        new_path.write_text(content, encoding="utf-8")
        if new_path.resolve() != path.resolve():
            path.unlink(missing_ok=True)
        migrated += 1
        logger.info("已迁移旧发布页面: %s -> %s", path, new_path)

    if migrated:
        store.export_json(settings.glossary_file)
    logger.info("旧页面迁移完成: %d 页。", migrated)
    return migrated
