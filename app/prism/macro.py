"""FRED macro series and the packet's ``macro`` section.

Massive returns 403 for index tickers (``I:VIX`` included), so every rate, spread
and volatility *level* comes from FRED and every tradable proxy (GLD, BTC) comes
from Massive. Each series is compressed to a :class:`~app.prism.contract.MacroSeries`:
the current level, changes over 1/3/12 months, and the last twelve monthly
observations, all with the FRED series id attached so a memo can cite it.

Level series (yields, spreads, VIX) report *differences* — a 10-year yield going
from 4.10 to 4.35 is ``+0.25``, not ``+6.1%`` — while price series (gold, oil,
the dollar index, bitcoin) report fractional changes. Which convention applies is
stated in each series' ``change_mode`` field so nothing downstream has to guess.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
import requests

from app.prism.cache import (
    MACRO_NAMESPACE,
    PrismCache,
    as_of_month,
    payload_to_series,
    series_to_payload,
)
from app.prism.contract import MacroSeries, MonthlyPoint, empty_macro_series
from app.prism.data import finite, resolve_as_of

DEFAULT_FRED_BASE_URL = "https://api.stlouisfed.org/fred"
FRED_OBSERVATIONS_PATH = "/series/observations"
DEFAULT_TIMEOUT = 20.0
DEFAULT_YEARS = 12


class FredError(RuntimeError):
    """A FRED request failed or returned no usable observations."""


#: FRED authenticates by query string, so a transport exception's message embeds
#: the full URL — key included. Every exception that becomes packet text goes
#: through this first. Mirrors ``app/massive.py``'s ``apiKey`` stripping.
_SECRET_QUERY_KEYS = ("api_key", "apikey", "token", "access_token")
_SECRET_QUERY_RE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_QUERY_KEYS) + r")=[^&\s'\"]+"
)


def scrub_secrets(value: Any) -> str:
    """``str(value)`` with any ``api_key=...`` query parameter redacted.

    The packet, ``meta.unavailable``, the stored JSON, Supabase and the shareable
    txt/PDF export all carry FRED failure reasons verbatim, so a raw
    ``requests`` exception must never reach them.
    """
    text = str(value)
    return _SECRET_QUERY_RE.sub(lambda match: f"{match.group(1)}=***", text)


class FredSeriesSpec:
    """Static metadata for one FRED series (label, units, change convention)."""

    __slots__ = ("series_id", "label", "units", "change_mode", "frequency")

    def __init__(
        self,
        series_id: str,
        label: str,
        *,
        units: str,
        change_mode: str = "diff",
        frequency: str = "daily",
    ) -> None:
        self.series_id = series_id
        self.label = label
        self.units = units
        self.change_mode = change_mode
        self.frequency = frequency


FRED_SERIES: dict[str, FredSeriesSpec] = {
    spec.series_id: spec
    for spec in (
        FredSeriesSpec("DGS2", "2-Year Treasury Constant Maturity", units="percent"),
        FredSeriesSpec("DGS5", "5-Year Treasury Constant Maturity", units="percent"),
        FredSeriesSpec("DGS10", "10-Year Treasury Constant Maturity", units="percent"),
        FredSeriesSpec("DGS20", "20-Year Treasury Constant Maturity", units="percent"),
        FredSeriesSpec("T10Y2Y", "10-Year minus 2-Year Treasury Spread", units="percent"),
        FredSeriesSpec("VIXCLS", "CBOE Volatility Index (close)", units="index"),
        FredSeriesSpec(
            "BAMLH0A0HYM2",
            "ICE BofA US High Yield Option-Adjusted Spread",
            units="percent",
        ),
        FredSeriesSpec(
            "DTWEXBGS",
            "Nominal Broad US Dollar Index",
            units="index (Jan 2006 = 100)",
            change_mode="pct",
        ),
        FredSeriesSpec(
            "DCOILWTICO", "WTI Crude Oil Spot", units="USD per barrel", change_mode="pct"
        ),
        FredSeriesSpec(
            "DCOILBRENTEU", "Brent Crude Oil Spot", units="USD per barrel", change_mode="pct"
        ),
        FredSeriesSpec(
            "PAYEMS",
            "All Employees, Total Nonfarm",
            units="thousands of persons",
            frequency="monthly",
        ),
        FredSeriesSpec("DEXJPUS", "Yen per USD", units="JPY/USD", change_mode="pct"),
        FredSeriesSpec("DEXUSEU", "USD per Euro", units="USD/EUR", change_mode="pct"),
        FredSeriesSpec("DEXCHUS", "Yuan per USD", units="CNY/USD", change_mode="pct"),
        FredSeriesSpec("DEXSZUS", "Swiss Franc per USD", units="CHF/USD", change_mode="pct"),
        FredSeriesSpec("DEXCAUS", "Canadian Dollar per USD", units="CAD/USD", change_mode="pct"),
        FredSeriesSpec("DEXUSAL", "USD per Australian Dollar", units="USD/AUD", change_mode="pct"),
    )
}

#: Yield-curve members of the packet's ``macro.yields`` block, short end first.
YIELD_SERIES: tuple[str, ...] = ("DGS2", "DGS5", "DGS10", "DGS20", "T10Y2Y")

#: ``macro.fx`` keys mapped onto their FRED series. Quote direction matters, so
#: each entry keeps the FRED convention and states it in the label.
FX_SERIES: dict[str, str] = {
    "JPY": "DEXJPUS",
    "EUR": "DEXUSEU",
    "CNY": "DEXCHUS",
    "CHF": "DEXSZUS",
    "CAD": "DEXCAUS",
    "AUD": "DEXUSAL",
}

#: Massive-sourced members of the macro block (FRED has no tradable proxy).
MASSIVE_MACRO_SYMBOLS: dict[str, str] = {"gold": "GLD", "btc": "X:BTCUSD"}


class FredClient:
    """Minimal FRED observations client (requests + retries, no SDK)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 2,
    ) -> None:
        if not str(api_key or "").strip():
            raise FredError("FRED_API_KEY is required")
        self.api_key = str(api_key).strip()
        self.base_url = (base_url or os.getenv("FRED_BASE_URL") or DEFAULT_FRED_BASE_URL).rstrip(
            "/"
        )
        self.session = session or requests.Session()
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))

    @classmethod
    def from_env(cls, *, session: requests.Session | None = None) -> FredClient | None:
        """Build from ``FRED_API_KEY``; ``None`` when the key is not configured."""
        key = os.getenv("FRED_API_KEY", "").strip()
        if not key:
            return None
        return cls(key, session=session)

    def get_series(
        self,
        series_id: str,
        *,
        start: date | str | None = None,
        end: date | str | None = None,
    ) -> pd.Series:
        """Fetch one FRED series as a date-indexed float series.

        FRED writes missing observations as ``"."``; those rows are dropped
        rather than forward-filled, so a memo never quotes an invented level.
        """
        params: dict[str, Any] = {
            "series_id": str(series_id).strip().upper(),
            "api_key": self.api_key,
            "file_type": "json",
        }
        if start is not None:
            params["observation_start"] = _iso_date(start)
        if end is not None:
            params["observation_end"] = _iso_date(end)
        payload = self._request(FRED_OBSERVATIONS_PATH, params=params)
        observations = payload.get("observations")
        if not isinstance(observations, list) or not observations:
            raise FredError(f"FRED returned no observations for {series_id}")
        dates: list[str] = []
        values: list[float] = []
        for row in observations:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("value", "")).strip()
            if not raw or raw == ".":
                continue
            try:
                values.append(float(raw))
            except ValueError:
                continue
            dates.append(str(row.get("date")))
        if not values:
            raise FredError(f"FRED returned only missing values for {series_id}")
        series = pd.Series(values, index=pd.to_datetime(pd.Index(dates)), dtype="float64")
        series.name = str(series_id).strip().upper()
        return series[~series.index.duplicated(keep="last")].sort_index()

    def _request(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                # Never carry the exception object forward: its message embeds the
                # request URL, and FRED puts the API key in the query string.
                last_error = FredError(f"transport error ({type(exc).__name__})")
                continue
            status = getattr(response, "status_code", 200)
            if status == 429 or status >= 500:
                last_error = FredError(f"FRED responded {status}")
                if attempt < self.max_retries:
                    continue
            if status >= 400:
                raise FredError(f"FRED request failed with status {status}")
            try:
                payload = response.json()
            except ValueError as exc:
                raise FredError("FRED returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise FredError("FRED returned a non-object response")
            return payload
        raise FredError(f"FRED request failed: {scrub_secrets(last_error)}")


def fetch_fred_series(
    fred: FredClient,
    series_id: str,
    *,
    years: int = DEFAULT_YEARS,
    as_of: date | str | None = None,
    cache: PrismCache | None = None,
) -> pd.Series:
    """:meth:`FredClient.get_series` with the ``(series_id, as-of month)`` cache."""
    resolved_id = str(series_id).strip().upper()
    end = resolve_as_of(as_of)
    generation = as_of_month(end)
    if cache is not None:
        entry = cache.get(MACRO_NAMESPACE, resolved_id, generation=generation)
        if isinstance(entry, dict) and isinstance(entry.get("payload"), dict):
            try:
                cached = payload_to_series(entry["payload"], name=resolved_id)
            except Exception:
                cached = pd.Series(dtype="float64")
            # The row is keyed by calendar month; only reuse it for an as-of date
            # it was already built for, so a build later in the month still sees
            # the observations FRED published since. Same-day rebuilds stay free.
            cached_as_of = str(entry.get("as_of") or "")
            if not cached.empty and cached_as_of and cached_as_of >= end.isoformat():
                return cached[cached.index <= pd.Timestamp(end)]
    start = end - timedelta(days=int(365.25 * max(1, years)))
    series = fred.get_series(resolved_id, start=start, end=end)
    if cache is not None:
        cache.set(
            MACRO_NAMESPACE,
            resolved_id,
            {
                "provider": "fred",
                "series_id": resolved_id,
                "as_of": end.isoformat(),
                "payload": series_to_payload(series),
            },
            generation=generation,
        )
    return series


def value_as_of(series: pd.Series, when: pd.Timestamp | date) -> float | None:
    """The last observation on or before ``when`` (``None`` when there is none)."""
    if series.empty:
        return None
    stamp = pd.Timestamp(when)
    window = series[series.index <= stamp]
    if window.empty:
        return None
    return finite(window.iloc[-1])


def monthly_points(
    series: pd.Series,
    *,
    months: int = 12,
    change_mode: str = "diff",
    as_of: date | str | None = None,
) -> list[MonthlyPoint]:
    """The last ``months`` calendar months as ``{month, value, avg, change}``.

    ``value`` is the month's final observation, ``avg`` its mean, and ``change``
    the month-over-month move of ``value`` under ``change_mode``.
    """
    if series.empty:
        return []
    end = pd.Timestamp(resolve_as_of(as_of))
    trimmed = series[series.index <= end]
    if trimmed.empty:
        return []
    grouped = trimmed.groupby(trimmed.index.to_period("M"))
    last = grouped.last()
    mean = grouped.mean()
    periods = list(last.index)[-max(1, months) :]
    points: list[MonthlyPoint] = []
    previous: float | None = None
    start_index = len(last) - len(periods)
    if start_index > 0:
        previous = finite(last.iloc[start_index - 1])
    for period in periods:
        value = finite(last.loc[period])
        points.append(
            MonthlyPoint(
                month=str(period),
                value=value,
                avg=finite(mean.loc[period]),
                change=_change(value, previous, change_mode),
            )
        )
        previous = value if value is not None else previous
    return points


def build_macro_series(
    series_id: str,
    series: pd.Series,
    *,
    provider: str = "fred",
    label: str | None = None,
    units: str | None = None,
    change_mode: str | None = None,
    as_of: date | str | None = None,
    months: int = 12,
) -> MacroSeries:
    """Compress a raw level/price series into the packet's ``MacroSeries`` shape."""
    spec = FRED_SERIES.get(str(series_id).strip().upper())
    resolved_mode = change_mode or (spec.change_mode if spec else "diff")
    payload = empty_macro_series(
        series_id,
        provider=provider,
        label=label or (spec.label if spec else series_id),
        units=units if units is not None else (spec.units if spec else None),
        change_mode=resolved_mode,
    )
    if series is None or series.empty:
        payload["error"] = "series has no observations"
        return payload
    end = pd.Timestamp(resolve_as_of(as_of))
    trimmed = series[series.index <= end].dropna()
    if trimmed.empty:
        payload["error"] = f"no observations on or before {end.date().isoformat()}"
        return payload
    current = finite(trimmed.iloc[-1])
    last_stamp = pd.Timestamp(trimmed.index[-1])
    payload["current"] = current
    payload["as_of"] = last_stamp.date().isoformat()
    payload["n_observations"] = int(len(trimmed))
    for label_key, month_offset in (("change_1m", 1), ("change_3m", 3), ("change_12m", 12)):
        past = value_as_of(trimmed, last_stamp - pd.DateOffset(months=month_offset))
        payload[label_key] = _change(current, past, resolved_mode)  # type: ignore[literal-required]
    payload["monthly_12"] = monthly_points(
        trimmed, months=months, change_mode=resolved_mode, as_of=end
    )
    return payload


def curve_shape(yields: dict[str, MacroSeries]) -> dict[str, Any]:
    """2s10s / 5s20s spreads and a plain-English label for the curve."""
    two = _current(yields.get("DGS2"))
    five = _current(yields.get("DGS5"))
    ten = _current(yields.get("DGS10"))
    twenty = _current(yields.get("DGS20"))
    twos_tens = ten - two if ten is not None and two is not None else None
    fives_twenties = twenty - five if twenty is not None and five is not None else None
    if twos_tens is None:
        label = "unknown"
    elif twos_tens < -0.05:
        label = "inverted"
    elif twos_tens < 0.25:
        label = "flat"
    elif twos_tens < 1.5:
        label = "normal"
    else:
        label = "steep"
    return {
        "2s10s": finite(twos_tens),
        "5s20s": finite(fives_twenties),
        "label": label,
        "components": {"DGS2": two, "DGS5": five, "DGS10": ten, "DGS20": twenty},
    }


def build_macro_section(
    fred: FredClient | None,
    *,
    market_client: Any | None = None,
    as_of: date | str | None = None,
    cache: PrismCache | None = None,
    years: int = DEFAULT_YEARS,
    max_workers: int = 6,
) -> dict[str, Any]:
    """Build ``packet["macro"]`` from FRED (levels) and Massive (gold, bitcoin).

    Nothing here raises: a missing FRED key, a dead series or a Massive outage
    each produce a ``MacroSeries`` carrying an ``error`` string, and the section's
    ``unavailable`` list names what could not be fetched.
    """
    end = resolve_as_of(as_of)
    unavailable: list[dict[str, str]] = []
    fetched: dict[str, pd.Series] = {}
    failures: dict[str, str] = {}

    wanted = list(
        dict.fromkeys(
            [
                *YIELD_SERIES,
                "VIXCLS",
                "BAMLH0A0HYM2",
                "DTWEXBGS",
                "DCOILWTICO",
                "DCOILBRENTEU",
                "PAYEMS",
                *FX_SERIES.values(),
            ]
        )
    )

    if fred is None:
        for series_id in wanted:
            failures[series_id] = "FRED_API_KEY is not configured"
        unavailable.append({"source": "fred", "reason": "FRED_API_KEY is not configured"})
    else:
        def _fetch(series_id: str) -> tuple[str, pd.Series | None, str | None]:
            try:
                return series_id, fetch_fred_series(
                    fred, series_id, years=years, as_of=end, cache=cache
                ), None
            except Exception as exc:
                # ``str(exc)`` on a transport failure can carry the request URL,
                # and the reason lands in the packet, the export and Supabase.
                return series_id, None, scrub_secrets(exc)

        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(wanted)))) as pool:
            for series_id, series, error in pool.map(_fetch, wanted):
                if series is None:
                    failures[series_id] = error or "unknown error"
                else:
                    fetched[series_id] = series

    def _series(series_id: str, *, months: int = 12) -> MacroSeries:
        if series_id in fetched:
            return build_macro_series(series_id, fetched[series_id], as_of=end, months=months)
        spec = FRED_SERIES.get(series_id)
        return empty_macro_series(
            series_id,
            provider="fred",
            label=spec.label if spec else series_id,
            units=spec.units if spec else None,
            change_mode=spec.change_mode if spec else "diff",
            error=failures.get(series_id, "series was not requested"),
        )

    yields = {series_id: _series(series_id) for series_id in YIELD_SERIES}
    vix = _series("VIXCLS")
    vix_monthly = [
        {"month": point["month"], "avg": point["avg"], "change": point["change"]}
        for point in vix.get("monthly_12", [])
    ]
    section: dict[str, Any] = {
        "as_of": end.isoformat(),
        "yields": yields,
        "curve_shape": curve_shape(yields),
        "vix": {**vix, "monthly": vix_monthly},
        "hy_spread": _series("BAMLH0A0HYM2"),
        "dollar": _series("DTWEXBGS"),
        "wti": _series("DCOILWTICO"),
        "brent": _series("DCOILBRENTEU"),
        "nfp": _series("PAYEMS"),
        "fx": {key: _series(series_id) for key, series_id in FX_SERIES.items()},
        "gold": empty_macro_series(
            "GLD",
            provider="massive",
            label="SPDR Gold Shares",
            units="USD",
            change_mode="pct",
            error="market client not provided",
        ),
        "btc": empty_macro_series(
            "X:BTCUSD",
            provider="massive",
            label="Bitcoin (USD)",
            units="USD",
            change_mode="pct",
            error="market client not provided",
        ),
        "unavailable": unavailable,
    }

    if market_client is not None:
        for key, symbol in MASSIVE_MACRO_SYMBOLS.items():
            section[key] = massive_macro_series(
                market_client,
                symbol,
                label="SPDR Gold Shares" if key == "gold" else "Bitcoin (USD)",
                as_of=end,
                cache=cache,
                years=min(years, 10),
            )
    else:
        unavailable.append(
            {"source": "massive", "reason": "market client not provided to build_macro_section"}
        )

    for series_id, error in failures.items():
        unavailable.append({"source": f"fred:{series_id}", "reason": error})
    return section


