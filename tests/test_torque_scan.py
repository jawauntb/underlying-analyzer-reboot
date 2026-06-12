"""Tests for the Torque Scan multi-ticker pipeline."""

from __future__ import annotations

import json
from typing import Any

from pytest import MonkeyPatch

from app import torque_scan as torque_scan_module
from app.torque_scan import (
    TorqueScanFilter,
    build_torque_scan_response,
    stream_torque_scan_rows,
)
from app.watchlists import WatchlistResult, WatchlistSymbol


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


class FakeMarketClient:
    """Sentinel — the build_cockpit_row patch consumes this without calling methods."""


class FakeWatchlistClient:
    def __init__(self, watchlist: WatchlistResult | None = None) -> None:
        self.watchlist = watchlist
        self.calls: list[str] = []

    def get_watchlist(self, url: str) -> WatchlistResult:
        self.calls.append(url)
        if self.watchlist is None:
            return WatchlistResult(
                id=1,
                name="Test Watchlist",
                source_url=url,
                symbols=[
                    WatchlistSymbol(raw="NASDAQ:AAPL", exchange="NASDAQ", symbol="AAPL", ticker="AAPL"),
                    WatchlistSymbol(raw="NASDAQ:MSFT", exchange="NASDAQ", symbol="MSFT", ticker="MSFT"),
                ],
            )
        return self.watchlist


def _stage_row(
    ticker: str,
    *,
    stage_label: str,
    total_score: float,
    torque_total_score: float | None = None,
) -> dict[str, Any]:
    """Build a cockpit-row-shaped dict for the trim helper to consume."""
    torque_dict: dict[str, Any] | None = None
    if stage_label is not None:
        torque_dict = {
            "total_score": torque_total_score if torque_total_score is not None else total_score,
            "stage_label": stage_label,
            "recommendation": "BUY" if stage_label == "Coiled Spring" else "WAIT",
            "target_zone": "12-24 month re-rate",
            "components": [
                {"name": "Revenue Inflection", "score": 80.0, "weight": 0.25, "detail": "ok"},
            ],
        }
    return {
        "rank": 0,
        "ticker": ticker,
        "name": f"{ticker} Inc",
        "sector": "Technology",
        "industry": "Software",
        "price": 100.0,
        "change_percent": 1.5,
        "scanner_score": 60.0,
        "score": total_score,
        "lane": "Watch",
        "setup": "BUY / LONG / above value",
        "provider": "fake",
        "provider_note": "synthetic",
        "summary": {
            "ticker": ticker,
            "name": f"{ticker} Inc",
            "sector": "Technology",
            "industry": "Software",
            "market_cap": "10B",
        },
        "ridge": {
            "recommendation": "BUY",
            "total_return": 0.4,
        },
        "flow": {
            "state": "LONG",
            "score": 72.0,
        },
        "auction": {
            "location": "above value",
        },
        "torque": torque_dict,
        "reclassification": {
            "old_noun": "Chipmaker",
            "primary_new_verb": "Accelerates",
            "functional_layer": "AI Infrastructure",
            "proof_stage": 3,
            "proof_stage_label": "Proof",
            "reclassification_gap": 25.0,
            "target_low": 120.0,
            "target_mid": 150.0,
            "target_high": 200.0,
        },
    }


def _patch_build_row(
    monkeypatch: MonkeyPatch, row_map: dict[str, dict[str, Any]]
) -> list[str]:
    """Patch build_cockpit_row in the torque_scan namespace.

    Returns a list that records the order tickers were invoked.
    """
    seen: list[str] = []

    def fake_build_cockpit_row(
        client: Any,
        ticker: str,
        *,
        period: str = "1y",
        sec_client: Any | None = None,
        exa_client: Any | None = None,
        include_torque: bool = False,
    ) -> dict[str, Any]:
        seen.append(ticker)
        assert include_torque is True
        if ticker not in row_map:
            raise ValueError(f"no fake row for {ticker}")
        result = row_map[ticker]
        if isinstance(result, BaseException):
            raise result
        if callable(result):
            return result(ticker)
        return result

    monkeypatch.setattr(torque_scan_module, "build_cockpit_row", fake_build_cockpit_row)
    return seen


# ---------------------------------------------------------------------------
# TorqueScanFilter
# ---------------------------------------------------------------------------


def test_filter_defaults() -> None:
    filt = TorqueScanFilter()
    assert filt.stage_labels is None
    assert filt.min_score is None
    assert filt.max_score is None
    assert filt.sort_by == "score_desc"
    assert filt.limit is None


