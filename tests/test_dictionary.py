"""Dictionary loading fails closed and honours every configured source."""

import json
from dataclasses import replace

import pytest

import nlab_pipeline.dictionary as dictionary
from nlab_pipeline.config import settings
from nlab_pipeline.dictionary import DictionaryError, load_merged_dictionaries, parse_smart_input


def _settings(tmp_path):
    return replace(
        settings,
        dictionary_file=tmp_path / "dictionary.json",
        external_glossary_file=tmp_path / "external.json",
        non_translatable_file=tmp_path / "non-translatable.json",
    )


def test_loads_explicit_non_translatable_terms(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    configured.dictionary_file.write_text(
        json.dumps({"yoneda": [{"translation": "Yoneda", "pos": "propn"}]}),
        encoding="utf-8",
    )
    configured.external_glossary_file.write_text("{}", encoding="utf-8")
    configured.non_translatable_file.write_text(
        json.dumps({"terms": ["Grothendieck"]}), encoding="utf-8"
    )
    monkeypatch.setattr(dictionary, "settings", configured)
    _, _, terms = load_merged_dictionaries()
    assert terms == {"yoneda", "grothendieck"}


def test_corrupt_dictionary_aborts_instead_of_becoming_empty(tmp_path, monkeypatch):
    configured = _settings(tmp_path)
    configured.dictionary_file.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(dictionary, "settings", configured)
    with pytest.raises(DictionaryError, match="避免降级或覆盖"):
        load_merged_dictionaries()


def test_empty_typed_translation_is_rejected():
    assert parse_smart_input("v:", "localization") is None
    assert parse_smart_input("p:", "Grothendieck") == {
        "pos": "propn",
        "translation": "Grothendieck",
    }
