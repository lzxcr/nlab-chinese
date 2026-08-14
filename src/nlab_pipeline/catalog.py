"""Catalog management: scan the nLab source tree into the state database.

Every page is keyed by its stable source-path ID (the directory path relative
to the nLab checkout) - never by the derived slug.  Slugs are an output
attribute with a uniqueness index and explicit -2/-3 suffixes on collision.
Alias ambiguity is preserved in the aliases table (candidate lists) instead of
last-write-wins.  The scan is incremental and idempotent and finishes with a
count assertion: valid source pages scanned == unique page ids in the DB.
"""

from __future__ import annotations

import os
import re

from .cleaner import extract_nlab_context, get_file_hash, to_safe_filename
from .config import logger, settings
from .dictionary import atomic_save_json, load_merged_dictionaries
from .state import StateStore, iso_now

_REDIRECT = re.compile(r"\[\[!redirects\s+(.*?)\]\]")


def _first_translation(term_lower: str, local_dict: dict, ext_dict: dict) -> str:
    """Return the first dictionary translation for *term_lower*, or ""."""
    for table in (local_dict, ext_dict):
        record = table.get(term_lower)
        if not record:
            continue
        if isinstance(record, list):
            for item in record:
                if isinstance(item, dict) and item.get("translation"):
                    return item["translation"]
        elif isinstance(record, dict) and record.get("translation"):
            return record["translation"]
    return ""


def _resolve_translation(
    title: str,
    aliases: list[str],
    local_dict: dict,
    ext_dict: dict,
    non_trans: set[str],
) -> str:
    """Resolve the Chinese title translation, preferring exact title matches.

    Priority: hard non-translation block-list (raw English kept) > local
    dictionary > external glossary > aliases (same order, aliases last).
    Returns "" when nothing is certain enough to commit.
    """
    title_lower = title.lower().strip()
    if title_lower in non_trans:
        return title
    if hit := _first_translation(title_lower, local_dict, ext_dict):
        return hit
    for alias in aliases:
        alias_lower = alias.lower().strip()
        if alias_lower in non_trans:
            return title
        if hit := _first_translation(alias_lower, local_dict, ext_dict):
            return hit
    return ""


def _is_junk_page(raw_content: str) -> bool:
    """Return True for stub/junk pages (empty, redirect-only, test placeholders)."""
    stripped = raw_content.strip()
    if not stripped:
        return True
    without_redirects = _REDIRECT.sub("", stripped).strip()
    if not without_redirects:
        return True
    return len(without_redirects) < 30


def _relative_path(root: str) -> str:
    """Convert an absolute page dir into a portable path relative to nlab_dir."""
    try:
        return os.path.relpath(root, settings.nlab_dir).replace(os.sep, "/")
    except ValueError:
        return root.replace(os.sep, "/")


