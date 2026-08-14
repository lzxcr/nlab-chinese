"""Recover legacy translated pages from the built public/ HTML.

The old pipeline wrote untracked content files that a buggy early scan deleted.
The built HTML still contains the rendered Chinese translation.  This module
walks the HTML back to markdown, restoring LaTeX formulas positionally from
the (deterministically regenerated) nLab source, and rewrites the pages in the
new format with provenance.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

from .cleaner import clean_nlab_garbage, fix_wikilinks, normalize_headings
from .config import logger, settings
from .processor import _bilingual_body, build_frontmatter
from .state import StateStore

# Same-line display math, lone $$ lines (rendered as empty display math by
# the old pipeline), and inline $...$.
_MATH_RE = re.compile(r"\$\$[^\n]*?\$\$|\$\$|\$[^\$\n]+?\$")
_MATH_MARK = re.compile(r"@@MATH:(\d+)@@")


class _MdWriter(HTMLParser):
    """Best-effort HTML -> markdown for the Chinese section."""

    _BLOCK = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li",
              "pre", "blockquote", "table", "tr", "hr", "br", "details", "summary"}

    def __init__(self, math_counter: list) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.math_counter = math_counter
        self._inline: list[str] = []
        self._skip = 0
        self._list_stack: list[str] = []
        self._li_depth = 0

    # -- helpers ------------------------------------------------------- #
    def _flush_inline(self) -> str:
        text = "".join(self._inline).strip()
        # Collapse horizontal whitespace only; keep newlines so raw-markdown
        # regions (from old broken renders) re-parse as markdown.
        text = re.sub(r"[ \t]+", " ", text)
        self._inline = []
        return text

    def _emit_block(self, inline: str) -> None:
        if inline:
            self.out.append(inline)
            self.out.append("\n\n")

    # -- parser -------------------------------------------------------- #
    def handle_starttag(self, tag: str, attrs: list) -> None:
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        if self._skip:
            self._skip += 1
            return
        if tag == "span" and "katex-display" in classes:
            self._emit_block(self._flush_inline())
            idx = self.math_counter[0]
            self.math_counter[0] += 1
            self.out.append(f"@@MATH:{idx}@@\n\n")
            self._skip = 1
            return
        if tag == "span" and "katex" in classes:
            idx = self.math_counter[0]
            self.math_counter[0] += 1
            self._inline.append(f"@@MATH:{idx}@@")
            self._skip = 1
            return
        if tag == "a" and (
            "tag-link" in classes
            or "anchor" in classes
            or attrs.get("role") == "anchor"
            or attrs.get("aria-hidden") == "true"
        ):
            self._skip = 1
            return
        if tag == "a":
            href = attrs.get("href", "")
            slug = attrs.get("data-slug", "")
            if slug:
                self._inline.append(f"[[{slug}|")
            else:
                self._inline.append("[")
            self._link = (href, slug)
            return
        if tag in ("strong", "b"):
            self._inline.append("**")
        elif tag in ("em", "i"):
            self._inline.append("_")
        elif tag == "code" and "language-" not in classes:
            self._inline.append("\u0060")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._emit_block(self._flush_inline())
            self._inline.append("#" * int(tag[1]) + " ")
        elif tag == "p" and self._li_depth:
            pass  # <li><p>..</p></li>: keep the marker, no premature flush
        elif tag in ("p", "blockquote", "pre", "table", "details"):
            self._emit_block(self._flush_inline())
            if tag == "blockquote":
                self._inline.append("> ")
        elif tag == "ul":
            self._emit_block(self._flush_inline())
            self._list_stack.append("- ")
        elif tag == "ol":
            self._emit_block(self._flush_inline())
            self._list_stack.append("1. ")
        elif tag == "li":
            self._emit_block(self._flush_inline())
            marker = self._list_stack[-1] if self._list_stack else "- "
            self._inline.append(marker)
            self._li_depth += 1
        elif tag == "br":
            self._inline.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self._skip:
            self._skip -= 1
            return
        if tag == "a":
            href, slug = getattr(self, "_link", ("", ""))
            if slug:
                self._inline.append("]]")
            else:
                self._inline.append(f"]({href})")
            return
        if tag in ("strong", "b"):
            self._inline.append("**")
        elif tag in ("em", "i"):
            self._inline.append("_")
        elif tag == "code":
            self._inline.append("\u0060")
        elif tag in ("h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "pre"):
            self._emit_block(self._flush_inline())
        elif tag == "li":
            self._emit_block(self._flush_inline())
            self._li_depth = max(0, self._li_depth - 1)
        elif tag in ("ul", "ol"):
            if self._list_stack:
                self._list_stack.pop()
            self._emit_block(self._flush_inline())
        elif tag in ("table", "details", "div"):
            self._emit_block(self._flush_inline())

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        self._inline.append(data)

    def result(self) -> str:
        self._emit_block(self._flush_inline())
        return "\n".join(line.rstrip() for line in self.out).strip()


def _extract_section(html: str, heading: str, next_heading: str) -> str | None:
    start = re.search(r'<h2[^>]*>' + re.escape(heading), html)
    if not start:
        return None
    body = html[start.end():]
    # Skip to the end of the heading element (its anchor svg lives inside h2).
    closing = body.find("</h2>")
    if closing >= 0:
        body = body[closing + len("</h2>"):]
    # The English section is either an <h2> (pre-rewrite builds) or a
    # <details class="english-original"> wrapper (current builds).
    end = re.search(
        r'<h2[^>]*>' + re.escape(next_heading)
        + r"|<details[^>]*class=\s*[\"']english-original|<summary\b",
        body,
    )
    return body[: end.start()] if end else body


def recover_translation(html: str, original_md: str) -> str:
    """Rebuild the translated markdown section from rendered HTML."""
    section = _extract_section(html, "中文翻译 / Chinese Translation", "英文原文 / Original English")
    if not section:
        raise ValueError("HTML 中找不到中文翻译区块")
    counter = [0]
    writer = _MdWriter(counter)
    writer.feed(section)
    md = writer.result()
    # The old render swallowed the English section into raw text when the old
    # model emitted a stray "$$"; cut everything from the English heading on.
    cut = re.search(r"\n-{3,}\s*\n##\s*英文原文|\n##\s*英文原文|\n>\s*\[!NOTE\]", md)
    if cut:
        md = md[: cut.start()]
    # Normalise heading artifacts introduced by old broken renders.
    md = re.sub(r"^# # ", "## ", md, flags=re.MULTILINE)
    md = re.sub(r"^(#{1,6})(?=[^\s#])", r"\1 ", md, flags=re.MULTILINE)
    maths = _MATH_RE.findall(original_md)
    used = counter[0]
    if used > len(maths):
        raise ValueError(f"数学公式数量不匹配: HTML {used} > 原文 {len(maths)}")

    def _sub(match: re.Match) -> str:
        idx = int(match.group(1))
        if idx >= len(maths):
            raise ValueError(f"HTML 数学占位越界: {idx} >= {len(maths)}")
        return maths[idx]

    return _MATH_MARK.sub(_sub, md)


def recover_pages(store: StateStore, html_dir: Path, *, dry_run: bool = False) -> int:
    """Recover published pages whose content files are missing."""
    lookup = store.get_lookup()
    published = store.pages_in_status("published")
    recovered = 0
    for page in published:
        target = settings.content_dir / page["category_primary"] / page["category_secondary"]             / (page["slug"] + ".md")
        if target.exists():
            continue
        # locate built html: categories may have changed since the old build,
        # so search the public tree by slug.
        html_path = html_dir / page["category_primary"] / page["category_secondary"]             / (page["slug"] + ".html")
        if not html_path.exists():
            matches = list(html_dir.rglob(page["slug"] + ".html")) if html_dir.is_dir() else []
            html_path = matches[0] if matches else None
        if not html_path:
            logger.warning("找不到构建产物，跳过恢复: %s", page["true_title"])
            continue
        html = html_path.read_text(encoding="utf-8")
        source_dir = settings.nlab_dir / page["source_path"]
        if not source_dir.is_dir():
            logger.warning("源路径不存在，跳过恢复: %s", page["source_path"])
            continue
        raw = (source_dir / "content.md").read_text(encoding="utf-8")
        original = fix_wikilinks(
            normalize_headings(clean_nlab_garbage(raw)), lookup, settings.nlab_base_url
        )
        # Positional math alignment must run against the pre-fix markup (the
        # old build rendered $[$ ... $]$ as extra katex nodes).
        original_legacy = fix_wikilinks(
            normalize_headings(clean_nlab_garbage(raw, unwrap_instiki_links=False)),
            lookup,
            settings.nlab_base_url,
        )
        try:
            translated = recover_translation(html, original_legacy)
        except ValueError as exc:
            logger.error("恢复失败 %s: %s", page["true_title"], exc)
            continue
        if not translated.strip():
            logger.error("恢复出的译文为空: %s", page["true_title"])
            continue
        # The old pipeline let the model rewrite wiki-link targets (e.g.
        # '(∞,1)-limit' -> '(∞,1)-极限'); normalise them with the current
        # publication policy (published -> local slug, else -> nLab URL).
        translated = fix_wikilinks(translated, lookup, settings.nlab_base_url)
        # Repair half-broken links the old model emitted: [[target|display]
        translated = re.sub(r"\[\[([^\]\n]*)\|([^\]\n]*)\](?!\])", r"[[\1|\2]]", translated)
        # The old model sometimes emitted a stray inline "$$" (e.g.
        # "本质代数 $$-理论"), which remark-math reads as an unclosed display
        # block and swallows the rest of the page.  Drop "$$" tokens that are
        # not alone on their line (real display-math delimiters).
        translated = re.sub(r"(?<!^)\$\$(?!$)", "", translated, flags=re.MULTILINE)
        if dry_run:
            logger.info("[dry-run] 将恢复: %s (%s)", page["true_title"], target)
            continue
        page["source_revision"] = store.get_meta("source_revision") or ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            build_frontmatter(page) + "\n" + _bilingual_body(page["true_title"], translated, original),
            encoding="utf-8",
        )
        recovered += 1
        logger.info("已恢复页面: %s -> %s", page["true_title"], target)
    return recovered
