"""The constellation MCP for the Underlying Analyzer: ``POST /mcp``.

Six sites offer each other a small MCP apiece (lattice-animal's docs/constellation.md). This
module is Underlying's: a short allowlist of read-only, cheap tools that the lattice animals,
and any other member of the constellation, may call. ``create_app`` mounts it next to the
terminal.

It is not ``POST /api/mcp``. That endpoint serves the whole tool registry (30 tools),
including tools that call a language model or fan out to paid providers, and
``ToolSpec.mcp = False`` only hides a tool from ``tools/list``: ``tools/call`` still runs a
registry tool by name. Here the allowlist is the tuple of names below. ``ToolSpec.mcp`` is not
read, and a name that is not on the list is an unknown tool (JSON-RPC ``-32602``).

The tools are read-only and cheap, and none of them calls a language model: market data, EDGAR,
stored Situate and Prism summaries and the sector peer forecast. None takes a URL, a watchlist,
a user id or a key. What ``tests/test_constellation_mcp.py`` shows about them:

* the arguments are strict: an unknown name is refused rather than dropped, tickers, dates,
  horizons and enums go through the validators the routes use, and a refused call reaches no
  route, provider or model;
* the routes they reach are recorded in that test: none is a build, a chat, an export or the
  ticker-research route (in-process calls look like loopback, which skips that route's
  per-client admission limit);
* a result is compact JSON under the 8000 characters the library keeps, with each series cut
  to its last few points, and text that leaves the server has secrets scrubbed (an error
  message has endpoints scrubbed too);
* ``peer_forecast`` is the one tool that can spend model credits, so an uncached computation
  through ``/mcp`` counts against a per-process, per-UTC-day cap (``MCP_PEER_FORECAST_DAILY_CAP``,
  default 20). A cache hit is free, and the REST route is not capped.

Registry tools run in process through :func:`app.tool_executor.execute_tool`. The reads with no
registry entry (the stored summaries, the peer forecast) go through their own routes the same
way. Everything else is ``app.mcp_lite``, vendored verbatim from lattice-animal: the protocol,
``POST /mcp/lattice`` (the hub's own MCP, relayed one hop deeper), the manifest at
``/.well-known/mcp.json`` and the hop rule. ``ask_lattice_animals`` is the library's tool.
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from flask import Flask, current_app

from app import mcp_lite
from app.market_data import HISTORY_INTERVALS, clean_ticker
from app.mcp_lite import Mcp, create_mcp, dumps, lattice_tool
from app.peer_forecast import PEER_FORECAST_HORIZONS, cold_build_guard, parse_horizon
from app.prism.routes import clean_as_of as clean_prism_as_of
from app.prism.routes import clean_symbol as clean_prism_symbol
from app.situate.routes import clean_as_of as clean_situate_as_of
from app.situate.routes import clean_symbol as clean_situate_symbol
from app.tabular import TabularUnavailable
from app.tabular_routes import _clean_ticker as clean_peer_forecast_ticker
from app.tool_executor import execute_tool
from app.tool_registry import tool_catalog_payload

SERVER_NAME = "underlying-analyzer"
SERVER_TITLE = "Underlying Analyzer"
SERVER_VERSION = "1.0.0"
DEFAULT_ORIGIN = "https://underlying-terminal-production.up.railway.app"
SERVER_DESCRIPTION = (
    "Chart-led market research from the Underlying terminal: a small allowlist of read-only, "
    "cheap tools with no language-model calls, no user data and no orders. Capability and "
    "provider status, SEC source packs, chart, torque and options-moneyline data, stored "
    "Situate and Prism summaries, and the sector peer forecast. Research only; not investment "
    "advice."
)

#: Registry tools this server runs, by name. This tuple decides, not ``ToolSpec.mcp``.
ALLOWED_REGISTRY_TOOLS: tuple[str, ...] = (
    "list_capabilities",
    "health_check",
    "provider_status",
    "sec_source_pack",
    "chart_data",
    "torque_data",
    "moneyline_data",
    "situate_get",
    "prism_get",
)
#: A tool with no registry entry: ``GET /api/tabular/peer-forecast/{ticker}``.
PEER_FORECAST_TOOL = "peer_forecast"
#: The library's tool for reaching the lattice animals.
LATTICE_TOOL = "ask_lattice_animals"
#: Every tool ``tools/list`` offers, in order.
CONSTELLATION_TOOLS: tuple[str, ...] = (*ALLOWED_REGISTRY_TOOLS, PEER_FORECAST_TOOL, LATTICE_TOOL)

#: The library keeps 8000 characters of a tool's text and cuts the rest mid-JSON. Stay below.
RESULT_BUDGET = 7_500
#: Points of each dated series a result keeps (the most recent ones).
SERIES_TAIL = 5
MAX_STRIKES = 25
MAX_PEERS = 20

#: Uncached ``peer_forecast`` computations allowed through ``/mcp`` per process per UTC day.
PEER_FORECAST_CAP_ENV = "MCP_PEER_FORECAST_DAILY_CAP"
DEFAULT_PEER_FORECAST_DAILY_CAP = 20
#: Where the app keeps that count (``app.config``), one per app and so one per process.
PEER_FORECAST_BUDGET_KEY = "CONSTELLATION_PEER_FORECAST_BUDGET"

#: Single-ticker chart packs. ``torque`` is ``torque_data``, ``peer-forecast`` is
#: ``peer_forecast``, and ``portfolio`` needs several tickers and a benchmark.
CHART_PACKS: tuple[str, ...] = (
    "auction",
    "performance",
    "regression",
    "ridge-growth",
    "flow-compass",
    "volatility",
)
CHART_PERIODS: tuple[str, ...] = ("5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y")

_SECRET_ENV_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL", re.IGNORECASE)
_SECRET_PARAM = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)=[^&\s\"']+"
)
_URL = re.compile(r"https?://[^\s'\"<>)\]]+")
_HOST = re.compile(r"host='[^']*'")
_URL_PATH = re.compile(r"(with url: )\S+")


class ToolError(Exception):
    """The tool will not, or cannot, answer; the message says why (an ``isError`` result)."""


# -- text that leaves this server ---------------------------------------------------------


def _scrub_secrets(text: str) -> str:
    """Drop the value of any secret-looking environment variable, and ``apiKey=...`` params."""
    text = _SECRET_PARAM.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    for name, value in os.environ.items():
        if len(value) >= 8 and _SECRET_ENV_NAME.search(name):
            text = text.replace(value, "[redacted]")
    return text


def _message(error: object, limit: int = 300) -> str:
    """One scrubbed line for an error: no secrets, and no endpoint an upstream put in it."""
    text = re.sub(r"\s+", " ", str(error)).strip()
    text = _URL_PATH.sub(r"\1[path]", _HOST.sub("host='[host]'", _URL.sub("[url]", text)))
    return _scrub_secrets(text)[:limit]


def _one_line(text: object, limit: int) -> str:
    return re.sub(r"\s+", " ", "" if text is None else str(text)).strip()[:limit]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


# -- fitting a payload under the budget ---------------------------------------------------


class _Tail(list[Any]):
    """The last points of a series: when a payload must shrink, keep the newest ones."""


def _number(value: float) -> float | int | None:
    if not math.isfinite(value):
        return None  # NaN and inf are not JSON
    if abs(value) >= 1_000_000:
        return round(value)
    return float(f"{value:.6g}")


def _shrink(value: Any, *, chars: int, items: int) -> Any:
    """A copy of ``value`` with strings cut to ``chars``, lists to ``items``, floats rounded."""
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return value if len(value) <= chars else value[: max(1, chars - 1)] + "…"
    if isinstance(value, Mapping):
        return {str(key): _shrink(item, chars=chars, items=items) for key, item in value.items()}
    if isinstance(value, _Tail):
        return [_shrink(item, chars=chars, items=items) for item in value[-items:]]
    if isinstance(value, list | tuple):
        kept = [_shrink(item, chars=chars, items=items) for item in value[:items]]
        if len(value) > items:
            kept.append(f"… {len(value) - items} more")
        return kept
    return str(value)


#: (characters per string, items per list), tried in order until the JSON fits.
_LADDER: tuple[tuple[int, int], ...] = (
    (1_200, 40),
    (600, 25),
    (300, 15),
    (160, 10),
    (80, 6),
    (40, 4),
)


def _fit(payload: Any, *, budget: int = RESULT_BUDGET) -> str:
    """Compact JSON for ``payload`` within ``budget``: the first rung of the ladder that fits,
    else a valid envelope that says the result was cut."""
    text = ""
    for chars, items in _LADDER:
        text = dumps(_shrink(payload, chars=chars, items=items))
        if len(text) <= budget:
            return _scrub_secrets(text)
    cut = {"truncated": True, "total_chars": len(text), "preview": text[:2_500]}
    return _scrub_secrets(dumps(cut))


def _is_points(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(point, dict) and "date" in point for point in value[:3])
    )


def _summarize(value: Any, tail: int = SERIES_TAIL) -> Any:
    """Walk a payload: each dated series becomes ``{points, from, to, last}``."""
    if isinstance(value, dict):
        return {key: _summarize(item, tail) for key, item in value.items()}
    if _is_points(value):
        return {
            "points": len(value),
            "from": value[0].get("date"),
            "to": value[-1].get("date"),
            "last": _Tail(value[-tail:]),
        }
    if isinstance(value, list):
        return [_summarize(item, tail) for item in value]
    return value


# -- arguments ----------------------------------------------------------------------------


def _arguments(
    raw: Mapping[str, Any], allowed: Sequence[str], required: Sequence[str] = ()
) -> dict[str, Any]:
    """Refuse an unknown name (do not drop it), read null as absent, and require the rest."""
    unknown = sorted(str(key)[:40] for key in raw if key not in allowed)
    if unknown:
        accepted = ", ".join(allowed) if allowed else "none, this tool takes no arguments"
        raise ToolError(f"unknown argument(s): {', '.join(unknown[:5])} (accepted: {accepted})")
    given = {key: value for key, value in raw.items() if value is not None}
    missing = [key for key in required if key not in given]
    if missing:
        raise ToolError(f"missing required argument(s): {', '.join(missing)}")
    return given


def _symbol(value: Any, clean: Callable[[str], str] = clean_ticker) -> str:
    """A ticker, cleaned by the validator of the route that will receive it."""
    if not isinstance(value, str):
        raise ToolError("ticker must be a string, for example AAPL")
    try:
        symbol = clean(value)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not symbol.isascii():
        raise ToolError("ticker contains unsupported characters")
    return symbol


def _choice(name: str, value: Any, options: Sequence[str]) -> str:
    if not isinstance(value, str) or value not in options:
        raise ToolError(f"{name} must be one of: {', '.join(options)}")
    return value


def _month(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise ToolError("month must be an integer from 1 to 12")
    return value


def _horizon(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ToolError("horizon must be one of " + ", ".join(map(str, PEER_FORECAST_HORIZONS)))
    try:
        return parse_horizon(value)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _expiry(value: Any) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError("expiry must be a string")
        return datetime.strptime(value.strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ToolError("expiry must be a date as YYYY-MM-DD, for example 2026-10-16") from exc


def _as_of(value: Any, clean: Callable[[Any], str | None]) -> str:
    try:
        cleaned = clean(value) if isinstance(value, str) else None
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    if not cleaned:
        raise ToolError("as_of must be a date as YYYY-MM-DD")
    return cleaned


# -- running a route in process -----------------------------------------------------------


def _registry(name: str, arguments: dict[str, Any]) -> Any:
    """Run one allowlisted registry tool in process and return its parsed payload."""
    if name not in ALLOWED_REGISTRY_TOOLS:
        raise ToolError(f"{name} is not offered here")
    result = execute_tool(name, arguments, keep_images=False, result_view="full")
    if not result.ok:
        raise ToolError(f"{result.error or 'the request failed'} (HTTP {result.status})")
    return result.result


def _route(path: str, query: Mapping[str, Any] | None = None) -> tuple[Any, int]:
    """GET one of the app's own routes in process: ``(parsed JSON or None, status)``."""
    url = path + (f"?{urlencode(query)}" if query else "")
    response = current_app.test_client().get(url, headers={"Accept": "application/json"})
    try:
        payload = response.get_json(silent=True)
    finally:
        response.close()
    return payload, response.status_code