def scan_and_sync_catalog(store: StateStore, *, force: bool = False) -> int:
    """Walk the nLab source tree and reconcile it against the state database.

    Returns the number of pages scanned.  Raises on integrity failure.
    """
    logger.info("=== 启动：全自动增量物理磁盘扫描 ===")
    local_dict, ext_dict, non_trans = load_merged_dictionaries()

    pages_dir = settings.nlab_dir / "pages"
    if not pages_dir.is_dir():
        raise RuntimeError(f"未找到 nLab 物理源路径: {pages_dir}")

    slug_seen: dict[str, str] = {}
    for page in store.get_pages():
        if page["slug"]:
            slug_seen[page["slug"]] = page["page_id"]

    scanned_ids: set[str] = set()
    processed = 0
    page_batch: list[dict] = []
    alias_batch: list[tuple[str, list[str]]] = []

    def _flush() -> None:
        nonlocal page_batch, alias_batch
        if page_batch:
            store.upsert_pages(page_batch)
            page_batch = []
        if alias_batch:
            store.set_aliases_bulk(alias_batch)
            alias_batch = []
        store.checkpoint()

    for root, _dirs, files in os.walk(pages_dir):
        if "name" not in files or "content.md" not in files:
            continue
        page_id = _relative_path(root)
        scanned_ids.add(page_id)
        name_file = os.path.join(root, "name")
        content_file = os.path.join(root, "content.md")
        try:
            true_title = open(name_file, encoding="utf-8").read().strip()
            raw_content = open(content_file, encoding="utf-8").read()
        except (OSError, UnicodeError) as exc:
            # Page-level read failure: skip this file, keep scanning.
            logger.warning("读取物理页面失败 %s: %s", root, exc)
            continue
        # Database errors below are systemic and deliberately abort the scan
        # instead of being swallowed page by page.
        current_hash = get_file_hash(content_file)
        primary, secondary = extract_nlab_context(raw_content)
        redirects = _REDIRECT.findall(raw_content)
        aliases = sorted({true_title} | {r.strip() for r in redirects if r.strip()})
        processed += 1
        is_junk = _is_junk_page(raw_content)

        existing = store.get_page(page_id)
        if existing is None:
            # A page that moved upstream keeps its content hash and title;
            # re-key the old entry instead of duplicating it.
            moved = store.find_moved(current_hash, true_title, scanned_ids)
            if moved and moved["page_id"] != page_id:
                logger.info(
                    "上游路径变更: %s (%s) -> %s",
                    moved["true_title"], moved["page_id"], page_id,
                )
                store.rekey_page(moved["page_id"], page_id)
                existing = store.get_page(page_id)
                if existing and existing["slug"] in slug_seen:
                    del slug_seen[existing["slug"]]

        slug = _claim_slug(true_title, slug_seen, exclude=page_id)
        status = "skipped" if is_junk else "discovered"

        if existing is not None:
            new_page = dict(existing)
            new_page["true_title"] = true_title
            new_page["category_primary"] = primary
            new_page["category_secondary"] = secondary
            new_page["source_md5"] = current_hash
            new_page["source_path"] = page_id
            new_page["source_size"] = os.path.getsize(content_file)
            new_page["slug"] = slug

            if is_junk:
                _retire_junk_entry(store, new_page)
            else:
                if existing.get("source_md5") and existing["source_md5"] != current_hash:
                    if existing["status"] == "published":
                        new_page["status"] = "outdated"
                    elif existing["status"] in ("skipped",):
                        new_page["status"] = "discovered"
                if existing["slug"] != slug:
                    # Output path changed: retire the old file, rewrite later.
                    _remove_output_file(existing)
                    if existing["status"] == "published":
                        new_page["status"] = "queued"
                    logger.info("slug 更新: %s -> %s (%s)", existing["slug"], slug, true_title)
                if existing["status"] == "skipped" and not is_junk:
                    new_page["status"] = "discovered"
                # Backfill the title translation from the dictionaries when
                # the entry is still untranslated (never overwrite audited).
                if not new_page.get("chinese_translation"):
                    initial_cn = _resolve_translation(
                        true_title, aliases, local_dict, ext_dict, non_trans
                    )
                    if initial_cn:
                        new_page["chinese_translation"] = initial_cn

            if _page_changed(existing, new_page):
                new_page["fetched_at"] = iso_now()
                page_batch.append(new_page)
            if store.get_aliases(page_id) != aliases:
                alias_batch.append((page_id, aliases))
        else:
            initial_cn = _resolve_translation(
                true_title, aliases, local_dict, ext_dict, non_trans
            )
            page_batch.append(
                {
                    "page_id": page_id,
                    "true_title": true_title,
                    "chinese_translation": initial_cn,
                    "category_primary": primary,
                    "category_secondary": secondary,
                    "slug": slug,
                    "status": status,
                    "mode": "auto",
                    "source_md5": current_hash,
                    "source_path": page_id,
                    "source_size": os.path.getsize(content_file),
                    "fetched_at": iso_now(),
                }
            )
            alias_batch.append((page_id, aliases))

        if processed % 3000 == 0:
            logger.info("已静默重构映射了 %d 个物理源节点...", processed)
            _flush()

    _flush()

    # Prune entries whose pages vanished upstream; also remove stale outputs.
    for page in store.get_pages():
        if page["page_id"] not in scanned_ids:
            logger.info("上游已移除/重命名，清理冗余词条: %s (%s)", page["page_id"], page["true_title"])
            _remove_output_file(page)
            store.delete_page(page["page_id"])

    # Count assertion: valid source pages must equal unique page ids.
    db_ids = {p["page_id"] for p in store.get_pages()}
    if len(scanned_ids) != len(db_ids):
        raise RuntimeError(
            f"扫描断言失败: 源页面 {len(scanned_ids)} != 状态库唯一 page_id {len(db_ids)}"
        )
    logger.info("断言通过: 源页面数 == 唯一 page_id 数 == %d", len(scanned_ids))

    changed = store.export_json(settings.glossary_file)
    if changed:
        logger.info("状态库 JSON 导出已更新: %s", settings.glossary_file)
    ambiguous, affected = store.ambiguous_alias_stats()
    logger.info(
        "成功同步！中央状态库包含 %d 个活跃学术概念节点（别名 %d 条，歧义 %d 组/影响 %d 页）。",
        len(db_ids), store.alias_count(), ambiguous, affected,
    )
    return processed


_COMPARED_FIELDS = (
    "true_title", "chinese_translation", "category_primary", "category_secondary",
    "slug", "status", "mode", "source_md5", "source_path", "source_size",
)


def _page_changed(existing: dict, new_page: dict) -> bool:
    """True when any scan-relevant field differs (no-op write skipping)."""
    return any(new_page.get(field) != existing.get(field) for field in _COMPARED_FIELDS)


