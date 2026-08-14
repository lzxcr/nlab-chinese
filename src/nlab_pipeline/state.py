"""SQLite-backed state store: the single source of truth for the pipeline.

Replaces the 11 MB single-JSON-file catalog with a transactional database:

- ``pages``      one row per nLab page, keyed by its stable source-path ID
                (relative to the nLab checkout), never by the derived slug;
- ``aliases``    alias -> page candidates (ambiguity is preserved, not
                silently overwritten);
- ``chunk_cache`` translation cache keyed by source+model+prompt+glossary
                hash, enabling resumable runs;
- ``runs`` / ``errors``  run and failure history.

The database is opened with WAL journaling and every mutating command runs
inside a transaction.  A corrupt database aborts the run instead of being
silently replaced by an empty one.  The legacy JSON catalog is imported once
when the database is first created; afterwards the JSON file is only an
export artefact (written when its content actually changes).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA_VERSION = 3

# Strict publication state machine.
#
#   discovered -> queued -> translating -> translated -> validated -> published
#                          \\-> failed -> queued
#                              failed xN -> needs_review
#   published -> outdated -> queued
#   any -> skipped (junk/stub page, reversible)
VALID_STATUSES = {
    "discovered", "queued", "translating", "translated", "validated",
    "published", "failed", "needs_review", "skipped", "outdated",
}

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "discovered": {"queued", "skipped", "failed"},
    "queued": {"translating", "skipped", "failed"},
    "translating": {"translated", "failed", "queued", "skipped"},
    "translated": {"validated", "failed", "skipped"},
    "validated": {"published", "failed", "skipped"},
    "published": {"outdated", "queued", "skipped"},
    "outdated": {"queued", "skipped", "translating", "failed"},
    "failed": {"queued", "needs_review", "skipped", "translating"},
    "needs_review": {"queued", "validated", "skipped", "translating"},
    "skipped": {"queued", "discovered"},
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pages (
    page_id             TEXT PRIMARY KEY,
    true_title          TEXT NOT NULL,
    chinese_translation TEXT NOT NULL DEFAULT '',
    category_primary    TEXT NOT NULL,
    category_secondary  TEXT NOT NULL,
    slug                TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'discovered',
    mode                TEXT NOT NULL DEFAULT 'auto',
    source_md5          TEXT NOT NULL DEFAULT '',
    source_path         TEXT NOT NULL,
    source_size         INTEGER NOT NULL DEFAULT 0,
    fetched_at          TEXT,
    translated_at       TEXT,
    published_at        TEXT,
    last_error          TEXT,
    fail_count          INTEGER NOT NULL DEFAULT 0,
    review_note         TEXT,
    source_revision     TEXT NOT NULL DEFAULT '',
    translator_model    TEXT NOT NULL DEFAULT '',
    prompt_version      TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pages_slug ON pages(slug);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);
CREATE TABLE IF NOT EXISTS aliases (
    alias_lower TEXT NOT NULL,
    page_id     TEXT NOT NULL REFERENCES pages(page_id)
        ON DELETE CASCADE ON UPDATE CASCADE,
    alias_raw   TEXT NOT NULL,
    PRIMARY KEY (alias_lower, page_id)
);
CREATE INDEX IF NOT EXISTS idx_aliases_page ON aliases(page_id);
CREATE TABLE IF NOT EXISTS chunk_cache (
    cache_key  TEXT PRIMARY KEY,
    translated TEXT NOT NULL,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    command     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    exit_code   INTEGER,
    stats       TEXT
);
CREATE TABLE IF NOT EXISTS errors (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id     INTEGER,
    page_id    TEXT,
    stage      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_errors_run ON errors(run_id);
"""


class StateError(RuntimeError):
    """Raised when the state database cannot be used safely."""