def _failure(payload: Any, status: int, fallback: str) -> ToolError:
    """The reason a route gave, including the ``{available: false, reason}`` 503s that the tool
    executor drops."""
    reason = ""
    unavailable = False
    if isinstance(payload, Mapping):
        reason = str(payload.get("error") or payload.get("reason") or "")
        unavailable = payload.get("available") is False
    prefix = "unavailable: " if unavailable else ""
    return ToolError(f"{prefix}{reason or fallback} (HTTP {status})")


# -- views: what each tool returns --------------------------------------------------------


def _sec_view(pack: Mapping[str, Any], snippet: int = 300) -> dict[str, Any]:
    def sections(block: Any) -> dict[str, Any]:
        return {
            str(label): {
                "item": item.get("Item"),
                "form": item.get("Form"),
                "filed": item.get("Filing Date"),
                "excerpt": _one_line(item.get("Snippet"), snippet),
            }
            for label, item in _mapping(block).items()
            if isinstance(item, Mapping)
        }

    filings = _mapping(pack.get("Filings"))
    facts = _mapping(pack.get("Company Facts"))
    errors = _list(pack.get("Errors"))
    citations = _list(pack.get("Citations"))
    return {
        "status": pack.get("Status"),
        "provider": pack.get("Provider"),
        "ticker": pack.get("Ticker"),
        "cik": pack.get("CIK"),
        "company": pack.get("Company Name"),
        "sic": pack.get("SIC Description") or pack.get("SIC"),
        "exchanges": pack.get("Exchanges"),
        "filings": {
            str(form): {
                key: filing.get(key)
                for key in ("filing_date", "report_date", "accession_number", "url")
            }
            for form, filing in filings.items()
            if isinstance(filing, Mapping)
        },
        "filing_sections": sections(pack.get("Filing Sections")),
        "earnings_sections": sections(pack.get("Earnings Sections")),
        "company_facts": {
            str(label): {
                "value": fact.get("Value"),
                "unit": fact.get("Unit"),
                "form": fact.get("Form"),
                "period_end": fact.get("End Date"),
                "fiscal_year": fact.get("Fiscal Year"),
                "fiscal_period": fact.get("Fiscal Period"),
                "filed": fact.get("Filed"),
            }
            for label, fact in facts.items()
            if isinstance(fact, Mapping)
        },
        "citations": len(citations),
        "errors": [_one_line(error, 200) for error in errors[:6]],
        "note": "A summary: excerpts are cut and the citation list is counted, not listed.",
    }


