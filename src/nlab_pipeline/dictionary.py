"""Dictionary management for the nLab translation pipeline.

A "dictionary" is a JSON map from lower-cased English term to a list of
{"translation", "pos", "source", "note"} records.  Terms tagged with
pos == "propn" (proper nouns) are automatically promoted to a hard
non-translation whitelist so the LLM never transliterates people's names.

The dictionary file is created lazily; the bundled external glossary provides
seed translations for common mathematical vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import logger, settings

# Canonical part-of-speech tags accepted by the dictionary.
VALID_POS_TAGS: dict[str, str] = {
    "propn": "Proper Noun (专有名词/数学家名人-强制阻断)",
    "noun": "Noun (学术名词)",
    "verb": "Verb (动词变位)",
    "adj": "Adjective (形容词修饰)",
    "adv": "Adverb (副词修饰)",
    "phrase": "Phrase (多词复合短语)",
}

# Terminal shortcuts -> canonical tag.
POS_SHORTCUTS: dict[str, str] = {
    "p": "propn", "propn": "propn",
    "n": "noun", "noun": "noun",
    "v": "verb", "verb": "verb",
    "adj": "adj", "adv": "adv",
    "ph": "phrase", "phrase": "phrase",
}


def atomic_save_json(file_path: str | Path, data: Any) -> None:
    """Atomically write *data* as JSON (write-then-rename with fsync)."""
    file_path = Path(file_path)
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, file_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("failed to load %s: %s", path, exc)
        return default


def load_merged_dictionaries() -> tuple[dict, dict, set]:
    """Return (local_dict, external_dict, non_translatable_set).

    The local dictionary is authoritative; the external glossary supplies seed
    translations.  Every local entry tagged propn is added to the returned
    non-translatable set (the hard block-list for the LLM).
    """
    local_dict: dict = _read_json(settings.dictionary_file, {})
    non_trans: set[str] = set()

    for term_lower, items in local_dict.items():
        if isinstance(items, list) and any(
            isinstance(it, dict) and it.get("pos") == "propn" for it in items
        ):
            non_trans.add(term_lower)

    ext_dict: dict = _read_json(settings.external_glossary_file, {})
    return local_dict, ext_dict, non_trans


def dictionary_fingerprint() -> str:
    """Content hash of the dictionaries; part of every chunk-cache key.

    Any change to the glossary or the manual dictionary therefore
    automatically invalidates cached translations.
    """
    hasher = hashlib.sha256()
    for path in (settings.external_glossary_file, settings.dictionary_file,
                 settings.non_translatable_file):
        if path.exists():
            hasher.update(path.name.encode("utf-8"))
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    hasher.update(chunk)
    return hasher.hexdigest()[:16]


def parse_smart_input(raw: str, default_title: str) -> dict | None:
    """Parse an interactive audit input into a canonical {pos, translation}.

    Supported forms:
        "v:范畴化"        -> pos=verb,   translation=范畴化
        "p:" | "propn:"   -> pos=propn,  translation=原始英文 title
        "层"              -> pos=noun,   translation=层
    """
    clean = raw.strip()
    if not clean:
        return None

    if ":" in clean:
        prefix, _, body = clean.partition(":")
        prefix = prefix.strip().lower()
        body = body.strip()
        if prefix in POS_SHORTCUTS:
            pos = POS_SHORTCUTS[prefix]
            if pos == "propn" and not body:
                body = default_title.strip()
            return {"pos": pos, "translation": body}

    if clean.lower() in ("专有名词", "p", "propn"):
        return {"pos": "propn", "translation": default_title.strip()}

    return {"pos": "noun", "translation": clean}


def interact_assemble_translation(
    title: str, local_dict: dict, ext_dict: dict, non_trans: set
) -> str:
    """Interactively resolve *title* and persist the entry to the dictionary.

    Returns the resolved translation (or the original title for propn), or an
    empty string when the user skips the entry.
    """
    title_lower = title.lower().strip()

    if title_lower in non_trans:
        print(f" ⚠️  [{title}] 已是词典中的 [propn](专有名词)，大模型端已建立硬阻断防护。")
        return title

    print("────────────────────────────────────────────────────────")
    print(f" ✍️  正在审计词条 -> \033[1;36m{title}\033[0m")
    print("    [快捷语法参考]:")
    print("    - 输入纯中文         --> 默认录入为 noun (如: 预层)")
    print("    - 输入 p: 或 propn:  --> 强转为 propn (人名/专有不翻译硬阻断)")
    print("    - 输入 v:中文        --> 强转为 verb (如: v:局部化)")
    print("    - 输入 adj:中文      --> 强转为 adj  (如: adj:层化的)")
    print("    - 直接回车           --> 跳过本词条")

    user_input = input("  请决策 > ").strip()
    parsed = parse_smart_input(user_input, title)
    if not parsed:
        return ""

    final_pos = parsed["pos"]
    final_trans = parsed["translation"]
    term_metadata = {
        "translation": final_trans,
        "pos": final_pos,
        "source": "manual_audit",
        "note": f"交互式指令确立的 [{VALID_POS_TAGS[final_pos]}] 词条",
    }

    existing = local_dict.get(title_lower)
    if isinstance(existing, list):
        # Replace any same-pos entry to support one-term-multiple-pos.
        local_dict[title_lower] = [it for it in existing if it.get("pos") != final_pos]
        local_dict[title_lower].append(term_metadata)
    else:
        local_dict[title_lower] = [term_metadata]

    atomic_save_json(settings.dictionary_file, local_dict)

    color = "\033[1;33m" if final_pos == "propn" else "\033[1;32m"
    print(f"  ✅ 录入成功! 映射已绑定: '{title}' -> {color}[pos={final_pos}] {final_trans}\033[0m")
    return final_trans