def test_filter_to_dict_roundtrip() -> None:
    filt = TorqueScanFilter(
        stage_labels=["Coiled Spring"], min_score=10.0, max_score=90.0, sort_by="ticker", limit=5
    )
    serialized = filt.to_dict()
    assert serialized == {
        "stage_labels": ["Coiled Spring"],
        "min_score": 10.0,
        "max_score": 90.0,
        "sort_by": "ticker",
        "limit": 5,
    }


# ---------------------------------------------------------------------------
# build_torque_scan_response — happy path
# ---------------------------------------------------------------------------


def test_build_response_returns_all_rows_sorted(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": _stage_row("BBB", stage_label="Inflecting", total_score=50.0),
        "CCC": _stage_row("CCC", stage_label="No Setup", total_score=10.0),
    }
    _patch_build_row(monkeypatch, row_map)

    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {"tickers": ["AAA", "BBB", "CCC"]},
    )

    rows = response["rows"]
    assert [row["ticker"] for row in rows] == ["AAA", "BBB", "CCC"]
    assert [row["rank"] for row in rows] == [1, 2, 3]
    assert response["provider_note"] == "Torque scan"

    meta = response["meta"]
    assert meta["result_count"] == 3
    assert meta["error_count"] == 0
    assert meta["total_evaluated"] == 3
    assert meta["stage_counts"] == {"Coiled Spring": 1, "Inflecting": 1, "No Setup": 1}
    assert meta["watchlist_name"] is None

    export = response["export"]
    assert export["mode"] == "torque-scan"
    assert export["tickers"] == ["AAA", "BBB", "CCC"]
    assert export["rows"] is rows


def test_build_response_filter_stage_labels(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": _stage_row("BBB", stage_label="Inflecting", total_score=50.0),
        "CCC": _stage_row("CCC", stage_label="No Setup", total_score=10.0),
    }
    _patch_build_row(monkeypatch, row_map)

    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {
            "tickers": ["AAA", "BBB", "CCC"],
            "filter": {"stage_labels": ["Coiled Spring"]},
        },
    )

    assert len(response["rows"]) == 1
    assert response["rows"][0]["ticker"] == "AAA"
    # stage_counts is computed across ALL rows, pre-filter
    assert response["meta"]["stage_counts"] == {
        "Coiled Spring": 1,
        "Inflecting": 1,
        "No Setup": 1,
    }
    assert response["meta"]["total_evaluated"] == 3
    assert response["meta"]["result_count"] == 1
    assert response["meta"]["filter"]["stage_labels"] == ["Coiled Spring"]


def test_build_response_min_score_filter(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": _stage_row("BBB", stage_label="Inflecting", total_score=50.0),
        "CCC": _stage_row("CCC", stage_label="No Setup", total_score=10.0),
    }
    _patch_build_row(monkeypatch, row_map)

    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {
            "tickers": ["AAA", "BBB", "CCC"],
            "filter": {"min_score": 40.0},
        },
    )

    tickers = [row["ticker"] for row in response["rows"]]
    assert tickers == ["AAA", "BBB"]
    assert response["meta"]["total_evaluated"] == 3
    assert response["meta"]["result_count"] == 2


def test_build_response_max_score_filter(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": _stage_row("BBB", stage_label="Inflecting", total_score=50.0),
        "CCC": _stage_row("CCC", stage_label="No Setup", total_score=10.0),
    }
    _patch_build_row(monkeypatch, row_map)

    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {
            "tickers": ["AAA", "BBB", "CCC"],
            "filter": {"max_score": 60.0, "sort_by": "score_asc"},
        },
    )

    tickers = [row["ticker"] for row in response["rows"]]
    assert tickers == ["CCC", "BBB"]


def test_build_response_sort_by_ticker(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "MMM": _stage_row("MMM", stage_label="Coiled Spring", total_score=10.0),
        "AAA": _stage_row("AAA", stage_label="Inflecting", total_score=50.0),
        "ZZZ": _stage_row("ZZZ", stage_label="No Setup", total_score=80.0),
    }
    _patch_build_row(monkeypatch, row_map)

    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {"tickers": ["MMM", "AAA", "ZZZ"], "filter": {"sort_by": "ticker"}},
    )
    assert [row["ticker"] for row in response["rows"]] == ["AAA", "MMM", "ZZZ"]


def test_build_response_limit_applies_after_sort(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": _stage_row("BBB", stage_label="Inflecting", total_score=50.0),
        "CCC": _stage_row("CCC", stage_label="No Setup", total_score=10.0),
    }
    _patch_build_row(monkeypatch, row_map)

    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {"tickers": ["AAA", "BBB", "CCC"], "filter": {"limit": 2}},
    )
    assert [row["ticker"] for row in response["rows"]] == ["AAA", "BBB"]
    assert response["meta"]["result_count"] == 2


