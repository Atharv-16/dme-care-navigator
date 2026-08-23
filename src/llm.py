from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import APIStatusError, AsyncOpenAI
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

# Load project .env explicitly (stdin scripts break find_dotenv)
_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_ROOT / ".env")

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"

_pace_lock = asyncio.Lock()
_last_call = 0.0


def llm_provider() -> str:
    return os.getenv("LLM_PROVIDER", "ollama").lower()


def default_model() -> str:
    provider = llm_provider()
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    if provider == "gemini":
        return os.getenv("GEMINI_MODEL", "gemini-flash-lite-latest")
    return os.getenv("OLLAMA_MODEL", "llama3.2")


def manager_model() -> str:
    return os.getenv("OPENAI_MANAGER_MODEL") or default_model()


def _gemini_key() -> str:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""


def get_client() -> AsyncOpenAI:
    """
    Default: Ollama OpenAI-compatible API (free, local).
    Gemini free tier: LLM_PROVIDER=gemini + GEMINI_API_KEY (Google AI Studio).
    Optional paid: LLM_PROVIDER=openai + OPENAI_API_KEY.
    """
    provider = llm_provider()
    if provider == "openai":
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY required when LLM_PROVIDER=openai")
        return AsyncOpenAI(api_key=api_key)

    if provider == "gemini":
        api_key = _gemini_key()
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY required when LLM_PROVIDER=gemini")
        return AsyncOpenAI(
            api_key=api_key,
            base_url=os.getenv("GEMINI_BASE_URL", GEMINI_OPENAI_BASE),
            timeout=60.0,
        )

    base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1")
    # Ollama ignores the key but the client requires a non-empty string
    return AsyncOpenAI(base_url=base_url, api_key=os.getenv("OLLAMA_API_KEY", "ollama"))


async def _pace() -> None:
    """Stay under Gemini free-tier RPM (Flash is often ~10–15 req/min)."""
    if llm_provider() != "gemini":
        return
    min_interval = float(os.getenv("GEMINI_MIN_INTERVAL_SEC", "5"))
    global _last_call
    async with _pace_lock:
        wait = min_interval - (time.monotonic() - _last_call)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call = time.monotonic()


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # salvage first {...} block if model added prose
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        return json.loads(match.group(0))


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, APIStatusError) and exc.status_code in {408, 409, 429, 500, 502, 503, 504}:
        return True
    msg = str(exc).lower()
    return "connection" in msg or "temporarily" in msg


def _log_retry(retry_state) -> None:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    kind = type(exc).__name__ if exc else "?"
    print(f"llm retry {retry_state.attempt_number} after {kind}", flush=True)


@retry(
    wait=wait_exponential(multiplier=2, min=5, max=60),
    stop=stop_after_attempt(8),
    retry=retry_if_exception(_is_retryable),
    before_sleep=_log_retry,
    reraise=True,
)
async def chat_text(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.4,
) -> str:
    await _pace()
    client = get_client()
    resp = await client.chat.completions.create(
        model=model or default_model(),
        messages=messages,
        temperature=temperature,
    )
    return (resp.choices[0].message.content or "").strip()


@retry(
    wait=wait_exponential(multiplier=2, min=5, max=60),
    stop=stop_after_attempt(8),
    retry=retry_if_exception(_is_retryable),
    before_sleep=_log_retry,
    reraise=True,
)
async def chat_json(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    temperature: float = 0.2,
) -> Any:
    await _pace()
    client = get_client()
    # Ollama often lacks response_format=json_object; prompt-enforce instead.
    reinforced = list(messages)
    if reinforced and reinforced[0]["role"] == "system":
        reinforced[0] = {
            "role": "system",
            "content": reinforced[0]["content"]
            + "\n\nReply with ONLY a valid JSON object. No markdown fences, no prose.",
        }
    else:
        reinforced.insert(
            0,
            {
                "role": "system",
                "content": "Reply with ONLY a valid JSON object. No markdown fences, no prose.",
            },
        )

    kwargs: dict[str, Any] = {
        "model": model or default_model(),
        "messages": reinforced,
        "temperature": temperature,
    }
    if llm_provider() in {"openai", "gemini"}:
        kwargs["response_format"] = {"type": "json_object"}

    resp = await client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or "{}"
    return _extract_json(content)
