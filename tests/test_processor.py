"""Processor: YAML frontmatter, validation, atomic publish, crash recovery."""

from dataclasses import replace

import pytest
import yaml

import nlab_pipeline.processor as processor
from nlab_pipeline.config import settings
from nlab_pipeline.processor import (
    PublicationError,
    _bilingual_body,
    _validate_page,
    build_frontmatter,
    publish_page,
    reconcile_publication_state,
)
from nlab_pipeline.state import StateStore


def _page(**overrides):
    page = {
        "page_id": "pages/1/2/3",
        "true_title": "Test page",
        "chinese_translation": "测试页",
        "category_primary": "Algebra",
        "category_secondary": "General",
        "slug": "Test-page",
        "status": "validated",
        "aliases": ["Test page"],
        "fetched_at": "2026-08-14T00:00:00Z",
        "translated_at": "2026-08-14T01:00:00Z",
        "published_at": None,
        "source_md5": "0123456789abcdef",
        "source_path": "pages/1/2/3",
        "source_size": 100,
    }
    page.update(overrides)
    return page


def test_frontmatter_roundtrips_through_yaml():
    front = build_frontmatter(_page())
    assert front.startswith("---")
    header = front.strip().split("---")[1]
    data = yaml.safe_load(header)
    assert data["title"] == "测试页"
    assert data["nlab"]["source_url"].startswith("https://ncatlab.org/nlab/show/")
    assert "translator" in data["nlab"]
    assert "prompt_version" in data["nlab"]["translator"]
    assert data["nlab"]["source_content_md5"] == "0123456789abcdef"


def test_frontmatter_escapes_quotes_in_titles():
    # The old hand-built frontmatter broke YAML on titles containing quotes.
    page = _page(
        true_title="The " + chr(34) + "weird" + chr(34) + " page: it" + chr(39) + "s fine",
        chinese_translation="关于" + chr(34) + "怪异" + chr(34) + "的页",
        aliases=[
            "The " + chr(34) + "weird" + chr(34) + " page: it" + chr(39) + "s fine",
            "alias, with: colon",
        ],
    )
    front = build_frontmatter(page)
    data = yaml.safe_load(front.strip().split("---")[1])
    assert data["title"] == "关于" + chr(34) + "怪异" + chr(34) + "的页"
    # the alias equal to the title is dropped by design
    assert data["aliases"] == ["alias, with: colon"]


def test_bilingual_body_has_no_h1():
    body = _bilingual_body("Title", "译文内容", "English original")
    assert "# Title" not in body  # double-H1 fix
    assert "## 中文翻译" in body
    assert "## 英文原文" in body
    assert "译文内容" in body
    assert "English original" in body


def test_validate_page_rejects_empty_and_prompt_echo():
    assert _validate_page("T", "", "x")  # empty translation
    assert any("残留" in e for e in _validate_page("T", "包含 <source_text> 的内容", "x"))
    assert any("代码块" in e for e in _validate_page("T", "中文内容", "```py\ncode\n```"))
    assert _validate_page("T", "这是通顺的中文翻译内容。", "English without code.") == []


def test_validate_page_accepts_kept_english_terms():
    errors = _validate_page(
        "T", "Grothendieck 拓扑上的预层是一个函子。", "A presheaf on a Grothendieck topology."
    )
    assert errors == []


def test_validate_page_rejects_changed_formula_link_and_heading_order():
    original = "## Definition\n\nSee [paper](https://example.test) and $F(x)$."
    assert any(
        "内容被改写" in error
        for error in _validate_page(
            "T",
            "## 定义\n\n参见 [论文](https://other.test) 以及 $G(x)$。",
            original,
        )
    )
    assert any(
        "标题层级" in error
        for error in _validate_page("T", "### 定义\n\n内容。", "## Definition\n\nText.")
    )


def test_validate_page_rejects_damaged_markdown_scaffolding():
    original = "- [ ] Read [the paper](https://example.test).\n\n> quoted text"
    translated = "阅读 ](论文)(https://example.test)。\n\n引用文本"
    errors = _validate_page("T", translated, original)
    assert any("列表/任务项" in error for error in errors)
    assert any("引用层级" in error for error in errors)
    assert any("Markdown 链接" in error for error in errors)


def test_publish_is_atomic_and_records_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(processor, "settings", replace(settings, content_dir=tmp_path / "content"))
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    page = _page(status="translated")
    store.upsert_page(page)
    store.set_aliases(page["page_id"], [page["true_title"], "Alias"])
    store.set_meta("source_revision", "deadbeef")

    path = publish_page(
        store,
        page,
        "## 定义\n\n这是 $C$ 上的一个函子。",
        "## Definition\n\nThis is a functor on $C$.",
    )
    front = yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])
    assert front["nlab"]["source_content_md5"] == page["source_md5"]
    assert front["nlab"]["source_revision"] == "deadbeef"
    assert front["aliases"] == ["Alias"]
    assert store.get_page(page["page_id"])["status"] == "published"
    assert not list(path.parent.glob(".*.tmp"))
    store.close()


def test_failed_atomic_replace_keeps_previous_file(tmp_path, monkeypatch):
    monkeypatch.setattr(processor, "settings", replace(settings, content_dir=tmp_path / "content"))
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    page = _page(status="translated")
    store.upsert_page(page)
    target = processor._output_path(page)
    target.parent.mkdir(parents=True)
    target.write_text("previous complete page", encoding="utf-8")

    def fail_replace(_source, _target):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(processor.os, "replace", fail_replace)
    with pytest.raises(PublicationError, match="simulated disk failure"):
        publish_page(
            store,
            page,
            "## 定义\n\n这是 $C$ 上的函子。",
            "## Definition\n\nThis is a functor on $C$.",
        )
    assert target.read_text(encoding="utf-8") == "previous complete page"
    assert store.get_page(page["page_id"])["status"] == "validated"
    assert not list(target.parent.glob(".*.tmp"))
    store.close()


def test_reconcile_requeues_missing_published_file(tmp_path, monkeypatch):
    monkeypatch.setattr(processor, "settings", replace(settings, content_dir=tmp_path / "content"))
    store = StateStore(tmp_path / "state.db")
    store.init_schema()
    page = _page(status="published")
    store.upsert_page(page)
    assert reconcile_publication_state(store) == {"requeued": 1, "published": 0}
    assert store.get_page(page["page_id"])["status"] == "queued"
    store.close()
