from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import requests

DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
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


class StreamingTextGenerator(TextGenerator, Protocol):
    def stream_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> Iterator[str]:
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
            headers=self.headers(),
            json=self.payload(
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
            ),
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

    def stream_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> Iterator[str]:
        if not self.api_key:
            raise AnthropicError("ANTHROPIC_API_KEY is not configured for text generation")

        response = self.session.post(
            ANTHROPIC_MESSAGES_URL,
            headers=self.headers(),
            json=self.payload(
                system=system,
                prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                stream=True,
            ),
            stream=True,
            timeout=120,
        )
        if response.status_code >= 400:
            raise AnthropicError(anthropic_error_text(response))

        try:
            for raw_line in response.iter_lines(decode_unicode=True):
                line = stream_line(raw_line)
                if not line or not line.startswith("data:"):
                    continue
                data = line.removeprefix("data:").strip()
                if data == "[DONE]":
                    break
                chunk = stream_event_text(data)
                if chunk:
                    yield chunk
        finally:
            response.close()

    def headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key or "",
            "anthropic-version": ANTHROPIC_API_VERSION,
            "content-type": "application/json",
        }

    def payload(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        temperature: float,
        stream: bool = False,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if supports_temperature(self.model):
            payload["temperature"] = temperature
        if stream:
            payload["stream"] = True
        return payload


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


def stream_line(raw_line: object) -> str:
    if isinstance(raw_line, str):
        return raw_line.strip()
    if isinstance(raw_line, bytes):
        return raw_line.decode("utf-8").strip()
    return ""


def stream_event_text(data: str) -> str:
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return ""

    if not isinstance(payload, dict):
        return ""

    if payload.get("type") == "error":
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            raise AnthropicError(friendly_anthropic_error(str(error["message"])))
        raise AnthropicError("Anthropic streaming request failed")

    if payload.get("type") != "content_block_delta":
        return ""

    delta = payload.get("delta")
    if not isinstance(delta, dict) or delta.get("type") != "text_delta":
        return ""

    text = delta.get("text")
    return text if isinstance(text, str) else ""


def supports_temperature(model: str) -> bool:
    unsupported_prefixes = ("claude-opus-4-8", "claude-opus-4-7")
    return not model.startswith(unsupported_prefixes)


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
