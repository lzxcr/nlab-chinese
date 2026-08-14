"""Markdown structure protection for LLM translation.

Only plain prose is ever sent to the model.  Formulas, code, URLs, link
targets, wiki-links and raw HTML are replaced by opaque placeholders first and
restored verbatim afterwards, so the model can never corrupt page structure.
Validation then proves that every placeholder survived the round-trip.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN = re.compile(r"@@PROT\[(\d+)\]@@")

# Precedence matters: longer/outer constructs must be tried before inner ones.
_MASTER = re.compile(
    # 1. fenced code blocks (` or ~), with optional info string
    r"(?P<fence>\x60{3,}[^\n]*\n.*?\x60{3,}|~{3,}[^\n]*\n.*?~{3,})"
    # 2. display math, including the common multi-line nLab form
    r"|(?P<math_block>\$\$.*?\$\$)"
    # 3. LaTeX display/inline delimiter forms
    r"|(?P<math_bracket>\\\[.*?\\\])"
    r"|(?P<math_paren>\\\(.*?\\\))"
    # 4. LaTeX environments (\begin{...}...\end{...})
    r"|(?P<latex_env>\\begin\{(?P<env_name>[a-zA-Z*]+)\}.*?\\end\{(?P=env_name)\})"
    # 5. wiki-links [[...]] (target never enters the model)
    r"|(?P<wikilink>\[\[[^\[\]\n]*\]\])"
    # 6. markdown link destinations; labels remain translatable
    r"|(?P<mdlink_target>(?<=\]\()(?:(?:[^()\n]+|\([^()\n]*\))+)(?=\)))"
    # 7. reference-link ids and definitions
    r"|(?P<mdlink_reference>(?<=\]\[)[^\]\n]+(?=\]))"
    r"|(?P<mdlink_definition>^\[[^\]\n]+\]:[ \t]*\S+[^\n]*)"
    # 8. autolinks and raw URLs
    r"|(?P<url><https?://[^>\n]+>|https?://[^\s)>\]]+)"
    # 9. HTML tags (letter immediately after '<'; must close on the same line)
    r"|(?P<html></?[A-Za-z][A-Za-z0-9:-]*(?:\s+[^<>\n]{0,200})?>)"
    # 10. inline math $...$ (single line)
    r"|(?P<math_inline>\$[^\$\n]+?\$)"
    # 11. inline code `...`
    r"|(?P<code>\x60[^\x60\n]+\x60)",
    re.DOTALL | re.MULTILINE,
)

_HEADING = re.compile(r"^(#{1,6})[ \t]*([^#\n][^\n]*?)[ \t]*$")


def protected_spans(text: str) -> list[tuple[str, str]]:
    """Return ordered structural spans used by the publication validator.

    Comparing the complete values (rather than delimiter counts) catches
    dropped, duplicated, reordered, or subtly rewritten formulas, code,
    links, and HTML in recovery/import paths that do not pass through the
    normal placeholder round-trip.
    """
    return [
        ("latex_env" if match.lastgroup == "env_name" else match.lastgroup or "unknown", match.group(0))
        for match in _MASTER.finditer(text)
    ]


@dataclass
class Protector:
    _blocks: list[str] = field(default_factory=list)

    def protect(self, text: str) -> str:
        """Replace protected spans in *text* with @@PROT[i]@@ placeholders."""
        # Escape literal placeholder-looking source text first.  A single-pass
        # restore turns the generated token back into the literal string without
        # interpreting it a second time.
        def _escape_literal(match: re.Match) -> str:
            literal_index = int(match.group(1))
            if len(self._blocks) == literal_index:
                self._blocks.append("")  # reserve the colliding index
            self._blocks.append(match.group(0))
            return f"@@PROT[{len(self._blocks) - 1}]@@"

        text = _TOKEN.sub(_escape_literal, text)
        out: list[str] = []
        pos = 0
        for match in _MASTER.finditer(text):
            out.append(text[pos : match.start()])
            self._blocks.append(match.group(0))
            out.append(f"@@PROT[{len(self._blocks) - 1}]@@")
            pos = match.end()
        out.append(text[pos:])
        return "".join(out)

    def protect_terms(self, text: str, terms: set[str]) -> str:
        """Protect whole-word terms in unprotected prose, preserving source case."""
        cleaned = sorted({term.strip() for term in terms if term.strip()}, key=len, reverse=True)
        if not cleaned:
            return text
        pattern = re.compile(
            r"(?<![\w])(?:" + "|".join(re.escape(term) for term in cleaned) + r")(?![\w])",
            re.IGNORECASE,
        )
        out: list[str] = []
        pos = 0
        for token in _TOKEN.finditer(text):
            out.append(self._protect_terms_segment(text[pos : token.start()], pattern))
            out.append(token.group(0))
            pos = token.end()
        out.append(self._protect_terms_segment(text[pos:], pattern))
        return "".join(out)

    def _protect_terms_segment(self, text: str, pattern: re.Pattern) -> str:
        def _sub(match: re.Match) -> str:
            self._blocks.append(match.group(0))
            return f"@@PROT[{len(self._blocks) - 1}]@@"

        return pattern.sub(_sub, text)

    def protect_exact(self, text: str) -> str:
        """Protect an exact string (e.g. an already-Chinese heading line)."""
        self._blocks.append(text)
        return f"@@PROT[{len(self._blocks) - 1}]@@"

    def restore(self, text: str) -> str:
        """Replace every placeholder in *text* with its original span."""
        def _sub(match: re.Match) -> str:
            idx = int(match.group(1))
            if idx >= len(self._blocks):
                raise ValueError(f"unknown protection token @@PROT[{idx}]@@")
            return self._blocks[idx]

        return _TOKEN.sub(_sub, text)

    def placeholders_in(self, text: str) -> set[int]:
        return {int(m.group(1)) for m in _TOKEN.finditer(text)}

    def validate_roundtrip(self, source: str, result: str) -> list[str]:
        """Return a list of structural errors for a translated round-trip.

        Empty list means the structure survived: every placeholder appears
        exactly once in the result and no unknown placeholder was invented.
        """
        errors: list[str] = []
        expected_order = [int(m.group(1)) for m in _TOKEN.finditer(source)]
        actual = list(_TOKEN.finditer(result))
        actual_order = [int(m.group(1)) for m in actual]
        expected = Counter(expected_order)
        got = Counter(actual_order)
        if got != expected:
            missing = sorted((expected - got).elements())
            extra = sorted((got - expected).elements())
            if missing:
                errors.append(f"丢失保护块: {missing}")
            if extra:
                errors.append(f"模型生成或重复了保护块: {extra}")
        elif actual_order != expected_order:
            errors.append("保护块顺序发生变化")
        for m in actual:
            idx = int(m.group(1))
            if idx >= len(self._blocks):
                errors.append(f"越界保护块索引 {idx}")
        return errors
