"""Translation orchestration: turn catalog entries into Quartz pages.

For every entry in a translatable status, the raw nLab page is read, cleaned,
link-rewritten (only *published* targets become local links; everything else
points back to nLab), translated chunk-by-chunk with structural validation,
and written to the Quartz content tree as a bilingual page with
YAML-serialised frontmatter carrying full source provenance.

Status machine (enforced in state.py):

    discovered -> queued -> translating -> translated -> validated -> published
                           \\-> failed (x N -> needs_review)
    published  -> outdated -> queued
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

import yaml

from .cleaner import clean_nlab_garbage, fix_wikilinks, to_safe_filename
from .config import PROMPT_VERSION, logger, settings
from .dictionary import dictionary_fingerprint
from .llm import LLMClient
from .state import StateStore, iso_now
from .translator import Cache, TranslationError, translate_document

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def build_frontmatter(page: dict) -> str:
    """YAML-serialised Quartz frontmatter with provenance and Chinese title."""
    title_en = page["true_title"]
    title_cn = page.get("chinese_translation") or ""
    primary = page["category_primary"]
    secondary = page["category_secondary"]
    tags = sorted({primary, secondary, "中英对照"} - {"General"}) or ["中英对照"]
    aliases = [a for a in page.get("aliases", []) if a and a != title_en]

    front: dict = {
        "title": title_cn or title_en,
        "tags": tags,
        "date": (page.get("published_at") or iso_now())[:10],
    }
    if aliases:
        front["aliases"] = aliases
    front["nlab"] = {
        "page_id": page["page_id"],
        "source_url": settings.nlab_base_url + quote(title_en, safe=""),
        "source_revision": page.get("source_revision") or "",
        "fetched_at": page.get("fetched_at") or "",
        "translated_at": page.get("translated_at") or iso_now(),
        "translator": {"model": settings.llm_model, "prompt_version": PROMPT_VERSION},
        "license_note": "nLab 内容按 nLab 使用条款以署名方式转载，译文由机器翻译生成，仅供参考。",
    }
    body = yaml.safe_dump(
        front, allow_unicode=True, sort_keys=False, width=1000, default_flow_style=False
    )
    return "---\n" + body + "---\n"


def _bilingual_body(title: str, translated: str, original: str) -> str:
    # No H1 here: Quartz's ArticleTitle renders the (Chinese) title from
    # frontmatter, which avoids the double-H1 problem of the old pipeline.
    return (
        "## 中文翻译 / Chinese Translation\n\n"
        f"{translated}\n\n"
        "---\n\n"
        "## 英文原文 / Original English\n\n"
        "> [!NOTE]\n"
        "> 本文由自动化管道机器翻译，未经人工校对；下方为原始学术文献快照。\n\n"
        f"{original}"
    )


def _validate_page(title: str, translated: str, original: str) -> list[str]:
    """Whole-page quality gates applied right before publication."""
    errors: list[str] = []
    if not translated.strip():
        errors.append("译文为空")
    cjk = len(_CJK.findall(translated))
    latin = len(re.findall(r"[A-Za-z]{3,}", translated))
    if cjk == 0 and latin > 10:
        errors.append("译文几乎不含中文")
    for marker in ("<source_text>", "</source_text>", "<think>"):
        if marker in translated:
            errors.append(f"译文残留提示词标记: {marker}")
    fences = original.count("\u0060\u0060\u0060")
    if fences != translated.count("\u0060\u0060\u0060"):
        errors.append(f"代码块数量不一致 (原文 {fences}, 译文 {translated.count(chr(96) * 3)})")
    if translated.count("$$") % 2 != 0:
        errors.append("$$ 数量不平衡（未闭合的显示公式会吞掉后续内容）")
    if translated.count("$$") != original.count("$$"):
        errors.append(
            f"显示公式数量不一致 (原文 {original.count('$$')}, 译文 {translated.count('$$')})"
        )
    return errors


def _output_path(page: dict) -> Path:
    primary = to_safe_filename(page["category_primary"])
    secondary = to_safe_filename(page["category_secondary"])
    return settings.content_dir / primary / secondary / (page["slug"] + ".md")


def _remove_old_output(page: dict) -> None:
    """Delete a stale output file (moved/renamed/retired page)."""
    if not page.get("slug"):
        return
    primary = to_safe_filename(page["category_primary"])
    secondary = to_safe_filename(page["category_secondary"])
    candidates = [
        settings.content_dir / primary / secondary / (page["slug"] + ".md"),
    ]
    for candidate in candidates:
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("删除旧输出失败 %s: %s", candidate, exc)


def run_translation(
    store: StateStore,
    limit: int | None = None,
    category: str | None = None,
    *,
    dry_run: bool = False,
    use_cache: bool = True,
    force: bool = False,
) -> tuple[int, int, int]:
    """Translate pending entries.

    Returns (published, failed, skipped).  Raises SystemExit-style errors via
    exceptions; the CLI maps them to exit codes.
    """
    client = LLMClient(settings)
    if not dry_run and not client.ping():
        raise RuntimeError(
            f"LLM 后端不可用 ({settings.llm_base_url})。请先启动 llama-server（见 manage.sh serve）。"
        )

    lookup = store.get_lookup()
    glossary_hash = dictionary_fingerprint()
    cache = Cache(backend=store if use_cache else None)
    pending = store.pending_translation(limit=limit, category=category)
    if force:
        extra = store.pages_in_status("needs_review")
        pending += extra[:limit] if limit else extra

    if not pending:
        logger.info("[SUCCESS] 所有概念词条目前均处于最新状态。")
        return 0, 0, 0

    logger.info(
        "待翻译词条数: %d（limit=%s category=%s dry_run=%s）",
        len(pending), limit, category, dry_run,
    )

    published = failed = skipped = 0
    for idx, page in enumerate(pending, start=1):
        page_id = page["page_id"]
        title = page["true_title"]
        source_path = settings.nlab_dir / page["source_path"]
        if not source_path.is_dir():
            logger.warning("源路径不存在，跳过: %s (%s)", title, source_path)
            skipped += 1
            continue

        try:
            raw = (source_path / "content.md").read_text(encoding="utf-8")
        except OSError as exc:
            store.record_error(None, page_id, "read", str(exc))
            store.update_page(page_id, last_error=str(exc), fail_count=page["fail_count"] + 1)
            failed += 1
            continue

        cleaned = clean_nlab_garbage(raw)
        fixed = fix_wikilinks(cleaned, lookup, settings.nlab_base_url)

        if dry_run:
            logger.info("[%d/%d] (dry-run) 将翻译词条: [%s]", idx, len(pending), title)
            continue

        logger.info("[%d/%d] 开始大模型汉化词条: [%s]", idx, len(pending), title)
        try:
            if page["status"] == "discovered":
                store.transition(page_id, "queued")
            store.transition(page_id, "translating")
            translated = translate_document(client, fixed, title, lookup, cache, glossary_hash)
            store.transition(page_id, "translated")
        except TranslationError as exc:
            store.record_error(None, page_id, "translate", str(exc))
            store.update_page(page_id, last_error=str(exc)[:500],
                              fail_count=page["fail_count"] + 1)
            if page["fail_count"] + 1 >= settings.max_fail_count:
                store.transition(page_id, "needs_review")
            else:
                store.transition(page_id, "failed")
            logger.error("翻译失败 [%s]: %s", title, exc)
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - record, never fake-success
            store.record_error(None, page_id, "translate", str(exc))
            store.update_page(page_id, last_error=str(exc)[:500],
                              fail_count=page["fail_count"] + 1)
            if page["fail_count"] + 1 >= settings.max_fail_count:
                store.transition(page_id, "needs_review")
            else:
                store.transition(page_id, "failed")
            logger.error("翻译崩溃 [%s]: %s", title, exc)
            failed += 1
            continue

        errors = _validate_page(title, translated, fixed)
        if errors:
            store.record_error(None, page_id, "validate", "; ".join(errors))
            store.update_page(page_id, last_error="; ".join(errors)[:500],
                              fail_count=page["fail_count"] + 1)
            store.transition(page_id, "failed")
            logger.error("校验未通过 [%s]: %s", title, "; ".join(errors))
            failed += 1
            continue
        store.transition(page_id, "validated")

        page["source_revision"] = store.get_meta("source_revision") or ""
        target_path = _output_path(page)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        page_out = build_frontmatter(page) + "\n" + _bilingual_body(title, translated, fixed)
        try:
            target_path.write_text(page_out, encoding="utf-8")
        except OSError as exc:
            store.record_error(None, page_id, "write", str(exc))
            store.transition(page_id, "failed")
            logger.error("文件写入失败 %s: %s", target_path, exc)
            failed += 1
            continue

        now = iso_now()
        store.update_page(
            page_id,
            status="published",
            translated_at=now,
            published_at=now,
            last_error="",
            fail_count=0,
        )
        published += 1
        if published % 10 == 0:
            store.export_json(settings.glossary_file)

    store.export_json(settings.glossary_file)
    logger.info("=== 同步流转结束！成功汉化发布: %d 条, 失败: %d 条 ===", published, failed)
    return published, failed, skipped
