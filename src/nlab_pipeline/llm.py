r"""Thin LLM client supporting OpenAI-compatible and Ollama backends.

The recommended backend is "openai", which talks to llama.cpp's server (or any
OpenAI-compatible endpoint such as vLLM / llama.cpp / LM Studio).  The legacy
"ollama" backend is retained for users who already run Ollama locally.

Both backends share a single retry-enabled HTTP session.  All failure modes
(empty completion, truncation at max_tokens, transport errors) raise
\`LLMError\`; callers must never fall back to the untranslated text.
"""

from __future__ import annotations

import re
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from .config import Settings

_BACKTICK = chr(96)
_REASONING = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_SOURCE_TAG = re.compile(r"</?source_text>", re.IGNORECASE)


class LLMError(RuntimeError):
    """Raised when the LLM backend cannot return a usable completion."""


def _clean_output(text: str) -> str:
    """Strip reasoning blocks, prompt echoes and stray code fences."""
    fence = _BACKTICK * 3
    text = _REASONING.sub("", text)
    text = _SOURCE_TAG.sub("", text)
    text = re.sub(r"^" + fence + r"[a-zA-Z]*\n", "", text)
    text = re.sub(r"\n" + fence + r"$", "", text)
    return text.strip().strip(_BACKTICK).strip()


class LLMClient:
    """Minimal chat-completion client with retry/backoff."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1.0,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=frozenset(["POST", "GET"]),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Return a completion or raise LLMError. Never returns raw input."""
        if self.settings.llm_backend == "ollama":
            return self._chat_ollama(messages, max_tokens, temperature)
        if self.settings.llm_backend in ("openai", "openai-compatible"):
            return self._chat_openai(messages, max_tokens, temperature)
        raise LLMError(f"unknown llm_backend: {self.settings.llm_backend!r}")

    def ping(self) -> bool:
        """Return True when the backend responds to a health check."""
        try:
            if self.settings.llm_backend == "ollama":
                self._session.get(f"{self.settings.llm_base_url}/api/tags", timeout=5)
            else:
                health = self.settings.llm_base_url.replace("/v1", "") + "/health"
                self._session.get(health, timeout=5)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------ #
    # Backends
    # ------------------------------------------------------------------ #
    def _chat_openai(self, messages, max_tokens, temperature) -> str:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": self.settings.temperature if temperature is None else temperature,
            "top_p": self.settings.top_p,
            "max_tokens": max_tokens or self.settings.max_tokens,
            "stream": False,
        }
        headers = {}
        if self.settings.llm_api_key:
            headers["Authorization"] = "Bearer " + self.settings.llm_api_key

        try:
            resp = self._session.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.settings.request_timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                raise LLMError("completion truncated (finish_reason=length)")
            message = choice["message"]
            content = message.get("content") or ""
            if not content:
                # Some reasoning models expose the answer in a separate field.
                content = message.get("reasoning_content") or ""
            if not content:
                raise LLMError("empty completion from " + url)
            return _clean_output(content)
        except LLMError:
            raise
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            raise LLMError("openai backend failure: " + str(exc)) from exc

    def _chat_ollama(self, messages, max_tokens, temperature) -> str:
        url = self.settings.llm_base_url.rstrip("/") + "/api/chat"
        payload: dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.settings.temperature if temperature is None else temperature,
                "top_p": self.settings.top_p,
                "num_ctx": self.settings.num_ctx,
                "num_predict": max_tokens or self.settings.max_tokens,
            },
        }
        try:
            resp = self._session.post(url, json=payload, timeout=self.settings.request_timeout)
            resp.raise_for_status()
            data = resp.json()
            if data.get("done_reason") == "length":
                raise LLMError("completion truncated (done_reason=length)")
            content = data.get("message", {}).get("content", "")
            if not content:
                raise LLMError("empty completion from " + url)
            return _clean_output(content)
        except LLMError:
            raise
        except (requests.RequestException, KeyError, ValueError) as exc:
            raise LLMError("ollama backend failure: " + str(exc)) from exc