def massive_macro_series(
    market_client: Any,
    symbol: str,
    *,
    label: str,
    as_of: date | str | None = None,
    cache: PrismCache | None = None,
    years: int = 10,
) -> MacroSeries:
    """A ``MacroSeries`` built from Massive daily closes (gold, bitcoin)."""
    from app.prism.data import load_daily

    try:
        load = load_daily(market_client, symbol, years=years, as_of=as_of, cache=cache)
    except Exception as exc:
        return empty_macro_series(
            symbol,
            provider="massive",
            label=label,
            units="USD",
            change_mode="pct",
            error=scrub_secrets(exc)[:200],
        )
    payload = build_macro_series(
        symbol,
        load.series,
        provider=load.provider,
        label=label,
        units="USD",
        change_mode="pct",
        as_of=as_of,
    )
    return payload


def macro_snapshot(section: dict[str, Any]) -> dict[str, Any]:
    """One-line-per-series summary of a macro section, for memo projections."""
    summary: dict[str, Any] = {}
    for key in ("vix", "hy_spread", "dollar", "wti", "brent", "gold", "btc", "nfp"):
        entry = section.get(key)
        if isinstance(entry, dict):
            summary[key] = {
                "series_id": entry.get("series_id"),
                "current": entry.get("current"),
                "change_1m": entry.get("change_1m"),
                "change_12m": entry.get("change_12m"),
                "change_mode": entry.get("change_mode"),
                "error": entry.get("error"),
            }
    yields = section.get("yields")
    if isinstance(yields, dict):
        summary["yields"] = {
            series_id: entry.get("current")
            for series_id, entry in yields.items()
            if isinstance(entry, dict)
        }
    summary["curve_shape"] = section.get("curve_shape")
    return summary


def _current(payload: MacroSeries | None) -> float | None:
    if not isinstance(payload, dict):
        return None
    return finite(payload.get("current"))


def _change(current: float | None, past: float | None, mode: str) -> float | None:
    if current is None or past is None:
        return None
    if mode == "pct":
        if past == 0:
            return None
        return finite((current - past) / abs(past))
    return finite(current - past)


def _iso_date(value: date | str) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def fred_client_from_env(session: requests.Session | None = None) -> FredClient | None:
    """Module-level convenience wrapper around :meth:`FredClient.from_env`."""
    return FredClient.from_env(session=session)


def utc_today() -> date:
    """Today's date in UTC (the engine's default ``as_of``)."""
    return datetime.now(UTC).date()