def test_build_response_handles_per_ticker_error(monkeypatch: MonkeyPatch) -> None:
    row_map: dict[str, Any] = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": ValueError("BBB unavailable"),
    }
    _patch_build_row(monkeypatch, row_map)

    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {"tickers": ["AAA", "BBB"]},
    )

    assert [row["ticker"] for row in response["rows"]] == ["AAA"]
    meta = response["meta"]
    assert meta["error_count"] == 1
    assert meta["errors"][0]["ticker"] == "BBB"
    assert "unavailable" in meta["errors"][0]["error"]


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _consume(stream) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in stream:
        # NDJSON: one JSON object terminated by '\n'
        assert isinstance(raw, str)
        assert raw.endswith("\n")
        events.append(json.loads(raw))
    return events


def test_stream_yields_meta_rows_done(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": _stage_row("BBB", stage_label="Inflecting", total_score=50.0),
        "CCC": _stage_row("CCC", stage_label="No Setup", total_score=10.0),
    }
    _patch_build_row(monkeypatch, row_map)

    events = _consume(
        stream_torque_scan_rows(
            FakeMarketClient(),
            FakeWatchlistClient(),
            {"tickers": ["AAA", "BBB", "CCC"]},
        )
    )

    assert events[0]["type"] == "meta"
    assert events[0]["total"] == 3
    assert sorted(events[0]["tickers"]) == ["AAA", "BBB", "CCC"]
    assert events[-1]["type"] == "done"

    row_events = [event for event in events if event["type"] == "row"]
    assert len(row_events) == 3
    received = {event["ticker"] for event in row_events}
    assert received == {"AAA", "BBB", "CCC"}

    done = events[-1]
    assert done["meta"]["result_count"] == 3
    assert done["meta"]["stage_counts"] == {
        "Coiled Spring": 1,
        "Inflecting": 1,
        "No Setup": 1,
    }
    assert [row["ticker"] for row in done["rows_sorted"]] == ["AAA", "BBB", "CCC"]


def test_stream_emits_error_event_and_continues(monkeypatch: MonkeyPatch) -> None:
    row_map: dict[str, Any] = {
        "AAA": _stage_row("AAA", stage_label="Coiled Spring", total_score=80.0),
        "BBB": ValueError("BBB blew up"),
        "CCC": _stage_row("CCC", stage_label="No Setup", total_score=10.0),
    }
    _patch_build_row(monkeypatch, row_map)

    events = _consume(
        stream_torque_scan_rows(
            FakeMarketClient(),
            FakeWatchlistClient(),
            {"tickers": ["AAA", "BBB", "CCC"]},
        )
    )

    error_events = [event for event in events if event["type"] == "error"]
    assert len(error_events) == 1
    assert error_events[0]["ticker"] == "BBB"
    assert "blew up" in error_events[0]["error"]

    row_events = [event for event in events if event["type"] == "row"]
    assert len(row_events) == 2

    done = events[-1]
    assert done["type"] == "done"
    assert done["meta"]["error_count"] == 1
    assert done["meta"]["result_count"] == 2


def test_stream_empty_ticker_list_yields_meta_and_done(monkeypatch: MonkeyPatch) -> None:
    _patch_build_row(monkeypatch, {})

    events = _consume(
        stream_torque_scan_rows(
            FakeMarketClient(),
            FakeWatchlistClient(),
            {"tickers": []},
        )
    )

    assert len(events) == 2
    assert events[0]["type"] == "meta"
    assert events[0]["total"] == 0
    assert events[0]["tickers"] == []
    assert events[1]["type"] == "done"
    assert events[1]["meta"]["result_count"] == 0
    assert events[1]["rows_sorted"] == []


def test_build_response_empty_returns_empty_rows() -> None:
    response = build_torque_scan_response(
        FakeMarketClient(),
        FakeWatchlistClient(),
        {"tickers": []},
    )
    assert response["rows"] == []
    assert response["meta"]["result_count"] == 0
    assert response["meta"]["stage_counts"] == {}


def test_build_response_resolves_watchlist_url(monkeypatch: MonkeyPatch) -> None:
    row_map = {
        "AAPL": _stage_row("AAPL", stage_label="Coiled Spring", total_score=80.0),
        "MSFT": _stage_row("MSFT", stage_label="Inflecting", total_score=60.0),
    }
    _patch_build_row(monkeypatch, row_map)

    watchlist_client = FakeWatchlistClient()
    response = build_torque_scan_response(
        FakeMarketClient(),
        watchlist_client,
        {"watchlist_url": "https://www.tradingview.com/watchlists/123/"},
    )
    assert watchlist_client.calls == ["https://www.tradingview.com/watchlists/123/"]
    assert response["meta"]["watchlist_name"] == "Test Watchlist"
    assert {row["ticker"] for row in response["rows"]} == {"AAPL", "MSFT"}
