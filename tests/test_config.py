"""Configuration fails fast instead of surfacing obscure runtime errors."""

from dataclasses import replace

import pytest

from nlab_pipeline.cli import build_parser
from nlab_pipeline.config import settings


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("max_workers", 0, "MAX_WORKERS"),
        ("chunk_target_tokens", settings.chunk_max_tokens + 1, "CHUNK_TARGET_TOKENS"),
        ("validate_min_cjk_ratio", 1.1, "VALIDATE_MIN_CJK_RATIO"),
        ("llm_backend", "unknown", "LLM_BACKEND"),
        ("llm_base_url", "localhost:8080", "LLM_BASE_URL"),
    ],
)
def test_invalid_settings_are_rejected(field, value, message):
    with pytest.raises(ValueError, match=message):
        replace(settings, **{field: value})


def test_translate_limit_must_be_positive():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["translate", "--limit", "0"])
