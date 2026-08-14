"""Legacy migration must not rewrite pages already in the current format."""

from dataclasses import replace

import nlab_pipeline.migrate as migrate
from nlab_pipeline.config import settings
from nlab_pipeline.state import StateStore


def test_current_provenance_page_is_not_migrated(tmp_path, monkeypatch):
    content_dir = tmp_path / "content"
    monkeypatch.setattr(migrate, "settings", replace(settings, content_dir=content_dir))
    page = {
        "page_id": "pages/1",
        "true_title": "Test page",
        "category_primary": "Algebra",
        "category_secondary": "General",
        "slug": "Test-page",
        "status": "published",
        "source_path": "pages/1",
    }
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    store.upsert_page(page)
    target = content_dir / "Algebra" / "General" / "Test-page.md"
    target.parent.mkdir(parents=True)
    target.write_text(
        "---\ntitle: 测试页\nnlab:\n  source_content_md5: abc\n---\n"
        "## 中文翻译 / Chinese Translation\n\n译文\n\n---\n\n"
        "## 英文原文 / Original English\n\nOriginal",
        encoding="utf-8",
    )

    def unexpected_publish(*_args, **_kwargs):
        raise AssertionError("current page should not pass through legacy publisher")

    monkeypatch.setattr(migrate, "publish_page", unexpected_publish)
    assert migrate.migrate_legacy_pages(store) == 0
    store.close()
