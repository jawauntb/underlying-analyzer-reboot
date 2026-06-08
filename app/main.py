from __future__ import annotations

import json
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    current_app,
    jsonify,
    request,
    send_from_directory,
    stream_with_context,
)
from flask_cors import CORS

from app.analysis import build_scanner_rows, summarize_stock
from app.anthropic import DEFAULT_ANTHROPIC_MODEL, AnthropicError
from app.charts import (
    RenderedImage,
    render_auction_chart,
    render_performance_chart,
    render_portfolio_chart,
    render_regression_chart,
    render_volatility_chart,
)
from app.market_data import HistoryResult, MarketDataClient, MarketDataError, clean_ticker
from app.tools import (
    DEFAULT_OPENAI_IMAGE_MODEL,
    build_market_memo,
    build_stock_fax,
    build_stock_fax_data,
    generate_analysis_brief,
    generate_pixel_image,
    render_moneyline_chart,
    stream_market_memo_text,
)
from app.watchlists import (
    TradingViewWatchlistClient,
    WatchlistError,
    WatchlistResult,
    watchlist_payload,
)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_CAP = 50


@dataclass(frozen=True)
class TickerSelection:
    tickers: list[str]
    watchlist: WatchlistResult | None = None


def create_app() -> Flask:
    if os.getenv("UNDERLYING_SKIP_DOTENV") != "1":
        load_env_file()
    app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
    CORS(app)
    app.config["MARKET_DATA_CLIENT"] = MarketDataClient()
    app.config["WATCHLIST_CLIENT"] = TradingViewWatchlistClient()
    app.config["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
    app.config["OPENAI_IMAGE_MODEL"] = os.getenv("OPENAI_IMAGE_MODEL", DEFAULT_OPENAI_IMAGE_MODEL)
    app.config["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY")
    app.config["ANTHROPIC_TEXT_MODEL"] = os.getenv(
        "ANTHROPIC_TEXT_MODEL", DEFAULT_ANTHROPIC_MODEL
    )
    app.config["TEXT_GENERATOR"] = None

    @app.get("/")
    def index() -> Response:
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/vision")
    @app.get("/pixel")
    @app.get("/fax")
    @app.get("/moneyline")
    def legacy_tool() -> Response:
        return send_from_directory(STATIC_DIR, "legacy-tool.html")

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
            response = build_chart_response(
                get_market_client(), get_watchlist_client(), chart_type, payload
            )
            return jsonify(response)
        except (ValueError, MarketDataError, WatchlistError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected chart error")
            return jsonify({"error": f"Unexpected chart error: {exc}"}), 500

    @app.post("/api/analysis")
    def analysis_batch() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            response = build_analysis_response(
                get_market_client(),
                get_watchlist_client(),
                payload,
                **text_generation_options(),
            )
            return jsonify(response)
        except (ValueError, AnthropicError, MarketDataError, WatchlistError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/analysis/<ticker>")
    def analysis(ticker: str) -> Any:
        try:
            summary = summarize_stock(get_market_client(), ticker)
            scanner = build_scanner_rows([summary])
            generated = generate_analysis_brief(
                [summary], scanner, **text_generation_options()
            )
            return jsonify(
                {
                    **summary,
                    "Anthropic Brief": generated.text,
                    "Text Provider": generated.provider,
                    "Text Model": generated.model,
                }
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/watchlists/resolve")
    def resolve_watchlist() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            watchlist = get_watchlist_client().get_watchlist(
                str(payload.get("watchlist_url") or "")
            )
            limit = max_results(payload)
            return jsonify(
                {
                    "watchlist": watchlist_payload(watchlist),
                    "tickers": watchlist.tickers[:limit],
                    "max_results": limit,
                }
            )
        except WatchlistError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/tools/fax")
    def stock_fax_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            return jsonify(
                build_stock_fax(
                    get_market_client(),
                    str(payload.get("ticker") or ""),
                    **text_generation_options(),
                )
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/tools/vision")
    def vision_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            return jsonify(
                build_market_memo(
                    get_market_client(),
                    str(payload.get("ticker") or ""),
                    **text_generation_options(),
                )
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/tools/vision/stream")
    def vision_tool_stream() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            report = build_stock_fax_data(get_market_client(), str(payload.get("ticker") or ""))
            options = text_generation_options()
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

        return Response(
            stream_with_context(vision_stream_events(report, options)),
            mimetype="application/x-ndjson",
        )

    @app.post("/api/tools/moneyline")
    def moneyline_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            image, meta = render_moneyline_chart(
                str(payload.get("ticker") or ""), expiry=payload.get("expiry")
            )
            export = {
                "generated_at": datetime.now(UTC).isoformat(),
                "mode": "moneyline",
                "ticker": meta["ticker"],
                "meta": meta,
                "image_files": [{"filename": image.filename, "mime": image.mime}],
            }
            return jsonify({"image": image.__dict__, "meta": meta, "export": export})
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/tools/pixel")
    def pixel_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            return jsonify(
                generate_pixel_image(
                    str(payload.get("prompt") or ""),
                    api_key=current_app.config.get("OPENAI_API_KEY"),
                    image_model=current_app.config.get("OPENAI_IMAGE_MODEL"),
                )
            )
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    register_compat_routes(app)
    return app


def get_market_client() -> MarketDataClient:
    return current_app.config["MARKET_DATA_CLIENT"]


def get_watchlist_client() -> TradingViewWatchlistClient:
    return current_app.config["WATCHLIST_CLIENT"]


def text_generation_options() -> dict[str, Any]:
    configured_generator = current_app.config.get("TEXT_GENERATOR")
    if configured_generator is not None:
        return {"text_generator": configured_generator}
    return {
        "api_key": current_app.config.get("ANTHROPIC_API_KEY"),
        "text_model": current_app.config.get("ANTHROPIC_TEXT_MODEL"),
    }


def vision_stream_events(report: dict[str, Any], options: dict[str, Any]) -> Iterator[str]:
    text_provider, text_model = text_generation_identity(options)
    meta = vision_stream_meta(report, text_provider, text_model)
    yield ndjson({"type": "meta", **meta})

    chunks: list[str] = []
    try:
        for chunk in stream_market_memo_text(report, **options):
            chunks.append(chunk)
            yield ndjson({"type": "token", "text": chunk})
    except (AnthropicError, ValueError, MarketDataError) as exc:
        yield ndjson({"type": "error", "error": str(exc)})
        return
    except Exception as exc:
        current_app.logger.exception("Unexpected Vision stream error")
        yield ndjson({"type": "error", "error": f"Unexpected Vision stream error: {exc}"})
        return

    memo_text = "".join(chunks)
    export = export_payload(
        mode="vision",
        provider=str(report.get("Provider") or "market data"),
        provider_note=str(report.get("Provider Note") or "Vision market memo"),
        tickers=[str(report["Ticker"])],
        meta=meta,
        watchlist=None,
        image_files=[],
    )
    export["market_memo"] = memo_text
    export["report"] = report
    export["text_provider"] = text_provider
    export["text_model"] = text_model
    yield ndjson({"type": "done", "text": memo_text, "export": export, **meta})


def vision_stream_meta(
    report: dict[str, Any], text_provider: str, text_model: str
) -> dict[str, Any]:
    report_preview = vision_report_preview(report)
    return {
        "Ticker": report["Ticker"],
        "Report": report_preview,
        "Text Provider": text_provider,
        "Text Model": text_model,
        "provider": str(report.get("Provider") or "market data"),
        "provider_note": str(report.get("Provider Note") or "Vision market memo"),
        "meta": {
            "ticker": report["Ticker"],
            "name": report.get("Name"),
            "text_provider": text_provider,
            "text_model": text_model,
            "streamed": True,
        },
    }


def vision_report_preview(report: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key != "Export Rows"}


def text_generation_identity(options: dict[str, Any]) -> tuple[str, str]:
    generator = options.get("text_generator")
    if generator is not None:
        provider = str(getattr(generator, "provider", "anthropic"))
        model = str(getattr(generator, "model", DEFAULT_ANTHROPIC_MODEL))
        return provider, model
    return "anthropic", str(options.get("text_model") or DEFAULT_ANTHROPIC_MODEL)


def ndjson(payload: dict[str, Any]) -> str:
    return f"{json.dumps(payload, default=str, separators=(',', ':'))}\n"


def load_env_file(path: Path | None = None) -> None:
    env_path = path or Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_chart_response(
    client: MarketDataClient,
    watchlist_client: TradingViewWatchlistClient,
    chart_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    chart_key = chart_type.replace("_", "-")
    if chart_key == "auction":
        selection = resolve_ticker_selection(payload, watchlist_client)
        period = str(payload.get("period") or "1y")
        images: list[RenderedImage] = []
        histories = []
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for ticker in selection.tickers:
            try:
                history = client.get_history(ticker, period=period)
                image, meta = render_auction_chart(history, period=period)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(history)
            images.append(image)
            results.append(result_payload(history.ticker, history.provider, history.note, meta))
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return response_payload(
            images,
            mixed_provider(histories),
            "Batch auction render",
            meta,
            mode=chart_key,
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "performance":
        selection = resolve_ticker_selection(payload, watchlist_client)
        month = int(payload.get("month") or 1)
        images = []
        histories = []
        results = []
        errors = []
        for ticker in selection.tickers:
            try:
                history = client.get_history(ticker, period="10y")
                image, meta = render_performance_chart(history, month=month)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(history)
            images.append(image)
            results.append(result_payload(history.ticker, history.provider, history.note, meta))
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return response_payload(
            images,
            mixed_provider(histories),
            "Batch monthly performance render",
            meta,
            mode=chart_key,
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "regression":
        selection = resolve_ticker_selection(payload, watchlist_client)
        images = []
        histories = []
        results = []
        errors = []
        for ticker in selection.tickers:
            try:
                history = client.get_history(
                    ticker,
                    period=str(payload.get("period") or "1y"),
                    start=payload.get("start_date"),
                    end=payload.get("end_date"),
                )
                image, meta = render_regression_chart(history)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(history)
            images.append(image)
            results.append(result_payload(history.ticker, history.provider, history.note, meta))
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return response_payload(
            images,
            mixed_provider(histories),
            "Batch regression render",
            meta,
            mode=chart_key,
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "portfolio":
        selection = resolve_ticker_selection(payload, watchlist_client)
        history_options = {
            "start": payload.get("start_date"),
            "end": payload.get("end_date"),
            "period": "1y",
        }
        histories, errors = collect_histories(
            client,
            selection.tickers,
            **history_options,
        )
        require_histories(histories, errors)
        benchmark = collect_benchmark(client, payload, errors, **history_options)
        image, portfolio_meta = render_portfolio_chart(
            histories,
            investment_per_stock=float(payload.get("investment_per_stock") or 100),
            benchmark=benchmark,
        )
        results = [
            result_payload(
                history.ticker,
                history.provider,
                history.note,
                {"final_value": portfolio_meta["final_values"][history.ticker]},
            )
            for history in histories
        ]
        meta = {
            **portfolio_meta,
            **batch_meta(results, errors, selection.watchlist),
        }
        return response_payload(
            [image],
            mixed_provider(histories),
            "Mixed provider portfolio render",
            meta,
            mode=chart_key,
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "volatility":
        selection = resolve_ticker_selection(payload, watchlist_client)
        histories, errors = collect_histories(client, selection.tickers, period="1y")
        require_histories(histories, errors)
        image, meta = render_volatility_chart(histories)
        results = [
            result_payload(history.ticker, history.provider, history.note, row)
            for history, row in zip(histories, meta["rows"], strict=False)
        ]
        meta = {**meta, **batch_meta(results, errors, selection.watchlist)}
        return response_payload(
            [image],
            mixed_provider(histories),
            "Mixed provider volatility render",
            meta,
            mode=chart_key,
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    raise ValueError(f"Unsupported chart type: {chart_type}")


def build_analysis_response(
    client: MarketDataClient,
    watchlist_client: TradingViewWatchlistClient,
    payload: dict[str, Any],
    *,
    text_generator: Any | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
) -> dict[str, Any]:
    selection = resolve_ticker_selection(payload, watchlist_client)
    summaries, errors = collect_summaries(client, selection.tickers)
    require_results(summaries, errors)
    scanner = build_scanner_rows(summaries)
    generated = generate_analysis_brief(
        summaries,
        scanner,
        text_generator=text_generator,
        api_key=api_key,
        text_model=text_model,
    )
    providers = sorted({str(summary["provider"]) for summary in summaries})
    provider = "+".join(providers)
    meta = batch_meta(
        [
            result_payload(
                str(summary["ticker"]),
                str(summary["provider"]),
                str(summary["provider_note"]),
                summary,
            )
            for summary in summaries
        ],
        errors,
        selection.watchlist,
    )
    meta["scanner_count"] = len(scanner)
    meta["text_provider"] = generated.provider
    meta["text_model"] = generated.model
    export = export_payload(
        mode="analysis",
        provider=provider,
        provider_note="Batch stock brief",
        tickers=[str(summary["ticker"]) for summary in summaries],
        meta=meta,
        watchlist=selection.watchlist,
        image_files=[],
        summaries=summaries,
        scanner=scanner,
    )
    export["anthropic_brief"] = generated.text
    export["text_provider"] = generated.provider
    export["text_model"] = generated.model
    response = {
        "summaries": summaries,
        "scanner": scanner,
        "Anthropic Brief": generated.text,
        "Text Provider": generated.provider,
        "Text Model": generated.model,
        "provider": provider,
        "provider_note": "Batch stock brief",
        "meta": meta,
        "watchlist": watchlist_payload(selection.watchlist),
        "export": export,
    }
    return response


def response_payload(
    images: list[RenderedImage],
    provider: str,
    note: str,
    meta: dict[str, Any],
    *,
    mode: str,
    tickers: list[str],
    watchlist: WatchlistResult | None,
) -> dict[str, Any]:
    payload = {
        "images": [image.__dict__ for image in images],
        "provider": provider,
        "provider_note": note,
        "meta": meta,
        "watchlist": watchlist_payload(watchlist),
    }
    payload["export"] = export_payload(
        mode=mode,
        provider=provider,
        provider_note=note,
        tickers=tickers,
        meta=meta,
        watchlist=watchlist,
        image_files=[{"filename": image.filename, "mime": image.mime} for image in images],
    )
    return payload


def first_ticker(payload: dict[str, Any]) -> str:
    tickers = ticker_list(payload)
    return tickers[0]


def ticker_list(payload: dict[str, Any]) -> list[str]:
    raw = payload.get("tickers") or payload.get("ticker") or "AAPL"
    if isinstance(raw, str):
        tickers = [part.strip().upper() for part in raw.split(",")]
    else:
        tickers = [str(part).strip().upper() for part in raw]
    cleaned = [clean_ticker(ticker) for ticker in tickers if ticker]
    if not cleaned:
        raise ValueError("At least one ticker is required")
    return cleaned


def resolve_ticker_selection(
    payload: dict[str, Any], watchlist_client: TradingViewWatchlistClient
) -> TickerSelection:
    watchlist_url = str(payload.get("watchlist_url") or "").strip()
    limit = max_results(payload)
    if watchlist_url:
        watchlist = watchlist_client.get_watchlist(watchlist_url)
        return TickerSelection(tickers=watchlist.tickers[:limit], watchlist=watchlist)

    return TickerSelection(tickers=ticker_list(payload)[:limit])


def max_results(payload: dict[str, Any]) -> int:
    raw = payload.get("max_results") or DEFAULT_MAX_RESULTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_RESULTS
    return max(1, min(value, MAX_RESULTS_CAP))


def collect_benchmark(
    client: MarketDataClient,
    payload: dict[str, Any],
    errors: list[dict[str, str]],
    **history_options: Any,
) -> HistoryResult | None:
    benchmark_label = str(payload.get("benchmark_ticker") or "SPY").strip().upper() or "Benchmark"
    try:
        benchmark_ticker = clean_ticker(benchmark_label)
        return client.get_history(benchmark_ticker, **history_options)
    except (ValueError, MarketDataError) as exc:
        errors.append({"ticker": benchmark_label, "error": f"Benchmark unavailable: {exc}"})
        return None


def collect_histories(
    client: MarketDataClient, tickers: list[str], **history_options: Any
) -> tuple[list[HistoryResult], list[dict[str, str]]]:
    history_slots: list[HistoryResult | None] = [None] * len(tickers)
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=batch_worker_count(tickers)) as executor:
        futures = {
            executor.submit(client.get_history, ticker, **history_options): (index, ticker)
            for index, ticker in enumerate(tickers)
        }
        for future in as_completed(futures):
            index, ticker = futures[future]
            try:
                history_slots[index] = future.result()
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
    return [history for history in history_slots if history is not None], errors


def collect_summaries(
    client: MarketDataClient, tickers: list[str]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    summary_slots: list[dict[str, Any] | None] = [None] * len(tickers)
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=batch_worker_count(tickers)) as executor:
        futures = {
            executor.submit(summarize_stock, client, ticker): (index, ticker)
            for index, ticker in enumerate(tickers)
        }
        for future in as_completed(futures):
            index, ticker = futures[future]
            try:
                summary_slots[index] = future.result()
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
    return [summary for summary in summary_slots if summary is not None], errors


def batch_worker_count(tickers: list[str]) -> int:
    return max(1, min(len(tickers), 8))


def result_payload(
    ticker: str, provider: str, provider_note: str, meta: dict[str, Any]
) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "provider": provider,
        "provider_note": provider_note,
        "meta": meta,
    }


def batch_meta(
    results: list[dict[str, Any]],
    errors: list[dict[str, str]],
    watchlist: WatchlistResult | None,
) -> dict[str, Any]:
    return {
        "result_count": len(results),
        "error_count": len(errors),
        "watchlist_name": watchlist.name if watchlist else "Manual tickers",
        "results": results,
        "errors": errors,
    }


def require_results(results: list[Any], errors: list[dict[str, str]]) -> None:
    if results:
        return
    error_text = "; ".join(f"{error['ticker']}: {error['error']}" for error in errors)
    raise MarketDataError(error_text or "No results could be generated")


def require_histories(histories: list[Any], errors: list[dict[str, str]]) -> None:
    require_results(histories, errors)


def export_payload(
    *,
    mode: str,
    provider: str,
    provider_note: str,
    tickers: list[str],
    meta: dict[str, Any],
    watchlist: WatchlistResult | None,
    image_files: list[dict[str, str]],
    summaries: list[dict[str, Any]] | None = None,
    scanner: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "provider": provider,
        "provider_note": provider_note,
        "tickers": tickers,
        "watchlist": watchlist_payload(watchlist),
        "image_files": image_files,
        "meta": meta,
        "summaries": summaries or [],
        "scanner": scanner or [],
    }


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
    def compat_stock_fax(ticker: str) -> Any:
        try:
            return jsonify(
                build_stock_fax(get_market_client(), ticker, **text_generation_options())
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"Ticker": ticker.upper(), "Error": str(exc)}), 400

    @app.get("/micro_memo/<ticker>")
    def compat_micro_memo(ticker: str) -> Any:
        try:
            memo = build_market_memo(get_market_client(), ticker, **text_generation_options())
            return jsonify({"Ticker": memo["Ticker"], "Market Memo": memo["Market Memo"]})
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"Ticker": ticker.upper(), "Error": str(exc)}), 400

    @app.post("/generate-image")
    def compat_generate_image() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            result = generate_pixel_image(
                str(payload.get("prompt") or ""),
                api_key=current_app.config.get("OPENAI_API_KEY"),
                image_model=current_app.config.get("OPENAI_IMAGE_MODEL"),
            )
            return jsonify(
                {
                    "created": result["created"],
                    "image": result["image"],
                    "urls": [],
                }
            )
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/plot-moneylines")
    @app.post("/plot-moneywall")
    def compat_moneyline() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            image, _meta = render_moneyline_chart(
                str(payload.get("ticker") or ""), expiry=payload.get("expiry")
            )
            return jsonify({"image": image.data})
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400


def compat_chart(chart_type: str) -> Any:
    payload = request.get_json(silent=True) or {}
    try:
        response = build_chart_response(
            get_market_client(), get_watchlist_client(), chart_type, payload
        )
        return jsonify({"images": [image["data"] for image in response["images"]]})
    except (ValueError, MarketDataError, WatchlistError) as exc:
        return jsonify({"error": str(exc)}), 400


app = create_app()