def _scalars(value: Any) -> dict[str, Any]:
    """The short scalar entries of a mapping: what a table of windows needs."""
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if item is None
        or isinstance(item, bool | int | float)
        or (isinstance(item, str) and len(item) <= 60)
    }


def _chart_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    datasets = [item for item in payload.get("datasets") or [] if isinstance(item, Mapping)]
    meta = payload.get("meta")
    errors = meta.get("errors") if isinstance(meta, Mapping) else None
    view: dict[str, Any] = {
        "provider": payload.get("provider"),
        "provider_note": payload.get("provider_note"),
    }
    series = f"point count, date range and the last {SERIES_TAIL} points"
    if len(datasets) > 1:
        # Several windows of one ticker (ridge-growth: 6mo, 1y, 2y). Three full summaries do
        # not fit, so: every window's scalar results, and the 1y window's levels and series.
        primary = next((item for item in datasets if item.get("period") == "1y"), datasets[-1])
        view["windows"] = [_scalars(item.get("meta")) for item in datasets]
        view["datasets"] = [_summarize(primary)]
        view["note"] = (
            f"windows: each window's results. datasets: the 1y window, series as {series}."
        )
    else:
        view["datasets"] = _summarize(datasets)
        view["note"] = f"Series are summarized: {series}."
    if errors:
        view["errors"] = errors
    return view


