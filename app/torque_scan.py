"""Torque Scan — multi-ticker Torque + Reclassification scoring.

Given a list of tickers (or a TradingView watchlist URL), compute the cockpit
row with Torque + Reclassification for each ticker in parallel. Provides both
a blocking ``build_torque_scan_response`` API and a streaming NDJSON variant
(``stream_torque_scan_rows``) so a UI can render rows progressively.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.cockpit import build_cockpit_row
from app.market_data import MarketDataError, clean_ticker
from app.watchlists import WatchlistError, WatchlistResult, watchlist_payload

DEFAULT_PERIOD: str = "1y"
DEFAULT_MAX_RESULTS: int = 10
MAX_RESULTS_CAP: int = 50
SCAN_MAX_WORKERS: int = 8

VALID_SORTS: frozenset[str] = frozenset({"score_desc", "score_asc", "ticker", "rank"})


@dataclass(frozen=True)
class TorqueScanFilter:
    """Filter / sort configuration for a Torque Scan."""

    stage_labels: list[str] | None = None
    min_score: float | None = None
    max_score: float | None = None
    sort_by: str = "score_desc"
    limit: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage_labels": list(self.stage_labels) if self.stage_labels else None,
            "min_score": self.min_score,
            "max_score": self.max_score,
            "sort_by": self.sort_by,
            "limit": self.limit,
        }


@dataclass
class _ScanContext:
    tickers: list[str]
    watchlist: WatchlistResult | None
    period: str
    filter: TorqueScanFilter


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_torque_scan_response(
    market_client: Any,
    watchlist_client: Any,
    payload: dict[str, Any],
    *,
    sec_client: Any | None = None,
    exa_client: Any | None = None,
) -> dict[str, Any]:
    """Run a Torque Scan for every ticker, then return the filtered/sorted result."""

    context = _build_context(payload, watchlist_client)
    rows, errors = _collect_rows(
        market_client,
        context.tickers,
        period=context.period,
        sec_client=sec_client,
        exa_client=exa_client,
    )

    final_rows, stage_counts = _finalize_rows(rows, context.filter)
    meta = _build_meta(
        watchlist=context.watchlist,
        all_rows=rows,
        final_rows=final_rows,
        errors=errors,
        filter_obj=context.filter,
        stage_counts=stage_counts,
        period=context.period,
    )
    export = _build_export(
        rows=final_rows,
        meta=meta,
        watchlist=context.watchlist,
    )
    return {
        "rows": final_rows,
        "provider": _aggregate_provider(final_rows or rows),
        "provider_note": "Torque scan",
        "meta": meta,
        "watchlist": watchlist_payload(context.watchlist),
        "export": export,
    }


def stream_torque_scan_rows(
    market_client: Any,
    watchlist_client: Any,
    payload: dict[str, Any],
    *,
    sec_client: Any | None = None,
    exa_client: Any | None = None,
) -> Iterator[str]:
    """Yield NDJSON events for a Torque Scan as each ticker completes.

    Event types:
        meta  -- emitted first; describes the scan
        row   -- one per ticker that succeeds
        error -- one per ticker that fails (scan continues)
        done  -- emitted last; carries the final sorted/filtered list
    """

    try:
        context = _build_context(payload, watchlist_client)
    except (ValueError, WatchlistError) as exc:
        yield _ndjson(
            {"type": "error", "ticker": None, "index": -1, "error": str(exc)}
        )
        yield _ndjson(
            {
                "type": "done",
                "meta": {
                    "watchlist_name": None,
                    "result_count": 0,
                    "error_count": 1,
                    "filter": TorqueScanFilter().to_dict(),
                    "stage_counts": {},
                },
                "rows_sorted": [],
                "export": {},
            }
        )
        return

    yield _ndjson(
        {
            "type": "meta",
            "tickers": list(context.tickers),
            "watchlist": watchlist_payload(context.watchlist),
            "filter": context.filter.to_dict(),
            "total": len(context.tickers),
            "period": context.period,
        }
    )

    collected_rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    if not context.tickers:
        meta = _build_meta(
            watchlist=context.watchlist,
            all_rows=collected_rows,
            final_rows=collected_rows,
            errors=errors,
            filter_obj=context.filter,
            stage_counts={},
            period=context.period,
        )
        export = _build_export(
            rows=collected_rows,
            meta=meta,
            watchlist=context.watchlist,
        )
        yield _ndjson(
            {
                "type": "done",
                "meta": meta,
                "rows_sorted": [],
                "export": export,
            }
        )
        return

    worker_count = max(1, min(SCAN_MAX_WORKERS, len(context.tickers)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _safe_build_row,
                market_client,
                ticker,
                context.period,
                sec_client,
                exa_client,
            ): (index, ticker)
            for index, ticker in enumerate(context.tickers)
        }
        for future in as_completed(futures):
            index, ticker = futures[future]
            row, error = future.result()
            if row is not None:
                collected_rows.append(row)
                yield _ndjson(
                    {
                        "type": "row",
                        "ticker": ticker,
                        "index": index,
                        "row": row,
                    }
                )
            else:
                errors.append({"ticker": ticker, "error": error or "unknown error"})
                yield _ndjson(
                    {
                        "type": "error",
                        "ticker": ticker,
                        "index": index,
                        "error": error or "unknown error",
                    }
                )

    final_rows, stage_counts = _finalize_rows(collected_rows, context.filter)
    meta = _build_meta(
        watchlist=context.watchlist,
        all_rows=collected_rows,
        final_rows=final_rows,
        errors=errors,
        filter_obj=context.filter,
        stage_counts=stage_counts,
        period=context.period,
    )
    export = _build_export(rows=final_rows, meta=meta, watchlist=context.watchlist)

    yield _ndjson(
        {
            "type": "done",
            "meta": meta,
            "rows_sorted": final_rows,
            "export": export,
        }
    )


# ---------------------------------------------------------------------------
# Context + payload helpers
# ---------------------------------------------------------------------------


def _build_context(
    payload: dict[str, Any], watchlist_client: Any
) -> _ScanContext:
    period = str(payload.get("period") or DEFAULT_PERIOD)
    limit = _max_results(payload)
    watchlist_url = str(payload.get("watchlist_url") or "").strip()
    watchlist: WatchlistResult | None = None
    if watchlist_url:
        watchlist = watchlist_client.get_watchlist(watchlist_url)
        tickers = watchlist.tickers[:limit]
    else:
        tickers = _ticker_list(payload)[:limit]
    filter_obj = _build_filter(payload)
    return _ScanContext(
        tickers=tickers,
        watchlist=watchlist,
        period=period,
        filter=filter_obj,
    )


def _ticker_list(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("tickers") or payload.get("ticker")
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        parts = [part.strip().upper() for part in raw.split(",")]
    else:
        parts = [str(part).strip().upper() for part in raw]
    cleaned: list[str] = []
    for part in parts:
        if not part:
            continue
        cleaned.append(clean_ticker(part))
    return cleaned


def _max_results(payload: dict[str, Any]) -> int:
    raw = payload.get("max_results") or DEFAULT_MAX_RESULTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RESULTS
    return max(1, min(value, MAX_RESULTS_CAP))


def _build_filter(payload: dict[str, Any]) -> TorqueScanFilter:
    raw_filter = payload.get("filter")
    if not isinstance(raw_filter, dict):
        raw_filter = {}

    stage_labels_raw = raw_filter.get("stage_labels")
    stage_labels: list[str] | None = None
    if isinstance(stage_labels_raw, list) and stage_labels_raw:
        stage_labels = [str(label) for label in stage_labels_raw if str(label).strip()]
        if not stage_labels:
            stage_labels = None

    sort_by = str(raw_filter.get("sort_by") or "score_desc")
    if sort_by not in VALID_SORTS:
        sort_by = "score_desc"

    limit = raw_filter.get("limit")
    try:
        limit_int: int | None = int(limit) if limit is not None else None
    except (TypeError, ValueError):
        limit_int = None
    if limit_int is not None and limit_int <= 0:
        limit_int = None

    return TorqueScanFilter(
        stage_labels=stage_labels,
        min_score=_opt_float(raw_filter.get("min_score")),
        max_score=_opt_float(raw_filter.get("max_score")),
        sort_by=sort_by,
        limit=limit_int,
    )


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Row collection
# ---------------------------------------------------------------------------


def _collect_rows(
    market_client: Any,
    tickers: list[str],
    *,
    period: str,
    sec_client: Any | None,
    exa_client: Any | None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not tickers:
        return [], []
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    worker_count = max(1, min(SCAN_MAX_WORKERS, len(tickers)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(
                _safe_build_row,
                market_client,
                ticker,
                period,
                sec_client,
                exa_client,
            ): ticker
            for ticker in tickers
        }
        for future in as_completed(futures):
            ticker = futures[future]
            row, error = future.result()
            if row is not None:
                rows.append(row)
            else:
                errors.append({"ticker": ticker, "error": error or "unknown error"})
    return rows, errors


def _safe_build_row(
    market_client: Any,
    ticker: str,
    period: str,
    sec_client: Any | None,
    exa_client: Any | None,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        cockpit_row = build_cockpit_row(
            market_client,
            ticker,
            period=period,
            sec_client=sec_client,
            exa_client=exa_client,
            include_torque=True,
        )
    except (ValueError, MarketDataError) as exc:
        return None, str(exc)
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"unexpected error: {exc}"
    return _trim_to_scan_row(cockpit_row), None


def _trim_to_scan_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project a cockpit row down to the torque-scan-relevant fields."""

    summary = row.get("summary") or {}
    ridge = row.get("ridge") or {}
    flow = row.get("flow") or {}
    torque = row.get("torque")
    reclassification = row.get("reclassification")

    total_score = _opt_float(row.get("score"))

    return {
        "ticker": row.get("ticker"),
        "name": row.get("name") or summary.get("name"),
        "sector": row.get("sector") or summary.get("sector"),
        "industry": row.get("industry") or summary.get("industry"),
        "market_cap": summary.get("market_cap"),
        "price": row.get("price"),
        "change_percent": row.get("change_percent"),
        "scanner_score": row.get("scanner_score"),
        "total_score": total_score,
        "ridge": {
            "recommendation": ridge.get("recommendation"),
            "total_return": ridge.get("total_return"),
        },
        "flow": {
            "state": flow.get("state"),
            "score": flow.get("score"),
        },
        "torque": _project_torque(torque),
        "reclassification": _project_reclassification(reclassification),
        "lane": row.get("lane"),
        "setup": row.get("setup"),
        "provider": row.get("provider"),
        "provider_note": row.get("provider_note"),
        "error": None,
    }