class StateCorruptError(StateError):
    """Raised when the state database fails its integrity check."""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Translation chunks run in a thread pool.  Access remains serialized by
    # StateStore._lock, while check_same_thread=False permits those guarded
    # cache reads/writes to use this connection safely.
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    # Scan/translate write many small transactions; checkpointing every 1000
    # WAL pages makes each commit O(checkpoint).  Defer checkpoints and let
    # the run finish with an explicit wal_checkpoint(TRUNCATE).
    conn.execute("PRAGMA wal_autocheckpoint=100000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


class StateStore:
    """Transactional access to the pipeline state database."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.path = Path(db_path or settings.state_file)
        self._lock = threading.RLock()
        try:
            self.conn = _connect(self.path)
        except sqlite3.DatabaseError as exc:
            raise StateCorruptError(f"无法打开状态库 {self.path}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Lifecycle / integrity
    # ------------------------------------------------------------------ #
    def init_schema(self) -> None:
        with self._lock, self.conn:
            existing_version = None
            try:
                row = self.conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                existing_version = int(row[0]) if row else None
            except sqlite3.OperationalError:
                pass
            except (TypeError, ValueError) as exc:
                raise StateCorruptError("状态库 schema_version 损坏，拒绝覆盖") from exc
            if existing_version is not None and existing_version > SCHEMA_VERSION:
                raise StateError(
                    f"状态库 schema v{existing_version} 高于程序支持的 v{SCHEMA_VERSION}，拒绝降级打开"
                )
            self.conn.executescript(_SCHEMA)
            self._ensure_column("pages", "source_revision", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("pages", "translator_model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("pages", "prompt_version", "TEXT NOT NULL DEFAULT ''")
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {
            row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def check_integrity(self) -> None:
        """Abort on corruption instead of silently starting from scratch."""
        with self._lock:
            row = self.conn.execute("PRAGMA quick_check").fetchone()
            if not row or row[0] != "ok":
                raise StateCorruptError(
                    f"状态库完整性校验失败 ({row[0] if row else 'no result'}): {self.path}"
                    " - 未修改任何数据。请从备份恢复，或备份后使用 'scan --force' 重建。"
                )

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @contextmanager
    def exclusive_operation(self, operation: str):
        """Hold a non-blocking process lock for a mutating CLI operation."""
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - Linux is the supported runtime
            raise StateError("当前平台不支持状态库进程锁") from exc
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StateError(
                    f"另一个管道操作正在运行，拒绝并发执行: {operation}"
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    # ------------------------------------------------------------------ #
    # Legacy migration
    # ------------------------------------------------------------------ #
    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value)
            )

    def has_pages(self) -> bool:
        with self._lock:
            return self.conn.execute("SELECT 1 FROM pages LIMIT 1").fetchone() is not None

    def import_legacy_json(self, legacy_path: Path) -> tuple[int, int]:
        """Import the legacy JSON catalog. Returns (imported, skipped).

        Only runs when the database has no pages yet.  Recomputes slugs with
        collision suffixing (the legacy keys were slugs and already collided,
        silently overwriting each other).
        """
        if self.has_pages():
            raise StateError("状态库非空，拒绝导入旧 JSON 以避免覆盖现有数据")
        if not legacy_path.exists():
            return 0, 0
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise StateCorruptError(
                f"旧状态库 JSON 损坏，已停止以避免覆盖历史数据: {legacy_path} ({exc})"
                " - 请修复或移除该文件后重试。"
            ) from exc

        if not isinstance(data, dict):
            raise StateCorruptError(f"旧状态库 JSON 根节点必须是对象: {legacy_path}")

        export_version = data.get("schema_version")
        if isinstance(export_version, int) and export_version > SCHEMA_VERSION:
            raise StateCorruptError(
                f"旧状态导出为 schema v{export_version}，高于程序支持的 "
                f"v{SCHEMA_VERSION}，拒绝降级导入: {legacy_path}"
            )
        # Export envelopes were introduced in schema v2.  Accept every
        # supported envelope version so a fresh checkout can migrate forward
        # without first creating an old-version SQLite database.
        if isinstance(data.get("pages"), dict):
            entries = [
                (page_id, meta) for page_id, meta in data.get("pages", {}).items()
                if isinstance(meta, dict) and meta.get("source_path")
            ]
        else:
            entries = [
                (key, meta) for key, meta in data.items()
                if isinstance(meta, dict) and meta.get("source_path")
            ]
            if not entries:
                return 0, 0

        slug_seen: dict[str, int] = {}
        imported = skipped = 0
        rows: list[tuple] = []
        alias_items: list[tuple[str, list[str]]] = []
        for legacy_key, meta in entries:
            source_path = str(meta.get("source_path", "")).strip()
            # The stable id is the source path (the legacy key was a slug and
            # already collided, silently overwriting entries).
            page_id = source_path or str(legacy_key)
            title = str(meta.get("true_title", "")).strip()
            if not title:
                skipped += 1
                continue
            slug = self._unique_slug(title, slug_seen)
            status = meta.get("status", "discovered")
            if status not in VALID_STATUSES:
                status = "discovered"
            category = meta.get("category") or {}
            if not isinstance(category, dict):
                raise StateCorruptError(f"旧状态词条 {page_id} 的 category 不是对象")
            try:
                fail_count = int(meta.get("fail_count") or 0)
                source_size = int(meta.get("source_size") or 0)
            except (TypeError, ValueError) as exc:
                raise StateCorruptError(f"旧状态词条 {page_id} 的数值字段无效") from exc
            aliases = meta.get("aliases") or [title]
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise StateCorruptError(f"旧状态词条 {page_id} 的 aliases 必须是字符串数组")
            rows.append(
                (
                    page_id,
                    title,
                    str(meta.get("chinese_translation", "")),
                    str(category.get("primary", "General-Mathematics")),
                    str(category.get("secondary", "General")),
                    slug,
                    status,
                    str(meta.get("mode", "auto")),
                    str(meta.get("source_md5_or_sha", "")),
                    source_path,
                    source_size,
                    meta.get("fetched_at"),
                    meta.get("translated_at"),
                    meta.get("published_at"),
                    str(meta.get("last_error", "")),
                    fail_count,
                    str(meta.get("source_revision", "")),
                    str(meta.get("translator_model", "")),
                    str(meta.get("prompt_version", "")),
                )
            )
            alias_items.append((page_id, aliases))
            imported += 1
        sql = """INSERT INTO pages
                 (page_id, true_title, chinese_translation, category_primary,
                  category_secondary, slug, status, mode, source_md5,
                  source_path, source_size, fetched_at, translated_at,
                  published_at, last_error, fail_count, source_revision,
                  translator_model, prompt_version)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        with self._lock, self.conn:
            self.conn.executemany(sql, rows)
            for page_id, aliases in alias_items:
                self._set_aliases(page_id, aliases)
        return imported, skipped

    @staticmethod
    def _unique_slug(title: str, seen: dict[str, int]) -> str:
        from .cleaner import to_safe_filename

        base = to_safe_filename(title)
        slug = base
        while slug in seen:
            seen[base] += 1
            slug = f"{base}-{seen[base]}"
        seen[slug] = 1
        return slug

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #
    def get_page(self, page_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM pages WHERE page_id = ?", (page_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_pages(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute("SELECT * FROM pages ORDER BY page_id")]

    def upsert_page(self, page: dict) -> None:
        """Insert or update a page row (scan output)."""
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO pages
                   (page_id, true_title, chinese_translation, category_primary,
                    category_secondary, slug, status, mode, source_md5,
                    source_path, source_size, fetched_at, last_error, fail_count,
                    review_note, translated_at, published_at, source_revision,
                    translator_model, prompt_version)
                   VALUES (:page_id, :true_title, :chinese_translation,
                           :category_primary, :category_secondary, :slug,
                           :status, :mode, :source_md5, :source_path,
                           :source_size, :fetched_at, :last_error, :fail_count,
                           :review_note, :translated_at, :published_at,
                           :source_revision, :translator_model, :prompt_version)
                   ON CONFLICT(page_id) DO UPDATE SET
                     true_title=excluded.true_title,
                     chinese_translation=excluded.chinese_translation,
                     category_primary=excluded.category_primary,
                     category_secondary=excluded.category_secondary,
                     slug=excluded.slug,
                     status=excluded.status,
                     mode=excluded.mode,
                     source_md5=excluded.source_md5,
                     source_path=excluded.source_path,
                     source_size=excluded.source_size,
                     fetched_at=excluded.fetched_at,
                     last_error=excluded.last_error,
                     fail_count=excluded.fail_count,
                     review_note=excluded.review_note,
                     translated_at=excluded.translated_at,
                     published_at=excluded.published_at,
                     source_revision=excluded.source_revision,
                     translator_model=excluded.translator_model,
                     prompt_version=excluded.prompt_version""",
                {
                    "page_id": page["page_id"],
                    "true_title": page["true_title"],
                    "chinese_translation": page.get("chinese_translation", ""),
                    "category_primary": page["category_primary"],
                    "category_secondary": page["category_secondary"],
                    "slug": page["slug"],
                    "status": page["status"],
                    "mode": page.get("mode", "auto"),
                    "source_md5": page.get("source_md5", ""),
                    "source_path": page["source_path"],
                    "source_size": page.get("source_size", 0),
                    "fetched_at": page.get("fetched_at"),
                    "last_error": page.get("last_error"),
                    "fail_count": page.get("fail_count", 0),
                    "review_note": page.get("review_note"),
                    "translated_at": page.get("translated_at"),
                    "published_at": page.get("published_at"),
                    "source_revision": page.get("source_revision", ""),
                    "translator_model": page.get("translator_model", ""),
                    "prompt_version": page.get("prompt_version", ""),
                },
            )

    def find_moved(self, md5: str, title: str, exclude_ids: set[str]) -> dict | None:
        """Find a page moved upstream (same content hash and title, new path).

        Restricts to rows whose current page_id was not scanned yet, so pages
        with identical content (e.g. redirect stubs) never steal each other's
        identity.
        """
        if not md5:
            return None
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM pages WHERE source_md5 = ? AND true_title = ?",
                (md5, title),
            ).fetchall()
        for row in rows:
            row = dict(row)
            if row["page_id"] not in exclude_ids:
                return row
        return None

    def rekey_page(self, old_id: str, new_id: str) -> None:
        """Move a page (and its aliases via ON UPDATE CASCADE) to a new id."""
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE pages SET page_id = ?, source_path = ? WHERE page_id = ?",
                (new_id, new_id, old_id),
            )

    def update_page(self, page_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "true_title", "chinese_translation", "category_primary",
            "category_secondary", "slug", "status", "mode", "source_md5",
            "source_path", "source_size", "fetched_at", "translated_at",
            "published_at", "last_error", "fail_count", "review_note",
            "source_revision", "translator_model", "prompt_version",
        }
        columns = [k for k in fields if k in allowed]
        if not columns:
            return
        assignments = ", ".join(f"{c} = ?" for c in columns)
        values = [fields[c] for c in columns] + [page_id]
        with self._lock, self.conn:
            self.conn.execute(f"UPDATE pages SET {assignments} WHERE page_id = ?", values)

    def upsert_pages(self, pages: list[dict]) -> None:
        """Insert/update many page rows in a single transaction (scan batch)."""
        if not pages:
            return
        sql = """INSERT INTO pages
                  (page_id, true_title, chinese_translation, category_primary,
                   category_secondary, slug, status, mode, source_md5,
                   source_path, source_size, fetched_at, last_error, fail_count,
                   review_note, translated_at, published_at, source_revision,
                   translator_model, prompt_version)
                 VALUES (:page_id, :true_title, :chinese_translation,
                         :category_primary, :category_secondary, :slug,
                         :status, :mode, :source_md5, :source_path,
                         :source_size, :fetched_at, :last_error, :fail_count,
                         :review_note, :translated_at, :published_at,
                         :source_revision, :translator_model, :prompt_version)
                 ON CONFLICT(page_id) DO UPDATE SET
                   true_title=excluded.true_title,
                   chinese_translation=excluded.chinese_translation,
                   category_primary=excluded.category_primary,
                   category_secondary=excluded.category_secondary,
                   slug=excluded.slug,
                   status=excluded.status,
                   mode=excluded.mode,
                   source_md5=excluded.source_md5,
                   source_path=excluded.source_path,
                   source_size=excluded.source_size,
                   fetched_at=excluded.fetched_at,
                   last_error=excluded.last_error,
                   fail_count=excluded.fail_count,
                   review_note=excluded.review_note,
                   translated_at=excluded.translated_at,
                   published_at=excluded.published_at,
                   source_revision=excluded.source_revision,
                   translator_model=excluded.translator_model,
                   prompt_version=excluded.prompt_version"""
        optional_defaults: dict[str, Any] = {
            "chinese_translation": "",
            "mode": "auto",
            "source_md5": "",
            "source_size": 0,
            "fetched_at": None,
            "last_error": "",
            "fail_count": 0,
            "review_note": None,
            "translated_at": None,
            "published_at": None,
            "source_revision": "",
            "translator_model": "",
            "prompt_version": "",
        }
        normalized = [{**optional_defaults, **page} for page in pages]
        with self._lock, self.conn:
            self.conn.executemany(sql, normalized)

    def set_aliases_bulk(self, items: list[tuple[str, list[str]]]) -> None:
        """Replace the aliases of many pages in a single transaction."""
        if not items:
            return
        with self._lock, self.conn:
            for page_id, aliases in items:
                self._set_aliases(page_id, aliases)

    def checkpoint(self) -> None:
        """Checkpoint + truncate the WAL after big batch writes."""
        with self._lock, self.conn:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def transition(self, page_id: str, to: str, *, force: bool = False) -> None:
        """Validate and apply a state-machine transition."""
        if to not in VALID_STATUSES:
            raise StateError(f"非法状态: {to!r}")
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT status FROM pages WHERE page_id = ?", (page_id,)
            ).fetchone()
            if row is None:
                raise StateError(f"词条不存在: {page_id}")
            current = row[0]
            if not force and to != current and to not in ALLOWED_TRANSITIONS.get(current, set()):
                raise StateError(f"非法状态迁移: {current} -> {to} ({page_id})")
            self.conn.execute(
                "UPDATE pages SET status = ? WHERE page_id = ?", (to, page_id)
            )

    def delete_page(self, page_id: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM pages WHERE page_id = ?", (page_id,))

    # ------------------------------------------------------------------ #
    # Aliases
    # ------------------------------------------------------------------ #
    def _set_aliases(self, page_id: str, aliases: Iterable[str]) -> None:
        self.conn.execute("DELETE FROM aliases WHERE page_id = ?", (page_id,))
        rows = [(a.lower().strip(), page_id, a) for a in set(aliases) if a.strip()]
        self.conn.executemany(
            "INSERT OR IGNORE INTO aliases(alias_lower, page_id, alias_raw) VALUES (?, ?, ?)",
            rows,
        )

    def set_aliases(self, page_id: str, aliases: Iterable[str]) -> None:
        with self._lock, self.conn:
            self._set_aliases(page_id, aliases)
            self.conn.commit()

    def get_aliases(self, page_id: str) -> list[str]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT alias_raw FROM aliases WHERE page_id = ? ORDER BY alias_raw",
                (page_id,),
            ).fetchall()
            return [r["alias_raw"] for r in rows]

    def get_lookup(self) -> dict[str, list[dict]]:
        """alias_lower -> list of candidate dicts (ambiguity preserved)."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT a.alias_lower, a.alias_raw, p.page_id, p.slug,
                          p.chinese_translation, p.true_title, p.status
                   FROM aliases a JOIN pages p ON p.page_id = a.page_id"""
            ).fetchall()
        lookup: dict[str, list[dict]] = {}
        for row in rows:
            lookup.setdefault(row["alias_lower"], []).append(
                {
                    "english_raw": row["alias_raw"],
                    "page_id": row["page_id"],
                    "slug": row["slug"],
                    "chinese_translation": row["chinese_translation"],
                    "true_title": row["true_title"],
                    "status": row["status"],
                }
            )
        return lookup

    def ambiguous_alias_stats(self) -> tuple[int, int]:
        """(ambiguous alias count, affected pages)."""
        with self._lock:
            rows = self.conn.execute(
                """SELECT alias_lower, COUNT(DISTINCT page_id) AS n
                   FROM aliases GROUP BY alias_lower HAVING n > 1"""
            ).fetchall()
        affected = sum(r["n"] for r in rows)
        return len(rows), affected

    def alias_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]

    # ------------------------------------------------------------------ #
    # Status helpers
    # ------------------------------------------------------------------ #
    def status_counts(self) -> dict[str, int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM pages GROUP BY status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def pages_in_status(self, *statuses: str) -> list[dict]:
        if not statuses:
            return self.get_pages()
        placeholders = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM pages WHERE status IN ({placeholders}) ORDER BY page_id",
                list(statuses),
            ).fetchall()
        return [dict(r) for r in rows]

    def pending_translation(self, limit: int | None = None,
                            category: str | None = None) -> list[dict]:
        # translated/validated can remain after a process crash.  The
        # processor reconciles those intermediate states before selecting
        # work; keeping them visible here prevents permanently stranded rows.
        statuses = (
            "discovered", "queued", "outdated", "failed", "translating",
            "translated", "validated",
        )
        pages = self.pages_in_status(*statuses)
        if category:
            pages = [
                p for p in pages
                if category in (p["category_primary"], p["category_secondary"])
            ]
        return pages[:limit] if limit else pages

    # ------------------------------------------------------------------ #
    # Runs / errors
    # ------------------------------------------------------------------ #
    def start_run(self, command: str) -> int:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs(command, started_at) VALUES (?, ?)", (command, _now())
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, exit_code: int, stats: dict | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE runs SET finished_at = ?, exit_code = ?, stats = ? WHERE id = ?",
                (_now(), exit_code, json.dumps(stats or {}, ensure_ascii=False), run_id),
            )

    def record_error(self, run_id: int | None, page_id: str | None,
                     stage: str, message: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO errors(run_id, page_id, stage, message, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (run_id, page_id, stage, message[:2000], _now()),
            )

    # ------------------------------------------------------------------ #
    # Chunk cache
    # ------------------------------------------------------------------ #
    def cache_get(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT translated FROM chunk_cache WHERE cache_key = ?", (key,)
            ).fetchone()
            return row[0] if row else None

    def cache_put(self, key: str, translated: str, model: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO chunk_cache(cache_key, translated, model, created_at) "
                "VALUES (?, ?, ?, ?)",
                (key, translated, model, _now()),
            )

    def cache_delete(self, key: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM chunk_cache WHERE cache_key = ?", (key,))

    def cache_prune(self, keep_models: list[str] | None = None) -> int:
        with self._lock, self.conn:
            if keep_models:
                placeholders = ",".join("?" for _ in keep_models)
                cur = self.conn.execute(
                    f"DELETE FROM chunk_cache WHERE model NOT IN ({placeholders})",
                    keep_models,
                )
            else:
                cur = self.conn.execute("DELETE FROM chunk_cache")
            return int(cur.rowcount)

    def cache_count(self) -> int:
        with self._lock:
            return int(self.conn.execute("SELECT COUNT(*) FROM chunk_cache").fetchone()[0])

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, command, started_at, finished_at, exit_code, stats FROM runs "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def recent_errors(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT page_id, stage, message, created_at FROM errors "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # Backup / export
    # ------------------------------------------------------------------ #
    def backup(self) -> Path:
        """Snapshot the database (safe under WAL)."""
        target = self.path.with_suffix(self.path.suffix + ".bak")
        with self._lock:
            dest = sqlite3.connect(str(target))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        return target

    def export_json(self, path: Path) -> bool:
        """Write the human-readable JSON export; True when it changed."""
        pages = self.get_pages()
        with self._lock:
            alias_rows = self.conn.execute(
                "SELECT page_id, alias_raw FROM aliases ORDER BY page_id, alias_raw"
            ).fetchall()
        aliases_by_page: dict[str, list[str]] = {}
        for row in alias_rows:
            aliases_by_page.setdefault(row["page_id"], []).append(row["alias_raw"])
        for page in pages:
            page["aliases"] = sorted(set(aliases_by_page.get(page["page_id"], [])))
            if page["true_title"] not in page["aliases"]:
                page["aliases"].append(page["true_title"])
        payload = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": iso_now(),
            "stats": {"pages": len(pages), "statuses": self.status_counts()},
            "pages": {
                page["page_id"]: {
                    "page_id": page["page_id"],
                    "true_title": page["true_title"],
                    "chinese_translation": page["chinese_translation"],
                    "category": {
                        "primary": page["category_primary"],
                        "secondary": page["category_secondary"],
                    },
                    "slug": page["slug"],
                    "status": page["status"],
                    "mode": page["mode"],
                    "source_md5_or_sha": page["source_md5"],
                    "source_path": page["source_path"],
                    "source_size": page["source_size"],
                    "aliases": page["aliases"],
                    "fetched_at": page.get("fetched_at"),
                    "translated_at": page.get("translated_at"),
                    "published_at": page.get("published_at"),
                    "source_revision": page.get("source_revision", ""),
                    "translator_model": page.get("translator_model", ""),
                    "prompt_version": page.get("prompt_version", ""),
                    "last_error": page.get("last_error"),
                    "fail_count": page.get("fail_count"),
                }
                for page in pages
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if path.exists():
            try:
                old = path.read_text(encoding="utf-8")
                # exported_at is a timestamp: ignore it when deciding whether
                # the export actually changed.
                normalized_old = re.sub(
                    r'"exported_at": "[^"]*"', '"exported_at": "NORMALIZED"', old
                )
                normalized_new = text.replace(
                    f'"exported_at": "{payload["exported_at"]}"', '"exported_at": "NORMALIZED"'
                )
                if normalized_old == normalized_new:
                    return False
            except OSError:
                pass
        tmp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