def _torque_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    view = {
        key: payload.get(key) for key in ("ticker", "interval", "provider", "provider_note", "meta")
    }
    view["series"] = _summarize(payload.get("series"))
    view["note"] = f"Series are summarized: the last {SERIES_TAIL} points of each."
    return view


def _moneyline_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    meta = _mapping(payload.get("meta"))
    strikes = _list(_mapping(payload.get("series")).get("strikes"))
    return {
        "ticker": payload.get("ticker"),
        "expiry": meta.get("expiry"),
        "current_price": meta.get("current_price"),
        "provider": meta.get("provider"),
        "provider_note": meta.get("provider_note"),
        "strike_count": len(strikes),
        "strikes": strikes[:MAX_STRIKES],
    }


_PEER_FORECAST_KEYS = (
    "available",
    "ticker",
    "sector",
    "sector_etf",
    "horizon_months",
    "method",
    "as_of",
    "query_date",
    "last_labeled_date",
    "bucket",
    "probabilities",
    "expected_excess_return",
    "confidence",
    "confidence_floor",
    "context_rows",
    "bucket_means",
    "predictor",
)


def _peer_forecast_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    peers = _list(payload.get("peers"))
    view = {key: payload[key] for key in _PEER_FORECAST_KEYS if key in payload}
    view["peer_count"] = len(peers)
    view["peers"] = [
        {key: peer.get(key) for key in ("symbol", "bucket", "expected_excess_return", "confidence")}
        for peer in peers[:MAX_PEERS]
        if isinstance(peer, Mapping)
    ]
    view["disclaimer"] = "Research only. Not investment advice."
    return view


