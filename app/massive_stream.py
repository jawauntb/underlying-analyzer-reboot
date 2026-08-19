"""Massive WebSocket adapter exposed through the app's additive SSE surface.

The application is deployed as a WSGI Flask service, so browser and mobile
consumers receive a stable Server-Sent Events stream while this adapter owns
the upstream WebSocket connection.  The existing REST/provider contracts do
not depend on this module.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterator
from contextlib import suppress
from typing import Any, Protocol

from app.market_data import MarketDataCapabilityError, MarketDataError


class MassiveStreamError(MarketDataError):
    """A stream connection or upstream stream protocol failure."""


class MassiveStreamEntitlementError(MassiveStreamError):
    """Massive rejected the stream because the key or plan is not entitled."""


class WebSocketConnection(Protocol):
    def send(self, payload: str) -> Any: ...

    def recv(self) -> str | bytes: ...

    def close(self) -> Any: ...


TickerPattern = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
FEED_CODES = {
    ("stocks", "trades"): "T",
    ("stocks", "quotes"): "Q",
    ("stocks", "aggregates_minute"): "AM",
    ("stocks", "aggregates_second"): "A",
    ("options", "trades"): "T",
    ("options", "quotes"): "Q",
    ("options", "aggregates_minute"): "AM",
    ("options", "aggregates_second"): "A",
}


class MassiveStreamProvider:
    """Connect to a Massive stream without exposing credentials to callers."""

    name = "massive"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "wss://socket.massive.com",
        enabled: bool = True,
        timeout: float = 20.0,
        max_reconnects: int = 1,
        sleep: Any = time.sleep,
        connect: Any | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.enabled = enabled
        self.timeout = max(1.0, timeout)
        self.max_reconnects = max(0, min(3, max_reconnects))
        self.sleep = sleep
        self._connect = connect

    @classmethod
    def from_env(cls) -> MassiveStreamProvider:
        key = os.getenv("MASSIVE_API_KEY", "")
        try:
            timeout = float(os.getenv("MASSIVE_STREAM_TIMEOUT_SECONDS", "20"))
        except ValueError:
            timeout = 20.0
        try:
            reconnects = int(os.getenv("MASSIVE_STREAM_MAX_RECONNECTS", "1"))
        except ValueError:
            reconnects = 1
        return cls(
            key,
            base_url=os.getenv("MASSIVE_WS_BASE_URL", "wss://socket.massive.com"),
            # Presence of the server-side key enables the additive route by
            # default; operators can explicitly disable it during rollout.
            enabled=_env_bool("MASSIVE_STREAM_ENABLED", bool(key)),
            timeout=timeout,
            max_reconnects=reconnects,
        )

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    @property
    def freshness(self) -> str:
        return "realtime" if "delayed." not in self.base_url else "delayed_15m"

    def stream_events(
        self,
        ticker: str,
        *,
        asset_class: str = "stocks",
        feed: str = "trades",
        max_events: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        asset = asset_class.strip().lower()
        feed_name = feed.strip().lower()
        symbol = clean_stream_ticker(ticker, asset_class=asset)
        code = FEED_CODES.get((asset, feed_name))
        if code is None:
            raise ValueError(
                "feed must be trades, quotes, aggregates_minute, or aggregates_second; "
                "asset_class must be stocks or options"
            )
        if not self.enabled:
            raise MarketDataCapabilityError("Massive streaming is disabled by configuration")
        if not self.configured:
            raise MarketDataCapabilityError("MASSIVE_API_KEY is not configured")
        if max_events is not None and max_events < 1:
            raise ValueError("max_events must be at least 1")

        emitted = 0
        attempts = 0
        while attempts <= self.max_reconnects:
            connection: WebSocketConnection | None = None
            try:
                connection = self._open_connection(asset)
                self._authenticate(connection)
                connection.send(
                    json.dumps(
                        {"action": "subscribe", "params": f"{code}.{symbol}"},
                        separators=(",", ":"),
                    )
                )
                while max_events is None or emitted < max_events:
                    for message in _decode_messages(connection.recv()):
                        if message.get("ev") == "status":
                            _raise_for_status(message)
                            continue
                        if message.get("ev") not in {code, "T", "Q", "A", "AM"}:
                            continue
                        emitted += 1
                        yield message
                        if max_events is not None and emitted >= max_events:
                            return
            except MassiveStreamEntitlementError:
                raise
            except (MassiveStreamError, OSError, RuntimeError, ValueError) as exc:
                if attempts >= self.max_reconnects:
                    raise MassiveStreamError("Massive stream connection failed") from exc
                attempts += 1
                self.sleep(min(4.0, 0.5 * (2**(attempts - 1))))
            finally:
                if connection is not None:
                    with suppress(Exception):
                        connection.close()

    def _open_connection(self, asset_class: str) -> WebSocketConnection:
        url = f"{self.base_url}/{asset_class}"
        if self._connect is not None:
            return self._connect(url, timeout=self.timeout)
        try:
            import websocket

            return websocket.create_connection(url, timeout=self.timeout)
        except ImportError as exc:
            raise MassiveStreamError("Massive WebSocket support is not installed") from exc
        except Exception as exc:
            raise MassiveStreamError("Massive stream connection failed") from exc

    def _authenticate(self, connection: WebSocketConnection) -> None:
        connection.send(json.dumps({"action": "auth", "params": self.api_key}))
        for message in _decode_messages(connection.recv()):
            if message.get("ev") != "status":
                continue
            _raise_for_status(message)
            if str(message.get("status", "")).lower() in {
                "auth_success",
                "authenticated",
            }:
                return
        raise MassiveStreamError("Massive stream authentication did not complete")


def _decode_messages(raw: str | bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError) as exc:
        raise MassiveStreamError("Massive returned invalid stream JSON") from exc
    messages = payload if isinstance(payload, list) else [payload]
    return [message for message in messages if isinstance(message, dict)]


def _raise_for_status(message: dict[str, Any]) -> None:
    status = str(message.get("status", "")).lower()
    if status in {"auth_failed", "unauthorized", "forbidden", "max_connections"}:
        raise MassiveStreamEntitlementError(
            "Massive authentication or subscription rejected the stream"
        )
    if status in {"error", "closed"}:
        raise MassiveStreamError("Massive stream returned an upstream error")


def clean_stream_ticker(ticker: str, *, asset_class: str = "stocks") -> str:
    symbol = ticker.strip().upper()
    if symbol == "*":
        return symbol
    if asset_class == "options" and symbol.startswith("O:"):
        option_symbol = symbol[2:]
        if option_symbol and re.fullmatch(r"[A-Z0-9.-]{6,31}", option_symbol):
            return symbol
    if not symbol or not TickerPattern.fullmatch(symbol):
        raise ValueError("Ticker contains unsupported characters")
    return symbol


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "MassiveStreamEntitlementError",
    "MassiveStreamError",
    "MassiveStreamProvider",
    "clean_stream_ticker",
]
