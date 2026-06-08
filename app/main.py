from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, Response, current_app, jsonify, request, send_from_directory
from flask_cors import CORS

from app.analysis import summarize_stock
from app.charts import (
    RenderedImage,
    render_auction_chart,
    render_performance_chart,
    render_portfolio_chart,
    render_regression_chart,
    render_volatility_chart,
)
from app.market_data import MarketDataClient, MarketDataError, clean_ticker

STATIC_DIR = Path(__file__).parent / "static"


def create_app() -> Flask:
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    CORS(app)
    app.config["MARKET_DATA_CLIENT"] = MarketDataClient()

    @app.get("/")
    def index() -> Response:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/health")
    def health() -> Any:
        return jsonify({"ok": True, "service": "underlying-analyzer-reboot"})

    @app.get("/api/providers")
    def providers() -> Any:
        return jsonify(
            {
                "primary": "yfinance",
                "fallback": "nasdaq",
                "notes": [
                    (
                        "yfinance is current and still usable, but it is unofficial "
                        "Yahoo Finance access."
                    ),
                    "Daily US equity history falls back to Nasdaq when yfinance fails.",
                    "For production volume, add a keyed provider such as FMP or Twelve Data.",
                ],
            }
        )

    @app.post("/api/charts/<chart_type>")
    def chart(chart_type: str) -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            response = build_chart_response(get_market_client(), chart_type, payload)
            return jsonify(response)
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected chart error")
            return jsonify({"error": f"Unexpected chart error: {exc}"}), 500

    @app.get("/api/analysis/<ticker>")
    def analysis(ticker: str) -> Any:
        try:
            return jsonify(summarize_stock(get_market_client(), ticker))
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    register_compat_routes(app)
    return app


def get_market_client() -> MarketDataClient:
    return current_app.config["MARKET_DATA_CLIENT"]


def build_chart_response(
    client: MarketDataClient, chart_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    chart_key = chart_type.replace("_", "-")
    if chart_key == "auction":
        ticker = clean_ticker(str(payload.get("ticker") or first_ticker(payload)))
        period = str(payload.get("period") or "1y")
        history = client.get_history(ticker, period=period)
        image, meta = render_auction_chart(history, period=period)
        return response_payload([image], history.provider, history.note, meta)

    if chart_key == "performance":
        ticker = clean_ticker(str(payload.get("ticker") or first_ticker(payload)))
        month = int(payload.get("month") or 1)
        history = client.get_history(ticker, period="10y")
        image, meta = render_performance_chart(history, month=month)
        return response_payload([image], history.provider, history.note, meta)

    if chart_key == "regression":
        ticker = clean_ticker(str(payload.get("ticker") or first_ticker(payload)))
        history = client.get_history(
            ticker,
            period=str(payload.get("period") or "1y"),
            start=payload.get("start_date"),
            end=payload.get("end_date"),
        )
        image, meta = render_regression_chart(history)
        return response_payload([image], history.provider, history.note, meta)

    if chart_key == "portfolio":
        tickers = ticker_list(payload)
        histories = [
            client.get_history(
                ticker, start=payload.get("start_date"), end=payload.get("end_date"), period="1y"
            )
            for ticker in tickers
        ]
        image, meta = render_portfolio_chart(
            histories, investment_per_stock=float(payload.get("investment_per_stock") or 100)
        )
        return response_payload(
            [image], mixed_provider(histories), "Mixed provider portfolio render", meta
        )

    if chart_key == "volatility":
        histories = [client.get_history(ticker, period="1y") for ticker in ticker_list(payload)]
        image, meta = render_volatility_chart(histories)
        return response_payload(
            [image], mixed_provider(histories), "Mixed provider volatility render", meta
        )

    raise ValueError(f"Unsupported chart type: {chart_type}")


def response_payload(
    images: list[RenderedImage], provider: str, note: str, meta: dict[str, Any]
) -> dict[str, Any]:
    return {
        "images": [image.__dict__ for image in images],
        "provider": provider,
        "provider_note": note,
        "meta": meta,
    }


def first_ticker(payload: dict[str, Any]) -> str:
    tickers = ticker_list(payload)
    return tickers[0]


def ticker_list(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("tickers") or payload.get("ticker") or "AAPL"
    if isinstance(raw, str):
        tickers = [part.strip().upper() for part in raw.split(",")]
    else:
        tickers = [str(part).strip().upper() for part in raw]
    cleaned = [ticker for ticker in tickers if ticker]
    if not cleaned:
        raise ValueError("At least one ticker is required")
    return cleaned


def mixed_provider(histories: list[Any]) -> str:
    providers = sorted({history.provider for history in histories})
    return "+".join(providers)


def register_compat_routes(app: Flask) -> None:
    @app.post("/plot-auction-levels")
    def compat_auction() -> Any:
        return compat_chart("auction")

    @app.post("/plot-performance")
    def compat_performance() -> Any:
        return compat_chart("performance")

    @app.post("/plot-regression")
    def compat_regression() -> Any:
        return compat_chart("regression")

    @app.post("/plot-portfolio-performance")
    def compat_portfolio() -> Any:
        return compat_chart("portfolio")

    @app.post("/plot-volatility")
    def compat_volatility() -> Any:
        return compat_chart("volatility")

    @app.get("/stock_analysis/<ticker>")
    def compat_analysis(ticker: str) -> Any:
        return jsonify(summarize_stock(get_market_client(), ticker))


def compat_chart(chart_type: str) -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        response = build_chart_response(get_market_client(), chart_type, payload)
        return jsonify({"images": [image["data"] for image in response["images"]]})
    except (ValueError, MarketDataError) as exc:
        return jsonify({"error": str(exc)}), 400


app = create_app()
