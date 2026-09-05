"""Ken French monthly factor panel for Situate's named-factor exposure view.

Situate's exposure module (SPEC 5.1) runs one regularised ridge on a tradable
basket *and* a separate OLS on the Fama-French factors for a named-factor read.
That second regression is monthly, so this module wraps Prism's existing daily
Ken French download (``app.prism.factors.download_ken_french_factors``) and
compounds each factor to calendar-month decimal returns.

Reuse, not rebuild: the download, parsing, on-disk cache and the missing-value
handling all live in Prism. This module adds only the daily -> monthly
compounding and the point-in-time (``as_of``) truncation.

Point-in-time discipline: every row returned is dated on or before ``as_of``, and
a calendar month that has not fully elapsed by ``as_of`` (fewer than
:data:`MIN_TRADING_DAYS_PER_MONTH` factor observations) is dropped so a partial
month never leaks into a monthly regression.
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any, Protocol, cast

import numpy as np
import pandas as pd

from app.prism.data import resolve_as_of
from app.prism.factors import FACTOR_NAMES, download_ken_french_factors

__all__ = [
    "FACTOR_NAMES",
    "MIN_TRADING_DAYS_PER_MONTH",
    "compound_to_monthly",
    "load_ken_french_monthly",
]

#: A calendar month with fewer factor observations than this is treated as still
#: in progress (or a data gap) and dropped, so a partial month never enters a
#: monthly regression.
MIN_TRADING_DAYS_PER_MONTH = 15

#: Factor return columns (decimal), plus the risk-free rate.
_RETURN_COLUMNS: tuple[str, ...] = (*FACTOR_NAMES, "RF")


class _Session(Protocol):
    def get(self, url: str, timeout: float = ...) -> Any: ...


def compound_to_monthly(daily: pd.DataFrame) -> pd.DataFrame:
    """Compound daily decimal factor returns into calendar-month returns.

    Each column is treated as a decimal simple return, so the monthly return of a
    month is ``prod(1 + r_day) - 1`` over that month's observations. The index of
    the result is the month-end timestamp. Months with fewer than
    :data:`MIN_TRADING_DAYS_PER_MONTH` observations are dropped.
    """
    if daily is None or daily.empty:
        return pd.DataFrame()
    frame = daily.copy()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index)).normalize()
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    columns = [name for name in _RETURN_COLUMNS if name in frame.columns]
    if not columns:
        return pd.DataFrame()
    frame = frame[columns].apply(pd.to_numeric, errors="coerce")

    grouped = frame.groupby(frame.index.to_period("M"))
    counts = grouped.size()
    compounded = grouped.apply(lambda block: (1.0 + block).prod(min_count=1) - 1.0)
    if isinstance(compounded, pd.Series):  # single-column edge case
        compounded = compounded.to_frame()
    # `min_count=1` keeps a month NaN when a factor had no observations in it.
    keep = counts[counts >= MIN_TRADING_DAYS_PER_MONTH].index
    monthly = compounded.loc[compounded.index.isin(keep)]
    monthly.index = monthly.index.to_timestamp(how="end").normalize()
    monthly = monthly[[c for c in _RETURN_COLUMNS if c in monthly.columns]]
    return monthly.sort_index()


def load_ken_french_monthly(
    *,
    as_of: date | str | None = None,
    cache_dir: str | os.PathLike[str] | None = None,
    allow_download: bool = True,
    session: _Session | None = None,
    years: int = 20,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Return ``(monthly_factors, provenance)`` filtered to ``as_of``.

    ``monthly_factors`` is indexed by month-end with columns among
    ``MKT SMB HML RMW CMA MOM RF`` as decimal monthly returns. ``provenance``
    carries the underlying daily download's provenance plus the monthly date
    range and the number of months, and a ``monthly_note`` describing the
    compounding.

    Nothing here raises for a data outage: when the download and the cache both
    fail, an empty frame is returned with the reason in ``provenance``.
    """
    end = resolve_as_of(as_of)
    provenance: dict[str, Any] = {
        "provider": "ken_french_data_library",
        "frequency": "monthly (compounded from daily)",
        "as_of": end.isoformat(),
    }
    try:
        daily, download_prov = download_ken_french_factors(
            cache_dir=cache_dir, session=session, allow_download=allow_download
        )
    except (RuntimeError, ValueError) as exc:
        provenance["error"] = str(exc)
        return pd.DataFrame(), provenance

    provenance.update(
        {
            "urls": download_prov.get("urls"),
            "cache_path": download_prov.get("cache_path"),
            "from_cache": download_prov.get("from_cache"),
            "daily_first_date": download_prov.get("first_date"),
            "daily_last_date": download_prov.get("last_date"),
        }
    )
    if daily is None or daily.empty:
        provenance["error"] = "Ken French download returned no rows"
        return pd.DataFrame(), provenance

    daily = daily.copy()
    daily.index = pd.DatetimeIndex(pd.to_datetime(daily.index)).normalize()
    daily = daily[daily.index <= pd.Timestamp(end)]
    if years and years > 0:
        cutoff = pd.Timestamp(end) - pd.DateOffset(years=int(years))
        daily = daily[daily.index >= cutoff]
    monthly = compound_to_monthly(daily)
    monthly = monthly[monthly.index <= pd.Timestamp(end)]

    provenance["monthly_note"] = (
        "Daily Ken French factor returns compounded within each calendar month; "
        f"months with fewer than {MIN_TRADING_DAYS_PER_MONTH} factor observations "
        "are dropped so a partial month never enters a regression."
    )
    if monthly.empty:
        provenance["error"] = "no complete months on or before as_of"
        provenance["n_months"] = 0
        return monthly, provenance
    provenance["first_month"] = str(cast(pd.Timestamp, monthly.index[0]).date())
    provenance["last_month"] = str(cast(pd.Timestamp, monthly.index[-1]).date())
    provenance["n_months"] = int(monthly.shape[0])
    provenance["columns"] = list(monthly.columns)
    provenance["stale_days"] = int(
        (end - cast(pd.Timestamp, monthly.index[-1]).date()).days
    )
    return monthly.astype(np.float64), provenance
