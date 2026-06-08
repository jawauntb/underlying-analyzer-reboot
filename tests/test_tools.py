from __future__ import annotations

from typing import Any, cast

import requests
from pytest import MonkeyPatch

from app.tools import DEFAULT_OPENAI_IMAGE_MODEL, friendly_openai_error, generate_pixel_image


class FakeOpenAIImageResponse(requests.Response):
    def __init__(self) -> None:
        super().__init__()
        self.status_code = 200

    def json(self, **_: Any) -> dict[str, object]:
        return {"created": 123, "data": [{"b64_json": "abc123"}]}


class FakeOpenAISession:
    def __init__(self) -> None:
        self.last_json: dict[str, Any] | None = None

    def post(self, _: str, **kwargs: Any) -> FakeOpenAIImageResponse:
        self.last_json = kwargs["json"]
        return FakeOpenAIImageResponse()


def test_friendly_openai_error_explains_billing_limit() -> None:
    message = friendly_openai_error("Billing hard limit has been reached.")

    assert "billing limit reached" in message.lower()
    assert "project budget" in message.lower()


def test_friendly_openai_error_explains_invalid_key() -> None:
    message = friendly_openai_error("Incorrect API key provided")

    assert "api key was rejected" in message.lower()


def test_friendly_openai_error_preserves_unknown_message() -> None:
    message = friendly_openai_error("Something else failed")

    assert message == "Something else failed"


def test_generate_pixel_image_defaults_to_gpt_image_2(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    session = FakeOpenAISession()

    payload = generate_pixel_image(
        "market mascot",
        api_key="sk-test",
        session=cast(requests.Session, session),
    )

    assert session.last_json is not None
    assert session.last_json["model"] == DEFAULT_OPENAI_IMAGE_MODEL
    assert payload["model"] == DEFAULT_OPENAI_IMAGE_MODEL


def test_generate_pixel_image_accepts_model_override() -> None:
    session = FakeOpenAISession()

    payload = generate_pixel_image(
        "market mascot",
        api_key="sk-test",
        image_model="gpt-image-1-mini",
        session=cast(requests.Session, session),
    )

    assert session.last_json is not None
    assert session.last_json["model"] == "gpt-image-1-mini"
    assert payload["model"] == "gpt-image-1-mini"
