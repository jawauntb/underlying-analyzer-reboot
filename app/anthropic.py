from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import requests

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
ANTHROPIC_API_VERSION = "2023-06-01"
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"


class AnthropicError(Exception):
    """Raised when Anthropic text generation cannot complete."""


@dataclass(frozen=True)
class GeneratedText:
    text: str
    model: str
    provider: str = "anthropic"


class TextGenerator(Protocol):
    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> GeneratedText:
        ...


class AnthropicTextClient:
    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY")
        self.model = (
            model
            if model is not None
            else os.getenv("ANTHROPIC_TEXT_MODEL") or DEFAULT_ANTHROPIC_MODEL
        )
        self.session = session or requests.Session()

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> GeneratedText:
        if not self.api_key:
            raise AnthropicError("ANTHROPIC_API_KEY is not configured for text generation")

        response = self.session.post(
            ANTHROPIC_MESSAGES_URL,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=120,
        )
        if response.status_code >= 400:
            raise AnthropicError(anthropic_error_text(response))

        payload = response.json()
        text = message_text(payload)
        if not text:
            raise AnthropicError("Anthropic response did not include text content")

        return GeneratedText(
            text=text,
            model=str(payload.get("model") or self.model),
        )


def message_text(payload: Mapping[str, Any]) -> str:
    content = payload.get("content")
    if not isinstance(content, list):
        return ""

    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    return "\n\n".join(parts)


def anthropic_error_text(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or "Anthropic text request failed"

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return friendly_anthropic_error(str(error["message"]))
    return "Anthropic text request failed"


def friendly_anthropic_error(message: str) -> str:
    lowered = message.lower()
    if "invalid api key" in lowered or "authentication" in lowered:
        return (
            "Anthropic API key was rejected. Check ANTHROPIC_API_KEY in .env "
            "and the Modal secret."
        )
    if "credit balance" in lowered or "billing" in lowered:
        return "Anthropic billing or credit limit blocked text generation."
    return message
