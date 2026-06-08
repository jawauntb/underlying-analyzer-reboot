from __future__ import annotations

from app.tools import friendly_openai_error


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
