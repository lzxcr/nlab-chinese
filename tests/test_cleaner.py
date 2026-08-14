"""Cleaner: slug collisions, category extraction, link policy."""

from nlab_pipeline.cleaner import (
    extract_nlab_context,
    fix_wikilinks,
    sanitize_category,
    to_safe_filename,
)

CONTEXT_PAGE = (
    "+-- {: .rightHandSide}\n+-- {: .toc .clickDown tabindex=\"0\"}\n"
    "###Context###\n#### Higher algebra\n#### Homotopy theory\n=--\n=--\n\n"
    "## Definition\n\nReal content here."
)

NO_CONTEXT_PAGE = "## Definition\n\nA plain page without a context block."


def test_slug_collision_shows_the_problem():
    # 'Gödel numbering' and 'Gödel-numbering' collapse to the same slug; the
    # catalog layer must suffix them (covered by test_state).
    assert to_safe_filename("Gödel numbering") == to_safe_filename("Gödel-numbering")


def test_to_safe_filename_removes_unsafe_chars():
    assert to_safe_filename("a/b:c*d?e|f<g>h#i$j") == "abcdefghij"
    assert to_safe_filename("  ") == "unnamed_element"


def test_category_from_context_block_only():
    primary, secondary = extract_nlab_context(CONTEXT_PAGE)
    assert (primary, secondary) == ("Higher algebra", "Homotopy theory")


def test_category_fallback():
    primary, secondary = extract_nlab_context(NO_CONTEXT_PAGE)
    assert (primary, secondary) == ("General-Mathematics", "General")


def test_category_never_contains_heading_artifacts():
    content = "#### Category theory ####\n## Definition"
    primary, secondary = extract_nlab_context("### Context ###\n" + content)
    assert "##" not in primary


def test_sanitize_category_strips_math():
    assert sanitize_category("$(\\infty,1)$-Category theory") == "Category theory"


def test_link_policy_published_only():
    lookup = {
        "presheaf": [{"slug": "presheaf", "status": "published", "chinese_translation": "预层",
                      "english_raw": "presheaf", "true_title": "presheaf"}],
        "category": [{"slug": "category", "status": "discovered", "chinese_translation": "范畴",
                      "english_raw": "category", "true_title": "category"}],
    }
    out = fix_wikilinks("See [[presheaf]] and [[category]] and [[unknown]].",
                        lookup, "https://ncatlab.org/nlab/show/")
    assert "[[presheaf|预层]]" in out
    assert "[category](https://ncatlab.org/nlab/show/category)" in out
    assert "[unknown](https://ncatlab.org/nlab/show/unknown)" in out


def test_link_policy_ambiguous_alias_prefers_exact_published():
    lookup = {
        "type": [
            {"slug": "type-a", "status": "published", "chinese_translation": "类型A",
             "english_raw": "type", "true_title": "type"},
            {"slug": "type-b", "status": "published", "chinese_translation": "类型B",
             "english_raw": "type", "true_title": "type theory"},
        ],
    }
    out = fix_wikilinks("A [[type]] here.", lookup, "https://ncatlab.org/nlab/show/")
    # Two published candidates: exact title match wins over the alias-only one.
    assert "[[type-a|类型A]]" in out


def test_link_policy_no_published_candidate_goes_to_nlab():
    lookup = {
        "monad": [{"slug": "monad", "status": "queued", "chinese_translation": "单子",
                   "english_raw": "monad", "true_title": "monad"}],
    }
    out = fix_wikilinks("A [[monad]].", lookup, "https://ncatlab.org/nlab/show/")
    assert "[[monad" not in out
    assert "[monad](https://ncatlab.org/nlab/show/monad)" in out
