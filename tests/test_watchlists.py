from __future__ import annotations

import pytest

from app.watchlists import (
    WatchlistError,
    normalize_tradingview_symbol,
    normalize_watchlist_url,
    parse_tradingview_watchlist,
)


def test_parse_tradingview_watchlist_reads_embedded_init_data() -> None:
    html = """
    <script type="application/prs.init-data+json">{
      "sharedWatchlist": {
        "list": {
          "id": 334089913,
          "name": "Ignition and Digestion",
          "symbols": ["NASDAQ:AAPL", "NYSE:BRK.B", "ASX:TLS", "NASDAQ:AAPL"]
        }
      }
    }</script>
    """

    watchlist = parse_tradingview_watchlist(
        html, source_url="https://www.tradingview.com/watchlists/334089913/"
    )

    assert watchlist.id == 334089913
    assert watchlist.name == "Ignition and Digestion"
    assert watchlist.tickers == ["AAPL", "BRK-B", "TLS.AX"]


def test_normalize_tradingview_symbol_maps_common_exchange_suffixes() -> None:
    assert normalize_tradingview_symbol("NASDAQ:MSFT").ticker == "MSFT"
    assert normalize_tradingview_symbol("ASX:TLS").ticker == "TLS.AX"
    assert normalize_tradingview_symbol("LSE:VOD").ticker == "VOD.L"


def test_normalize_watchlist_url_requires_tradingview_watchlist_path() -> None:
    with pytest.raises(WatchlistError):
        normalize_watchlist_url("https://example.com/watchlists/1/")

    assert (
        normalize_watchlist_url("https://www.tradingview.com/watchlists/334089913/?x=1")
        == "https://www.tradingview.com/watchlists/334089913/"
    )