def _claim_slug(title: str, slug_seen: dict[str, str], *, exclude: str | None) -> str:
    base = to_safe_filename(title)
    slug = base
    suffix = 2
    while slug in slug_seen and slug_seen[slug] != exclude:
        slug = f"{base}-{suffix}"
        suffix += 1
    if slug in slug_seen and slug_seen[slug] == exclude:
        return slug
    slug_seen[slug] = exclude or slug
    return slug


def _remove_output_file(page: dict) -> None:
    """Delete the rendered page for *page* if it exists."""
    primary = to_safe_filename(page["category_primary"])
    secondary = to_safe_filename(page["category_secondary"])
    candidate = settings.content_dir / primary / secondary / (page["slug"] + ".md")
    try:
        if candidate.exists():
            candidate.unlink()
            logger.info("已删除旧输出页面: %s", candidate)
    except OSError as exc:
        logger.warning("删除旧输出页面失败 %s: %s", candidate, exc)


def _retire_junk_entry(store: StateStore, page: dict) -> None:
    """Mark *page* as skipped and remove any previously published page."""
    if page["status"] == "published":
        _remove_output_file(page)
    page["status"] = "skipped"
    store.update_page(page["page_id"], status="skipped")


def _pending_audits(store: StateStore) -> list[dict]:
    pages = store.get_pages()
    return [
        page for page in pages
        if not page.get("chinese_translation") or page.get("status") == "outdated"
    ]


def audit_and_populate_dictionary(store: StateStore) -> int:
    """Interactively review untranslated/outdated entries (manual dictionary fill)."""
    local_dict, ext_dict, non_trans = load_merged_dictionaries()
    pending = _pending_audits(store)

    if not pending:
        print("\n✨ 目前整个中央状态库中所有概念均已完成对齐，无需人工补录。")
        return 0

    print(f"\n🔍 检测到共有 \033[1;33m{len(pending)}\033[0m 个条目急需人工校对或录入词典。")
    action = input("是否现在开启交互式逐条评审流? [Y/N]: ").strip().lower()
    if action not in ("y", ""):
        print("已退出人工审计流。")
        return 0

    from .dictionary import interact_assemble_translation

    resolved_count = 0
    for idx, page in enumerate(pending):
        title = page["true_title"]
        print(f"\n进展 [{idx + 1}/{len(pending)}] 正在裁决词条 -> \033[1;36m{title}\033[0m")
        print(f"  位置: {page['category_primary']}/{page['category_secondary']}")

        resolved = interact_assemble_translation(title, local_dict, ext_dict, non_trans)
        if resolved:
            store.update_page(page["page_id"], chinese_translation=resolved)
            # Fixing the title alone must never resurrect an outdated body as
            # published: the page still needs its body re-translated.
            if page["status"] in ("outdated", "failed", "needs_review", "translating"):
                store.update_page(page["page_id"], status="queued", fail_count=0)
            resolved_count += 1
            print(f" ✅ 成功绑定映射: '{title}' -> \033[1;32m{resolved}\033[0m")
        else:
            print(" ⏩ 本条目选择跳过，留待后续批量文件处理。")

    store.export_json(settings.glossary_file)
    print(f"\n🎉 本轮人工集中审查与词典补录流执行完毕！共补录 {resolved_count} 条。")
    return resolved_count


def generate_tree_and_extract_pending(store: StateStore) -> int:
    """Render the virtual category tree and dump pending entries to a file."""
    pages = store.get_pages()
    tree: dict[str, dict[str, list[str]]] = {}
    pending: dict[str, dict] = {}

    for page in pages:
        primary = page["category_primary"]
        secondary = page["category_secondary"]
        title = page["true_title"]
        status = page.get("status", "discovered")

        tree.setdefault(primary, {}).setdefault(secondary, []).append(
            f"{title} -> [{page.get('chinese_translation', '') or '未翻译'}]"
        )
        if status in ("discovered", "outdated") and not page.get("chinese_translation"):
            pending[page["page_id"]] = {
                "true_title": title,
                "category": f"{primary}/{secondary}",
                "aliases": page.get("aliases", []),
            }

    atomic_save_json(settings.pending_review_file, pending)

    print("\n🌲 ============ NLAB 统一虚拟知识目录树 ============")
    for pri, secs in tree.items():
        print(f"├── {pri}")
        for sec, items in secs.items():
            print(f"│   ├── {sec}")
            for item in items[:8]:
                print(f"│   │   ├── {item}")
            if len(items) > 8:
                print(f"│   │   └── ... 略去其余 {len(items) - 8} 条学术词条")
    print("====================================================")
    print(f"📊 拓扑扫描完毕：共捕获到 \033[1;33m{len(pending)}\033[0m 个尚未汉化的原始学术节点。")
    print(f"💾 待翻译索引已实时写入: \033[1;34m{settings.pending_review_file}\033[0m\n")
    return len(pending)
