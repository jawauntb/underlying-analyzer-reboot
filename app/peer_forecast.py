"""Sector peer forecast: TabICL v2 in-context classification of forward excess return.

For a ticker's curated sector universe (:mod:`app.situate.peers`) this builds the
same long (date, symbol) feature panel the Situate stack uses
(:func:`app.situate.stack.build_feature_panel`), buckets the known forward
``h``-month excess returns (vs the sector ETF) into **quintiles** on the context,
and asks the tabular model for each peer's bucket probabilities at the latest
cross-section. No gradient step, no fitted parameters: the model reads the
context in one forward pass.

Honesty rules, in order of importance:

* **No look-ahead.** A context row observed at month ``m`` is used only when its
  label window ``[m, m + h]`` has closed on or before the query month, exactly
  the :func:`app.situate.stack.eligible_train_mask` rule with no embargo. The
  predicted-vs-realized view for the last *labelled* cross-section is produced
  by a second, stricter pass whose context closes before *that* month.
* **Bounded.** The context is capped at the ``max_context_rows`` most recent
  rows (8 000 by default; the generic contract's hard cap is 20 000).
* **Fail open.** Anything that stops a forecast (model not installed, no service,
  too little history, ticker outside the universe) raises
  :class:`app.tabular.TabularUnavailable`; routes answer ``503`` and packet
  builders leave the section ``null`` with the reason.
* **Confidence is user-facing.** ``confidence`` is the top bucket probability;
  :data:`CONFIDENCE_FLOOR` is the bar a point call ("over") must clear before a
  UI chip or memo headline states it; below it the distribution alone is shown.

The whole sector/horizon result is memoised in-process for 12 hours, keyed by
sector, horizon, as-of date, universe version and predictor, so every peer's
lookup, the chart and the memo cross-check share one model call.

A caller that pays for cold computations can count them, or refuse them: set
:data:`cold_build_guard` for the duration of a request and it is called when a lookup
misses the cache, before a panel is loaded or a model is asked (the constellation MCP
does this to cap what it spends). Unset, which is the default, nothing changes.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app._perf import TTLCache
from app.situate import peers as peers_mod
from app.situate.stack import (
    PRICE_FEATURES,
    _month_idx,
    build_feature_panel,
    eligible_train_mask,
)
from app.tabular import (
from app.utils import finite
    DEFAULT_N_ESTIMATORS,
    TabularPredictor,
    TabularUnavailable,
    get_predictor,
)

__all__ = [
    "BUCKETS",
    "PEER_FORECAST_HORIZONS",
    "DEFAULT_HORIZON",
    "DEFAULT_MAX_CONTEXT_ROWS",
    "MIN_CONTEXT_ROWS",
    "CONFIDENCE_FLOOR",
    "CACHE_TTL_SECONDS",
    "METHOD",
    "quintile_buckets",
    "bucket_means",
    "build_sector_forecast",
    "project_ticker",
    "peer_forecast_for_ticker",
    "clear_peer_forecast_cache",
    "cold_build_guard",
    "parse_horizon",
]

#: Quintile buckets of the context's forward excess return, bottom 20% first.
BUCKETS: tuple[str, ...] = ("strong_under", "under", "inline", "over", "strong_over")
#: Horizons (months) the endpoint accepts; the panel supports these directly.
PEER_FORECAST_HORIZONS: tuple[int, ...] = (1, 2, 3, 6, 12)
DEFAULT_HORIZON = 3
#: Most-recent context rows handed to the model (the contract's option default).
DEFAULT_MAX_CONTEXT_ROWS = 8_000
#: Below this many labelled rows the cross-section is too thin to read.
MIN_CONTEXT_ROWS = 60
#: Top-probability bar for a point call (bucket chip / memo headline).
CONFIDENCE_FLOOR = 0.55
#: Whole sector/horizon results are memoised in-process this long.
CACHE_TTL_SECONDS = 12 * 60 * 60
METHOD = "tabicl_v2_icl"
#: Years of daily history to load for the sector panel.
PANEL_YEARS = 12
_QUINTILE_LEVELS: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8)

_cache = TTLCache(CACHE_TTL_SECONDS, max_entries=128)

#: Set for one request by a caller that wants to count, or refuse, the computations that miss
#: the cache. It is called with no arguments when a computation (a panel load, then the model
#: calls) is about to start, and raising from it (``TabularUnavailable``, so a route answers
#: ``503`` with the reason) stops the computation before a provider or the model is asked. A
#: cache hit does not call it (``tests/test_constellation_mcp.py`` checks that a hit is not
#: counted). The default, ``None``, leaves every caller as it was.
cold_build_guard: ContextVar[Callable[[], None] | None] = ContextVar(
    "peer_forecast_cold_build_guard", default=None
)


def clear_peer_forecast_cache() -> None:
    """Drop every memoised sector forecast (tests, or after a data refresh)."""
    _cache.clear()


def parse_horizon(value: Any) -> int:
    """Validate a ``horizon`` query value; ``None``/empty means the default."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_HORIZON
    try:
        horizon = int(str(value).strip())
    except ValueError as exc:
        raise ValueError(
            "horizon must be one of " + ", ".join(str(h) for h in PEER_FORECAST_HORIZONS)
        ) from exc
    if horizon not in PEER_FORECAST_HORIZONS:
        raise ValueError(
            "horizon must be one of " + ", ".join(str(h) for h in PEER_FORECAST_HORIZONS)
        )
    return horizon


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------