def _project_torque(torque: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(torque, dict):
        return None
    components = torque.get("components")
    component_list: list[dict[str, Any]] = []
    if isinstance(components, list):
        for item in components:
            if isinstance(item, dict):
                component_list.append(
                    {
                        "name": item.get("name"),
                        "score": item.get("score"),
                        "weight": item.get("weight"),
                        "detail": item.get("detail"),
                    }
                )
    return {
        "total_score": torque.get("total_score"),
        "stage_label": torque.get("stage_label"),
        "recommendation": torque.get("recommendation"),
        "target_zone": torque.get("target_zone"),
        "components": component_list,
    }


def _project_reclassification(
    reclass: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(reclass, dict):
        return None
    return {
        "old_noun": reclass.get("old_noun"),
        "primary_new_verb": reclass.get("primary_new_verb"),
        "functional_layer": reclass.get("functional_layer"),
        "proof_stage": reclass.get("proof_stage"),
        "proof_stage_label": reclass.get("proof_stage_label"),
        "reclassification_gap": reclass.get("reclassification_gap"),
        "target_low": reclass.get("target_low"),
        "target_mid": reclass.get("target_mid"),
        "target_high": reclass.get("target_high"),
    }


# ---------------------------------------------------------------------------
# Filter + sort + meta
# ---------------------------------------------------------------------------


def _finalize_rows(
    rows: list[dict[str, Any]], filter_obj: TorqueScanFilter
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    stage_counts = _compute_stage_counts(rows)
    filtered = _apply_filter(rows, filter_obj)
    sorted_rows = _sort_rows(filtered, filter_obj.sort_by)
    if filter_obj.limit is not None:
        sorted_rows = sorted_rows[: filter_obj.limit]
    for index, row in enumerate(sorted_rows, start=1):
        row["rank"] = index
    return sorted_rows, stage_counts


def _compute_stage_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        torque = row.get("torque")
        if isinstance(torque, dict):
            stage = torque.get("stage_label")
        else:
            stage = None
        label = str(stage) if stage else "No Setup"
        counts[label] = counts.get(label, 0) + 1
    return counts


def _apply_filter(
    rows: list[dict[str, Any]], filter_obj: TorqueScanFilter
) -> list[dict[str, Any]]:
    stage_set: set[str] | None = None
    if filter_obj.stage_labels:
        stage_set = {label.strip().lower() for label in filter_obj.stage_labels if label.strip()}
        if not stage_set:
            stage_set = None

    out: list[dict[str, Any]] = []
    for row in rows:
        if stage_set is not None:
            torque = row.get("torque")
            stage = (
                str(torque.get("stage_label")) if isinstance(torque, dict) else ""
            )
            if stage.strip().lower() not in stage_set:
                continue
        if filter_obj.min_score is not None:
            score = _opt_float(row.get("total_score"))
            if score is None or score < filter_obj.min_score:
                continue
        if filter_obj.max_score is not None:
            score = _opt_float(row.get("total_score"))
            if score is None or score > filter_obj.max_score:
                continue
        out.append(row)
    return out


def _sort_rows(rows: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    rows = list(rows)
    if sort_by == "score_asc":
        rows.sort(key=lambda row: _opt_float(row.get("total_score")) or 0.0)
    elif sort_by == "ticker":
        rows.sort(key=lambda row: str(row.get("ticker") or ""))
    elif sort_by == "rank":
        rows.sort(
            key=lambda row: (
                row.get("rank") if isinstance(row.get("rank"), int) and row.get("rank") > 0
                else 10**9
            )
        )
    else:  # score_desc default
        rows.sort(
            key=lambda row: _opt_float(row.get("total_score")) or 0.0, reverse=True
        )
    return rows


def _build_meta(
    *,
    watchlist: WatchlistResult | None,
    all_rows: list[dict[str, Any]],
    final_rows: list[dict[str, Any]],
    errors: list[dict[str, str]],
    filter_obj: TorqueScanFilter,
    stage_counts: dict[str, int],
    period: str,
) -> dict[str, Any]:
    return {
        "watchlist_name": watchlist.name if watchlist else None,
        "result_count": len(final_rows),
        "total_evaluated": len(all_rows),
        "error_count": len(errors),
        "errors": errors,
        "filter": filter_obj.to_dict(),
        "stage_counts": stage_counts,
        "period": period,
    }


def _build_export(
    *,
    rows: list[dict[str, Any]],
    meta: dict[str, Any],
    watchlist: WatchlistResult | None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "torque-scan",
        "provider": _aggregate_provider(rows),
        "provider_note": "Torque scan",
        "tickers": [str(row.get("ticker") or "") for row in rows],
        "watchlist": watchlist_payload(watchlist),
        "image_files": [],
        "meta": meta,
        "rows": rows,
    }


def _aggregate_provider(rows: list[dict[str, Any]]) -> str:
    providers = sorted({str(row.get("provider") or "") for row in rows if row.get("provider")})
    return "+".join(providers) if providers else ""


# ---------------------------------------------------------------------------
# NDJSON helper
# ---------------------------------------------------------------------------


def _ndjson(payload: dict[str, Any]) -> str:
    return f"{json.dumps(payload, default=str, separators=(',', ':'))}\n"


__all__ = [
    "TorqueScanFilter",
    "build_torque_scan_response",
    "stream_torque_scan_rows",
]
