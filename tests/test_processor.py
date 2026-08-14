"""Processor: YAML frontmatter escaping, bilingual body, validation gates."""

import yaml

from nlab_pipeline.processor import _bilingual_body, _validate_page, build_frontmatter


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
