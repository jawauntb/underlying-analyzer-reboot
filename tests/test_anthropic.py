from __future__ import annotations

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


class FakeAnthropicSession:
    def __init__(self) -> None:
        self.last_headers: dict[str, str] | None = None
        self.last_json: dict[str, Any] | None = None

    def post(self, _: str, **kwargs: Any) -> FakeAnthropicResponse:
        self.last_headers = kwargs["headers"]
        self.last_json = kwargs["json"]
        return FakeAnthropicResponse()


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
