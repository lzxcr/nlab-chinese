"""Command-line interface for the nLab translation pipeline.

Usage:
    nlab-pipeline scan [--force]        # reconcile catalog with nLab source
    nlab-pipeline audit                 # interactive dictionary review
    nlab-pipeline tree                  # render category tree + pending list
    nlab-pipeline status                # print catalog summary
    nlab-pipeline translate [--limit N] [--category C] [--dry-run] [--no-cache]
    nlab-pipeline export                # write the JSON export of the state db
    nlab-pipeline prune-cache           # clear the chunk translation cache
    nlab-pipeline migrate-pages         # re-render legacy published pages
    nlab-pipeline report                # recent runs and recorded errors

Exit codes: 0 success, 1 partial failure, 2 LLM backend unavailable,
3 state database integrity error, 4 usage error.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import logger, settings
from .state import SCHEMA_VERSION, StateError, StateStore


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def _open_store() -> StateStore:
    store = StateStore()
    store.init_schema()
    store.check_integrity()
    return store


def _ensure_legacy_import(store: StateStore, force: bool) -> None:
    if store.has_pages():
        return
    if not settings.glossary_file.exists():
        return
    imported, skipped = store.import_legacy_json(settings.glossary_file)
    if imported:
        logger.info("已从旧 JSON 状态库导入 %d 条（跳过 %d 条），并重建无碰撞 slug。", imported, skipped)
    elif not force:
        raise StateError(
            "旧状态库 JSON 存在但无法导入，且数据库为空。"
            "请检查文件后用 'scan --force' 从头扫描。"
        )


def _cmd_scan(args: argparse.Namespace) -> int:
    from .catalog import scan_and_sync_catalog

    with _open_store() as store:
        with store.exclusive_operation("scan"):
            _ensure_legacy_import(store, args.force)
            store.backup()
            run_id = store.start_run("scan")
            try:
                from .processor import reconcile_publication_state

                reconciliation = reconcile_publication_state(store)
                scanned = scan_and_sync_catalog(
                    store,
                    force=args.force,
                    allow_large_prune=args.allow_large_prune,
                )
                store.finish_run(
                    run_id,
                    0,
                    {"pages_scanned": scanned, "publication_reconciliation": reconciliation},
                )
            except BaseException:
                store.finish_run(run_id, 1, {})
                raise
    return 0


def _cmd_audit(_args: argparse.Namespace) -> int:
    from .catalog import audit_and_populate_dictionary

    with _open_store() as store:
        with store.exclusive_operation("audit"):
            _ensure_legacy_import(store, False)
            audit_and_populate_dictionary(store)
    return 0


def _cmd_tree(_args: argparse.Namespace) -> int:
    from .catalog import generate_tree_and_extract_pending

    with _open_store() as store:
        with store.exclusive_operation("tree"):
            _ensure_legacy_import(store, False)
            generate_tree_and_extract_pending(store)
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    with _open_store() as store:
        _ensure_legacy_import(store, False)
        counts = store.status_counts()
        pages = store.get_pages()
        translated_titles = sum(1 for p in pages if p.get("chinese_translation"))
        ambiguous, affected = store.ambiguous_alias_stats()
        print(f"状态库: {store.path} (schema v{SCHEMA_VERSION}, WAL)")
        print("中央状态库统计:")
        print(f"  总节点数:        {len(pages)}")
        for status, n in sorted(counts.items()):
            print(f"  {status:<14} {n}")
        print(f"  已对齐标题翻译:  {translated_titles} ({translated_titles / max(len(pages), 1):.1%})")
        print(f"  别名词条:        {store.alias_count()}")
        print(f"  歧义别名:        {ambiguous} 组（影响 {affected} 个候选页，链接重写按发布状态消歧）")
    return 0


def _cmd_translate(args: argparse.Namespace) -> int:
    from .processor import run_translation

    with _open_store() as store:
        with store.exclusive_operation("translate"):
            _ensure_legacy_import(store, False)
            run_id = store.start_run("translate")
            try:
                published, failed, skipped = run_translation(
                    store,
                    limit=args.limit,
                    category=args.category,
                    dry_run=args.dry_run,
                    use_cache=not args.no_cache,
                    force=args.force,
                    run_id=run_id,
                )
                exit_code = 1 if failed else 0
                store.finish_run(
                    run_id,
                    exit_code,
                    {"published": published, "failed": failed, "skipped": skipped},
                )
            except StateError:
                store.finish_run(run_id, 3, {})
                raise
            except RuntimeError as exc:
                logger.error("%s", exc)
                store.finish_run(run_id, 2, {})
                return 2
            except BaseException:
                store.finish_run(run_id, 1, {})
                raise
    if failed:
        return 1
    return 0


def _cmd_export(_args: argparse.Namespace) -> int:
    with _open_store() as store:
        with store.exclusive_operation("export"):
            _ensure_legacy_import(store, False)
            if store.export_json(settings.glossary_file):
                print(f"JSON 导出已更新: {settings.glossary_file}")
            else:
                print("JSON 导出无变化。")
    return 0


def _cmd_prune_cache(args: argparse.Namespace) -> int:
    with _open_store() as store:
        if args.dry_run:
            print(f"将清除 {store.cache_count()} 条分块翻译缓存。")
            return 0
        with store.exclusive_operation("prune-cache"):
            removed = store.cache_prune()
        print(f"已清除 {removed} 条分块翻译缓存。")
    return 0


def _cmd_migrate_pages(args: argparse.Namespace) -> int:
    from .migrate import migrate_legacy_pages

    with _open_store() as store:
        with store.exclusive_operation("migrate-pages"):
            _ensure_legacy_import(store, False)
            run_id = store.start_run("migrate-pages")
            try:
                migrated = migrate_legacy_pages(store, dry_run=args.dry_run)
                store.finish_run(run_id, 0, {"migrated": migrated})
            except BaseException:
                store.finish_run(run_id, 1, {})
                raise
        print(f"已迁移 {migrated} 个页面。")
    return 0


def _cmd_recover(args: argparse.Namespace) -> int:
    from .recover import recover_pages

    with _open_store() as store:
        with store.exclusive_operation("recover"):
            _ensure_legacy_import(store, False)
            run_id = store.start_run("recover")
            try:
                recovered = recover_pages(store, Path(args.html_dir), dry_run=args.dry_run)
                store.finish_run(run_id, 0, {"recovered": recovered})
            except BaseException:
                store.finish_run(run_id, 1, {})
                raise
        print(f"已恢复 {recovered} 个页面。")
    return 0


def _cmd_report(_args: argparse.Namespace) -> int:
    with _open_store() as store:
        _ensure_legacy_import(store, False)
        runs = store.recent_runs()
        errors = store.recent_errors()
        print("=== 最近运行记录 ===")
        for run in runs:
            print(
                f"  #{run['id']} {run['command']} {run['started_at']}"
                f" -> exit={run['exit_code']} ({run['finished_at'] or 'running'})"
            )
        print("=== 最近错误记录 ===")
        for error in errors:
            print(f"  [{error['created_at']}] {error['page_id']} {error['stage']}: {error['message'][:160]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nlab-pipeline", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan/reconcile the nLab source tree")
    scan.add_argument(
        "--force",
        action="store_true",
        help="强制重写所有扫描元数据（保留翻译历史）",
    )
    scan.add_argument(
        "--allow-large-prune",
        action="store_true",
        help="确认允许删除超过安全阈值的上游缺失页面",
    )

    sub.add_parser("audit", help="interactive dictionary review")
    sub.add_parser("tree", help="render category tree + pending list")
    sub.add_parser("status", help="print catalog summary")
    sub.add_parser("export", help="write the JSON export of the state db")

    tr = sub.add_parser("translate", help="translate pending entries")
    tr.add_argument("--limit", type=_positive_int, default=None, help="max entries to translate")
    tr.add_argument("--category", default=None, help="restrict to a category (primary/secondary)")
    tr.add_argument("--dry-run", action="store_true", help="list entries without calling the LLM")
    tr.add_argument("--no-cache", action="store_true", help="ignore the chunk translation cache")
    tr.add_argument("--force", action="store_true", help="include needs_review entries")

    prune = sub.add_parser("prune-cache", help="clear the chunk translation cache")
    prune.add_argument(
        "--dry-run",
        action="store_true",
        help="report how many entries would be removed",
    )

    rec = sub.add_parser(
        "recover", help="recover published pages whose content files are missing (from public/ HTML)"
    )
    rec.add_argument("--html-dir", default="public", help="built site directory")
    rec.add_argument("--dry-run", action="store_true", help="list what would be recovered")

    mig = sub.add_parser("migrate-pages", help="re-render legacy published pages")
    mig.add_argument("--dry-run", action="store_true", help="list what would be migrated")

    sub.add_parser("report", help="recent runs and recorded errors")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "scan": _cmd_scan,
        "audit": _cmd_audit,
        "tree": _cmd_tree,
        "status": _cmd_status,
        "translate": _cmd_translate,
        "export": _cmd_export,
        "prune-cache": _cmd_prune_cache,
        "migrate-pages": _cmd_migrate_pages,
        "recover": _cmd_recover,
        "report": _cmd_report,
    }
    try:
        return handlers[args.command](args)
    except StateError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 3
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
