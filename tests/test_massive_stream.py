from __future__ import annotations

import json
from typing import Any

import pytest

from app.main import create_app
from app.massive_stream import MassiveStreamEntitlementError, MassiveStreamProvider


class FakeSocket:
    def __init__(self, messages: list[str], *, fail_after: int | None = None) -> None:
        self.messages = iter(messages)
        self.sent: list[dict[str, Any]] = []
        self.fail_after = fail_after
        self.received = 0
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self) -> str:
        if self.fail_after is not None and self.received >= self.fail_after:
            raise OSError("socket closed")
        self.received += 1
        return next(self.messages)

    def close(self) -> None:
        self.closed = True


def _provider(socket: FakeSocket, **kwargs: Any) -> MassiveStreamProvider:
    return MassiveStreamProvider(
        "fixture-key",
        connect=lambda _url, **_kwargs: socket,
        sleep=lambda _seconds: None,
        **kwargs,
    )


def test_stream_authenticates_and_subscribes_to_trades() -> None:
    socket = FakeSocket(
        [
            '[{"ev":"status","status":"auth_success","message":"authenticated"}]',
            '{"ev":"T","sym":"AAPL","p":200.1,"s":10,"t":1724000000000}',
        ]
    )
    events = list(_provider(socket).stream_events("AAPL", max_events=1))

    assert events == [{"ev": "T", "sym": "AAPL", "p": 200.1, "s": 10, "t": 1724000000000}]
    assert socket.sent == [
        {"action": "auth", "params": "fixture-key"},
        {"action": "subscribe", "params": "T.AAPL"},
    ]
    assert socket.closed is True


def test_stream_supports_option_contract_quotes() -> None:
    socket = FakeSocket(
        [
            '{"ev":"status","status":"auth_success"}',
            '{"ev":"Q","sym":"O:SPY241220P00720000","bp":9.7,"ap":9.8,"t":1724000000000}',
        ]
    )
    events = list(
        _provider(socket).stream_events(
            "O:SPY241220P00720000", asset_class="options", feed="quotes", max_events=1
        )
    )

    assert events[0]["ev"] == "Q"
    assert socket.sent[-1]["params"] == "Q.O:SPY241220P00720000"


def test_stream_reconnects_once_after_disconnect() -> None:
    first = FakeSocket(
        ['{"ev":"status","status":"auth_success"}'],
        fail_after=1,
    )
    second = FakeSocket(
        [
            '{"ev":"status","status":"auth_success"}',
            '{"ev":"AM","sym":"AAPL","o":1,"c":2,"t":1724000000000}',
        ]
    )
    sockets = iter([first, second])
    provider = MassiveStreamProvider(
        "fixture-key",
        max_reconnects=1,
        connect=lambda _url, **_kwargs: next(sockets),
        sleep=lambda _seconds: None,
    )

    events = list(provider.stream_events("AAPL", feed="aggregates_minute", max_events=1))
    assert events[0]["ev"] == "AM"
    assert first.closed is True
    assert second.closed is True


def test_stream_auth_failure_is_typed_and_sanitized() -> None:
    socket = FakeSocket(['{"ev":"status","status":"auth_failed","message":"bad key"}'])

    with pytest.raises(MassiveStreamEntitlementError, match="authentication or subscription"):
        list(_provider(socket).stream_events("AAPL", max_events=1))


def test_stream_endpoint_emits_ready_and_data_without_changing_rest_contract() -> None:
    socket = FakeSocket(
        [
            '{"ev":"status","status":"auth_success"}',
            '{"ev":"T","sym":"AAPL","p":200.1,"s":10,"t":1724000000000}',
        ]
    )
    app = create_app()
    app.config["MASSIVE_STREAM_PROVIDER"] = _provider(socket)
    response = app.test_client().get(
        "/api/data/market/stream",
        query_string={"ticker": "AAPL", "feed": "trades", "max_events": "1"},
    )

    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert "event: ready" in body
    assert '"ticker":"AAPL"' in body
    assert "event: market_data" in body


def test_stream_endpoint_validates_options_and_entitlement_errors() -> None:
    app = create_app()
    app.config["MASSIVE_STREAM_PROVIDER"] = MassiveStreamProvider("fixture-key", enabled=True)
    client = app.test_client()

    invalid = client.get(
        "/api/data/market/stream", query_string={"ticker": "AAPL", "feed": "unknown"}
    )
    assert invalid.status_code == 400

    socket = FakeSocket(['{"ev":"status","status":"auth_failed"}'])
    app.config["MASSIVE_STREAM_PROVIDER"] = _provider(socket)
    response = client.get(
        "/api/data/market/stream", query_string={"ticker": "AAPL", "max_events": "1"}
    )
    assert response.status_code == 200
    assert '"code":"not_entitled"' in response.get_data(as_text=True)