def quintile_buckets(targets: Sequence[float] | np.ndarray) -> tuple[list[str], list[float]]:
    """Label each target with its quintile bucket on **this** sample.

    Returns ``(labels, edges)`` where ``edges`` are the 20/40/60/80 percentiles.
    Ties on an edge fall into the lower bucket.
    """
    arr = np.asarray(targets, dtype=float)
    if arr.size == 0:
        return [], []
    edges = [float(np.quantile(arr, level)) for level in _QUINTILE_LEVELS]
    positions = np.searchsorted(np.asarray(edges), arr, side="left")
    labels = [BUCKETS[int(min(max(p, 0), len(BUCKETS) - 1))] for p in positions]
    return labels, edges


def bucket_means(
    targets: Sequence[float] | np.ndarray, labels: Sequence[str]
) -> dict[str, float | None]:
    """Mean realised excess return inside each bucket (``None`` when empty)."""
    arr = np.asarray(targets, dtype=float)
    out: dict[str, float | None] = {}
    lab = np.asarray(list(labels), dtype=object)
    for bucket in BUCKETS:
        mask = lab == bucket
        if not mask.any():
            out[bucket] = None
            continue
        values = arr[mask]
        values = values[np.isfinite(values)]
        out[bucket] = float(values.mean()) if values.size else None
    return out


def _expected_return(
    probabilities: dict[str, float], means: dict[str, float | None]
) -> float | None:
    total = 0.0
    weight = 0.0
    for bucket in BUCKETS:
        mean = means.get(bucket)
        prob = float(probabilities.get(bucket, 0.0) or 0.0)
        if mean is None:
            continue
        total += prob * mean
        weight += prob
    if weight <= 0.0:
        return None
    # Renormalise over the buckets that have a mean so a missing bucket does not
    # silently shrink the expectation toward zero.
    return total / weight


    return number if math.isfinite(number) else None


# --------------------------------------------------------------------------
# Core builder (pure over a Panel-like object)
# --------------------------------------------------------------------------


