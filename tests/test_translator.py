"""Translator: chunking, headings, validation, cache, strict failures."""

import pytest

from nlab_pipeline.protector import Protector
from nlab_pipeline.translator import (
    Cache,
    TranslationError,
    _preprocess_headings,
    _split_chunks,
    estimate_tokens,
    translate_document,
    validate_chunk_translation,
)


class FakeClient:
    """Translates text by replacing latin words with 中文, keeping tokens."""

    def __init__(self, fail_times: int = 0, drop_tokens: bool = False):
        self.calls = 0
        self.fail_times = fail_times
        self.drop_tokens = drop_tokens

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        import re

        content = messages[-1]["content"]
        match = re.search(r"<source_text>\n(.*?)\n</source_text>", content, re.DOTALL)
        body = match.group(1) if match else content
        # crude but deterministic: latin word runs -> 中文; restore tokens.
        out = re.sub(r"[A-Za-z]{4,}", "中文", body)
        out = re.sub(r"@@中文\[(\d+)\]@@", r"@@PROT[\1]@@", out)
        if self.drop_tokens:
            out = out.replace("@@PROT[0]@@", "LOST")
        return out


def test_heading_preprocess_protects_known_headings():
    protector = Protector()
    text = "## Idea\n\nBody text\n\n## Unknown heading"
    out = _preprocess_headings(text, protector)
    assert "@@PROT[0]@@" in out
    assert protector.restore(out).startswith("## 想法")
    assert "## Unknown heading" in out  # unknown headings stay translatable


def test_split_chunks_respects_budget():
    paragraphs = ["para one with some text " * 20, "para two " * 20, "para three " * 20]
    chunks = _split_chunks("\n\n".join(paragraphs), 30, 60)
    assert all(estimate_tokens(c) <= 60 for c in chunks)
    assert len(chunks) >= 3


def test_validate_chunk_passes_clean_roundtrip():
    protector = Protector()
    source = protector.protect("A [[category]] is $C$ and math $$x$$.")
    translated = "一个 @@PROT[0]@@ 是 @@PROT[1]@@ 和数学 @@PROT[2]@@。"
    assert validate_chunk_translation(source, translated, protector) == []


def test_validate_chunk_rejects_english_output():
    protector = Protector()
    source = protector.protect("A category is a small object.")
    errors = validate_chunk_translation(source, "A category is a small object.", protector)
    assert any("中文比例" in e for e in errors)


def test_translate_document_roundtrips_with_fake():
    client = FakeClient()
    lookup = {}
    content = "## Definition\n\nA [[monad]] on $C$ is a functor $T: C \\to C$.\n\n$$F = G$$\n\nMore prose about [[filtered colimits]]."
    out = translate_document(client, content, "Test page", lookup, Cache())
    assert "中文" in out
    assert "[[monad" in out
    assert "$T: C \\to C$" in out
    assert "$$F = G$$" in out


def test_translate_document_fails_instead_of_fallback():
    client = FakeClient(fail_times=2, drop_tokens=True)
    content = "## Idea\n\nA [[monad]] on $C$ is functorial with plenty of words to translate here."
    with pytest.raises(TranslationError):
        translate_document(client, content, "Test page", {}, Cache())


def test_chunk_cache_hit_skips_llm():
    class CountingClient(FakeClient):
        pass

    client = CountingClient()
    cache = Cache()
    content = "## Idea\n\nA small piece of text that will be translated twice."
    first = translate_document(client, content, "T", {}, cache)
    calls = client.calls
    second = translate_document(client, content, "T", {}, cache)
    assert first == second
    assert client.calls == calls  # cached, no new call
