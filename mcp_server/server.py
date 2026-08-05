"""stdio MCP server for The Underlying Analyzer public HTTP API."""

from __future__ import annotations

import json
import os
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

DEFAULT_BASE_URL = "https://underlying-terminal-production.up.railway.app"
CHART_TYPES = (
    "auction",
    "performance",
    "regression",
    "ridge-growth",
    "flow-compass",
    "torque",
    "portfolio",
    "volatility",
)

mcp = FastMCP(
    "underlying-analyzer",
    instructions=(
        "Public market research tools for The Underlying Analyzer Terminal. "
        "Call these tools without an API key. Prefer compact JSON responses "
        "(images stripped by default)."
    ),
)


def _base_url() -> str:
    return (
        os.getenv("UNDERLYING_BASE_URL")
        or os.getenv("APP_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")


def _timeout() -> float:
    raw = os.getenv("UNDERLYING_MCP_TIMEOUT", "180")
    try:
        return max(5.0, float(raw))
    except ValueError:
        return 180.0


def _strip_heavy_fields(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_heavy_fields(item) for item in value]
    if not isinstance(value, dict):
        return value

    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"data", "image"} and isinstance(item, str) and len(item) > 200:
            cleaned[key] = f"<omitted base64 ({len(item)} chars)>"
            continue
        if key == "images" and isinstance(item, list):
            cleaned[key] = [
                {
                    "filename": img.get("filename"),
                    "mime": img.get("mime"),
                    "data": (
                        f"<omitted base64 ({len(img.get('data', ''))} chars)>"
                        if isinstance(img, dict) and isinstance(img.get("data"), str)
                        else None
                    ),
                }
                if isinstance(img, dict)
                else img
                for img in item
            ]
            continue
        cleaned[key] = _strip_heavy_fields(item)
    return cleaned