def _classify(
    predictor: TabularPredictor,
    x_context: pd.DataFrame,
    y_context: list[str],
    x_query: pd.DataFrame,
    *,
    max_context_rows: int,
) -> list[dict[str, float]]:
    """Run the model and return one ``{bucket: p}`` dict per query row."""
    prediction = predictor.predict(
        "classification",
        x_context.reset_index(drop=True),
        pd.Series(list(y_context), name="bucket"),
        x_query.reset_index(drop=True),
        categorical=(),
        options={"max_context_rows": int(max_context_rows), "n_estimators": DEFAULT_N_ESTIMATORS},
    )
    classes = [str(c) for c in (prediction.classes or [])]
    matrix = prediction.probabilities or []
    if len(matrix) != x_query.shape[0]:
        raise TabularUnavailable("tabular model returned a malformed probability matrix")
    rows: list[dict[str, float]] = []
    for row in matrix:
        probs = dict.fromkeys(BUCKETS, 0.0)
        for cls, prob in zip(classes, row, strict=False):
            if cls in probs:
                probs[cls] = max(0.0, float(finite(prob) or 0.0))
        total = sum(probs.values())
        if total > 0.0:
            probs = {k: v / total for k, v in probs.items()}
        rows.append(probs)
    return rows


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).date().isoformat()
    except (TypeError, ValueError):
        return str(value)


def build_sector_forecast(
    panel: Any,
    *,
    sector: str,
    universe: Sequence[str],
    etf_of: dict[str, str | None],
    sector_etf: str | None,
    horizon: int,
    predictor: TabularPredictor,
    as_of: str | None = None,
    max_context_rows: int = DEFAULT_MAX_CONTEXT_ROWS,
    features: Sequence[str] = PRICE_FEATURES,
) -> dict[str, Any]:
    """Forecast every peer in ``universe`` from a loaded Panel-like ``panel``.

    ``panel`` only needs ``daily_close(symbol)`` (and optionally ``as_of``). Raises
    :class:`TabularUnavailable` whenever an honest forecast cannot be produced.
    """
    horizon = int(horizon)
    if horizon not in PEER_FORECAST_HORIZONS:
        raise ValueError(f"unsupported horizon {horizon}")
    universe = [str(s).strip().upper() for s in universe]
    frame, absent = build_feature_panel(panel, universe, etf_of=etf_of, horizons=(horizon,))
    if frame.empty:
        raise TabularUnavailable(
            "insufficient data: "
            f"{absent.get('all') or 'no usable month-end history for the sector'}"
        )
    feats = [f for f in features if f in frame.columns and frame[f].notna().any()]
    if not feats:
        raise TabularUnavailable("insufficient data: no usable features across the sector panel")
    target_col = f"target_h{horizon}"
    if target_col not in frame.columns:
        raise TabularUnavailable(f"insufficient data: no {target_col} column in the panel")

    work = frame.copy()
    work["_m"] = _month_idx(work["date"])
    latest_m = int(work["_m"].max())
    query = work[work["_m"] == latest_m].dropna(subset=feats)
    if query.empty:
        raise TabularUnavailable("insufficient data: the latest cross-section has no complete rows")

    labelled = work.dropna(subset=[target_col, *feats])
    context = labelled[eligible_train_mask(labelled["_m"].to_numpy(), latest_m, horizon, 0)]
    context = context.sort_values(["date", "symbol"]).tail(int(max_context_rows))
    if context.shape[0] < MIN_CONTEXT_ROWS:
        raise TabularUnavailable(
            f"insufficient data: {int(context.shape[0])} labelled rows < {MIN_CONTEXT_ROWS}"
        )
    labels, edges = quintile_buckets(context[target_col].to_numpy(dtype=float))
    if len(set(labels)) < 2:
        raise TabularUnavailable("insufficient data: the context has a degenerate target")
    means = bucket_means(context[target_col].to_numpy(dtype=float), labels)

    live = _classify(
        predictor, context[feats], labels, query[feats], max_context_rows=max_context_rows
    )

    # Predicted-vs-realized on the most recent labelled cross-section, with a
    # context that closed before that month (the same purge, one step back).
    last_labelled_m = int(labelled["_m"].max())
    backtest_query = labelled[labelled["_m"] == last_labelled_m]
    backtest: dict[str, float | None] = {}
    backtest_date: str | None = (
        _iso(backtest_query["date"].iloc[0]) if not backtest_query.empty else None
    )
    if not backtest_query.empty:
        bt_context = labelled[
            eligible_train_mask(labelled["_m"].to_numpy(), last_labelled_m, horizon, 0)
        ]
        bt_context = bt_context.sort_values(["date", "symbol"]).tail(int(max_context_rows))
        if bt_context.shape[0] >= MIN_CONTEXT_ROWS:
            bt_labels, _ = quintile_buckets(bt_context[target_col].to_numpy(dtype=float))
            if len(set(bt_labels)) >= 2:
                bt_means = bucket_means(bt_context[target_col].to_numpy(dtype=float), bt_labels)
                bt_probs = _classify(
                    predictor,
                    bt_context[feats],
                    bt_labels,
                    backtest_query[feats],
                    max_context_rows=max_context_rows,
                )
                for symbol, probs in zip(backtest_query["symbol"].tolist(), bt_probs, strict=True):
                    backtest[str(symbol)] = _expected_return(probs, bt_means)
    realized: dict[str, float | None] = {
        str(symbol): finite(value)
        for symbol, value in zip(
            backtest_query["symbol"].tolist(),
            backtest_query[target_col].tolist(),
            strict=True,
        )
    }

    peers: list[dict[str, Any]] = []
    for symbol, probs in zip(query["symbol"].tolist(), live, strict=True):
        top = max(BUCKETS, key=lambda b: probs.get(b, 0.0))
        sym = str(symbol)
        peers.append(
            {
                "symbol": sym,
                "bucket": top,
                "expected_excess_return": _expected_return(probs, means),
                "probabilities": {b: float(probs.get(b, 0.0)) for b in BUCKETS},
                "confidence": float(probs.get(top, 0.0)),
                "realized_excess_return_last": realized.get(sym),
                "predicted_excess_return_last": backtest.get(sym),
            }
        )
    peers.sort(
        key=lambda p: (
            p["expected_excess_return"] is None,
            -(p["expected_excess_return"] or 0.0),
            p["symbol"],
        )
    )

    resolved_as_of = (
        as_of or (str(getattr(panel, "as_of", "")) or None) or _iso(query["date"].iloc[0])
    )
    return {
        "available": True,
        "sector": sector,
        "sector_etf": sector_etf,
        "horizon_months": horizon,
        "method": METHOD,
        "as_of": resolved_as_of,
        "query_date": _iso(query["date"].iloc[0]),
        "last_labeled_date": backtest_date,
        "context_rows": int(context.shape[0]),
        "context_start": _iso(context["date"].iloc[0]),
        "context_end": _iso(context["date"].iloc[-1]),
        "features": list(feats),
        "features_absent": {k: v for k, v in absent.items() if k in features},
        "bucket_edges": edges,
        "bucket_means": means,
        "universe": list(universe),
        "predictor": str(getattr(predictor, "name", "unknown")),
        "peers": peers,
    }