# -- the tools ----------------------------------------------------------------------------


def _no_arguments(args: dict[str, Any]) -> None:
    _arguments(args, ())


def _list_capabilities(args: dict[str, Any]) -> str:
    _no_arguments(args)
    return _fit(_capabilities_view())


def _health_check(args: dict[str, Any]) -> str:
    _no_arguments(args)
    return _fit(_registry("health_check", {}))


def _provider_status(args: dict[str, Any]) -> str:
    _no_arguments(args)
    return _fit(_registry("provider_status", {}))


def _sec_source_pack(args: dict[str, Any]) -> str:
    given = _arguments(args, ("ticker",), ("ticker",))
    pack = _registry("sec_source_pack", {"ticker": _symbol(given["ticker"])})
    return _fit(_sec_view(pack if isinstance(pack, Mapping) else {}))


def _chart_data(args: dict[str, Any]) -> str:
    given = _arguments(
        args,
        ("chart_type", "ticker", "period", "interval", "month"),
        ("chart_type", "ticker"),
    )
    request: dict[str, Any] = {
        "chart_type": _choice("chart_type", given["chart_type"], CHART_PACKS),
        "ticker": _symbol(given["ticker"]),
    }
    if "period" in given:
        request["period"] = _choice("period", given["period"], CHART_PERIODS)
    if "interval" in given:
        request["interval"] = _choice("interval", given["interval"], HISTORY_INTERVALS)
    if "month" in given:
        request["month"] = _month(given["month"])
    payload = _registry("chart_data", request)
    return _fit(_chart_view(payload if isinstance(payload, Mapping) else {}))


def _torque_data(args: dict[str, Any]) -> str:
    given = _arguments(args, ("ticker",), ("ticker",))
    payload = _registry("torque_data", {"ticker": _symbol(given["ticker"])})
    return _fit(_torque_view(payload if isinstance(payload, Mapping) else {}))


def _moneyline_data(args: dict[str, Any]) -> str:
    given = _arguments(args, ("ticker", "expiry"), ("ticker",))
    request: dict[str, Any] = {"ticker": _symbol(given["ticker"])}
    if "expiry" in given:
        request["expiry"] = _expiry(given["expiry"])
    payload = _registry("moneyline_data", request)
    return _fit(_moneyline_view(payload if isinstance(payload, Mapping) else {}))


def _stored_summary(
    label: str,
    base: str,
    args: dict[str, Any],
    clean_symbol: Callable[[str], str],
    clean_as_of: Callable[[Any], str | None],
) -> str:
    """The bounded summary of a stored packet: a GET of ``.../summary``, the read route."""
    given = _arguments(args, ("ticker", "as_of"), ("ticker",))
    symbol = _symbol(given["ticker"], clean_symbol)
    query = {"as_of": _as_of(given["as_of"], clean_as_of)} if "as_of" in given else None
    payload, status = _route(f"{base}/{quote(symbol, safe='')}/summary", query)
    if status == 404:
        raise ToolError(
            f"no stored {label} packet for {symbol}; this server only reads stored packets "
            "and does not build one"
        )
    if status != 200 or not isinstance(payload, Mapping):
        raise _failure(payload, status, f"could not read the {label} packet")
    return _fit(payload)


def _situate_get(args: dict[str, Any]) -> str:
    return _stored_summary(
        "Situate", "/api/situate", args, clean_situate_symbol, clean_situate_as_of
    )


def _prism_get(args: dict[str, Any]) -> str:
    return _stored_summary("Prism", "/api/prism", args, clean_prism_symbol, clean_prism_as_of)


class DailyBudget:
    """A count of something spent, kept per process and reset at 00:00 UTC. It is thread-safe,
    and ``clock`` (epoch seconds) can be replaced, which is how the tests cross midnight."""

    def __init__(self, cap: int, clock: Callable[[], float] = time.time) -> None:
        self.cap = max(0, cap)
        self.clock = clock
        self._lock = threading.Lock()
        self._day = ""
        self._spent = 0

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), tz=UTC)

    def _roll(self, now: datetime) -> None:
        """Start a new count when the UTC day has changed. The caller holds the lock."""
        day = now.date().isoformat()
        if day != self._day:
            self._day, self._spent = day, 0

    def try_spend(self) -> bool:
        """Spend one unit if one is left today. When the cap is spent, nothing is counted."""
        with self._lock:
            self._roll(self._now())
            if self._spent >= self.cap:
                return False
            self._spent += 1
            return True

    @property
    def spent(self) -> int:
        with self._lock:
            self._roll(self._now())
            return self._spent

    def resets_at(self) -> datetime:
        """The next 00:00 UTC."""
        now = self._now()
        return datetime(now.year, now.month, now.day, tzinfo=UTC) + timedelta(days=1)