def _request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    include_images: bool = False,
) -> dict[str, Any]:
    url = f"{_base_url()}{path}"
    response = requests.request(
        method,
        url,
        json=json_body,
        timeout=_timeout(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        payload: Any = response.json()
    except ValueError:
        payload = {"raw": response.text[:4000]}

    if not isinstance(payload, dict):
        payload = {"data": payload}

    result = {
        "ok": response.ok,
        "status_code": response.status_code,
        "url": url,
        "body": payload if include_images else _strip_heavy_fields(payload),
    }
    return result


def _ticker_payload(
    ticker: str | None = None,
    tickers: str | None = None,
    watchlist_url: str | None = None,
    max_results: int = 10,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {"max_results": max_results, **extra}
    if watchlist_url:
        body["watchlist_url"] = watchlist_url
    elif tickers:
        body["tickers"] = tickers
    elif ticker:
        body["ticker"] = ticker
    else:
        raise ValueError("Provide ticker, tickers, or watchlist_url")
    return body


@mcp.tool()
def health() -> dict[str, Any]:
    """Check Underlying Analyzer API health."""
    return _request("GET", "/api/health")


@mcp.tool()
def api_docs() -> dict[str, Any]:
    """Fetch the public machine-readable API catalog from /api/docs."""
    return _request("GET", "/api/docs")


@mcp.tool()
def analyze_ticker(ticker: str) -> dict[str, Any]:
    """Run a single-ticker analysis brief."""
    return _request("GET", f"/api/analysis/{ticker.strip().upper()}")


@mcp.tool()
def analyze_batch(
    tickers: str | None = None,
    watchlist_url: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Batch analysis for tickers or a TradingView watchlist URL."""
    body = _ticker_payload(
        tickers=tickers, watchlist_url=watchlist_url, max_results=max_results
    )
    return _request("POST", "/api/analysis", json_body=body)


@mcp.tool()
def render_chart(
    chart_type: str,
    ticker: str | None = None,
    tickers: str | None = None,
    watchlist_url: str | None = None,
    period: str = "1y",
    max_results: int = 10,
    include_images: bool = False,
) -> dict[str, Any]:
    """Render a chart pack.

    chart_type: auction, performance, regression, ridge-growth, flow-compass,
    torque, portfolio, or volatility.
    """
    normalized = chart_type.strip().lower().replace("_", "-")
    if normalized not in CHART_TYPES:
        return {
            "ok": False,
            "error": f"Unsupported chart_type '{chart_type}'. Use one of: {', '.join(CHART_TYPES)}",
        }
    body = _ticker_payload(
        ticker=ticker,
        tickers=tickers,
        watchlist_url=watchlist_url,
        max_results=max_results,
        period=period,
    )
    return _request(
        "POST",
        f"/api/charts/{normalized}",
        json_body=body,
        include_images=include_images,
    )


@mcp.tool()
def stock_fax(ticker: str) -> dict[str, Any]:
    """Generate a Stock Fax narrative pack for a ticker."""
    return _request("POST", "/api/tools/fax", json_body={"ticker": ticker})


@mcp.tool()
def vision_memo(ticker: str, version: str = "v2") -> dict[str, Any]:
    """Generate a Vision market memo. version: v2 (default) or classic."""
    path = "/api/tools/vision/v2" if version.strip().lower() != "classic" else "/api/tools/vision"
    return _request("POST", path, json_body={"ticker": ticker})


@mcp.tool()
def torque(ticker: str, include_images: bool = False) -> dict[str, Any]:
    """Compute torque score and chart for a ticker."""
    return _request(
        "POST",
        "/api/tools/torque",
        json_body={"ticker": ticker},
        include_images=include_images,
    )


@mcp.tool()
def torque_scan(
    tickers: str | None = None,
    watchlist_url: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Batch torque scan over tickers or a watchlist."""
    body = _ticker_payload(
        tickers=tickers, watchlist_url=watchlist_url, max_results=max_results
    )
    return _request("POST", "/api/tools/torque/scan", json_body=body)


@mcp.tool()
def moneyline(
    ticker: str, expiry: str | None = None, include_images: bool = False
) -> dict[str, Any]:
    """Render options moneyline / moneywall chart."""
    body: dict[str, Any] = {"ticker": ticker}
    if expiry:
        body["expiry"] = expiry
    return _request(
        "POST",
        "/api/tools/moneyline",
        json_body=body,
        include_images=include_images,
    )


@mcp.tool()
def watchlist_cockpit(
    tickers: str | None = None,
    watchlist_url: str | None = None,
    max_results: int = 10,
) -> dict[str, Any]:
    """Rank tickers in a cockpit table (lane, ridge, flow, auction)."""
    body = _ticker_payload(
        tickers=tickers, watchlist_url=watchlist_url, max_results=max_results
    )
    return _request("POST", "/api/watchlists/cockpit", json_body=body)


@mcp.tool()
def resolve_watchlist(watchlist_url: str, max_results: int = 10) -> dict[str, Any]:
    """Resolve a public TradingView watchlist URL to tickers."""
    return _request(
        "POST",
        "/api/watchlists/resolve",
        json_body={"watchlist_url": watchlist_url, "max_results": max_results},
    )


@mcp.tool()
def sec_source_pack(ticker: str) -> dict[str, Any]:
    """Fetch SEC EDGAR source pack for a ticker."""
    return _request("GET", f"/api/sec/{ticker.strip().upper()}")


@mcp.tool()
def pixel_image(prompt: str, include_images: bool = False) -> dict[str, Any]:
    """Generate a Pixel image from a text prompt."""
    return _request(
        "POST",
        "/api/tools/pixel",
        json_body={"prompt": prompt},
        include_images=include_images,
    )


@mcp.resource("underlying://docs/api")
def api_docs_resource() -> str:
    """Raw public API catalog JSON."""
    result = _request("GET", "/api/docs")
    return json.dumps(result.get("body", result), indent=2)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