def project_ticker(sector_result: dict[str, Any], ticker: str) -> dict[str, Any]:
    """The SHARED CONTRACT view for one ticker inside a sector result."""
    symbol = str(ticker).strip().upper()
    own = next((p for p in sector_result.get("peers", []) if p.get("symbol") == symbol), None)
    if own is None:
        raise TabularUnavailable(
            f"insufficient data: no complete latest cross-section row for {symbol}"
        )
    return {
        "available": True,
        "ticker": symbol,
        "sector": sector_result.get("sector"),
        "sector_etf": sector_result.get("sector_etf"),
        "horizon_months": sector_result.get("horizon_months"),
        "method": sector_result.get("method", METHOD),
        "as_of": sector_result.get("as_of"),
        "bucket": own["bucket"],
        "probabilities": dict(own["probabilities"]),
        "expected_excess_return": own.get("expected_excess_return"),
        "confidence": own.get("confidence"),
        "confidence_floor": CONFIDENCE_FLOOR,
        "context_rows": sector_result.get("context_rows"),
        "features": list(sector_result.get("features") or []),
        "peers": [dict(p) for p in sector_result.get("peers", [])],
        "query_date": sector_result.get("query_date"),
        "last_labeled_date": sector_result.get("last_labeled_date"),
        "bucket_edges": list(sector_result.get("bucket_edges") or []),
        "bucket_means": dict(sector_result.get("bucket_means") or {}),
        "predictor": sector_result.get("predictor"),
    }


