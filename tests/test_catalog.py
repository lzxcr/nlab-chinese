"""Catalog scans are deterministic and fail closed around missing input."""

from dataclasses import replace

import pytest

import nlab_pipeline.catalog as catalog
from nlab_pipeline.config import settings
from nlab_pipeline.state import StateStore


def _configure(tmp_path, monkeypatch):
    nlab_dir = tmp_path / "nlab-content"
    configured = replace(
        settings,
        nlab_dir=nlab_dir,
        content_dir=tmp_path / "content",
        glossary_file=tmp_path / "catalog.json",
        dictionary_file=tmp_path / "dictionary.json",
        external_glossary_file=tmp_path / "external.json",
        non_translatable_file=tmp_path / "non-translatable.json",
    )
    monkeypatch.setattr(catalog, "settings", configured)
    return configured


def _source_page(configured, page_id="pages/1", title="Test page"):
    directory = configured.nlab_dir / page_id
    directory.mkdir(parents=True)
    (directory / "name").write_text(title, encoding="utf-8")
    (directory / "content.md").write_text(
        "## Definition\n\nThis source page contains enough meaningful text to scan.",
        encoding="utf-8",
    )


def _page(page_id: str) -> dict:
    return {
        "page_id": page_id,
        "true_title": page_id,
        "category_primary": "General-Mathematics",
        "category_secondary": "General",
        "slug": page_id.replace("/", "-"),
        "status": "discovered",
        "source_path": page_id,
    }


def test_scan_imports_a_valid_page(tmp_path, monkeypatch):
    configured = _configure(tmp_path, monkeypatch)
    _source_page(configured)
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    assert catalog.scan_and_sync_catalog(store) == 1
    assert store.get_page("pages/1")["true_title"] == "Test page"
    assert configured.glossary_file.is_file()
    store.close()


def test_scan_aborts_prune_when_any_source_is_unreadable(tmp_path, monkeypatch):
    configured = _configure(tmp_path, monkeypatch)
    directory = configured.nlab_dir / "pages/1"
    directory.mkdir(parents=True)
    (directory / "name").write_text("Test page", encoding="utf-8")
    (directory / "content.md").write_bytes(b"\xff\xfe")
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    store.upsert_page(_page("pages/1"))
    with pytest.raises(RuntimeError, match="无法读取"):
        catalog.scan_and_sync_catalog(store)
    assert store.get_page("pages/1") is not None
    store.close()


def test_scan_requires_confirmation_for_large_prune(tmp_path, monkeypatch):
    configured = _configure(tmp_path, monkeypatch)
    _source_page(configured)
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    store.upsert_pages([_page(f"pages/missing-{index}") for index in range(101)])
    with pytest.raises(RuntimeError, match="allow-large-prune"):
        catalog.scan_and_sync_catalog(store)
    assert len(store.get_pages()) == 102
    store.close()


def test_changed_source_requeues_review_failure_and_resets_counter(tmp_path, monkeypatch):
    configured = _configure(tmp_path, monkeypatch)
    _source_page(configured)
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    page = _page("pages/1")
    page.update(status="needs_review", source_md5="old", fail_count=3, last_error="old error")
    store.upsert_page(page)
    catalog.scan_and_sync_catalog(store)
    updated = store.get_page("pages/1")
    assert updated["status"] == "queued"
    assert updated["fail_count"] == 0
    assert updated["last_error"] == ""
    store.close()
