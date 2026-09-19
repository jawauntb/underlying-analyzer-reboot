"""Offline tests for the TabICL sector peer forecast and everything that reads it.

The model is an injected fake that records every context/query it sees, so the
load-bearing properties are checked directly:

* **shape** — the SHARED CONTRACT payload, per peer and for the ticker;
* **no look-ahead** — every context row's label window closes on or before the
  query month, and the backtest pass closes before the last labelled month;
* **caps** — the context never exceeds ``max_context_rows``;
* **cache** — a second lookup in the same sector/horizon makes no model call;
* **fail-open** — ``503`` from the route and a ``null`` section in the packets
  whenever the model or the data is unavailable;
* the chart builders (PNG + JSON) and the memo projections (present / absent).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest
from flask import Flask

from app import peer_forecast as pf
from app.chart_data import build_peer_forecast_chart_data
from app.charts import render_peer_forecast_chart
from app.main import create_app
from app.situate import peers
from app.situate.stack import _month_idx
from app.tabular import TabularPrediction, TabularUnavailable

SECTOR = "technology"
UNIVERSE = list(peers.PEERS_BY_SECTOR[SECTOR])
PANEL_SYMBOLS = [*UNIVERSE, "XLK"]


class RecordingPredictor:
    """Fake in-context classifier: leans 'over' when 12-1 momentum is positive."""

    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: Any = (),
        options: dict[str, Any] | None = None,
    ) -> TabularPrediction:
        del categorical
        if self.fail:
            raise TabularUnavailable("fake model is down")
        self.calls.append(
            {
                "task": task,
                "x_context": x_context.copy(),
                "y_context": y_context.copy(),
                "x_query": x_query.copy(),
                "options": dict(options or {}),
            }
        )
        classes = list(pf.BUCKETS)
        probabilities: list[list[float]] = []
        for value in x_query["mom_12_1"].fillna(0.0):
            if value > 0:
                base = np.array([0.05, 0.10, 0.15, 0.25, 0.45])
            else:
                base = np.array([0.45, 0.25, 0.15, 0.10, 0.05])
            probabilities.append((base / base.sum()).tolist())
        predictions = [classes[int(np.argmax(row))] for row in probabilities]
        return TabularPrediction(
            task=task,
            predictions=predictions,
            context_rows_used=int(x_context.shape[0]),
            classes=classes,
            probabilities=probabilities,
        )


class _History:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.data = frame
        self.dataframe = frame


class FakeClient:
    """Deterministic daily closes for the technology universe + XLK."""

    def __init__(self, *, end: str = "2026-08-31", days: int = 2600, seed: int = 3) -> None:
        rng = np.random.default_rng(seed)
        index = pd.date_range(end=end, periods=days, freq="B")
        market = rng.normal(0.0004, 0.011, days)
        self.series: dict[str, pd.Series] = {}
        for offset, symbol in enumerate(PANEL_SYMBOLS):
            beta = 0.8 + 0.05 * (offset % 7)
            idio = rng.normal(0.0001 * (offset % 3), 0.009, days)
            prices = 100.0 * np.exp(np.cumsum(beta * market + idio))
            self.series[symbol] = pd.Series(prices, index=index, name=symbol)
        self.calls: list[str] = []

    def get_history(self, ticker: str, *, start: Any, end: Any, interval: str = "1d") -> _History:
        del interval
        symbol = str(ticker).upper()
        self.calls.append(symbol)
        if symbol not in self.series:
            raise ValueError(f"unknown symbol {symbol}")
        series = self.series[symbol]
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        windowed = series[(series.index >= lo) & (series.index <= hi)]
        return _History(pd.DataFrame({"Close": windowed}))

    def get_profile(self, ticker: str) -> dict[str, Any]:
        return {"longName": f"{ticker} Corp", "sector": "Technology", "industry": "Software"}


class StubPanel:
    """Just ``daily_close``; what :func:`build_sector_forecast` needs."""

    def __init__(self, client: FakeClient, *, as_of: str = "2026-08-31") -> None:
        self._client = client
        self.as_of = as_of

    def daily_close(self, symbol: str) -> pd.Series:
        series = self._client.series.get(str(symbol).upper())
        if series is None:
            return pd.Series(dtype="float64")
        return series[series.index <= pd.Timestamp(self.as_of)]


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("PRISM_CACHE_ENABLED", "0")
    monkeypatch.delenv("TABULAR_INFERENCE_URL", raising=False)
    pf.clear_peer_forecast_cache()
    yield
    pf.clear_peer_forecast_cache()


def _sector_forecast(predictor: RecordingPredictor, **kwargs: Any) -> dict[str, Any]:
    client = FakeClient()
    defaults: dict[str, Any] = {
        "sector": SECTOR,
        "universe": UNIVERSE,
        "etf_of": peers.etf_map(UNIVERSE),
        "sector_etf": "XLK",
        "horizon": 3,
        "predictor": predictor,
        "as_of": "2026-08-31",
    }
    defaults.update(kwargs)
    return pf.build_sector_forecast(StubPanel(client), **defaults)


# ---------------------------------------------------------------------------
# bucketing helpers
# ---------------------------------------------------------------------------


def test_quintile_buckets_and_means() -> None:
    targets = np.arange(100, dtype=float) / 100.0
    labels, edges = pf.quintile_buckets(targets)
    assert len(edges) == 4 and edges == sorted(edges)
    counts = {b: labels.count(b) for b in pf.BUCKETS}
    assert all(18 <= count <= 22 for count in counts.values()), counts
    means = pf.bucket_means(targets, labels)
    ordered = [float(means[b] or 0.0) for b in pf.BUCKETS]
    assert ordered == sorted(ordered)
    assert pf.quintile_buckets([]) == ([], [])


def test_parse_horizon() -> None:
    assert pf.parse_horizon(None) == 3
    assert pf.parse_horizon("") == 3
    assert pf.parse_horizon("12") == 12
    with pytest.raises(ValueError):
        pf.parse_horizon("4")
    with pytest.raises(ValueError):
        pf.parse_horizon("soon")


# ---------------------------------------------------------------------------
# sector forecast: shape, purge, caps, backtest
# ---------------------------------------------------------------------------


def test_sector_forecast_shape() -> None:
    predictor = RecordingPredictor()
    result = _sector_forecast(predictor)
    assert result["available"] is True
    assert result["sector"] == SECTOR and result["sector_etf"] == "XLK"
    assert result["horizon_months"] == 3 and result["method"] == "tabicl_v2_icl"
    assert result["features"] == ["mom_12_1", "rev_1m", "vol_dummy", "trend_dummy"]
    assert result["context_rows"] >= pf.MIN_CONTEXT_ROWS
    assert len(result["bucket_edges"]) == 4
    assert set(result["bucket_means"]) == set(pf.BUCKETS)
    assert {p["symbol"] for p in result["peers"]} == set(UNIVERSE)
    expected = [p["expected_excess_return"] for p in result["peers"]]
    assert expected == sorted(expected, reverse=True)
    for peer in result["peers"]:
        assert peer["bucket"] in pf.BUCKETS
        assert abs(sum(peer["probabilities"].values()) - 1.0) < 1e-9
        assert peer["confidence"] == pytest.approx(max(peer["probabilities"].values()))
        assert peer["realized_excess_return_last"] is not None
        assert peer["predicted_excess_return_last"] is not None
    # Two model calls: the live cross-section and the last-labelled backtest.
    assert len(predictor.calls) == 2


def test_sector_forecast_has_no_look_ahead() -> None:
    predictor = RecordingPredictor()
    result = _sector_forecast(predictor, horizon=3)
    query_month = int(_month_idx(pd.Series([pd.Timestamp(result["query_date"])]))[0])
    context_end = int(_month_idx(pd.Series([pd.Timestamp(result["context_end"])]))[0])
    # A context row's label window [m, m+3] must close on or before the query month.
    assert context_end + 3 <= query_month
    # The last labelled cross-section is exactly h months before the query.
    last_labelled = int(_month_idx(pd.Series([pd.Timestamp(result["last_labeled_date"])]))[0])
    assert last_labelled == query_month - 3
    # The backtest pass (second call) used only rows whose labels closed before
    # the last labelled month: strictly fewer rows than the live context.
    live_rows = predictor.calls[0]["x_context"].shape[0]
    backtest_rows = predictor.calls[1]["x_context"].shape[0]
    assert backtest_rows < live_rows
    assert predictor.calls[1]["x_query"].shape[0] == len(UNIVERSE)


def test_sector_forecast_caps_the_context_to_the_most_recent_rows() -> None:
    predictor = RecordingPredictor()
    result = _sector_forecast(predictor, max_context_rows=pf.MIN_CONTEXT_ROWS + 5)
    assert result["context_rows"] == pf.MIN_CONTEXT_ROWS + 5
    assert predictor.calls[0]["x_context"].shape[0] == pf.MIN_CONTEXT_ROWS + 5
    assert predictor.calls[0]["options"]["max_context_rows"] == pf.MIN_CONTEXT_ROWS + 5
    # The kept rows are the latest ones: the context still ends h months before the query.
    assert result["context_end"] == "2026-05-31"


def test_sector_forecast_fails_open_on_thin_data() -> None:
    client = FakeClient(days=200)
    with pytest.raises(TabularUnavailable, match="insufficient data"):
        pf.build_sector_forecast(
            StubPanel(client),
            sector=SECTOR,
            universe=UNIVERSE,
            etf_of=peers.etf_map(UNIVERSE),
            sector_etf="XLK",
            horizon=3,
            predictor=RecordingPredictor(),
        )


def test_sector_forecast_propagates_model_unavailable() -> None:
    with pytest.raises(TabularUnavailable, match="fake model is down"):
        _sector_forecast(RecordingPredictor(fail=True))


def test_project_ticker_is_the_shared_contract() -> None:
    result = _sector_forecast(RecordingPredictor())
    view = pf.project_ticker(result, "nvda")
    for key in (
        "available", "ticker", "sector", "sector_etf", "horizon_months", "method", "as_of",
        "bucket", "probabilities", "expected_excess_return", "confidence", "context_rows",
        "features", "peers",
    ):
        assert key in view, key
    assert view["ticker"] == "NVDA" and view["available"] is True
    assert view["confidence"] == pytest.approx(max(view["probabilities"].values()))
    assert len(view["peers"]) == len(UNIVERSE)
    with pytest.raises(TabularUnavailable):
        pf.project_ticker(result, "ZZZZ")


# ---------------------------------------------------------------------------
# cached entry point
# ---------------------------------------------------------------------------


def test_peer_forecast_for_ticker_caches_the_sector_result() -> None:
    predictor = RecordingPredictor()
    client = FakeClient()
    loads: list[list[str]] = []

    def _loader(_client: Any, symbols: list[str], as_of: str) -> StubPanel:
        loads.append(symbols)
        return StubPanel(_client, as_of=as_of)

    first = pf.peer_forecast_for_ticker(
        "NVDA", horizon=3, predictor=predictor, client=client, as_of="2026-08-31",
        panel_loader=_loader,
    )
    second = pf.peer_forecast_for_ticker(
        "AAPL", horizon=3, predictor=predictor, client=client, as_of="2026-08-31",
        panel_loader=_loader,
    )
    assert first["ticker"] == "NVDA" and second["ticker"] == "AAPL"
    assert len(loads) == 1 and "XLK" in loads[0]
    assert len(predictor.calls) == 2  # live + backtest, once for the whole sector
    # A different horizon is a different cross-section: one more sector build.
    pf.peer_forecast_for_ticker(
        "NVDA", horizon=6, predictor=predictor, client=client, as_of="2026-08-31",
        panel_loader=_loader,
    )
    assert len(loads) == 2 and len(predictor.calls) == 4


def test_peer_forecast_for_ticker_outside_the_universe_is_unavailable() -> None:
    with pytest.raises(TabularUnavailable, match="curated sector universe"):
        pf.peer_forecast_for_ticker("ZZZZ", predictor=RecordingPredictor(), client=FakeClient())


# ---------------------------------------------------------------------------
# HTTP route
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Flask:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.setenv("PRISM_CACHE_DIR", str(tmp_path))
    application = create_app()
    application.config["SITUATE_MARKET_CLIENT"] = FakeClient()
    application.config["TABULAR_PREDICTOR"] = RecordingPredictor()
    return application


def test_peer_forecast_route_returns_the_shared_contract(app: Flask) -> None:
    response = app.test_client().get("/api/tabular/peer-forecast/NVDA?horizon=3&as_of=2026-08-31")
    assert response.status_code == 200
    body = response.get_json()
    assert body["available"] is True
    assert body["ticker"] == "NVDA" and body["sector"] == SECTOR and body["sector_etf"] == "XLK"
    assert body["horizon_months"] == 3 and body["method"] == "tabicl_v2_icl"
    assert body["bucket"] in pf.BUCKETS
    assert set(body["probabilities"]) == set(pf.BUCKETS)
    assert 0.0 <= body["confidence"] <= 1.0
    assert isinstance(body["context_rows"], int) and body["features"]
    peer = body["peers"][0]
    for key in (
        "symbol", "bucket", "expected_excess_return", "probabilities", "confidence",
        "realized_excess_return_last",
    ):
        assert key in peer, key


def test_peer_forecast_route_defaults_horizon_and_rejects_bad_values(app: Flask) -> None:
    client = app.test_client()
    ok = client.get("/api/tabular/peer-forecast/aapl?as_of=2026-08-31")
    assert ok.status_code == 200 and ok.get_json()["horizon_months"] == 3
    assert client.get("/api/tabular/peer-forecast/AAPL?horizon=4").status_code == 400
    assert client.get("/api/tabular/peer-forecast/AAPL?horizon=abc").status_code == 400
    assert client.get("/api/tabular/peer-forecast/AA$PL").status_code == 400


def test_peer_forecast_route_fails_open_with_503(app: Flask) -> None:
    client = app.test_client()
    app.config["TABULAR_PREDICTOR"] = RecordingPredictor(fail=True)
    response = client.get("/api/tabular/peer-forecast/NVDA?as_of=2026-08-31")
    assert response.status_code == 503
    assert response.get_json() == {"available": False, "reason": "fake model is down"}
    # Unknown ticker: also 503, not a 500.
    app.config["TABULAR_PREDICTOR"] = RecordingPredictor()
    response = client.get("/api/tabular/peer-forecast/ZZZZ")
    assert response.status_code == 503
    assert response.get_json()["available"] is False


def test_peer_forecast_route_503_when_no_model_configured(
    monkeypatch: pytest.MonkeyPatch, app: Flask
) -> None:
    from app import tabular as tabular_module

    app.config["TABULAR_PREDICTOR"] = None
    monkeypatch.setattr(tabular_module, "tabicl_installed", lambda: False)
    response = app.test_client().get("/api/tabular/peer-forecast/NVDA")
    assert response.status_code == 503
    assert "TABULAR_INFERENCE_URL" in response.get_json()["reason"]


# ---------------------------------------------------------------------------
# chart builders (both paths)
# ---------------------------------------------------------------------------


def test_peer_forecast_chart_data_builder() -> None:
    view = pf.project_ticker(_sector_forecast(RecordingPredictor()), "NVDA")
    dataset = build_peer_forecast_chart_data(view)
    assert dataset["chart_type"] == "peer-forecast" and dataset["ticker"] == "NVDA"
    ranked = dataset["series"]["ranked"]
    assert [row["symbol"] for row in ranked] == dataset["tickers"]
    assert [row["rank"] for row in ranked] == list(range(1, len(ranked) + 1))
    assert sum(1 for row in ranked if row["is_focus"]) == 1
    pairs = dataset["series"]["predicted_vs_realized"]
    assert pairs and {"symbol", "predicted", "realized", "is_focus"} <= set(pairs[0])
    assert dataset["meta"]["buckets"] == list(pf.BUCKETS)
    assert dataset["meta"]["confidence_floor"] == pf.CONFIDENCE_FLOOR


def test_peer_forecast_png_renderer() -> None:
    view = pf.project_ticker(_sector_forecast(RecordingPredictor()), "NVDA")
    image, meta = render_peer_forecast_chart(view)
    assert image.mime == "image/png" and image.filename == "nvda-peer-forecast-3m.png"
    assert len(image.data) > 1000
    assert meta["ticker"] == "NVDA" and meta["pairs"] == len(UNIVERSE)
    # Without any backtest pairs the second panel still renders.
    bare = dict(view)
    bare["peers"] = [
        {**p, "predicted_excess_return_last": None, "realized_excess_return_last": None}
        for p in view["peers"]
    ]
    image, meta = render_peer_forecast_chart(bare)
    assert meta["pairs"] == 0 and len(image.data) > 1000


def test_chart_routes_serve_peer_forecast_on_both_paths(app: Flask) -> None:
    client = app.test_client()
    png = client.post("/api/charts/peer-forecast", json={"ticker": "NVDA", "as_of": "2026-08-31"})
    assert png.status_code == 200
    body = png.get_json()
    assert body["images"][0]["mime"] == "image/png"
    assert body["export"]["mode"] == "peer-forecast" and body["export"]["tickers"] == ["NVDA"]
    assert body["meta"]["bucket"] in pf.BUCKETS

    data = client.post(
        "/api/data/charts/peer_forecast",
        json={"ticker": "NVDA", "horizon": 6, "as_of": "2026-08-31"},
    )
    assert data.status_code == 200
    payload = data.get_json()
    assert "images" not in payload
    dataset = payload["datasets"][0]
    assert dataset["chart_type"] == "peer-forecast"
    assert dataset["meta"]["horizon_months"] == 6
    assert payload["export"]["mode"] == "peer-forecast-data"


def test_chart_routes_fail_open_with_503(app: Flask) -> None:
    app.config["TABULAR_PREDICTOR"] = RecordingPredictor(fail=True)
    client = app.test_client()
    for path in ("/api/charts/peer-forecast", "/api/data/charts/peer-forecast"):
        response = client.post(path, json={"ticker": "NVDA"})
        assert response.status_code == 503, path
        assert response.get_json()["available"] is False
    bad = client.post("/api/charts/peer-forecast", json={"ticker": "NVDA", "horizon": 5})
    assert bad.status_code == 400


# ---------------------------------------------------------------------------
# memo projections: Situate + Prism, present and absent
# ---------------------------------------------------------------------------


def _situate_packet_with(tabular: dict[str, Any] | None) -> dict[str, Any]:
    from app.situate.contract import empty_packet

    packet = empty_packet("NVDA", as_of="2026-08-31")
    packet["profile"] = {"name": "NVDA Corp"}
    if tabular is not None:
        packet["tabular"] = tabular
        packet["tabular_error"] = None
    else:
        packet["tabular_error"] = "no tabular model"
    return packet


def test_situate_memo_projects_the_tabular_section_only_when_present() -> None:
    from app.situate.memo import TABULAR_SECTION_TITLE, build_citations, fallback_memo

    view = pf.project_ticker(_sector_forecast(RecordingPredictor()), "NVDA")
    absent = fallback_memo(_situate_packet_with(None), reason="test")
    assert TABULAR_SECTION_TITLE not in absent["text"]
    assert not any(c["module"] == "tabular" for c in absent["citations"])

    present = fallback_memo(_situate_packet_with(view), reason="test")
    text = present["text"]
    assert TABULAR_SECTION_TITLE in text
    assert "Bucket probabilities" in text
    assert "ranks" in text
    assert any(c["module"] == "tabular" for c in build_citations(_situate_packet_with(view)))
    # The fake is confident (0.45 < floor 0.55): the distribution is shown, not a call.
    assert "confidence floor" in text
    # No buy/sell grammar sneaks in through the cross-check section itself.
    section = text.split(TABULAR_SECTION_TITLE, 1)[1].split("\n## ", 1)[0].lower()
    assert " buy" not in section and " sell" not in section
    # A confident read states the bucket.
    confident = dict(view)
    confident["confidence"] = 0.8
    confident["probabilities"] = {**view["probabilities"], view["bucket"]: 0.8}
    text = fallback_memo(_situate_packet_with(confident), reason="test")["text"]
    assert f"**{view['bucket'].replace('_', ' ')}**" in text


def test_prism_memo_projects_the_tabular_section_only_when_present() -> None:
    from app.prism.contract import empty_packet
    from app.prism.memo import project_packet

    packet = empty_packet("NVDA", as_of="2026-08-31")
    assert "TabICL" not in project_packet(packet)
    packet["tabular"] = pf.project_ticker(_sector_forecast(RecordingPredictor()), "NVDA")
    packet["tabular_error"] = None
    briefing = project_packet(packet)
    assert "## Quantitative cross-check (TabICL peer forecast)" in briefing
    assert "never the recommendation itself" in briefing
    assert "sector ranking" in briefing


# ---------------------------------------------------------------------------
# engines: the guarded section is present with a model and null without one
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_factor_download(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.situate.factors_data as fd

    def _stub(**_: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
        return pd.DataFrame(), {"error": "download disabled in tests"}

    monkeypatch.setattr(fd, "load_ken_french_monthly", _stub)


def test_situate_engine_carries_the_tabular_section(
    tmp_path: Path, no_factor_download: None  # noqa: ARG001
) -> None:
    from app.prism.store import PrismStore
    from app.situate.contract import validate_packet
    from app.situate.engine import build_situate_packet, situate_summary

    store = PrismStore(base_dir=tmp_path / "situate", supabase=None)
    client = FakeClient()
    predictor = RecordingPredictor()
    # Base panel symbols the engine always loads.
    for symbol in ("SPY", "IWM", "UUP", "FXY", "USO", "GLD"):
        client.series[symbol] = client.series["XLK"].rename(symbol)
    packet = build_situate_packet(
        client,
        "NVDA",
        as_of="2026-08-31",
        include_stack=False,
        years=10,
        store=store,
        cache=None,
        fred_client=None,
        tabular_predictor=predictor,
    )
    assert validate_packet(packet) == []
    assert packet["tabular"] is not None and packet["tabular_error"] is None
    assert packet["tabular"]["ticker"] == "NVDA" and packet["tabular"]["horizon_months"] == 3
    assert packet["meta"]["versions"]["tabular"] == "1.0.0"
    assert "Quantitative cross-check (TabICL peer forecast)" in packet["memo"]["text"]
    summary = situate_summary(packet)
    assert summary["tabular"]["bucket"] == packet["tabular"]["bucket"]

    # Without a model the section is null with a reason, in meta.unavailable (not errors).
    pf.clear_peer_forecast_cache()
    packet = build_situate_packet(
        client, "NVDA", as_of="2026-08-31", include_stack=False, years=10, store=store,
        cache=None, fred_client=None, tabular_predictor=RecordingPredictor(fail=True), force=True,
    )
    assert validate_packet(packet) == []
    assert packet["tabular"] is None and packet["tabular_error"] == "fake model is down"
    assert any(row["source"] == "tabular" for row in packet["meta"]["unavailable"])
    assert not any(row["source"] == "tabular" for row in packet["meta"]["errors"])
    assert "Quantitative cross-check" not in packet["memo"]["text"]
    assert "tabular" in situate_summary(packet)["unavailable_sections"]
