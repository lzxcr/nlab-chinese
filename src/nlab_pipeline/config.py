"""Centralised configuration for the nLab translation pipeline.

Every value can be overridden via environment variables, a local `.env` file
in the project root, or the flags of the CLI.  The defaults target a local
llama.cpp server (OpenAI-compatible endpoint).

Environment variables (all optional):
    NLAB_DIR               path to the cloned nLab source repository
    QUARTZ_CONTENT_DIR     path where translated Quartz pages are written
    STATE_FILE             path to the SQLite state database
    GLOSSARY_FILE          path of the JSON export of the state database
    DICTIONARY_FILE        manually curated dictionary (propn hard block-list)
    EXTERNAL_GLOSSARY_FILE optional, locally authorised seed term dictionary
    NON_TRANSLATABLE_FILE  extra hard block-list file
    PENDING_REVIEW_FILE    pending-review dump
    LOG_DIR                log directory
    NLAB_BASE_URL          base URL used when linking to upstream nLab pages

    LLM_BACKEND            "openai" (default) or "ollama"
    LLM_BASE_URL           base URL of the LLM server (no trailing slash)
    LLM_API_KEY            optional bearer token
    LLM_MODEL              model identifier sent to the server
    LLM_TEMPERATURE / LLM_TOP_P / LLM_NUM_CTX / LLM_MAX_TOKENS / LLM_TIMEOUT
    MAX_WORKERS            concurrent translation requests

    CHUNK_TARGET_TOKENS    target token budget per LLM request
    VALIDATE_MIN_CJK_RATIO minimum CJK ratio required to publish a chunk
    MAX_FAIL_COUNT         consecutive failures before an entry needs review
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PACKAGE_DIR = Path(__file__).resolve().parent        # .../src/nlab_pipeline
PROJECT_ROOT = PACKAGE_DIR.parents[1]                 # repository root

# Bump whenever the prompt, chunking strategy or validation rules change in a
# way that invalidates previously cached translations.
PROMPT_VERSION = "structured-v2"


def _env(key: str, default: str = "") -> str:
    return (os.environ.get(key) or "").strip() or default


def _env_int(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = _env(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _load_dotenv(path: Path) -> None:
    """Minimal dependency-free .env loader (KEY=VALUE, ignores comments)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip("\"'").strip()


