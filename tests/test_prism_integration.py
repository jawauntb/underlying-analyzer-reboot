"""Integration-level regressions for the wired Prism engine.

These pin the fixes made while running the engine end to end against live
Massive/FRED/SEC/Exa/Anthropic for NVDA, MU and SPY:

* the (symbol, as-of month) caches must not serve a series that predates the
  as-of date being built — a monthly key alone would freeze prices for a month;
* the relational frame must be measured on trading days, because ``X:BTCUSD``
  quotes seven days a week and silently shortens every "252 day" window;
* every price multiple must be struck off the one price the packet quotes;
* the HTTP surface must build Prism's own market client, with the yfinance
  fallback off, rather than borrowing the terminal's;
* a persistence tier that refused the write has to show up in the packet.

Nothing here touches the network.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.prism import data as data_module
from app.prism import macro as macro_module
from app.prism import relational as relational_module
from app.prism import routes as routes_module
from app.prism.cache import PrismCache
from app.prism.fundamentals import derive_ratios


class RecordingHistorySource:
    """A market client stand-in that counts the fetches it is asked for."""

    def __init__(self, series: pd.Series) -> None:
        self.series = series
        self.calls: list[tuple[str, date]] = []

    def get_history(self, ticker: str, *, start: date, end: date, interval: str) -> Any:
        self.calls.append((ticker, end))
        # The provider filters by calendar date, not by timestamp, so a bar
        # stamped 04:00 on the end date is still returned.
        days = pd.DatetimeIndex(self.series.index).normalize()
        window = self.series[
            (days >= pd.Timestamp(start)) & (days <= pd.Timestamp(end))
        ]
        frame = pd.DataFrame({"Close": window, "Adj Close": window})
        return SimpleNamespace(
            data=frame,
            provider="massive",
            note="test fixture",
            ticker=ticker,
            interval=interval,
        )


def _daily(n: int = 400, *, end: str = "2026-09-01") -> pd.Series:
    index = pd.bdate_range(end=pd.Timestamp(end), periods=n)
    values = 100.0 + np.arange(n, dtype=float) * 0.1
    return pd.Series(values, index=index, name="TEST")


# --------------------------------------------------------------------------
# Cache freshness
# --------------------------------------------------------------------------


def test_series_cache_hits_for_the_as_of_it_was_built_for(tmp_path: Any) -> None:
    cache = PrismCache(base_dir=tmp_path / "cache")
    source = RecordingHistorySource(_daily())

    first = data_module.load_daily(source, "TEST", years=2, as_of="2026-09-01", cache=cache)
    second = data_module.load_daily(source, "TEST", years=2, as_of="2026-09-01", cache=cache)

    assert first.cached is False
    assert second.cached is True
    assert len(source.calls) == 1
    assert second.n_days == first.n_days


def test_series_cache_refuses_a_row_older_than_the_requested_as_of(tmp_path: Any) -> None:
    """A month-keyed row must not freeze prices for the rest of the month."""
    cache = PrismCache(base_dir=tmp_path / "cache")
    source = RecordingHistorySource(_daily(end="2026-09-30"))

    data_module.load_daily(source, "TEST", years=2, as_of="2026-09-01", cache=cache)
    later = data_module.load_daily(source, "TEST", years=2, as_of="2026-09-21", cache=cache)

    assert later.cached is False
    assert [call[1] for call in source.calls] == [date(2026, 9, 1), date(2026, 9, 21)]
    assert later.last_date is not None
    assert later.last_date > "2026-09-01"


def test_macro_cache_refuses_a_row_older_than_the_requested_as_of(tmp_path: Any) -> None:
    cache = PrismCache(base_dir=tmp_path / "cache")
    index = pd.bdate_range("2026-01-01", "2026-09-30")
    series = pd.Series(np.linspace(4.0, 5.0, len(index)), index=index, name="DGS10")

    class FakeFred:
        def __init__(self) -> None:
            self.calls: list[date] = []

        def get_series(
            self, series_id: str, *, start: date, end: date  # noqa: ARG002
        ) -> pd.Series:
            self.calls.append(end)
            assert series_id == "DGS10"
            return series[series.index <= pd.Timestamp(end)]

    fred = FakeFred()
    macro_module.fetch_fred_series(fred, "DGS10", as_of="2026-09-01", cache=cache)
    macro_module.fetch_fred_series(fred, "DGS10", as_of="2026-09-01", cache=cache)
    assert len(fred.calls) == 1

    later = macro_module.fetch_fred_series(fred, "DGS10", as_of="2026-09-21", cache=cache)
    assert len(fred.calls) == 2
    assert later.index[-1] > pd.Timestamp("2026-09-01")


# --------------------------------------------------------------------------
# One index convention
# --------------------------------------------------------------------------


def test_daily_bars_are_snapped_to_midnight() -> None:
    """Massive stamps daily bars at 04:00; the cache round-trip uses plain dates.

    Left alone, ``index <= Timestamp(as_of)`` silently drops the as-of session on
    a cold build and keeps it on the cached one, so the same ticker built twice
    produced two different betas.
    """
    sessions = pd.bdate_range(end=pd.Timestamp("2026-09-01"), periods=60)
    stamped = pd.Series(
        np.linspace(100.0, 130.0, len(sessions)),
        index=pd.DatetimeIndex([session.replace(hour=4) for session in sessions]),
    )
    source = RecordingHistorySource(stamped)

    load = data_module.load_daily(source, "TEST", years=1, as_of="2026-09-01")

    assert list(load.series.index[-2:]) == [
        pd.Timestamp("2026-08-31"),
        pd.Timestamp("2026-09-01"),
    ]
    assert (load.series.index <= pd.Timestamp("2026-09-01")).all()
    assert load.last_date == "2026-09-01"


# --------------------------------------------------------------------------
# Trading-day frame
# --------------------------------------------------------------------------


def test_restrict_to_trading_days_drops_seven_day_only_rows() -> None:
    sessions = pd.bdate_range("2026-01-01", "2026-06-30")
    every_day = pd.date_range("2026-01-01", "2026-06-30", freq="D")
    frame = pd.DataFrame(
        {
            "NVDA": pd.Series(np.arange(len(sessions), dtype=float), index=sessions),
            "SPY": pd.Series(np.arange(len(sessions), dtype=float), index=sessions),
            "X:BTCUSD": pd.Series(np.arange(len(every_day), dtype=float), index=every_day),
        }
    )
    assert len(frame.index) == len(every_day)

    trimmed = relational_module.restrict_to_trading_days(frame, "NVDA", "SPY")

    assert list(trimmed.index) == list(sessions)
    assert trimmed["X:BTCUSD"].notna().all()


def test_relational_windows_count_trading_days_not_calendar_rows() -> None:
    sessions = pd.bdate_range(end="2026-09-01", periods=400)
    every_day = pd.date_range(end="2026-09-01", periods=560, freq="D")
    rng = np.random.default_rng(20260901)
    spy_returns = rng.normal(0.0003, 0.008, len(sessions))
    spy = pd.Series(100.0 * np.exp(np.cumsum(spy_returns)), index=sessions)
    # Twice SPY's daily return, so the true beta is 2.0 whatever the window.
    nvda = pd.Series(50.0 * np.exp(np.cumsum(2.0 * spy_returns)), index=sessions)
    series_map = {
        "NVDA": nvda,
        "SPY": spy,
        "X:BTCUSD": pd.Series(
            50_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.02, len(every_day)))),
            index=every_day,
        ),
    }

    section = relational_module.build_relational_section("NVDA", series_map, as_of="2026-09-01")

    # 252 rows of the frame must be 252 sessions, so the window ends exactly one
    # trading year back rather than eight months back.
    assert section["symbols"] == ["NVDA", "SPY", "X:BTCUSD"]
    assert section["beta"]["SPY"]["1y"] == pytest.approx(2.0, abs=0.05)


# --------------------------------------------------------------------------
# 10-Q section extraction
# --------------------------------------------------------------------------


def test_tenq_mdna_survives_a_part_ii_cross_reference() -> None:
    """NVDA's 10-Q MD&A opens by pointing at "Part II, Item 1A. Risk Factors".

    With PART II as a terminator the section ended at the forward-looking
    statements preamble — 3,180 characters of boilerplate and no discussion of
    results — and the cross-filing synthesis then reported that no financial
    results were disclosed.
    """
    from app.prism.filings import section_specs_for, select_section

    document = (
        "Item 2. Management's Discussion and Analysis of Financial Condition "
        "and Results of Operations Forward-Looking Statements This Quarterly "
        "Report contains forward-looking statements. "
        + "Boilerplate about safe harbour. " * 60
        + "This discussion should be read together with Item 1A. Risk Factors of "
        "our Annual Report and Part II, Item 1A of this report. "
        + "Revenue for the quarter was $96,221 million, up 106% year over year. " * 60
        + "Item 3. Quantitative and Qualitative Disclosures About Market Risk "
        + "Interest rate exposure. " * 40
    )
    spec = section_specs_for("10-Q")["mdna"]
    section = select_section(document, starts=spec["starts"], ends=spec["ends"])

    assert section is not None
    assert "Revenue for the quarter was $96,221 million" in section
    assert "Quantitative and Qualitative" not in section
    assert len(section) > 4000


def test_truncated_synthesis_json_is_salvaged_not_discarded() -> None:
    """The reply is one JSON object; the token limit can cut it mid-string.

    Discarding it dropped all six synthesis fields back to raw excerpts. Every
    key that closed its string is still good, and the result says it is partial.
    """
    from app.prism.filings import parse_synthesis

    reply = (
        '{"performance": "Revenue rose 106% year over year.", '
        '"risks": "Export controls and customer concentration.", '
        '"growth_opportunities": "Data centre bui'
    )
    parsed = parse_synthesis(reply)

    assert parsed is not None
    assert parsed["performance"].startswith("Revenue rose 106%")
    assert parsed["risks"].startswith("Export controls")
    assert "growth_opportunities" not in parsed
    assert parsed["truncated"] is True
    # A complete reply is unaffected and carries no truncation flag.
    whole = parse_synthesis('{"performance": "a", "risks": "b"}')
    assert whole == {"performance": "a", "risks": "b"}
    assert parse_synthesis("no json at all") is None


# --------------------------------------------------------------------------
# One price per packet
# --------------------------------------------------------------------------


def test_ratios_are_all_struck_off_the_packets_own_price() -> None:
    quarters = [
        {
            "period_end": "2026-07-26",
            "eps": 2.0,
            "revenue": 100.0,
            "net_income": 50.0,
            "shares": 10.0,
            "total_equity": 200.0,
            "cash": 40.0,
            "total_debt": 10.0,
            "operating_income": 60.0,
            "gross_profit": 70.0,
            "fcf": 30.0,
            "ebitda": 65.0,
        }
    ] * 4
    ratios, source = derive_ratios(
        quarters,
        current_price=217.48,
        market_cap=5_331_174_660_000.0,  # a vendor snapshot struck at another price
        provider_ratios={"price": 220.78, "market_cap": 5_331_174_660_000.0},
    )

    assert ratios["current_price"] == pytest.approx(217.48)
    assert ratios["market_cap"] == pytest.approx(217.48 * 10.0)
    assert ratios["market_cap_provider"] == pytest.approx(5_331_174_660_000.0)
    assert source["market_cap"] == "price_x_shares_outstanding"
    # P/E and P/S therefore agree with each other about what a share costs.
    assert ratios["pe"] == pytest.approx(217.48 / 8.0)
    assert ratios["ps"] == pytest.approx((217.48 * 10.0) / 400.0)


# --------------------------------------------------------------------------
# HTTP surface uses Prism's own client
# --------------------------------------------------------------------------


def test_route_market_client_disables_the_legacy_fallback() -> None:
    from flask import Flask

    app = Flask(__name__)
    app.config["MARKET_DATA_CLIENT"] = object()
    app.config["PRISM_MARKET_CLIENT"] = None

    with app.test_request_context("/api/prism"):
        client = routes_module.market_client()
        assert getattr(client, "fallback_enabled", None) is False
        # Built once and reused, so the HTTP session and caches are shared.
        assert routes_module.market_client() is client
        assert app.config["PRISM_MARKET_CLIENT"] is client


def test_route_market_client_prefers_an_injected_client() -> None:
    from flask import Flask

    sentinel = object()
    app = Flask(__name__)
    app.config["PRISM_MARKET_CLIENT"] = sentinel
    with app.test_request_context("/api/prism"):
        assert routes_module.market_client() is sentinel


# --------------------------------------------------------------------------
# A refused persistence tier is reported
# --------------------------------------------------------------------------


def test_a_refused_store_tier_is_recorded_as_unavailable(monkeypatch: Any) -> None:
    from app.prism import engine as engine_module

    class Store:
        def save_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
            return {
                "ticker": packet["ticker"],
                "as_of": packet["as_of"],
                "local_path": "/tmp/x.json",
                "supabase_id": None,
                "errors": ["supabase write failed: prism_packets POST failed: 404"],
            }

        def load_packet(self, ticker: str, as_of: Any = None) -> None:  # noqa: ARG002
            return None

    class Client:
        def get_profile(self, ticker: str) -> dict[str, Any]:  # noqa: ARG002
            raise RuntimeError("offline")

        def get_history(self, ticker: str, **kwargs: Any) -> Any:  # noqa: ARG002
            raise RuntimeError("offline")

    monkeypatch.setenv("PRISM_CACHE_ENABLED", "0")
    packet = engine_module.build_prism_packet(
        Client(),
        "NVDA",
        as_of="2026-09-01",
        include_memo=False,
        force=True,
        store=Store(),
        max_workers=2,
    )

    reasons = [row["reason"] for row in packet["meta"]["unavailable"] if row["source"] == "store"]
    assert reasons and "404" in reasons[0]
    assert packet["meta"]["stored"]["local_path"] == "/tmp/x.json"
