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

import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import yaml

from .cleaner import clean_nlab_garbage, fix_wikilinks, to_safe_filename
from .config import PROMPT_VERSION, logger, settings
from .dictionary import dictionary_fingerprint
from .llm import LLMClient
from .protector import protected_spans
from .state import StateStore, iso_now
from .translator import Cache, TranslationError, translate_document

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_HEADING_LEVEL = re.compile(r"^(#{1,6})[ \t]+", re.MULTILINE)
_INLINE_LINK = re.compile(r"(!?)\[[^\]\n]*\]\((?:(?:[^()\n]+|\([^()\n]*\))*)\)")
_REFERENCE_LINK = re.compile(r"(!?)\[[^\]\n]*\]\[[^\]\n]+\]")
_LIST_PREFIX = re.compile(r"^([ \t]*)([-+*]|\d+[.)])[ \t]+(?:\[([ xX])\][ \t]+)?", re.MULTILINE)
_QUOTE_PREFIX = re.compile(r"^[ \t]*((?:>[ \t]?)+)", re.MULTILINE)
_TABLE_DELIMITER = re.compile(
    r"^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(?:\|[ \t]*:?-{3,}:?[ \t]*)+\|?[ \t]*$",
    re.MULTILINE,
)


def _markdown_shape(text: str) -> dict[str, list]:
    """Capture translatable Markdown scaffolding not covered as opaque spans."""
    return {
        "links": [match.group(1) == "!" for match in _INLINE_LINK.finditer(text)],
        "reference_links": [
            match.group(1) == "!" for match in _REFERENCE_LINK.finditer(text)
        ],
        "lists": [
            (len(match.group(1).expandtabs(4)), match.group(2), match.group(3) or "")
            for match in _LIST_PREFIX.finditer(text)
        ],
        "quotes": [match.group(1).count(">") for match in _QUOTE_PREFIX.finditer(text)],
        "tables": [match.group(0).count("|") for match in _TABLE_DELIMITER.finditer(text)],
    }


class PublicationError(RuntimeError):
    """Raised when a page cannot pass validation or be committed safely."""

    def __init__(self, stage: str, errors: list[str] | str) -> None:
        self.stage = stage
        self.errors = [errors] if isinstance(errors, str) else errors
        super().__init__("; ".join(self.errors))


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
        "source_content_md5": page.get("source_md5") or "",
        "translator": {
            "model": page.get("translator_model") or settings.llm_model,
            "prompt_version": page.get("prompt_version") or PROMPT_VERSION,
        },
        "quality_status": "machine-translated-unreviewed",
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
    original_spans = protected_spans(original)
    translated_spans = protected_spans(translated)
    if translated_spans != original_spans:
        original_kinds = [kind for kind, _ in original_spans]
        translated_kinds = [kind for kind, _ in translated_spans]
        if original_kinds.count("fence") != translated_kinds.count("fence"):
            errors.append(
                "代码块数量不一致 "
                f"(原文 {original_kinds.count('fence')}, "
                f"译文 {translated_kinds.count('fence')})"
            )
        if translated_kinds != original_kinds:
            errors.append(
                "受保护结构的类型/顺序不一致 "
                f"(原文 {original_kinds}, 译文 {translated_kinds})"
            )
        else:
            changed = [
                str(index)
                for index, (source, result) in enumerate(
                    zip(original_spans, translated_spans, strict=True)
                )
                if source != result
            ]
            errors.append(f"受保护结构内容被改写（位置 {', '.join(changed[:20])}）")
    original_headings = _HEADING_LEVEL.findall(original)
    translated_headings = _HEADING_LEVEL.findall(translated)
    if original_headings != translated_headings:
        errors.append(
            f"标题层级/顺序不一致 (原文 {original_headings}, 译文 {translated_headings})"
        )
    original_shape = _markdown_shape(original)
    translated_shape = _markdown_shape(translated)
    for kind, label in (
        ("links", "Markdown 链接"),
        ("reference_links", "引用式链接"),
        ("lists", "列表/任务项"),
        ("quotes", "引用层级"),
        ("tables", "表格分隔行"),
    ):
        if translated_shape[kind] != original_shape[kind]:
            errors.append(f"{label}结构不一致")
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


