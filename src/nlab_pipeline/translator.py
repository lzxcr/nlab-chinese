"""Document chunking and LLM-based translation with structural protection.

The raw markdown is first tokenised by the protector (formulas, code, URLs,
wiki-links and HTML become opaque placeholders), then split into token-budget
chunks, and only the remaining plain prose is sent to the model.  After every
chunk the placeholders must survive verbatim, the output must contain a sane
share of CJK characters and must not look truncated - otherwise the chunk is
retried once and then the page fails (it is never silently published with the
untranslated English fallback).
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from .aho import AhoCorasick
from .cleaner import normalize_headings
from .config import PROMPT_VERSION, logger, settings
from .dictionary import load_merged_dictionaries
from .llm import LLMClient
from .protector import Protector

_SYSTEM_PROMPT = (
    "You are a strict academic translation engine. Translate the English text "
    "inside <source_text> into rigorous mathematical Chinese.\n\n"
    "[CRITICAL - PROTECTION TOKENS]:\n"
    "1. Tokens of the form @@PROT[n]@@ are machine-protected content "
    "(formulas, code, links, wiki-links, HTML). Preserve EVERY token EXACTLY "
    "as it appears: never translate, rewrite, reorder, duplicate or drop them.\n"
    "2. NEVER invent new @@PROT[n]@@ tokens.\n\n"
    "[CRITICAL MANDATE - PROPER NOUNS & NAMES]:\n"
    "3. DO NOT translate proper names of persons/mathematicians (e.g., Grothendieck, "
    "Kan, Serre, Yoneda, Quillen). Keep personal names EXACTLY in their original "
    "English/Latin script. Never transliterate names into Chinese characters.\n\n"
    "[GENERAL RULES]:\n"
    "4. Translate ONLY the text inside <source_text>...</source_text>. Do not invent "
    "intro/extro explanations.\n"
    "5. Preserve all remaining Markdown tokens (**, *, emphasis) exactly.\n"
    "6. If an advanced specific concept cannot be translated precisely, keep the "
    "English term intact.\n"
    "7. Output the translation only - nothing else."
)

# Standard nLab section headings mapped to their conventional Chinese
# renderings (applied deterministically; never sent to the model).
_HEADING_MAP: dict[str, str] = {
    "Idea": "想法",
    "Introduction": "引言",
    "Definition": "定义",
    "Definitions": "定义",
    "Example": "例子",
    "Examples": "例子",
    "Proposition": "命题",
    "Theorem": "定理",
    "Lemma": "引理",
    "Corollary": "推论",
    "Proof": "证明",
    "Properties": "性质",
    "Remarks": "注记",
    "Remark": "注记",
    "Notes": "注记",
    "References": "参考文献",
    "Related concepts": "相关概念",
    "Related entries": "相关词条",
    "History": "历史",
    "Applications": "应用",
    "Constructions": "构造",
    "Formalization": "形式化",
    "General": "一般",
    "Statements": "陈述",
    "Contents": "目录",
    "Idea of": "想法",
}

_HEADING = re.compile(r"^(#{1,6})[ \t]*([^#\n][^\n]*?)[ \t]*$")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
_TRUNCATION_ENDINGS = (",", ";", ":", "-", "–", "—", "…", "(", "（", "[", "and", "or", "of", "the")


class TranslationError(RuntimeError):
    """Raised when a chunk cannot be translated with structural integrity."""


@dataclass
class Cache:
    """Dict-like chunk cache (backed by the state store in production)."""

    backend: object | None = None
    memory: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.memory is None:
            self.memory = {}

    def get(self, key: str) -> str | None:
        if key in self.memory:
            return self.memory[key]
        if self.backend is not None:
            value = self.backend.cache_get(key)
            if value is not None:
                self.memory[key] = value
                return value
        return None

    def put(self, key: str, value: str) -> None:
        self.memory[key] = value
        if self.backend is not None:
            self.backend.cache_put(key, value, settings.llm_model)


def estimate_tokens(text: str) -> int:
    """Conservative token estimate (~2.5 chars/token for mixed text)."""
    return max(1, len(text) // 2)


def _cjk_ratio(text: str) -> float:
    cjk = len(_CJK.findall(text))
    total = len(re.findall(r"\w", text)) + cjk
    return cjk / max(1, total)


def _preprocess_headings(content: str, protector: Protector) -> str:
    """Deterministically translate known headings; protect them from the LLM."""
    lines = content.split("\n")
    out: list[str] = []
    for line in lines:
        match = _HEADING.match(line)
        if match and match.group(2).strip() in _HEADING_MAP:
            known = f"{match.group(1)} {_HEADING_MAP[match.group(2).strip()]}"
            out.append(protector.protect_exact(known))
        else:
            out.append(line)
    return "\n".join(out)


def _build_glossary_hints(
    chunk: str, automaton: AhoCorasick, non_trans_automaton: AhoCorasick
) -> str:
    """Collect glossary hints by scanning the chunk once with Aho-Corasick."""
    hints: set[str] = set()
    seen: set[tuple[int, int, str]] = set()
    for start, end, payload in automaton.scan(chunk):
        cn = payload
        key = (start, end, cn)
        if key not in seen:
            seen.add(key)
            hints.add(f"- {chunk[start:end]} -> {cn}")
    if not hints:
        return ""
    return "\n[学术术语对齐基准字典]:\n" + "\n".join(sorted(hints)) + "\n"


def _build_non_trans_prompt(chunk: str, non_trans_automaton: AhoCorasick) -> str:
    matched: set[str] = set()
    for start, end, _payload in non_trans_automaton.scan(chunk):
        matched.add(chunk[start:end])
    if not matched:
        return ""
    joined = ", ".join(f"'{t}'" for t in sorted(matched))
    return "\n[DO NOT TRANSLATE THE FOLLOWING TERMS - KEEP RAW English]:\n" + joined + "\n"


def _split_chunks(tokenized: str, target_tokens: int, max_tokens: int) -> list[str]:
    """Split tokenised text into token-budget chunks at paragraph/sentence edges."""
    paragraphs = [p.strip() for p in tokenized.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def _sentence_split(paragraph: str, budget: int) -> list[str]:
        if estimate_tokens(paragraph) <= budget:
            return [paragraph]
        pieces = re.split(r"(?<=[.!?])\s+", paragraph)
        out: list[str] = []
        buf: list[str] = []
        size = 0
        for piece in pieces:
            piece_tokens = estimate_tokens(piece)
            if buf and size + piece_tokens > budget:
                out.append(" ".join(buf))
                buf, size = [piece], piece_tokens
            else:
                buf.append(piece)
                size += piece_tokens
        if buf:
            out.append(" ".join(buf))
        # Hard fallback: paragraphs without sentence punctuation still must
        # respect the budget, so split the oversized remainders at words.
        final: list[str] = []
        for piece in out:
            while estimate_tokens(piece) > budget:
                words = piece.split(" ")
                cut = max(1, int(len(words) * budget / estimate_tokens(piece)))
                final.append(" ".join(words[:cut]))
                piece = " ".join(words[cut:])
            final.append(piece)
        return [p for p in final if p.strip()]

    for paragraph in paragraphs:
        paragraph_tokens = estimate_tokens(paragraph)
        if current and current_tokens + paragraph_tokens > max_tokens:
            chunks.append("\n\n".join(current))
            current, current_tokens = [], 0
        if paragraph_tokens <= max_tokens:
            current.append(paragraph)
            current_tokens += paragraph_tokens
        else:
            if current:
                chunks.append("\n\n".join(current))
                current, current_tokens = [], 0
            for piece in _sentence_split(paragraph, max_tokens):
                chunks.append(piece)
    if current:
        chunks.append("\n\n".join(current))
    return [c for c in chunks if c.strip()]


def _chunk_cache_key(tokenized: str, glossary_hash: str) -> str:
    raw = "|".join([PROMPT_VERSION, settings.llm_model, glossary_hash, tokenized])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def validate_chunk_translation(source_tokenized: str, translated: str, protector: Protector) -> list[str]:
    """Structural + quality checks on one translated chunk."""
    errors: list[str] = []
    errors.extend(protector.validate_roundtrip(source_tokenized, translated))
    stripped = translated.strip()
    if not stripped:
        return ["空翻译输出"]
    source_plain = _TOKEN_SUB.sub("", source_tokenized)
    translated_plain = _TOKEN_SUB.sub("", translated)
    if _LATIN_WORD.search(source_plain) and _cjk_ratio(translated_plain) < settings.validate_min_cjk_ratio:
        errors.append(f"中文比例过低 ({_cjk_ratio(translated_plain):.2f})")
    if len(source_plain) > 120 and len(translated_plain) < len(source_plain) * 0.15:
        errors.append("译文过短，疑似截断")
    low = stripped.lower()
    for ending in _TRUNCATION_ENDINGS:
        if low.endswith(ending) and len(stripped) > 40:
            errors.append(f"疑似截断（以 '{ending}' 结尾）")
            break
    return errors


_TOKEN_SUB = re.compile(r"@@PROT\[\d+\]@@")


def _translate_chunk(
    client: LLMClient,
    chunk: str,
    title: str,
    automaton: AhoCorasick,
    non_trans_automaton: AhoCorasick,
    protector: Protector,
    glossary_hash: str,
    cache: Cache,
) -> str:
    chunk = chunk.strip()
    if not chunk:
        return ""
    # Pure protected content (formulas/code only) needs no translation.
    if not _TOKEN_SUB.sub("", chunk).strip():
        return chunk

    key = _chunk_cache_key(chunk, glossary_hash)
    cached = cache.get(key)
    if cached is not None:
        return cached

    user = (
        _build_glossary_hints(chunk, automaton, non_trans_automaton)
        + _build_non_trans_prompt(chunk, non_trans_automaton)
        + "\n<source_text>\n"
        + chunk
        + "\n</source_text>"
    )
    last_error: str | None = None
    for _attempt in range(2):
        try:
            result = client.chat(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user},
                ]
            )
            errors = validate_chunk_translation(chunk, result, protector)
            if errors:
                last_error = "; ".join(errors)
                logger.warning("[%s] 分块校验失败(第%d次): %s", title, _attempt + 1, last_error)
                continue
            cache.put(key, result)
            return result
        except Exception as exc:  # noqa: BLE001 - every chunk failure is fatal here
            last_error = str(exc)
            logger.warning("[%s] 分块翻译失败(第%d次): %s", title, _attempt + 1, exc)
    raise TranslationError(f"分块翻译失败: {last_error}")


def build_glossary_automaton(lookup: Mapping[str, Sequence[dict]]) -> AhoCorasick:
    """Aho-Corasick over alias -> Chinese translation pairs."""
    automaton = AhoCorasick()
    for alias_lower, candidates in lookup.items():
        cn = next((c.get("chinese_translation") for c in candidates if c.get("chinese_translation")), "")
        if cn and len(alias_lower) > 3:
            automaton.add(alias_lower, cn)
    automaton.build()
    return automaton


def build_non_trans_automaton(non_trans: set[str]) -> AhoCorasick:
    automaton = AhoCorasick()
    for term in non_trans:
        if len(term) > 2:
            automaton.add(term, None)
    automaton.build()
    return automaton


def translate_document(
    client: LLMClient,
    content: str,
    title: str,
    lookup: Mapping[str, Sequence[dict]],
    cache: Cache | None = None,
    glossary_hash: str | None = None,
) -> str:
    """Translate *content* chunk-by-chunk and reassemble the result.

    Raises TranslationError when any chunk fails; the caller must not fall
    back to the untranslated text.
    """
    if cache is None:
        cache = Cache()
    if glossary_hash is None:
        from .dictionary import dictionary_fingerprint
        glossary_hash = dictionary_fingerprint()

    _local, _ext, non_trans = load_merged_dictionaries()
    content = normalize_headings(content)
    protector = Protector()
    content = _preprocess_headings(content, protector)
    tokenized = protector.protect(content)
    chunks = _split_chunks(tokenized, settings.chunk_target_tokens, settings.chunk_max_tokens)

    automaton = build_glossary_automaton(lookup)
    non_trans_automaton = build_non_trans_automaton(non_trans)

    results: list[str | None] = [None] * len(chunks)
    pending = [(i, chunk) for i, chunk in enumerate(chunks) if _TOKEN_SUB.sub("", chunk).strip()]

    thread_local = threading.local()

    def _worker(index: int, chunk: str) -> None:
        if not hasattr(thread_local, "client"):
            if isinstance(client, LLMClient):
                # requests.Session is not thread-safe: one client per thread.
                thread_local.client = LLMClient(settings)
            else:
                thread_local.client = client
        results[index] = _translate_chunk(
            thread_local.client, chunk, title,
            automaton, non_trans_automaton, protector, glossary_hash, cache,
        )

    # Pre-fill pure-placeholder chunks (no LLM call needed).
    for i, chunk in enumerate(chunks):
        if not _TOKEN_SUB.sub("", chunk).strip():
            results[i] = chunk

    if len(pending) == 1:
        _worker(*pending[0])
    elif pending:
        with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
            futures = {executor.submit(_worker, i, c): i for i, c in pending}
            for future in as_completed(futures):
                future.result()  # re-raises worker exceptions

    joined = "\n\n".join(r for r in results if r is not None)
    return protector.restore(joined)
