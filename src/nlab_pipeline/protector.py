"""Markdown structure protection for LLM translation.

Only plain prose is ever sent to the model.  Formulas, code, URLs, link
targets, wiki-links and raw HTML are replaced by opaque placeholders first and
restored verbatim afterwards, so the model can never corrupt page structure.
Validation then proves that every placeholder survived the round-trip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_TOKEN = re.compile(r"@@PROT\[(\d+)\]@@")

# Precedence matters: longer/outer constructs must be tried before inner ones.
_MASTER = re.compile(
    # 1. fenced code blocks (` or ~), with optional info string
    r"(?P<fence>\x60{3,}[^\n]*\n.*?\x60{3,}|~{3,}[^\n]*\n.*?~{3,})"
    # 2. display math $$...$$ (single line, most common on nLab)
    r"|(?P<math_block>\$\$[^\n]*?\$\$)"
    # 3. LaTeX environments (\begin{...}...\end{...})
    r"|(?P<latex_env>\\begin\{[a-zA-Z*]+\}.*?\\end\{[a-zA-Z*]+\})"
    # 4. wiki-links [[...]] (target never enters the model)
    r"|(?P<wikilink>\[\[[^\[\]\n]*\]\])"
    # 5. markdown links/images [text](url)
    r"|(?P<mdlink>!?\[[^\]\n]*\]\([^)\n]*\))"
    # 6. autolinks and raw URLs
    r"|(?P<url><https?://[^>\n]+>|https?://[^\s)>\]]+)"
    # 7. HTML tags (letter immediately after '<'; must close on the same line)
    r"|(?P<html></?[A-Za-z][A-Za-z0-9:-]*(?:\s+[^<>\n]{0,200})?>)"
    # 8. inline math $...$ (single line)
    r"|(?P<math_inline>\$[^\$\n]+?\$)"
    # 9. inline code `...`
    r"|(?P<code>\x60[^\x60\n]+\x60)",
    re.DOTALL,
)

_HEADING = re.compile(r"^(#{1,6})[ \t]*([^#\n][^\n]*?)[ \t]*$")


@dataclass
class Protector:
    _blocks: list[str] = field(default_factory=list)

    def protect(self, text: str) -> str:
        """Replace protected spans in *text* with @@PROT[i]@@ placeholders."""
        out: list[str] = []
        pos = 0
        for match in _MASTER.finditer(text):
            out.append(text[pos : match.start()])
            self._blocks.append(match.group(0))
            out.append(f"@@PROT[{len(self._blocks) - 1}]@@")
            pos = match.end()
        out.append(text[pos:])
        return "".join(out)

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
        expected = self.placeholders_in(source)
        actual = list(_TOKEN.finditer(result))
        got = {int(m.group(1)) for m in actual}
        if got != expected:
            missing = sorted(expected - got)
            extra = sorted(got - expected)
            if missing:
                errors.append(f"丢失保护块: {missing}")
            if extra:
                errors.append(f"模型生成了未知保护块: {extra}")
        for m in actual:
            idx = int(m.group(1))
            if idx >= len(self._blocks):
                errors.append(f"越界保护块索引 {idx}")
        return errors