_load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings resolved once at startup."""

    # -- filesystem ------------------------------------------------------- #
    nlab_dir: Path
    content_dir: Path
    state_file: Path
    glossary_file: Path
    dictionary_file: Path
    external_glossary_file: Path
    non_translatable_file: Path
    pending_review_file: Path
    log_dir: Path
    nlab_base_url: str

    # -- LLM backend ------------------------------------------------------ #
    llm_backend: str          # "openai" (llama.cpp/OpenAI-compatible) | "ollama"
    llm_base_url: str         # no trailing slash
    llm_api_key: str
    llm_model: str
    temperature: float
    top_p: float
    num_ctx: int
    max_tokens: int
    request_timeout: int

    # -- concurrency ------------------------------------------------------ #
    max_workers: int

    # -- translation / validation ---------------------------------------- #
    chunk_target_tokens: int
    chunk_max_tokens: int
    validate_min_cjk_ratio: float
    max_fail_count: int

    def __post_init__(self) -> None:
        errors: list[str] = []
        if self.llm_backend not in {"openai", "openai-compatible", "ollama"}:
            errors.append(f"LLM_BACKEND 不支持: {self.llm_backend!r}")
        for name, value in (
            ("LLM_BASE_URL", self.llm_base_url),
            ("NLAB_BASE_URL", self.nlab_base_url),
        ):
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                errors.append(f"{name} 必须是完整的 HTTP(S) URL")
        if self.max_workers < 1:
            errors.append("MAX_WORKERS 必须 >= 1")
        if self.chunk_target_tokens < 1 or self.chunk_max_tokens < 1:
            errors.append("CHUNK_TARGET_TOKENS/CHUNK_MAX_TOKENS 必须为正整数")
        elif self.chunk_target_tokens > self.chunk_max_tokens:
            errors.append("CHUNK_TARGET_TOKENS 不得大于 CHUNK_MAX_TOKENS")
        if self.num_ctx < 1 or self.max_tokens < 1 or self.request_timeout < 1:
            errors.append("LLM_NUM_CTX/LLM_MAX_TOKENS/LLM_TIMEOUT 必须为正整数")
        if not 0 <= self.temperature <= 2:
            errors.append("LLM_TEMPERATURE 必须介于 0 和 2")
        if not 0 < self.top_p <= 1:
            errors.append("LLM_TOP_P 必须介于 0（不含）和 1")
        if not 0 <= self.validate_min_cjk_ratio <= 1:
            errors.append("VALIDATE_MIN_CJK_RATIO 必须介于 0 和 1")
        if self.max_fail_count < 1:
            errors.append("MAX_FAIL_COUNT 必须 >= 1")
        if errors:
            raise ValueError("配置无效: " + "; ".join(errors))

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            nlab_dir=Path(_env("NLAB_DIR", str(PROJECT_ROOT / "nlab-content"))),
            content_dir=Path(_env("QUARTZ_CONTENT_DIR", str(PROJECT_ROOT / "content"))),
            state_file=Path(_env("STATE_FILE", str(PROJECT_ROOT / "state" / "nlab_state.db"))),
            glossary_file=Path(_env("GLOSSARY_FILE", str(PROJECT_ROOT / "scripts" / "nlab_glossary.json"))),
            dictionary_file=Path(
                _env("DICTIONARY_FILE", str(PROJECT_ROOT / "scripts" / "dictionary.json"))
            ),
            external_glossary_file=Path(
                _env(
                    "EXTERNAL_GLOSSARY_FILE",
                    str(PROJECT_ROOT / "scripts" / "external_glossary.json"),
                )
            ),
            non_translatable_file=Path(
                _env("NON_TRANSLATABLE_FILE", str(PROJECT_ROOT / "scripts" / "non_translatable.json"))
            ),
            pending_review_file=Path(
                _env("PENDING_REVIEW_FILE", str(PROJECT_ROOT / "scripts" / "pending_review.json"))
            ),
            log_dir=Path(_env("LOG_DIR", str(PROJECT_ROOT / "logs"))),
            nlab_base_url=_env("NLAB_BASE_URL", "https://ncatlab.org/nlab/show/").rstrip("/")
            + "/",
            llm_backend=_env("LLM_BACKEND", "openai").lower(),
            llm_base_url=_env("LLM_BASE_URL", "http://127.0.0.1:8080/v1").rstrip("/"),
            llm_api_key=_env("LLM_API_KEY", ""),
            # For the "openai"/llama.cpp backend this is only a label (the server
            # loads the GGUF file given to manage.sh serve).  For "ollama" it is the
            # actual model name. The default remains only a configurable label.
            llm_model=_env("LLM_MODEL", "gemma-4-12b-it"),
            temperature=_env_float("LLM_TEMPERATURE", 0.0),
            top_p=_env_float("LLM_TOP_P", 0.1),
            num_ctx=_env_int("LLM_NUM_CTX", 8192),
            max_tokens=_env_int("LLM_MAX_TOKENS", 1024),
            request_timeout=_env_int("LLM_TIMEOUT", 120),
            max_workers=_env_int("MAX_WORKERS", 4),
            chunk_target_tokens=_env_int("CHUNK_TARGET_TOKENS", 700),
            chunk_max_tokens=_env_int("CHUNK_MAX_TOKENS", 900),
            validate_min_cjk_ratio=_env_float("VALIDATE_MIN_CJK_RATIO", 0.05),
            max_fail_count=_env_int("MAX_FAIL_COUNT", 3),
        )


settings = Settings.from_env()

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
LOG_FORMAT = "%(asctime)s - [%(levelname)s] - %(message)s"
logger = logging.getLogger("nlab-pipeline")
logger.setLevel(logging.DEBUG)


def _configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    if logger.handlers:
        return

    file_handler = TimedRotatingFileHandler(
        log_dir / "pipeline.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    console_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


_configure_logging(settings.log_dir)