def _daily_cap(env: Mapping[str, str]) -> int:
    """``MCP_PEER_FORECAST_DAILY_CAP``: a whole number of uncached forecasts a day, and 0 allows
    none (cached ones still answer). Anything else takes the default."""
    try:
        cap = int(str(env.get(PEER_FORECAST_CAP_ENV) or "").strip())
    except ValueError:
        return DEFAULT_PEER_FORECAST_DAILY_CAP
    return cap if cap >= 0 else DEFAULT_PEER_FORECAST_DAILY_CAP


def _cap_message(budget: DailyBudget) -> str:
    if budget.cap == 0:
        return (
            "uncached forecasts are switched off here (the daily cap is 0); "
            "forecasts already cached still answer"
        )
    reset = budget.resets_at().strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        f"the daily cap on uncached forecasts ({budget.cap} a day) is spent; "
        f"it resets at {reset}; forecasts already cached still answer"
    )


class _ColdBuildGuard:
    """What ``app.peer_forecast`` calls when a lookup misses the cache and a computation (a panel
    load, then model calls) is about to start: it spends one unit of the day's budget, or
    refuses. A computation that has started stays counted if it fails."""

    def __init__(self, budget: DailyBudget) -> None:
        self.budget = budget
        self.refusal: str | None = None

    def __call__(self) -> None:
        if not self.budget.try_spend():
            self.refusal = _cap_message(self.budget)
            raise TabularUnavailable(self.refusal)


def _peer_forecast_budget() -> DailyBudget:
    """This process's count: made when the MCP is mounted, and here if it was not."""
    budget: DailyBudget = current_app.config.setdefault(
        PEER_FORECAST_BUDGET_KEY, DailyBudget(_daily_cap(os.environ))
    )
    return budget


def _peer_forecast(args: dict[str, Any]) -> str:
    """No ``as_of``: the route hands it to a cache key unvalidated, so each new date a caller
    sent would buy a fresh sector build (a panel load and a model call).

    The route runs in process with :data:`app.peer_forecast.cold_build_guard` set for this call
    only, so a cache hit is free and a cold computation spends one unit of the daily budget
    (or is refused). The REST route is not called with a guard and is not capped."""
    given = _arguments(args, ("ticker", "horizon"), ("ticker",))
    symbol = _symbol(given["ticker"], clean_peer_forecast_ticker)
    query = {"horizon": _horizon(given["horizon"])} if "horizon" in given else None
    guard = _ColdBuildGuard(_peer_forecast_budget())
    token = cold_build_guard.set(guard)
    try:
        payload, status = _route(f"/api/tabular/peer-forecast/{quote(symbol, safe='')}", query)
    finally:
        cold_build_guard.reset(token)
    if guard.refusal:
        raise ToolError(guard.refusal)
    if status != 200 or not isinstance(payload, Mapping):
        raise _failure(payload, status, "peer forecast failed")
    return _fit(_peer_forecast_view(payload))


_TICKER = {"type": "string", "description": "One ticker symbol, for example AAPL", "maxLength": 32}
_TICKER_SHORT = {**_TICKER, "maxLength": 16}
_AS_OF = {
    "type": "string",
    "format": "date",
    "description": "ISO date (YYYY-MM-DD) of a specific stored build; default the latest",
}


@dataclass(frozen=True)
class _Tool:
    name: str
    #: Shown in ``tools/list``; the manifest keeps the first 300 characters.
    description: str
    #: One line, for ``list_capabilities``.
    summary: str
    handler: Callable[[dict[str, Any]], Any]
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()