# --------------------------------------------------------------------------
# Cached entry point
# --------------------------------------------------------------------------


def _resolve_as_of(as_of: date | str | None) -> str:
    if as_of is None:
        return date.today().isoformat()
    if isinstance(as_of, date):
        return as_of.isoformat()
    return str(as_of).strip()[:10]


def _cache_key(
    sector: str, horizon: int, as_of: str, universe: Sequence[str], predictor: Any
) -> tuple[Any, ...]:
    digest = hashlib.sha256(
        ("|".join(universe) + f"|{peers_mod.PEERS_VERSION}|{','.join(PRICE_FEATURES)}").encode()
    ).hexdigest()[:16]
    return (sector, int(horizon), as_of, digest, str(getattr(predictor, "name", "unknown")))


def _default_panel_loader(client: Any, symbols: list[str], as_of: str) -> Any:
    from app.situate.panel import load_panel

    cache = None
    try:
        from app.prism.cache import PrismCache

        cache = PrismCache.from_env()
    except Exception:  # noqa: BLE001 - the cache is an optimisation, never a dependency
        cache = None
    return load_panel(client, symbols, as_of=as_of, years=PANEL_YEARS, cache=cache)


def peer_forecast_for_ticker(
    ticker: str,
    *,
    horizon: int = DEFAULT_HORIZON,
    predictor: TabularPredictor | None = None,
    client: Any | None = None,
    panel: Any | None = None,
    as_of: date | str | None = None,
    panel_loader: Callable[[Any, list[str], str], Any] | None = None,
    use_cache: bool = True,
    max_context_rows: int = DEFAULT_MAX_CONTEXT_ROWS,
) -> dict[str, Any]:
    """The SHARED CONTRACT payload for ``ticker`` at ``horizon`` months.

    Resolves the sector from the curated universe, loads (or accepts) the sector
    panel, runs :func:`build_sector_forecast` once per sector/horizon/as-of (the
    result is memoised for 12 hours) and projects the ticker's own row. Raises
    :class:`TabularUnavailable` on every honest failure.
    """
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    horizon = int(horizon)
    if horizon not in PEER_FORECAST_HORIZONS:
        raise ValueError(f"unsupported horizon {horizon}")
    sector = peers_mod.sector_of(symbol)
    if sector is None:
        raise TabularUnavailable(f"{symbol} is not in the curated sector universe")
    universe = list(peers_mod.PEERS_BY_SECTOR[sector])
    if symbol not in universe:
        universe = [symbol, *universe]
    etf_of = peers_mod.etf_map(universe)
    sector_etf = peers_mod.industry_etf_of(symbol)
    if not sector_etf:
        raise TabularUnavailable(f"no sector ETF mapped for {symbol}")

    resolved_as_of = _resolve_as_of(as_of)
    if predictor is None:
        predictor = get_predictor()
    key = _cache_key(sector, horizon, resolved_as_of, universe, predictor)
    sector_result = _cache.get(key) if use_cache else None
    if sector_result is None:
        if panel is None and client is None:
            raise TabularUnavailable("no market data client available to load the sector panel")
        guard = cold_build_guard.get()
        if guard is not None:
            guard()
        if panel is None:
            loader = panel_loader or _default_panel_loader
            wanted = sorted({*universe, *(etf for etf in etf_of.values() if etf)})
            panel = loader(client, wanted, resolved_as_of)
        sector_result = build_sector_forecast(
            panel,
            sector=sector,
            universe=universe,
            etf_of=etf_of,
            sector_etf=sector_etf,
            horizon=horizon,
            predictor=predictor,
            as_of=resolved_as_of,
            max_context_rows=max_context_rows,
        )
        if use_cache:
            _cache.set(key, sector_result)
    return project_ticker(sector_result, symbol)
