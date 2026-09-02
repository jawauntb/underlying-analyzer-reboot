"""Fama-French factor exposures.

Primary path: the daily Fama-French 5-factor set plus the daily momentum factor,
downloaded straight from Ken French's data library and cached on disk under
``PRISM_CACHE_DIR``. Fallback path: factors reconstructed from ETF closes the
engine has already fetched, with every proxy definition written into the packet
so nobody has to guess what "HML" meant on a given run.

Exposures are estimated by ordinary least squares (numpy ``lstsq``) over 1/3/5/10
year windows with heteroskedasticity-naive t-statistics, R-squared and annualised
residual volatility, plus the cumulative *residual* (idiosyncratic) return over
the last 20 and 60 trading days — the part of the move the factor model does not
explain.

Nothing here touches the network unless :func:`download_ken_french_factors` is
called with ``allow_download=True``; the tests never do.
"""

from __future__ import annotations

import io
import math
import os
import zipfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import numpy as np
import numpy.typing as npt
import pandas as pd

from app.prism.cache import cache_dir_from_env

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "FACTOR_NAMES",
    "FACTOR_WINDOWS",
    "KEN_FRENCH_FF5_URL",
    "KEN_FRENCH_MOM_URL",
    "build_factors",
    "download_ken_french_factors",
    "etf_proxy_factors",
    "factor_regression",
    "load_cached_ken_french",
    "normalize_daily_index",
    "ols_with_stats",
]

KEN_FRENCH_BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp"
KEN_FRENCH_FF5_URL = f"{KEN_FRENCH_BASE}/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"
KEN_FRENCH_MOM_URL = f"{KEN_FRENCH_BASE}/F-F_Momentum_Factor_daily_CSV.zip"

FACTOR_NAMES = ("MKT", "SMB", "HML", "RMW", "CMA", "MOM")
#: Window label -> trading days.
FACTOR_WINDOWS: dict[str, int] = {"1y": 252, "3y": 756, "5y": 1260, "10y": 2520}

_MISSING_SENTINELS = (-99.99, -999.0, -99.99e2)
_TRADING_DAYS = 252.0


class _Session(Protocol):
    def get(self, url: str, timeout: float = ...) -> Any: ...


# --------------------------------------------------------------------------
# Cache plumbing
# --------------------------------------------------------------------------


def normalize_daily_index(obj: pd.Series | pd.DataFrame) -> Any:
    """Snap a date-like index to tz-naive midnight so sources can be joined.

    Massive returns daily bars stamped at the exchange open (``04:00:00``) while
    Ken French and FRED publish plain dates. Without this the inner joins below
    silently produce zero overlapping rows.
    """
    out = obj.copy()
    index = out.index
    if not isinstance(index, pd.DatetimeIndex):
        converted = pd.to_datetime(index, errors="coerce")
        out = out[converted.notna()]
        index = pd.DatetimeIndex(converted[converted.notna()])
    if index.tz is not None:
        index = index.tz_convert("UTC").tz_localize(None)
    out.index = index.normalize()
    return out[~out.index.duplicated(keep="last")].sort_index()


def _cache_root(cache_dir: str | os.PathLike[str] | None) -> Path:
    """Resolve the on-disk cache root, sharing W1's ``PRISM_CACHE_DIR`` convention."""
    if cache_dir is not None:
        return Path(cache_dir)
    return cache_dir_from_env()


def _factor_cache_dir(cache_dir: str | os.PathLike[str] | None) -> Path:
    return _cache_root(cache_dir) / "factors"


def _cache_path(cache_dir: str | os.PathLike[str] | None, as_of: datetime | None = None) -> Path:
    stamp = (as_of or datetime.now(UTC)).strftime("%Y-%m")
    return _factor_cache_dir(cache_dir) / f"ken_french_daily_{stamp}.csv"