#: Descriptions follow the registry's, and say what differs here.
_TOOLS: tuple[_Tool, ...] = (
    _Tool(
        "list_capabilities",
        "List what the Underlying terminal can do (each tool's lane and cost class: fast, slow "
        "or llm) and which of those tools this server runs. Call first when unsure which tool "
        "answers a question. No arguments.",
        "What the terminal can do, and which tools this server runs",
        _list_capabilities,
    ),
    _Tool(
        "health_check",
        "Liveness probe for the terminal API. Use only to verify the service is reachable. "
        "No arguments.",
        "Liveness probe for the terminal API",
        _health_check,
    ),
    _Tool(
        "provider_status",
        "Market data provider notes and fallback order: primary and fallback provider, whether "
        "Massive is configured (a yes or no, not the key), streaming status and caveats. Use "
        "when a data call failed or to say where the numbers come from. No arguments.",
        "Market data provider notes and fallback order",
        _provider_status,
    ),
    _Tool(
        "sec_source_pack",
        "EDGAR source pack for one ticker: latest 10-K, 10-Q and 8-K metadata, short excerpts "
        "(business, risk factors, MD&A, earnings) and key XBRL company facts. Use to ground a "
        "claim in primary sources. Excerpts are cut to about 300 characters.",
        "EDGAR filing metadata, short excerpts and XBRL company facts",
        _sec_source_pack,
        {"ticker": _TICKER},
        ("ticker",),
    ),
    _Tool(
        "chart_data",
        "Chartable JSON for one ticker and one chart pack (auction, performance, regression, "
        "ridge-growth, flow-compass, volatility), no images. Levels and metadata in full; each "
        "series as its point count, date range and last few points.",
        "Chart pack data for one ticker: six packs, series summarized to their last points",
        _chart_data,
        {
            "chart_type": {
                "type": "string",
                "enum": list(CHART_PACKS),
                "description": "Which chart pack's data to return",
            },
            "ticker": _TICKER,
            "period": {
                "type": "string",
                "enum": list(CHART_PERIODS),
                "description": "History window (default 1y); not used by performance",
            },
            "interval": {
                "type": "string",
                "enum": list(HISTORY_INTERVALS),
                "description": "Bar interval (default 1d)",
            },
            "month": {
                "type": "integer",
                "minimum": 1,
                "maximum": 12,
                "description": "Month number, performance pack only",
            },
        },
        ("chart_type", "ticker"),
    ),
    _Tool(
        "torque_data",
        "Misclassified-revenue-torque composite for one ticker: score, stage, recommendation, "
        "component breakdown, and price and fundamental series (no image; series cut to their "
        "last few points). Use for inflection questions.",
        "Torque score, stage and components, with price and fundamental series",
        _torque_data,
        {"ticker": _TICKER},
        ("ticker",),
    ),
    _Tool(
        "moneyline_data",
        "Options open-interest ladder for one ticker and expiry as JSON (no image): call and "
        "put open interest, last prices, net open interest and put/call ratio at the strikes "
        "nearest spot. Omit expiry for the nearest.",
        "Options open-interest ladder near spot for one expiry",
        _moneyline_data,
        {
            "ticker": _TICKER,
            "expiry": {
                "type": "string",
                "format": "date",
                "description": "Expiry as YYYY-MM-DD; omit for the nearest",
            },
        },
        ("ticker",),
    ),
    _Tool(
        "situate_get",
        "Read the last stored Situate summary for a ticker: posture, what is priced in, "
        "falsifiers, per-horizon odds and factor exposure. It reads what was already built, "
        "does not build one or call a model, and errors when nothing is stored.",
        "The last stored Situate summary for a ticker (read only)",
        _situate_get,
        {"ticker": _TICKER_SHORT, "as_of": _AS_OF},
        ("ticker",),
    ),
    _Tool(
        "prism_get",
        "Read the last stored Prism summary for a ticker: recommendation, fair value, exit "
        "targets, scenarios and regime. It reads what was already built, does not build one "
        "or call a model, and errors when nothing is stored.",
        "The last stored Prism summary for a ticker (read only)",
        _prism_get,
        {"ticker": _TICKER_SHORT, "as_of": _AS_OF},
        ("ticker",),
    ),
    _Tool(
        PEER_FORECAST_TOOL,
        "Sector peer forecast for one ticker: TabICL v2 bucket (strong_under to strong_over) of "
        "forward excess return vs curated peers, with probabilities. A cold call can take "
        "longer than the hub's 25 s deadline and spends model credits under a daily cap "
        "(default 20); a cached one is free. Errors say why.",
        "TabICL v2 sector peer forecast: excess-return bucket vs curated peers (cold calls capped)",
        _peer_forecast,
        {
            "ticker": _TICKER_SHORT,
            "horizon": {
                "type": "integer",
                "enum": list(PEER_FORECAST_HORIZONS),
                "description": "Forward horizon in months (default 3)",
            },
        },
        ("ticker",),
    ),
)

