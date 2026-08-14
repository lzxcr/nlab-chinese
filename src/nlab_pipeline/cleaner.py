"""Cleaning helpers that convert raw nLab markup into Quartz-friendly Markdown.

nLab stores each page as a directory containing a `name` file (the human
readable title) and a `content.md` file carrying Instiki-style markup.  The
functions here strip that markup, normalise wiki-links, derive category
metadata, and produce filesystem-safe slugs.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from urllib.parse import quote

_LINK = re.compile(r"\[\[(.*?)\]\]")
_REDIRECT = re.compile(r"\[\[!redirects\s+(.*?)\]\]")
_CONTEXT = re.compile(r"###\s*Context\s*###\s*\n((?:[ \t]*####[^\n]*\n)*)")
_CONTEXT_HEADING = re.compile(r"####\s*([^\n]+)")
# '######Definition' (no space) is not a valid heading; normalise it.
_HEADING_SQUASH = re.compile(r"^(#{1,6})(?=[^\s#])", re.MULTILINE)
_MATH_STRIP = re.compile(r"\$[^\$]*\$")


def get_file_hash(filepath: str) -> str:
    """Return the MD5 digest of *filepath*, or an empty string on error."""
    hasher = hashlib.md5()
    try:
        with open(filepath, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
    except OSError:
        return ""
    return hasher.hexdigest()


def to_safe_filename(title: str) -> str:
    """Convert a page title into a filesystem-safe, Quartz-safe slug."""
    safe = re.sub(r'[\\/*?:"<>|#$\x00-\x1f]', "", title).strip()
    safe = re.sub(r"\s+", "-", safe)
    safe = re.sub(r"-{2,}", "-", safe).strip(".-_")
    return safe if safe else "unnamed_element"


def sanitize_category(value: str) -> str:
    """Turn a context heading into a clean, short category label."""
    value = value.strip()
    value = _MATH_STRIP.sub("", value)
    value = re.sub(r"[\[\]{}#]", "", value)
    value = re.sub(r"\s+", " ", value).strip(" -+")
    value = value[:80].strip(" -+")
    return value if value else "General"


def extract_nlab_context(content: str) -> tuple[str, str]:
    """Extract the primary/secondary category from nLab's context block.

    Only headings inside the page's '### Context ###' block are considered
    (the old heuristic matched every '####' anywhere in the page, including
    the tail end of headings such as '#### ... ####' followed by '## Theorem',
    which produced garbage categories like '## Definition').  Pages without
    a context block fall back to "General-Mathematics" / "General".
    """
    values: list[str] = []
    match = _CONTEXT.search(content)
    if match:
        for heading in _CONTEXT_HEADING.findall(match.group(1)):
            cleaned = sanitize_category(heading)
            if cleaned and cleaned not in values:
                values.append(cleaned)
    primary = values[0] if values else "General-Mathematics"
    secondary = values[1] if len(values) > 1 else "General"
    if secondary == primary:
        secondary = "General"
    return primary, secondary


def clean_nlab_garbage(content: str, *, unwrap_instiki_links: bool = True) -> str:
    """Remove Instiki control markup, includes, redirects and TOC boilerplate.

    unwrap_instiki_links=False keeps the legacy $[$ ... $]$ wrappers, which is
    needed when aligning recovered math positionally against pre-fix builds.
    """
    # right-hand side / toc blocks wrapped in { ... } and terminated by =--
    content = re.sub(
        r"\+--\s*\{\s*:\s*\.rightHandSide\s*\}.*?^=--\s*$",
        "",
        content,
        flags=re.DOTALL | re.MULTILINE,
    )
    content = re.sub(
        r"\+--\s*\{\s*:\s*\.toc\s+.*?\}.*?^=--\s*$",
        "",
        content,
        flags=re.DOTALL | re.MULTILINE,
    )
    content = re.sub(
        r"\+--\s*\{\s*:\s*\.hide\s*\}.*?^=--\s*$",
        "",
        content,
        flags=re.DOTALL | re.MULTILINE,
    )
    content = content.replace("=--", "").replace("+--", "")
    content = re.sub(r"\{\s*:\s*\.[^}\n]+\}", "", content)
    content = re.sub(r"\{\s*:\s*toc\s*\}", "", content)
    content = re.sub(r"\[\[!redirects.*?\]\]", "", content, flags=re.DOTALL)
    content = re.sub(r"\[\[!include.*?\]\]", "", content, flags=re.DOTALL)
    content = re.sub(r"\{:\s*\.query\}.*?$", "", content, flags=re.DOTALL)
    content = re.sub(
        r"#Contents#.*?\* table of contents.*?\n",
        "",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    # Instiki external-link wrapping: $[$[doi:...](https://...)$]$ -> [doi:...](https://...)
    if unwrap_instiki_links:
        content = content.replace("$[$", "").replace("$]$", "")
    return content.strip()


def normalize_headings(content: str) -> str:
    """Normalise squashed headings such as '######Definition' to '###### Definition'."""
    return _HEADING_SQUASH.sub(r"\1 ", content)


def _split_wikilink_target(link_text: str) -> tuple[str, str]:
    """Split 'target|display' into (target, display)."""
    target, _, display = link_text.partition("|")
    target = target.strip()
    display = display.strip() or target
    return target, display


def fix_wikilinks(
    content: str,
    runtime_lookup: Mapping[str, Sequence[dict]],
    nlab_base_url: str = "https://ncatlab.org/nlab/show/",
) -> str:
    """Rewrite [[...]] links following the publication policy.

    - Target published locally  -> [[slug|display]] (display uses the Chinese
      translation when available).
    - Target not yet published  -> absolute link back to nLab.
    - Ambiguous aliases resolve only when exactly one *published* candidate
      remains; otherwise the link points at nLab instead of guessing.
    - Unknown targets            -> absolute link back to nLab.
    """

    def _candidate_score(candidate: dict, target: str) -> tuple[int, int, int]:
        published = 1 if candidate.get("status") == "published" else 0
        raw_lower = candidate.get("english_raw", "").lower()
        title_lower = candidate.get("true_title", "").lower()
        exact = 1 if raw_lower == target.lower() else 0
        is_title = 1 if title_lower == target.lower() else 0
        return (published, exact, is_title)

    def _replace(match: re.Match) -> str:
        link_text = match.group(1).strip()
        if not link_text or link_text.startswith("!"):
            return match.group(0)
        target, display = _split_wikilink_target(link_text)
        candidates = runtime_lookup.get(target.lower().strip(), [])
        if candidates:
            pool = [c for c in candidates if c.get("status") == "published"]
            if pool:
                scored = [(candidate, _candidate_score(candidate, target)) for candidate in pool]
                best_score = max(score for _, score in scored)
                best_candidates = [
                    candidate for candidate, score in scored if score == best_score
                ]
                # Equal-best published candidates are genuinely ambiguous;
                # link upstream instead of depending on database row order.
                if len(best_candidates) == 1 and best_candidates[0].get("slug"):
                    best = best_candidates[0]
                    display_text = best.get("chinese_translation") or display
                    return f"[[{best['slug']}|{display_text}]]"
        # Fallback: absolute link to the upstream nLab page.
        url = nlab_base_url + quote(target, safe="")
        return f"[{display}]({url})"

    return _LINK.sub(_replace, content)


# Backwards-compatible alias kept for any external imports.
fix_links_and_latex = fix_wikilinks
