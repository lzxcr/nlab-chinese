"""State store: collisions, transitions, ambiguity, migration, corruption."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from nlab_pipeline.state import (
    SCHEMA_VERSION,
    StateCorruptError,
    StateError,
    StateStore,
)


def _page(page_id, title, slug=None, status="discovered", aliases=None, md5="abc"):
    return {
        "page_id": page_id,
        "true_title": title,
        "chinese_translation": "",
        "category_primary": "Algebra",
        "category_secondary": "General",
        "slug": slug or title.replace(" ", "-"),
        "status": status,
        "mode": "auto",
        "source_md5": md5,
        "source_path": page_id,
        "source_size": 10,
        "fetched_at": "2026-01-01T00:00:00Z",
    }


def test_upsert_and_get(store):
    store.upsert_page(_page("pages/1", "Test page"))
    page = store.get_page("pages/1")
    assert page["true_title"] == "Test page"
    assert page["slug"] == "Test-page"


def test_bulk_upsert_fills_optional_schema_fields(store):
    store.upsert_pages([_page("pages/1", "Test page")])
    page = store.get_page("pages/1")
    assert page["source_revision"] == ""
    assert page["translator_model"] == ""
    assert page["prompt_version"] == ""


def test_slug_uniqueness_is_enforced(store):
    import sqlite3

    store.upsert_page(_page("pages/1", "Gödel numbering", slug="Gödel-numbering"))
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_page(_page("pages/2", "Gödel-numbering", slug="Gödel-numbering"))


def test_alias_ambiguity_is_preserved(store):
    store.upsert_page(_page("pages/1", "type", aliases=["type"]))
    store.upsert_page(_page("pages/2", "type II string theory", aliases=["type"]))
    store.set_aliases("pages/1", ["type", "t"])
    store.set_aliases("pages/2", ["type"])
    lookup = store.get_lookup()
    assert len(lookup["type"]) == 2  # candidates, not last-write-wins
    assert store.ambiguous_alias_stats() == (1, 2)


def test_transition_machine_rejects_illegal(store):
    store.upsert_page(_page("pages/1", "Test page", status="published"))
    with pytest.raises(StateError):
        store.transition("pages/1", "discovered")  # published -> discovered illegal
    store.transition("pages/1", "outdated")
    store.transition("pages/1", "queued")
    store.transition("pages/1", "translating")
    store.transition("pages/1", "translated")
    store.transition("pages/1", "validated")
    store.transition("pages/1", "published")
    assert store.get_page("pages/1")["status"] == "published"


def test_failed_promotion_flow(store):
    store.upsert_page(_page("pages/1", "Test page", status="failed", md5="x"))
    store.transition("pages/1", "needs_review")
    store.transition("pages/1", "queued")  # human fixed it, back to the queue
    assert store.get_page("pages/1")["status"] == "queued"


def test_rekey_cascades_aliases(store):
    store.upsert_page(_page("pages/1", "Test page", aliases=["Test page", "test"]))
    store.set_aliases("pages/1", ["Test page", "test"])
    store.rekey_page("pages/1", "pages/2")
    assert store.get_page("pages/1") is None
    assert store.get_page("pages/2")["true_title"] == "Test page"
    lookup = store.get_lookup()
    assert lookup["test"][0]["page_id"] == "pages/2"


def test_legacy_import_recomputes_colliding_slugs(tmp_path):
    legacy = tmp_path / "legacy.json"
    # Legacy format: keyed by slug; both keys collapse to the same slug base.
    legacy.write_text(
        json.dumps(
            {
                "Gödel-numbering": {
                    "true_title": "Gödel numbering",
                    "aliases": ["Gödel numbering"],
                    "category": {"primary": "Algebra", "secondary": "General"},
                    "status": "published",
                    "source_md5_or_sha": "h1",
                    "source_path": "pages/1",
                },
                "Gödel-numbering-2": {
                    "true_title": "Gödel-numbering",
                    "aliases": ["Gödel-numbering"],
                    "category": {"primary": "Algebra", "secondary": "General"},
                    "status": "discovered",
                    "source_md5_or_sha": "h2",
                    "source_path": "pages/2",
                },
            }
        ),
        encoding="utf-8",
    )
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    imported, skipped = store.import_legacy_json(legacy)
    assert (imported, skipped) == (2, 0)
    slugs = {p["slug"] for p in store.get_pages()}
    assert len(slugs) == 2  # unique, suffixed
    assert store.get_page("pages/1")["status"] == "published"
    store.close()


def test_schema_v2_export_imports_into_current_database(tmp_path):
    legacy = tmp_path / "legacy-v2.json"
    page = _page("pages/1", "Test page")
    page["category"] = {
        "primary": page.pop("category_primary"),
        "secondary": page.pop("category_secondary"),
    }
    page["source_md5_or_sha"] = page.pop("source_md5")
    page["translated_at"] = "2026-01-02T00:00:00Z"
    page["published_at"] = "2026-01-03T00:00:00Z"
    legacy.write_text(
        json.dumps({"schema_version": 2, "pages": {"pages/1": page}}),
        encoding="utf-8",
    )
    migrated = StateStore(tmp_path / "migrated.db")
    migrated.init_schema()
    assert migrated.import_legacy_json(legacy) == (1, 0)
    imported = migrated.get_page("pages/1")
    assert imported["true_title"] == "Test page"
    assert imported["source_size"] == 10
    assert imported["fetched_at"] == "2026-01-01T00:00:00Z"
    assert imported["published_at"] == "2026-01-03T00:00:00Z"
    migrated.close()


def test_corrupt_legacy_json_aborts(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{broken json", encoding="utf-8")
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    with pytest.raises(StateCorruptError):
        store.import_legacy_json(legacy)
    store.close()


def test_non_object_legacy_json_aborts(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("[]", encoding="utf-8")
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    with pytest.raises(StateCorruptError, match="根节点"):
        store.import_legacy_json(legacy)
    store.close()


def test_legacy_import_refuses_to_overwrite_nonempty_database(tmp_path):
    legacy = tmp_path / "legacy.json"
    legacy.write_text("{}", encoding="utf-8")
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    store.upsert_page(_page("pages/1", "Existing"))
    with pytest.raises(StateError, match="非空"):
        store.import_legacy_json(legacy)
    assert store.get_page("pages/1")["true_title"] == "Existing"
    store.close()


def test_invalid_sqlite_file_has_domain_error(tmp_path):
    database = tmp_path / "broken.db"
    database.write_text("not sqlite", encoding="utf-8")
    with pytest.raises(StateCorruptError, match="无法打开"):
        StateStore(database)


def test_future_sqlite_schema_is_rejected(tmp_path):
    database = tmp_path / "future.db"
    store = StateStore(database)
    store.init_schema()
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'schema_version'",
            (str(SCHEMA_VERSION + 1),),
        )
    reopened = StateStore(database)
    with pytest.raises(StateError, match="高于程序支持"):
        reopened.init_schema()
    reopened.close()


def test_export_json_current_shape(store, tmp_path):
    store.upsert_page(_page("pages/1", "Test page", aliases=["Test page"]))
    store.set_aliases("pages/1", ["Test page", "alias"])
    out = tmp_path / "export.json"
    assert store.export_json(out) is True
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["pages"]["pages/1"]["slug"] == "Test-page"
    assert "alias" in data["pages"]["pages/1"]["aliases"]
    # unchanged -> no rewrite
    assert store.export_json(out) is False


def test_chunk_cache_roundtrip(store):
    assert store.cache_get("k") is None
    store.cache_put("k", "译文", "gemma-4-12b-it")
    assert store.cache_get("k") == "译文"
    assert store.cache_prune() == 1
    assert store.cache_get("k") is None


def test_chunk_cache_connection_is_safe_across_workers(store):
    def write_and_read(index: int) -> str | None:
        key = f"key-{index}"
        store.cache_put(key, f"译文-{index}", "model")
        return store.cache_get(key)

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write_and_read, range(100)))
    assert results == [f"译文-{index}" for index in range(100)]


def test_exclusive_operation_rejects_concurrent_writer(tmp_path):
    first = StateStore(tmp_path / "state.db")
    second = StateStore(tmp_path / "state.db")
    first.init_schema()
    second.init_schema()
    with first.exclusive_operation("first"):
        with pytest.raises(StateError, match="并发"):
            with second.exclusive_operation("second"):
                pass
    first.close()
    second.close()