#: The two tools the terminal's own catalog does not list: the forecast has no registry entry,
#: and the lattice tool is the library's.
_EXTRA_CATALOG: dict[str, tuple[str, str, str]] = {
    PEER_FORECAST_TOOL: ("signals", "slow", ""),
    LATTICE_TOOL: (
        "constellation",
        "llm",
        "Ask the lattice animals one question (relayed one hop deeper; slow)",
    ),
}


def _capabilities_view() -> dict[str, Any]:
    catalog = tool_catalog_payload()
    summaries = {tool.name: tool.summary for tool in _TOOLS}
    offered: list[dict[str, Any]] = []
    elsewhere: list[dict[str, Any]] = []
    for tool in catalog["tools"]:
        row = {"name": tool["name"], "group": tool["group"], "cost": tool["cost"]}
        if tool["name"] in summaries:
            offered.append({**row, "summary": summaries[tool["name"]]})
        else:
            elsewhere.append(row)
    for name, (group, cost, summary) in _EXTRA_CATALOG.items():
        offered.append(
            {"name": name, "group": group, "cost": cost, "summary": summary or summaries[name]}
        )
    return {
        "ok": True,
        "server": SERVER_NAME,
        "terminal_tools": catalog["tool_count"],
        "callable_here": offered,
        "not_offered_here": elsewhere,
        "note": (
            "callable_here is what this server runs; tools/list has each schema. The others "
            "exist in the terminal but call language models or paid providers, or take URLs "
            "or user data, so they are not offered here."
        ),
    }


def _runner(tool: _Tool) -> Callable[[dict[str, Any], Any], Any]:
    """A library ``run(args, ctx)``. A refusal is an error result that says why; anything
    else is logged here (the library swallows it) and answered without detail."""

    def run(args: dict[str, Any], _ctx: Any) -> Any:
        try:
            return tool.handler(args)
        except ToolError as exc:
            return {"text": f"{tool.name}: {_message(exc)}", "isError": True}
        except Exception:
            current_app.logger.exception("constellation tool %s failed", tool.name)
            return {"text": f"{tool.name} failed", "isError": True}

    return run


def _library_tool(tool: _Tool) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(tool.properties),
        "additionalProperties": False,
    }
    if tool.required:
        schema["required"] = list(tool.required)
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": schema,
        "readOnly": True,
        "run": _runner(tool),
    }


def constellation_tools() -> list[dict[str, Any]]:
    """The tool list handed to ``create_mcp``: the allowlist, then the lattice tool."""
    return [*(_library_tool(tool) for tool in _TOOLS), lattice_tool(name=LATTICE_TOOL)]


def build_constellation_mcp(env: Mapping[str, str] | None = None) -> Mcp:
    """The server, from the environment: ``LATTICE_MCP_URL`` (the hub, default the deployed
    one), ``MCP_PUBLIC_ORIGIN`` (what the manifest advertises) and ``MCP_ALLOW_LOCAL=1``
    (lets the hub be plain http on loopback, for a laptop or a test)."""
    env = os.environ if env is None else env
    server: Mcp = create_mcp(
        name=SERVER_NAME,
        title=SERVER_TITLE,
        description=SERVER_DESCRIPTION,
        version=SERVER_VERSION,
        origin=env.get("MCP_PUBLIC_ORIGIN", DEFAULT_ORIGIN).rstrip("/"),
        peers={"lattice": env.get("LATTICE_MCP_URL") or mcp_lite.HUB_URL},
        allow_local=env.get("MCP_ALLOW_LOCAL") == "1",
        tools=constellation_tools(),
    )
    return server


def mount_constellation_mcp(app: Flask, env: Mapping[str, str] | None = None) -> Mcp:
    """Mount ``POST /mcp``, ``POST /mcp/lattice`` and ``GET /.well-known/mcp.json`` (also
    ``/.well-known/mcp/server-card.json``) on ``app``. ``/api/mcp`` is left as it is. The
    ``peer_forecast`` daily budget (``MCP_PEER_FORECAST_DAILY_CAP``) is made here, one per app."""
    env = os.environ if env is None else env
    server = build_constellation_mcp(env)
    server.flask(app)
    app.config["CONSTELLATION_MCP"] = server
    app.config[PEER_FORECAST_BUDGET_KEY] = DailyBudget(_daily_cap(env))
    return server