def _atomic_write_text(path: Path, content: str) -> None:
    """Durably replace *path* without exposing a partial Markdown file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # The file replacement is already atomic; directory fsync is an
            # extra durability guarantee unavailable on some filesystems.
            pass
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def publish_page(
    store: StateStore,
    page: dict,
    translated: str,
    original: str,
    *,
    translator_model: str | None = None,
    prompt_version: str | None = None,
) -> Path:
    """Validate and atomically publish one bilingual page.

    The database deliberately reaches ``validated`` before the filesystem
    replacement.  If the process dies between those operations, the next run
    can distinguish a complete atomic file from a missing one and reconcile
    it without claiming that partial content was published.
    """
    errors = _validate_page(page["true_title"], translated, original)
    if errors:
        raise PublicationError("validate", errors)

    page_id = page["page_id"]
    current = store.get_page(page_id)
    if current is None:
        raise PublicationError("state", f"词条不存在: {page_id}")
    current_status = current["status"]
    if current_status == "translated":
        store.transition(page_id, "validated")
    elif current_status == "needs_review":
        store.transition(page_id, "validated")
    elif current_status not in {"validated", "published"}:
        raise PublicationError("state", f"状态 {current_status} 不允许发布")

    now = iso_now()
    output_page = {
        **current,
        **page,
        "aliases": store.get_aliases(page_id),
        "source_revision": current.get("source_revision")
        or store.get_meta("source_revision")
        or "",
        "translated_at": now,
        "published_at": now,
        "translator_model": translator_model
        or current.get("translator_model")
        or settings.llm_model,
        "prompt_version": prompt_version
        or current.get("prompt_version")
        or PROMPT_VERSION,
    }
    target_path = _output_path(output_page)
    page_out = build_frontmatter(output_page) + "\n" + _bilingual_body(
        output_page["true_title"], translated, original
    )
    try:
        _atomic_write_text(target_path, page_out)
    except (OSError, UnicodeError) as exc:
        raise PublicationError("write", str(exc)) from exc

    if current_status == "validated" or current_status == "translated":
        store.transition(page_id, "published")
    elif current_status == "needs_review":
        store.transition(page_id, "published")
    store.update_page(
        page_id,
        translated_at=now,
        published_at=now,
        source_revision=output_page["source_revision"],
        translator_model=output_page["translator_model"],
        prompt_version=output_page["prompt_version"],
        last_error="",
        fail_count=0,
    )
    return target_path


def _frontmatter(path: Path) -> dict | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return None
    return parsed if isinstance(parsed, dict) else None


def reconcile_publication_state(store: StateStore) -> dict[str, int]:
    """Repair states left by crashes or files removed outside the pipeline."""
    stats = {"requeued": 0, "published": 0}
    for page in store.get_pages():
        status = page["status"]
        if status in {"translating", "translated"}:
            store.transition(page["page_id"], "queued", force=True)
            stats["requeued"] += 1
            continue
        if status not in {"validated", "published"}:
            continue
        target = _output_path(page)
        front = _frontmatter(target) if target.is_file() else None
        source = front.get("nlab", {}) if isinstance(front, dict) else {}
        complete = (
            isinstance(source, dict)
            and source.get("page_id") == page["page_id"]
            and source.get("source_content_md5") == page.get("source_md5")
            and not front.get("draft", False)
        )
        if status == "validated" and complete:
            store.transition(page["page_id"], "published")
            stats["published"] += 1
        elif not complete:
            store.transition(page["page_id"], "queued", force=status == "validated")
            stats["requeued"] += 1
    return stats


def _record_failure(
    store: StateStore,
    page: dict,
    stage: str,
    message: str,
    run_id: int | None,
) -> None:
    page_id = page["page_id"]
    fail_count = int(page.get("fail_count") or 0) + 1
    store.record_error(run_id, page_id, stage, message)
    store.update_page(page_id, last_error=message[:500], fail_count=fail_count)
    current = store.get_page(page_id)
    if current and current["status"] not in {"failed", "needs_review"}:
        store.transition(page_id, "failed")
    if fail_count >= settings.max_fail_count:
        store.transition(page_id, "needs_review")


def run_translation(
    store: StateStore,
    limit: int | None = None,
    category: str | None = None,
    *,
    dry_run: bool = False,
    use_cache: bool = True,
    force: bool = False,
    run_id: int | None = None,
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

    recovery = {"requeued": 0, "published": 0}
    if not dry_run:
        recovery = reconcile_publication_state(store)
    if any(recovery.values()):
        logger.warning(
            "已协调上次中断的发布状态: 重新入队 %d，确认发布 %d",
            recovery["requeued"], recovery["published"],
        )
    lookup = store.get_lookup()
    glossary_hash = dictionary_fingerprint()
    cache = Cache(backend=store if use_cache else None)
    pending = store.pending_translation(limit=limit, category=category)
    if force:
        extra = store.pages_in_status("needs_review")
        if category:
            extra = [
                page for page in extra
                if category in (page["category_primary"], page["category_secondary"])
            ]
        pending += extra
        if limit:
            pending = pending[:limit]

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
            message = f"源路径不存在: {source_path}"
            logger.error("%s (%s)", message, title)
            _record_failure(store, page, "read", message, run_id)
            failed += 1
            continue

        try:
            raw = (source_path / "content.md").read_text(encoding="utf-8")
        except OSError as exc:
            _record_failure(store, page, "read", str(exc), run_id)
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
            _record_failure(store, page, "translate", str(exc), run_id)
            logger.error("翻译失败 [%s]: %s", title, exc)
            failed += 1
            continue
        except Exception as exc:  # noqa: BLE001 - record, never fake-success
            _record_failure(store, page, "translate", str(exc), run_id)
            logger.error("翻译崩溃 [%s]: %s", title, exc)
            failed += 1
            continue

        try:
            publish_page(store, page, translated, fixed)
        except PublicationError as exc:
            _record_failure(store, page, exc.stage, str(exc), run_id)
            logger.error("发布失败 [%s]: %s", title, exc)
            failed += 1
            continue
        published += 1

    logger.info("=== 同步流转结束！成功汉化发布: %d 条, 失败: %d 条 ===", published, failed)
    return published, failed, skipped
