"""Minimal Aho-Corasick automaton for glossary term matching.

Scanning a chunk against ~76k aliases per chunk was quadratic (catalog.py
iterated the whole alias table for every chunk).  This automaton matches all
dictionary terms in a single linear pass over the chunk text.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Node:
    children: dict[str, _Node] = field(default_factory=dict)
    fail: _Node | None = None
    outputs: list[tuple[int, Any]] = field(default_factory=list)  # (length, payload)


class AhoCorasick:
    """Case-insensitive multi-pattern matcher.

    Payloads are arbitrary; a term may carry several payloads (e.g. several
    alias candidates pointing at different pages).
    """

    def __init__(self) -> None:
        self._root = _Node()
        self._built = False

    def add(self, term: str, payload: Any) -> None:
        self._built = False
        term = term.lower()
        node = self._root
        for ch in term:
            node = node.children.setdefault(ch, _Node())
        node.outputs.append((len(term), payload))

    def build(self) -> None:
        queue: deque[_Node] = deque()
        for child in self._root.children.values():
            child.fail = self._root
            queue.append(child)
        while queue:
            node = queue.popleft()
            for ch, child in node.children.items():
                queue.append(child)
                fail = node.fail
                while fail is not None and ch not in fail.children:
                    fail = fail.fail
                child.fail = fail.children[ch] if fail else self._root
                child.outputs.extend(child.fail.outputs)
        self._built = True

    def scan(self, text: str) -> Iterable[tuple[int, int, Any]]:
        """Yield (start, end, payload) for every match of every term."""
        if not self._built:
            self.build()
        text = text.lower()
        node = self._root
        for i, ch in enumerate(text):
            while node is not self._root and ch not in node.children:
                node = node.fail
            node = node.children.get(ch, self._root)
            for length, payload in node.outputs:
                yield (i + 1 - length, i + 1, payload)
