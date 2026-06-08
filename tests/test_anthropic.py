from __future__ import annotations

from collections.abc import Iterator
from typing import Any, cast

import pytest
import requests

from app.anthropic import (
    ANTHROPIC_API_VERSION,
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicError,
    AnthropicTextClient,
)


class FakeAnthropicResponse(requests.Response):
    def __init__(self) -> None:
        super().__init__()
        self.status_code = 200

    def json(self, **_: Any) -> dict[str, object]:
        return {
            "model": "claude-test",
            "content": [{"type": "text", "text": "Anthropic generated brief."}],
        }


class FakeAnthropicStreamResponse(requests.Response):
    def __init__(self) -> None:
        super().__init__()
        self.status_code = 200

    def iter_lines(
        self,
        chunk_size: int | None = 512,
        decode_unicode: bool = False,
        delimiter: str | bytes | None = None,
    ) -> Iterator[Any]:
        del chunk_size, decode_unicode, delimiter
        yield "event: message_start"
        yield 'data: {"type":"message_start","message":{"model":"claude-test"}}'
        yield 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"One "}}'
        yield 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"two."}}'
        yield 'data: {"type":"message_stop"}'

    def close(self) -> None:
        self._content_consumed = True


class FakeAnthropicSession:
    def __init__(self, response: requests.Response | None = None) -> None:
        self.last_headers: dict[str, str] | None = None
        self.last_json: dict[str, Any] | None = None
        self.response = response or FakeAnthropicResponse()

    def post(self, _: str, **kwargs: Any) -> requests.Response:
        self.last_headers = kwargs["headers"]
        self.last_json = kwargs["json"]
        return self.response


def test_anthropic_text_client_reports_missing_key() -> None:
    client = AnthropicTextClient(api_key="", session=cast(requests.Session, FakeAnthropicSession()))

    with pytest.raises(AnthropicError, match="ANTHROPIC_API_KEY"):
        client.generate_text(system="system", prompt="prompt")


def test_anthropic_text_client_sends_messages_request() -> None:
    session = FakeAnthropicSession()
    client = AnthropicTextClient(api_key="sk-ant-test", session=cast(requests.Session, session))

    generated = client.generate_text(system="system prompt", prompt="user prompt")

    assert generated.text == "Anthropic generated brief."
    assert generated.model == "claude-test"
    assert session.last_headers is not None
    assert session.last_headers["x-api-key"] == "sk-ant-test"
    assert session.last_headers["anthropic-version"] == ANTHROPIC_API_VERSION
    assert session.last_json is not None
    assert session.last_json["model"] == DEFAULT_ANTHROPIC_MODEL
    assert session.last_json["messages"] == [{"role": "user", "content": "user prompt"}]
    assert "temperature" not in session.last_json


def test_anthropic_text_client_streams_text_deltas() -> None:
    session = FakeAnthropicSession(FakeAnthropicStreamResponse())
    client = AnthropicTextClient(api_key="sk-ant-test", session=cast(requests.Session, session))

    chunks = list(client.stream_text(system="system prompt", prompt="user prompt"))

    assert chunks == ["One ", "two."]
    assert session.last_json is not None
    assert session.last_json["stream"] is True


def test_anthropic_text_client_keeps_temperature_for_supported_models() -> None:
    session = FakeAnthropicSession()
    client = AnthropicTextClient(
        api_key="sk-ant-test",
        model="claude-sonnet-4-6",
        session=cast(requests.Session, session),
    )

    client.generate_text(system="system prompt", prompt="user prompt")

    assert session.last_json is not None
    assert session.last_json["temperature"] == 0.2