def load_cached_ken_french(
    cache_dir: str | os.PathLike[str] | None = None, *, as_of: datetime | None = None
) -> tuple[pd.DataFrame | None, str | None]:
    """Return the newest cached Ken French frame, or ``(None, None)``.

    Prefers the current month's file; otherwise falls back to the most recent
    cached month so a failed download still yields (slightly stale) real data
    rather than a proxy.
    """
    directory = _factor_cache_dir(cache_dir)
    preferred = _cache_path(cache_dir, as_of)
    candidates: list[Path] = []
    if preferred.exists():
        candidates.append(preferred)
    if directory.is_dir():
        candidates.extend(
            sorted(
                (path for path in directory.glob("ken_french_daily_*.csv") if path != preferred),
                reverse=True,
            )
        )
    for path in candidates:
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
        except (OSError, ValueError):
            continue
        if not frame.empty:
            return frame.astype(float), str(path)
    return None, None


def _write_cache(frame: pd.DataFrame, cache_dir: str | os.PathLike[str] | None) -> str | None:
    path = _cache_path(cache_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(path)
    except OSError:
        return None
    return str(path)


# --------------------------------------------------------------------------
# Ken French parsing
# --------------------------------------------------------------------------


def _parse_ken_french_csv(text: str) -> pd.DataFrame:
    """Parse one Ken French daily CSV payload into a decimal-return frame."""
    header: list[str] | None = None
    rows: list[tuple[str, list[float]]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            if header is not None and rows:
                break
            continue
        parts = [part.strip() for part in line.split(",")]
        if header is None:
            if parts[0] == "" and len(parts) > 1 and all(parts[1:]):
                header = [part for part in parts[1:] if part]
            continue
        if not (len(parts[0]) == 8 and parts[0].isdigit()):
            if rows:
                break
            continue
        try:
            values = [float(part) for part in parts[1 : len(header) + 1]]
        except ValueError:
            continue
        if len(values) != len(header):
            continue
        rows.append((parts[0], values))

    if header is None or not rows:
        raise ValueError("could not locate a daily data block in the Ken French CSV")

    index = pd.to_datetime([row[0] for row in rows], format="%Y%m%d")
    frame = pd.DataFrame([row[1] for row in rows], index=index, columns=header, dtype=float)
    for sentinel in _MISSING_SENTINELS:
        frame = frame.mask(np.isclose(frame.to_numpy(dtype=float), sentinel))
    return frame / 100.0


def _read_zip(payload: bytes) -> str:
    archive = zipfile.ZipFile(io.BytesIO(payload))
    names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
    if not names:
        raise ValueError("Ken French archive contains no CSV")
    return archive.read(names[0]).decode("latin-1")


def download_ken_french_factors(
    *,
    cache_dir: str | os.PathLike[str] | None = None,
    session: _Session | None = None,
    timeout: float = 30.0,
    allow_download: bool = True,
    use_cache: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Fetch (or load from cache) the daily FF5 + momentum factors.

    Returns ``(frame, provenance)``. ``frame`` is indexed by date with columns
    ``MKT SMB HML RMW CMA MOM RF`` as **decimal daily returns** (Ken French
    publishes percent). ``provenance`` records the URLs, the cache path, the
    date range and whether the data came from the network or from disk.

    Raises ``RuntimeError`` when neither the cache nor the download yields data.
    """
    provenance: dict[str, Any] = {
        "provider": "ken_french_data_library",
        "urls": [KEN_FRENCH_FF5_URL, KEN_FRENCH_MOM_URL],
        "cache_path": None,
        "from_cache": False,
        "fetched_at": None,
    }
    if use_cache:
        cached, path = load_cached_ken_french(cache_dir)
        if cached is not None and _cache_path(cache_dir).exists():
            provenance.update({"cache_path": path, "from_cache": True})
            provenance["first_date"] = str(cached.index[0].date())
            provenance["last_date"] = str(cached.index[-1].date())
            return cached, provenance

    if not allow_download:
        cached, path = load_cached_ken_french(cache_dir)
        if cached is None:
            raise RuntimeError("Ken French factors are not cached and downloads are disabled")
        provenance.update(
            {
                "cache_path": path,
                "from_cache": True,
                "first_date": str(cached.index[0].date()),
                "last_date": str(cached.index[-1].date()),
                "stale": True,
            }
        )
        return cached, provenance

    if session is None:
        import requests

        http: _Session = cast(_Session, requests.Session())
    else:
        http = session

    try:
        ff5_response = http.get(KEN_FRENCH_FF5_URL, timeout=timeout)
        ff5_response.raise_for_status()
        five = _parse_ken_french_csv(_read_zip(ff5_response.content))
        mom_response = http.get(KEN_FRENCH_MOM_URL, timeout=timeout)
        mom_response.raise_for_status()
        momentum = _parse_ken_french_csv(_read_zip(mom_response.content))
    except Exception as exc:  # noqa: BLE001 - any transport/parse failure falls back
        cached, path = load_cached_ken_french(cache_dir)
        if cached is None:
            raise RuntimeError(f"Ken French factor download failed: {exc}") from exc
        provenance.update(
            {
                "cache_path": path,
                "from_cache": True,
                "stale": True,
                "download_error": str(exc),
                "first_date": str(cached.index[0].date()),
                "last_date": str(cached.index[-1].date()),
            }
        )
        return cached, provenance

    frame = five.rename(
        columns={
            "Mkt-RF": "MKT",
            "SMB": "SMB",
            "HML": "HML",
            "RMW": "RMW",
            "CMA": "CMA",
            "RF": "RF",
        }
    )
    mom_column = next((name for name in momentum.columns if name.lower().startswith("mom")), None)
    if mom_column is not None:
        frame = frame.join(momentum[[mom_column]].rename(columns={mom_column: "MOM"}), how="left")
    ordered = [name for name in (*FACTOR_NAMES, "RF") if name in frame.columns]
    frame = frame[ordered].dropna(how="all")
    provenance["cache_path"] = _write_cache(frame, cache_dir)
    provenance["fetched_at"] = datetime.now(UTC).isoformat()
    provenance["first_date"] = str(frame.index[0].date())
    provenance["last_date"] = str(frame.index[-1].date())
    return frame, provenance


# --------------------------------------------------------------------------
# ETF proxy factors
# --------------------------------------------------------------------------

#: Ordered fallbacks for each proxy factor: (long leg, short leg or None).
_PROXY_RECIPES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "SMB": (("IWM", "SPY"),),
    "HML": (("IWD", "IWF"), ("VTV", "VUG"), ("XLF", "XLK")),
    "RMW": (("QUAL", "SPY"), ("XLP", "SPY")),
    "CMA": (("XLU", "XLI"), ("XLP", "XLY")),
    "MOM": (("MTUM", "SPY"),),
}


def _returns_frame(closes: Mapping[str, pd.Series]) -> pd.DataFrame:
    columns: dict[str, pd.Series] = {}
    for symbol, series in closes.items():
        clean = normalize_daily_index(pd.to_numeric(pd.Series(series), errors="coerce").dropna())
        clean = clean[clean > 0]
        if clean.shape[0] > 2:
            columns[str(symbol)] = clean.pct_change()
    if not columns:
        return pd.DataFrame()
    return pd.DataFrame(columns).sort_index()


def _cross_sectional_momentum(
    closes: Mapping[str, pd.Series], *, lookback: int = 252, skip: int = 21
) -> pd.Series | None:
    """12-1 cross-sectional momentum: top tercile minus bottom tercile, daily.

    Used only when no momentum ETF (``MTUM``) is in the supplied universe.
    Needs at least six symbols to make terciles meaningful.
    """
    prices = pd.DataFrame(
        {
            str(symbol): normalize_daily_index(pd.to_numeric(pd.Series(series), errors="coerce"))
            for symbol, series in closes.items()
        }
    ).sort_index()
    prices = prices.dropna(axis=1, thresh=lookback + skip + 40)
    if prices.shape[1] < 6:
        return None
    signal = prices.shift(skip) / prices.shift(lookback) - 1.0
    returns = prices.pct_change()
    ranks = signal.rank(axis=1, pct=True)
    top = returns.where(ranks >= 2.0 / 3.0)
    bottom = returns.where(ranks <= 1.0 / 3.0)
    factor = top.mean(axis=1) - bottom.mean(axis=1)
    factor = factor.dropna()
    return factor if factor.shape[0] > 60 else None


def etf_proxy_factors(
    closes: Mapping[str, pd.Series],
    *,
    risk_free_annual: pd.Series | float | None = None,
    market_symbol: str = "SPY",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Reconstruct factor-like return series from ETF closes.

    Definitions actually used are returned in the second element so the packet
    can state them verbatim:

    ==== ==========================================================
    MKT  ``SPY`` daily return minus the daily risk-free rate
    SMB  ``IWM - SPY`` (small minus large)
    HML  ``IWD - IWF``, else ``VTV - VUG``, else ``XLF - XLK``
    RMW  ``QUAL - SPY``, else ``XLP - SPY`` (profitability/quality)
    CMA  ``XLU - XLI``, else ``XLP - XLY`` (conservative vs aggressive investment)
    MOM  ``MTUM - SPY``, else a 12-1 cross-sectional top-minus-bottom tercile
    ==== ==========================================================

    These are *proxies*, not the Fama-French portfolios: they are long/short ETF
    spreads and inherit each ETF's sector tilts. Any factor whose ingredients are
    missing is simply omitted and listed under ``unavailable``.
    """
    returns = _returns_frame(closes)
    definitions: dict[str, str] = {}
    unavailable: dict[str, str] = {}
    columns: dict[str, pd.Series] = {}

    if market_symbol not in returns.columns:
        return pd.DataFrame(), {
            "model": "etf_proxy",
            "definitions": {},
            "unavailable": {"MKT": f"{market_symbol} closes missing"},
            "market_symbol": market_symbol,
        }

    if isinstance(risk_free_annual, pd.Series):
        daily_rf = (
            pd.to_numeric(risk_free_annual, errors="coerce").reindex(returns.index).ffill()
            / _TRADING_DAYS
        ).fillna(0.0)
        rf_note = "FRED annualised yield / 252"
    elif risk_free_annual is not None:
        daily_rf = pd.Series(float(risk_free_annual) / _TRADING_DAYS, index=returns.index)
        rf_note = f"constant {float(risk_free_annual):.4f} annual / 252"
    else:
        daily_rf = pd.Series(0.0, index=returns.index)
        rf_note = "zero (no risk-free series supplied)"

    columns["MKT"] = returns[market_symbol] - daily_rf
    definitions["MKT"] = f"{market_symbol} daily return minus daily risk-free ({rf_note})"
    columns["RF"] = daily_rf

    for factor, recipes in _PROXY_RECIPES.items():
        built = False
        for long_leg, short_leg in recipes:
            if long_leg in returns.columns and (short_leg is None or short_leg in returns.columns):
                series = returns[long_leg] - (returns[short_leg] if short_leg else 0.0)
                columns[factor] = series
                definitions[factor] = f"{long_leg} - {short_leg}" if short_leg else long_leg
                built = True
                break
        if built:
            continue
        if factor == "MOM":
            cross = _cross_sectional_momentum(closes)
            if cross is not None:
                columns["MOM"] = cross
                definitions["MOM"] = (
                    "cross-sectional 12-1 momentum: equal-weight top tercile minus bottom "
                    "tercile of the supplied ETF universe"
                )
                continue
        unavailable[factor] = "no ingredient ETFs present in the supplied universe"

    frame = pd.DataFrame(columns).dropna(how="all")
    ordered = [name for name in (*FACTOR_NAMES, "RF") if name in frame.columns]
    provenance = {
        "model": "etf_proxy",
        "definitions": definitions,
        "unavailable": unavailable,
        "market_symbol": market_symbol,
        "caveat": (
            "ETF long/short spreads standing in for the Fama-French research "
            "portfolios; sector composition differs, so betas are indicative."
        ),
        "first_date": str(frame.index[0].date()) if not frame.empty else None,
        "last_date": str(frame.index[-1].date()) if not frame.empty else None,
    }
    return frame[ordered], provenance


# --------------------------------------------------------------------------
# Regression
# --------------------------------------------------------------------------


def ols_with_stats(
    y: Sequence[float] | FloatArray,
    x: Sequence[Sequence[float]] | FloatArray,
    *,
    feature_names: Sequence[str],
) -> dict[str, Any]:
    """OLS of ``y`` on ``x`` with an intercept.

    Returns ``alpha``, ``betas``, ``t_stats`` (including ``alpha``), ``r2``,
    ``adj_r2``, ``residual_std``, ``n``, ``dof`` and the residual vector.
    Standard errors are the classical ``s^2 (X'X)^-1`` form.
    """
    target = np.asarray(y, dtype=np.float64).reshape(-1)
    design_raw = np.asarray(x, dtype=np.float64)
    if design_raw.ndim == 1:
        design_raw = design_raw.reshape(-1, 1)
    if design_raw.shape[0] != target.shape[0]:
        raise ValueError("y and x must have the same number of rows")
    names = list(feature_names)
    if len(names) != design_raw.shape[1]:
        raise ValueError("feature_names must match the number of regressors")

    n, k = design_raw.shape
    dof = n - k - 1
    if dof <= 0:
        raise ValueError(f"not enough observations ({n}) for {k} regressors")

    design = np.column_stack([np.ones(n, dtype=np.float64), design_raw])
    coefficients, *_ = np.linalg.lstsq(design, target, rcond=None)
    fitted = design @ coefficients
    residual = target - fitted
    sse = float(np.sum(residual**2))
    sst = float(np.sum((target - target.mean()) ** 2))
    sigma2 = sse / dof
    try:
        xtx_inv = np.linalg.pinv(design.T @ design)
        standard_errors = np.sqrt(np.clip(np.diag(xtx_inv) * sigma2, 0.0, None))
    except np.linalg.LinAlgError:  # pragma: no cover - pinv basically never fails
        standard_errors = np.full(k + 1, np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        t_values = np.where(standard_errors > 0, coefficients / standard_errors, np.nan)

    r2 = float(1.0 - sse / sst) if sst > 0 else float("nan")
    adj_r2 = float(1.0 - (1.0 - r2) * (n - 1) / dof) if math.isfinite(r2) else float("nan")
    return {
        "alpha": float(coefficients[0]),
        "betas": {name: float(value) for name, value in zip(names, coefficients[1:], strict=True)},
        "standard_errors": {
            "alpha": float(standard_errors[0]),
            **{
                name: float(value)
                for name, value in zip(names, standard_errors[1:], strict=True)
            },
        },
        "t_stats": {
            "alpha": float(t_values[0]) if math.isfinite(float(t_values[0])) else None,
            **{
                name: (float(value) if math.isfinite(float(value)) else None)
                for name, value in zip(names, t_values[1:], strict=True)
            },
        },
        "r2": r2 if math.isfinite(r2) else None,
        "adj_r2": adj_r2 if math.isfinite(adj_r2) else None,
        "residual_std": float(math.sqrt(sigma2)),
        "n": int(n),
        "dof": int(dof),
        "residuals": residual,
    }


def factor_regression(
    excess_returns: pd.Series,
    factors: pd.DataFrame,
    *,
    window_days: int | None = None,
    factor_names: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Regress ``excess_returns`` on ``factors`` over the trailing window.

    ``excess_returns`` must already be in excess of the risk-free rate.
    Returns the packet's per-window shape: ``alpha_annual``, ``betas``, ``r2``,
    ``residual_vol_annual``, ``t_stats``, ``n`` — plus the residual series for
    downstream cumulative-residual work.
    """
    names = list(factor_names) if factor_names else [
        name for name in FACTOR_NAMES if name in factors.columns
    ]
    aligned = pd.concat(
        [
            normalize_daily_index(pd.to_numeric(excess_returns, errors="coerce")).rename("y"),
            normalize_daily_index(factors[names]),
        ],
        axis=1,
    ).dropna()
    if window_days is not None and window_days > 0:
        aligned = aligned.iloc[-window_days:]
    if aligned.shape[0] < max(len(names) * 10, 30):
        return {
            "alpha_annual": None,
            "betas": dict.fromkeys(names),
            "r2": None,
            "residual_vol_annual": None,
            "t_stats": dict.fromkeys(names),
            "n": int(aligned.shape[0]),
            "error": (
                f"need at least {max(len(names) * 10, 30)} aligned observations, "
                f"got {aligned.shape[0]}"
            ),
            "residuals": pd.Series(dtype="float64"),
        }

    result = ols_with_stats(
        aligned["y"].to_numpy(dtype=np.float64),
        aligned[names].to_numpy(dtype=np.float64),
        feature_names=names,
    )
    residuals = pd.Series(result["residuals"], index=aligned.index, name="residual")
    return {
        "alpha_daily": result["alpha"],
        "alpha_annual": float(result["alpha"] * _TRADING_DAYS),
        "betas": result["betas"],
        "r2": result["r2"],
        "adj_r2": result["adj_r2"],
        "residual_vol_annual": float(result["residual_std"] * math.sqrt(_TRADING_DAYS)),
        "residual_vol_daily": float(result["residual_std"]),
        "t_stats": result["t_stats"],
        "standard_errors": result["standard_errors"],
        "n": result["n"],
        "factor_means": {
            name: float(aligned[name].mean()) for name in names
        },
        "factor_vols": {
            name: float(aligned[name].std(ddof=1)) for name in names
        },
        "start": str(pd.Timestamp(aligned.index[0]).date()),
        "end": str(pd.Timestamp(aligned.index[-1]).date()),
        "error": None,
        "residuals": residuals,
    }


def _cumulative(series: pd.Series, days: int) -> float | None:
    subset = series.dropna().iloc[-days:]
    if subset.shape[0] < max(days // 2, 5):
        return None
    return float(subset.sum())


def build_factors(
    ticker_close: pd.Series,
    *,
    factors: pd.DataFrame | None = None,
    proxy_closes: Mapping[str, pd.Series] | None = None,
    risk_free_annual: pd.Series | float | None = None,
    windows: Mapping[str, int] | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    allow_download: bool = True,
    session: _Session | None = None,
    source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the packet's ``factors`` section.

    Parameters
    ----------
    ticker_close:
        Daily closes for the analysed ticker.
    factors:
        Pre-built factor frame (columns among ``MKT SMB HML RMW CMA MOM RF``,
        decimal daily). When ``None`` the Ken French library is used, falling
        back to ETF proxies built from ``proxy_closes``.
    proxy_closes:
        Symbol -> daily closes, used for the ETF-proxy fallback.
    risk_free_annual:
        Annualised risk-free yield (e.g. FRED ``DGS2`` as a decimal, or a Series
        of them) used by the proxy path and to build excess returns when the
        factor frame carries no ``RF`` column.
    """
    window_map = dict(FACTOR_WINDOWS if windows is None else windows)
    section: dict[str, Any] = {
        "model": None,
        "source": dict(source) if source else {},
        "windows": dict.fromkeys(window_map),
        "residuals": {"last_20d_cum": None, "last_60d_cum": None, "z_score": None},
        "error": None,
    }

    prices = normalize_daily_index(pd.to_numeric(pd.Series(ticker_close), errors="coerce").dropna())
    prices = prices[prices > 0]
    if prices.shape[0] < 60:
        section["error"] = f"need at least 60 closes, got {prices.shape[0]}"
        return section
    returns = prices.pct_change().dropna()

    frame = factors
    provenance: dict[str, Any] = dict(source) if source else {}
    model = "fama_french_5_mom"
    if frame is None:
        try:
            frame, provenance = download_ken_french_factors(
                cache_dir=cache_dir, session=session, allow_download=allow_download
            )
        except (RuntimeError, ValueError) as exc:
            provenance = {"ken_french_error": str(exc)}
            frame = None
        if frame is None or frame.empty:
            if not proxy_closes:
                section["error"] = (
                    "Ken French factors unavailable and no proxy ETF closes supplied: "
                    f"{provenance.get('ken_french_error', 'unknown reason')}"
                )
                section["source"] = provenance
                return section
            frame, proxy_provenance = etf_proxy_factors(
                proxy_closes, risk_free_annual=risk_free_annual
            )
            provenance = {**provenance, **proxy_provenance}
            model = "etf_proxy"
    elif source and source.get("model"):
        model = str(source["model"])

    if frame is None or frame.empty:
        section["error"] = "no usable factor series"
        section["source"] = provenance
        return section

    frame = normalize_daily_index(frame)

    names = [name for name in FACTOR_NAMES if name in frame.columns]
    if not names:
        section["error"] = "factor frame carries none of the expected factor columns"
        section["source"] = provenance
        return section

    if "RF" in frame.columns:
        rf = frame["RF"].reindex(returns.index).ffill().fillna(0.0)
        rf_note = "risk-free from the factor source"
    elif isinstance(risk_free_annual, pd.Series):
        annual = normalize_daily_index(pd.to_numeric(risk_free_annual, errors="coerce"))
        rf = (annual.reindex(returns.index).ffill() / _TRADING_DAYS).fillna(0.0)
        rf_note = "annualised risk-free series / 252"
    elif risk_free_annual is not None:
        rf = pd.Series(float(risk_free_annual) / _TRADING_DAYS, index=returns.index)
        rf_note = "constant annual risk-free / 252"
    else:
        rf = pd.Series(0.0, index=returns.index)
        rf_note = "zero (no risk-free supplied)"
    excess = (returns - rf).dropna()

    section["model"] = model
    provenance = {
        **provenance,
        "model": model,
        "factors_used": names,
        "risk_free": rf_note,
        "factor_first_date": str(frame.index[0].date()),
        "factor_last_date": str(frame.index[-1].date()),
    }
    section["source"] = provenance

    # The Ken French library publishes with a lag, so the newest factor date can
    # be months behind the packet's as-of. Every window and the residual block
    # therefore end at `factor_last_date`, not today; say so in one number so
    # nothing downstream describes a two-month-old residual as "recent".
    factor_last = pd.Timestamp(frame.index[-1]).date()
    price_last = pd.Timestamp(prices.index[-1]).date()
    section["stale_days"] = int((price_last - factor_last).days)
    section["as_of"] = factor_last.isoformat()

    # Premia priced off the *full* factor history rather than the fitted window's
    # own means. With an intercept, alpha + sum(beta_i * xbar_i) == ybar by the
    # OLS normal equations, so pricing exposures with the fitted window's means
    # reproduces the ticker's trailing mean excess return exactly and says
    # nothing a factor model did not already assume.
    section["premia"] = {
        "daily": {name: float(frame[name].mean()) for name in names},
        "source": "full_sample_factor_means",
        "start": str(frame.index[0].date()),
        "end": str(frame.index[-1].date()),
        "n": int(frame.shape[0]),
        "note": (
            "Mean daily factor return over the whole published factor history, used "
            "to price the exposures. Deliberately not the fitted window's own means: "
            "those make the implied return an OLS identity for the ticker's own "
            "trailing mean."
        ),
    }

    longest_residuals: pd.Series | None = None
    longest_vol: float | None = None
    longest_label: str | None = None
    for label, days in window_map.items():
        result = factor_regression(excess, frame, window_days=days, factor_names=names)
        residuals = result.pop("residuals")
        section["windows"][label] = result
        if result.get("error") is None and residuals.shape[0] > 0:
            longest_residuals = residuals
            longest_vol = result.get("residual_vol_daily")
            longest_label = label

    if longest_residuals is not None and longest_residuals.shape[0] >= 60:
        cum20 = _cumulative(longest_residuals, 20)
        cum60 = _cumulative(longest_residuals, 60)
        z_score = None
        if cum20 is not None and longest_vol and longest_vol > 0:
            z_score = float(cum20 / (longest_vol * math.sqrt(20.0)))
        section["residuals"] = {
            "last_20d_cum": cum20,
            "last_60d_cum": cum60,
            "z_score": z_score,
            "window_used": longest_label,
            "residual_vol_daily": longest_vol,
            "as_of": str(pd.Timestamp(longest_residuals.index[-1]).date()),
            "note": (
                "Cumulative idiosyncratic return: the part of the move the factor "
                "model does not explain, summed over the trailing window."
            ),
        }
    else:
        section["residuals"]["reason"] = "no window produced enough residual observations"
    return section
