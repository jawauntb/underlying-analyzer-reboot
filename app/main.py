from __future__ import annotations

import hmac
import json
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import requests
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

from app._perf import ttl_cached
from app.agent import (
    AgentError,
    normalize_history,
    run_agent_stream,
    select_tools,
)
from app.api_catalog import build_api_docs_payload
from app.articles import (
    ArticleError,
    article_markdown,
    article_summary,
    normalize_article,
)
from app.mcp_http import (
    handle_mcp_payload,
    parse_error_response,
    server_descriptor,
)
from app.openapi import build_openapi_document
from app.tool_registry import tool_catalog_payload
from app.alert_scheduler import (
    DEFAULT_SCHEDULED_RULE_LIMIT,
    MAX_SCHEDULED_RULE_LIMIT,
    AlertDeliveryResult,
    AlertStoreError,
    ScheduledAlertRule,
    SupabaseAlertStore,
    alert_payload_from_rule,
    deliver_alert_webhook,
)
from app.alerts import (
    DEFAULT_ALERT_LIMIT,
    DEFAULT_VOLATILITY_THRESHOLD,
    MAX_ALERT_LIMIT,
    build_alert_digest,
)
from app.analysis import build_scanner_rows, summarize_stock
from app.anthropic import (
    DEFAULT_ANTHROPIC_MODEL,
    AnthropicError,
    AnthropicTextClient,
    MessageStreamer,
)
from app.charts import (
    RenderedImage,
    build_ridge_growth_memo,
    render_auction_chart,
    render_flow_compass_chart,
    render_performance_chart,
    render_portfolio_chart,
    render_regression_chart,
    render_ridge_growth_chart,
    render_volatility_chart,
)
from app.chart_data import (
    build_auction_chart_data,
    build_flow_compass_chart_data,
    build_performance_chart_data,
    build_portfolio_chart_data,
    build_regression_chart_data,
    build_ridge_growth_chart_data,
    build_torque_chart_data,
    build_volatility_chart_data,
)
from app.cockpit import build_cockpit_row
from app.exa import ExaClient
from app.market_data import (
    MAX_SEARCH_QUERY_LENGTH,
    SEARCH_PROVIDER,
    HistoryResult,
    MarketDataClient,
    MarketDataError,
    clean_ticker,
)
from app.memo_pdf import MemoPdfPayload, render_memo_pdf
from app.sec import SecClient, SecDataError
from app.torque import compute_torque_score, render_torque_chart
from app.torque_scan import (
    build_torque_scan_response,
    stream_torque_scan_rows,
)
from app.vision_v2 import (
    build_vision_v2_data,
    build_vision_v2_memo,
    parse_memo_sections,
    stream_vision_v2_text,
)
from app.tools import (
    DEFAULT_OPENAI_IMAGE_MODEL,
    build_market_memo,
    build_market_memo_charts,
    build_moneyline_data,
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

ROOT_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).parent / "static"
DOCS_DIR = ROOT_DIR / "docs"
DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_CAP = 50
RIDGE_GROWTH_PERIODS = ("6mo", "1y", "2y")
# Short TTL: SEC source packs are idempotent per ticker and only change when a new
# filing lands (quarterly cadence), so a few minutes of response caching is safe and
# spares repeat callers the per-filing fetch + assembly work on cache-cold clients.
SEC_SOURCE_PACK_TTL_SECONDS = 300


class SupabaseAuthError(ValueError):
    pass


@ttl_cached(SEC_SOURCE_PACK_TTL_SECONDS)
def _cached_sec_source_pack(client: SecClient, ticker: str) -> dict[str, Any]:
    """Memoize the idempotent SEC source pack by (client, ticker) for a short TTL.

    Keyed on the client instance so distinct app instances (and tests) never share
    cache state. Side-effect-free read; the returned dict is already a shared cached
    object inside ``SecClient`` and callers here only serialize it.
    """
    return client.get_source_pack(ticker)


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
    app.config["SEC_CLIENT"] = SecClient(user_agent=os.getenv("SEC_USER_AGENT"))
    app.config["EXA_API_KEY"] = os.getenv("EXA_API_KEY")
    app.config["EXA_CLIENT"] = ExaClient(api_key=app.config["EXA_API_KEY"])
    app.config["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
    app.config["OPENAI_IMAGE_MODEL"] = os.getenv("OPENAI_IMAGE_MODEL", DEFAULT_OPENAI_IMAGE_MODEL)
    app.config["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY")
    app.config["ANTHROPIC_TEXT_MODEL"] = os.getenv(
        "ANTHROPIC_TEXT_MODEL", DEFAULT_ANTHROPIC_MODEL
    )
    app.config["ANTHROPIC_AGENT_MODEL"] = os.getenv("ANTHROPIC_AGENT_MODEL")
    app.config["AGENT_CLIENT"] = None
    app.config["SUPABASE_URL"] = public_env("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    app.config["SUPABASE_ANON_KEY"] = public_env(
        "SUPABASE_ANON_KEY", "NEXT_PUBLIC_SUPABASE_ANON_KEY"
    )
    app.config["SUPABASE_SERVICE_ROLE_KEY"] = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    app.config["ALERT_SCHEDULER_TOKEN"] = os.getenv("ALERT_SCHEDULER_TOKEN")
    app.config["ALERT_STORE"] = None
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

    @app.get("/chat")
    def chat_console() -> Response:
        return send_from_directory(STATIC_DIR, "chat.html")

    @app.get("/design")
    def design_sandbox() -> Response:
        return send_from_directory(STATIC_DIR, "design.html")

    @app.get("/docs")
    def docs_page() -> Response:
        return send_from_directory(STATIC_DIR, "docs.html")

    @app.get("/docs/api.md")
    def docs_api_markdown() -> Response:
        return send_from_directory(DOCS_DIR, "api.md", mimetype="text/markdown")

    @app.get("/docs/chart-data-rendering.md")
    def docs_chart_data_rendering_markdown() -> Response:
        return send_from_directory(
            DOCS_DIR, "chart-data-rendering.md", mimetype="text/markdown"
        )

    @app.get("/api/health")
    def health() -> Any:
        return jsonify({"ok": True, "service": "underlying-analyzer-reboot"})

    @app.get("/api/docs")
    def api_docs_catalog() -> Any:
        base_url = request.url_root.rstrip("/") if request.url_root else None
        return jsonify(build_api_docs_payload(base_url=base_url))

    @app.get("/api/config")
    def public_config() -> Any:
        supabase_url = current_app.config.get("SUPABASE_URL")
        supabase_anon_key = current_app.config.get("SUPABASE_ANON_KEY")
        supabase_enabled = bool(supabase_url and supabase_anon_key)
        return jsonify(
            {
                "supabase": {
                    "enabled": supabase_enabled,
                    "url": supabase_url if supabase_enabled else None,
                    "anon_key": supabase_anon_key if supabase_enabled else None,
                }
            }
        )

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

    @app.get("/api/data/search")
    def security_search() -> Any:
        try:
            query = request.args.get("q", "").strip()
            if not query:
                raise ValueError("Search query is required")
            if len(query) > MAX_SEARCH_QUERY_LENGTH:
                raise ValueError(
                    f"Search query must be at most {MAX_SEARCH_QUERY_LENGTH} characters"
                )

            limit_value = request.args.get("limit")
            if limit_value is None:
                limit = 8
            else:
                try:
                    limit = int(limit_value)
                except ValueError as exc:
                    raise ValueError("Search limit must be an integer from 1 to 10") from exc
                if not 1 <= limit <= 10:
                    raise ValueError("Search limit must be an integer from 1 to 10")

            return jsonify(
                {
                    "query": query,
                    "results": get_market_client().search_securities(query, limit=limit),
                    "provider": SEARCH_PROVIDER,
                }
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except MarketDataError as exc:
            return jsonify({"error": str(exc)}), 502

    @app.get("/api/sec/<ticker>")
    def sec_source_pack(ticker: str) -> Any:
        try:
            return jsonify(_cached_sec_source_pack(get_sec_client(), ticker))
        except (ValueError, SecDataError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

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

    @app.post("/api/data/charts/<chart_type>")
    def chart_data(chart_type: str) -> Any:
        """Same chart math as /api/charts/<type>, but JSON series instead of PNGs."""
        try:
            payload = request.get_json(silent=True) or {}
            response = build_chart_data_response(
                get_market_client(), get_watchlist_client(), chart_type, payload
            )
            return jsonify(response)
        except (ValueError, MarketDataError, WatchlistError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected chart data error")
            return jsonify({"error": f"Unexpected chart data error: {exc}"}), 500

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

    @app.post("/api/watchlists/cockpit")
    def watchlist_cockpit() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            response = build_cockpit_response(
                get_market_client(), get_watchlist_client(), payload
            )
            return jsonify(response)
        except (ValueError, WatchlistError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - defensive request boundary
            app.logger.exception("Unexpected watchlist cockpit error")
            return jsonify({"error": f"Unexpected watchlist cockpit error: {exc}"}), 500

    @app.post("/api/watchlists/alerts")
    def watchlist_alerts() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            response = build_alerts_response(
                get_market_client(), get_watchlist_client(), payload
            )
            return jsonify(response)
        except (ValueError, WatchlistError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - defensive request boundary
            app.logger.exception("Unexpected watchlist alerts error")
            return jsonify({"error": f"Unexpected watchlist alerts error: {exc}"}), 500

    @app.get("/api/alerts/scheduler/status")
    def alert_scheduler_status() -> Any:
        return jsonify(
            {
                "configured": alert_scheduler_configured(),
                "service_role_configured": bool(
                    current_app.config.get("SUPABASE_SERVICE_ROLE_KEY")
                ),
                "token_configured": bool(current_app.config.get("ALERT_SCHEDULER_TOKEN")),
                "schedule": "daily",
            }
        )

    @app.post("/api/alerts/webhook/test")
    def alert_webhook_test() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            user_id = supabase_user_id_from_bearer(request.headers.get("Authorization"))
            response = run_alert_webhook_test(
                get_alert_store(),
                user_id=user_id,
                rule_id=str(payload.get("rule_id") or "").strip(),
                run_date=scheduled_run_date(payload),
            )
            return jsonify(response)
        except SupabaseAuthError as exc:
            return jsonify({"error": str(exc)}), 401
        except (ValueError, AlertStoreError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - defensive delivery boundary
            app.logger.exception("Unexpected webhook test error")
            return jsonify({"error": f"Unexpected webhook test error: {exc}"}), 500

    @app.post("/api/alerts/scheduled/run")
    def scheduled_alert_run() -> Any:
        auth_error = scheduler_auth_error(request.headers.get("Authorization"))
        if auth_error:
            status_code = 503 if auth_error == "Scheduler token is not configured" else 401
            return jsonify({"error": auth_error}), status_code

        payload = request.get_json(silent=True) or {}
        try:
            response = run_scheduled_alert_rules(
                get_market_client(),
                get_watchlist_client(),
                get_alert_store(),
                run_date=scheduled_run_date(payload),
                force=bool(payload.get("force")),
                limit=scheduled_rule_limit(payload),
            )
            return jsonify(response)
        except (ValueError, AlertStoreError, MarketDataError, WatchlistError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - defensive scheduler boundary
            app.logger.exception("Unexpected scheduled alert error")
            return jsonify({"error": f"Unexpected scheduled alert error: {exc}"}), 500

    @app.post("/api/tools/fax")
    def stock_fax_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            return jsonify(
                build_stock_fax(
                    get_market_client(),
                    str(payload.get("ticker") or ""),
                    sec_client=get_sec_client(),
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
                    sec_client=get_sec_client(),
                    **text_generation_options(),
                )
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

    @app.post("/api/tools/vision/stream")
    def vision_tool_stream() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            ticker = str(payload.get("ticker") or "")
            client = get_market_client()
            report = build_stock_fax_data(client, ticker, sec_client=get_sec_client())
            charts, chart_errors = build_market_memo_charts(client, ticker)
            options = text_generation_options()
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

        return _ndjson_streaming_response(
            vision_stream_events(report, options, charts, chart_errors)
        )

    @app.post("/api/tools/vision/v2")
    def vision_v2_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            return jsonify(
                build_vision_v2_memo(
                    get_market_client(),
                    str(payload.get("ticker") or ""),
                    sec_client=get_sec_client(),
                    exa_client=get_exa_client(),
                    **text_generation_options(),
                )
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected Vision v2 error")
            return jsonify({"error": f"Unexpected Vision v2 error: {exc}"}), 500

    @app.post("/api/tools/vision/v2/stream")
    def vision_v2_tool_stream() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            ticker = clean_ticker(str(payload.get("ticker") or ""))
            options = text_generation_options()
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400

        return _ndjson_streaming_response(
            vision_v2_phased_stream(
                ticker=ticker,
                market_client=get_market_client(),
                sec_client=get_sec_client(),
                exa_client=get_exa_client(),
                options=options,
            )
        )

    @app.post("/api/tools/vision/v2/pdf")
    def vision_v2_pdf_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            ticker = str(payload.get("ticker") or "")
            if not ticker.strip():
                return jsonify({"error": "ticker is required"}), 400
            client = get_market_client()
            memo_text = str(payload.get("memo_text") or "").strip()
            report_payload = payload.get("report")

            if memo_text and isinstance(report_payload, dict):
                report = report_payload
            else:
                memo = build_vision_v2_memo(
                    client,
                    ticker,
                    sec_client=get_sec_client(),
                    exa_client=get_exa_client(),
                    **text_generation_options(),
                )
                memo_text = str(memo.get("Memo Text") or "")
                report = memo

            charts_input = payload.get("charts")
            if not isinstance(charts_input, list):
                try:
                    rendered_charts, _ = build_market_memo_charts(client, ticker)
                    charts_input = [
                        {
                            "title": c.get("meta", {}).get("title") or "Chart",
                            "data": c.get("image", {}).get("data"),
                            "mime": c.get("image", {}).get("mime", "image/png"),
                            "caption": c.get("meta", {}).get("caption"),
                        }
                        for c in rendered_charts
                        if isinstance(c, dict)
                    ]
                except Exception:
                    charts_input = []

            pdf_bytes = render_memo_pdf(
                build_memo_pdf_payload(ticker, memo_text, report, charts_input)
            )
            filename = f"{ticker.upper()}-vision-memo.pdf"
            return Response(
                pdf_bytes,
                mimetype="application/pdf",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(pdf_bytes)),
                    "Cache-Control": "no-store",
                },
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected Vision v2 PDF error")
            return jsonify({"error": f"Unexpected Vision v2 PDF error: {exc}"}), 500

    @app.post("/api/tools/torque")
    def torque_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            ticker = clean_ticker(str(payload.get("ticker") or ""))
            client = get_market_client()
            sec_client = get_sec_client()

            def _load_profile() -> dict[str, Any]:
                try:
                    return client.get_profile(ticker)
                except Exception:
                    return {}

            def _load_sec_trend() -> dict[str, Any] | None:
                try:
                    from app.sec_trend import build_sec_trend_pack

                    return build_sec_trend_pack(sec_client, ticker, quarters=8)
                except Exception:
                    return None

            # These three fetches are independent (all keyed by ticker); run them
            # concurrently. history_future.result() re-raises MarketDataError/ValueError
            # into the route's existing handler exactly as the sequential version did.
            with ThreadPoolExecutor(max_workers=3) as executor:
                history_future = executor.submit(
                    client.get_history, ticker, period="2y", interval="1d"
                )
                profile_future = executor.submit(_load_profile)
                sec_trend_future = executor.submit(_load_sec_trend)
                history = history_future.result()
                profile = profile_future.result()
                sec_trend_pack: dict[str, Any] | None = sec_trend_future.result()
            torque_result = compute_torque_score(
                history=history,
                sec_trend=sec_trend_pack,
                profile=profile,
                market_cap=profile.get("marketCap") if isinstance(profile, dict) else None,
            )
            image, meta = render_torque_chart(
                history=history,
                sec_trend=sec_trend_pack,
                profile=profile,
                torque=torque_result,
            )
            from dataclasses import asdict as _asdict

            export = {
                "generated_at": datetime.now(UTC).isoformat(),
                "mode": "torque",
                "ticker": ticker,
                "torque": _asdict(torque_result),
                "meta": meta,
                "image_files": [{"filename": image.filename, "mime": image.mime}],
            }
            return jsonify(
                {
                    "image": image.__dict__,
                    "meta": meta,
                    "torque": _asdict(torque_result),
                    "export": export,
                }
            )
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected torque error")
            return jsonify({"error": f"Unexpected torque error: {exc}"}), 500

    @app.post("/api/data/tools/torque")
    def torque_data_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            ticker = clean_ticker(str(payload.get("ticker") or ""))
            client = get_market_client()
            sec_client = get_sec_client()

            def _load_profile() -> dict[str, Any]:
                try:
                    return client.get_profile(ticker)
                except Exception:
                    return {}

            def _load_sec_trend() -> dict[str, Any] | None:
                try:
                    from app.sec_trend import build_sec_trend_pack

                    return build_sec_trend_pack(sec_client, ticker, quarters=8)
                except Exception:
                    return None

            with ThreadPoolExecutor(max_workers=3) as executor:
                history_future = executor.submit(
                    client.get_history, ticker, period="2y", interval="1d"
                )
                profile_future = executor.submit(_load_profile)
                sec_trend_future = executor.submit(_load_sec_trend)
                history = history_future.result()
                profile = profile_future.result()
                sec_trend_pack: dict[str, Any] | None = sec_trend_future.result()
            dataset = build_torque_chart_data(
                history=history,
                sec_trend=sec_trend_pack,
                profile=profile,
            )
            export = {
                "generated_at": datetime.now(UTC).isoformat(),
                "mode": "torque-data",
                "ticker": ticker,
                "torque": dataset.get("torque"),
                "meta": dataset.get("meta"),
                "image_files": [],
            }
            return jsonify({**dataset, "export": export})
        except (ValueError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            app.logger.exception("Unexpected torque data error")
            return jsonify({"error": f"Unexpected torque data error: {exc}"}), 500

    @app.post("/api/tools/torque/scan")
    def torque_scan_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            response = build_torque_scan_response(
                get_market_client(),
                get_watchlist_client(),
                payload,
                sec_client=current_app.config.get("SEC_CLIENT"),
                exa_client=current_app.config.get("EXA_CLIENT"),
            )
            return jsonify(response)
        except (ValueError, WatchlistError, MarketDataError) as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:  # pragma: no cover - defensive request boundary
            app.logger.exception("Unexpected torque scan error")
            return jsonify({"error": f"Unexpected torque scan error: {exc}"}), 500

    @app.post("/api/tools/torque/scan/stream")
    def torque_scan_tool_stream() -> Any:
        payload = request.get_json(silent=True) or {}
        market_client = get_market_client()
        watchlist_client = get_watchlist_client()
        sec_client = current_app.config.get("SEC_CLIENT")
        exa_client = current_app.config.get("EXA_CLIENT")
        return _ndjson_streaming_response(
            stream_torque_scan_rows(
                market_client,
                watchlist_client,
                payload,
                sec_client=sec_client,
                exa_client=exa_client,
            )
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

    @app.post("/api/data/tools/moneyline")
    def moneyline_data_tool() -> Any:
        try:
            payload = request.get_json(silent=True) or {}
            dataset = build_moneyline_data(
                str(payload.get("ticker") or ""), expiry=payload.get("expiry")
            )
            export = {
                "generated_at": datetime.now(UTC).isoformat(),
                "mode": "moneyline-data",
                "ticker": dataset["ticker"],
                "meta": dataset["meta"],
                "image_files": [],
            }
            return jsonify({**dataset, "export": export})
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

    @app.post("/api/news")
    def news_search() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            return jsonify(build_news_response(get_exa_client(), payload))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

    @app.get("/api/openapi")
    def openapi_document() -> Any:
        base_url = request.url_root.rstrip("/") if request.url_root else None
        return jsonify(build_openapi_document(base_url=base_url))

    @app.get("/api/mcp")
    def mcp_descriptor() -> Any:
        base_url = request.url_root.rstrip("/") if request.url_root else None
        return jsonify(server_descriptor(base_url=base_url))

    @app.post("/api/mcp")
    def mcp_endpoint() -> Any:
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify(parse_error_response()), 400

        result = handle_mcp_payload(payload)
        if result is None:
            return Response(status=202)

        if "text/event-stream" in (request.headers.get("Accept") or ""):
            body = f"event: message\ndata: {json.dumps(result)}\n\n"
            response = Response(body, mimetype="text/event-stream")
            response.headers["Cache-Control"] = "no-cache, no-transform"
            response.headers["X-Accel-Buffering"] = "no"
            return response
        return jsonify(result)

    @app.get("/api/agent/tools")
    def agent_tool_catalog() -> Any:
        catalog = tool_catalog_payload()
        catalog["agent_ready"] = bool(
            current_app.config.get("ANTHROPIC_API_KEY")
            or current_app.config.get("AGENT_CLIENT")
        )
        catalog["model"] = current_app.config.get("ANTHROPIC_AGENT_MODEL")
        return jsonify(catalog)

    @app.post("/api/agent/article")
    def agent_article() -> Any:
        payload = request.get_json(silent=True) or {}
        try:
            article = normalize_article(payload)
        except ArticleError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(
            {
                "article": article,
                "markdown": article_markdown(article),
                "summary": article_summary(article),
            }
        )

    @app.post("/api/agent/chat/stream")
    def agent_chat_stream() -> Any:
        try:
            options = agent_run_options(request.get_json(silent=True) or {})
        except (AgentError, AnthropicError) as exc:
            return jsonify({"error": str(exc)}), 400

        def events() -> Iterator[str]:
            for event in run_agent_stream(**options):
                yield ndjson(event)

        return _ndjson_streaming_response(events())

    @app.post("/api/agent/chat")
    def agent_chat() -> Any:
        try:
            options = agent_run_options(request.get_json(silent=True) or {})
        except (AgentError, AnthropicError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(collect_agent_turn(run_agent_stream(**options)))

    register_compat_routes(app)
    return app


def get_market_client() -> MarketDataClient:
    return current_app.config["MARKET_DATA_CLIENT"]


def get_watchlist_client() -> TradingViewWatchlistClient:
    return current_app.config["WATCHLIST_CLIENT"]


def get_sec_client() -> SecClient:
    return current_app.config["SEC_CLIENT"]


def get_exa_client() -> ExaClient:
    return current_app.config["EXA_CLIENT"]


def get_alert_store() -> SupabaseAlertStore:
    configured_store = current_app.config.get("ALERT_STORE")
    if configured_store is not None:
        return configured_store
    return SupabaseAlertStore(
        supabase_url=str(current_app.config.get("SUPABASE_URL") or ""),
        service_role_key=str(current_app.config.get("SUPABASE_SERVICE_ROLE_KEY") or ""),
    )


def alert_scheduler_configured() -> bool:
    return bool(
        current_app.config.get("SUPABASE_URL")
        and current_app.config.get("SUPABASE_SERVICE_ROLE_KEY")
        and current_app.config.get("ALERT_SCHEDULER_TOKEN")
    )


def scheduler_auth_error(header_value: str | None) -> str | None:
    expected = str(current_app.config.get("ALERT_SCHEDULER_TOKEN") or "")
    if not expected:
        return "Scheduler token is not configured"
    prefix = "Bearer "
    if not header_value or not header_value.startswith(prefix):
        return "Scheduler authorization is required"
    provided = header_value.removeprefix(prefix).strip()
    if not hmac.compare_digest(provided, expected):
        return "Scheduler authorization is invalid"
    return None


def supabase_user_id_from_bearer(header_value: str | None) -> str:
    prefix = "Bearer "
    if not header_value or not header_value.startswith(prefix):
        raise SupabaseAuthError("Supabase authorization is required")
    token = header_value.removeprefix(prefix).strip()
    if not token:
        raise SupabaseAuthError("Supabase authorization is required")

    supabase_url = str(current_app.config.get("SUPABASE_URL") or "").rstrip("/")
    supabase_anon_key = str(current_app.config.get("SUPABASE_ANON_KEY") or "")
    if not supabase_url or not supabase_anon_key:
        raise SupabaseAuthError("Supabase auth is not configured")

    try:
        response = requests.get(
            f"{supabase_url}/auth/v1/user",
            headers={
                "apikey": supabase_anon_key,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise SupabaseAuthError("Supabase authorization could not be verified") from exc
    if response.status_code >= 400:
        raise SupabaseAuthError("Supabase authorization is invalid")
    try:
        data = response.json()
    except ValueError as exc:
        raise SupabaseAuthError("Supabase authorization is invalid") from exc
    user_id = str(data.get("id") or "").strip()
    if not user_id:
        raise SupabaseAuthError("Supabase authorization is invalid")
    return user_id


def _ndjson_streaming_response(generator: Iterator[str]) -> Response:
    """Wrap an NDJSON generator with anti-buffering headers so Railway / nginx /
    intermediate proxies don't hold rows until the connection closes."""

    def _prelude_then(gen: Iterator[str]) -> Iterator[str]:
        # ~4KB of leading newlines forces nginx/Railway proxy buffers past
        # their initial threshold so the first real event flushes promptly.
        # Newlines are skippable by every line-based NDJSON consumer.
        yield "\n" * 4096
        yield from gen

    response = Response(
        stream_with_context(_prelude_then(generator)),
        mimetype="application/x-ndjson",
    )
    response.headers["Cache-Control"] = "no-cache, no-transform"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Connection"] = "keep-alive"
    return response


def public_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return None


def get_agent_client() -> MessageStreamer:
    """Streaming Messages client for the research agent.

    Tests inject ``AGENT_CLIENT``; production builds one from the configured
    Anthropic key, preferring ``ANTHROPIC_AGENT_MODEL`` when it is set so the
    conversational agent can run a different model from memo generation.
    """
    configured = current_app.config.get("AGENT_CLIENT")
    if configured is not None:
        return configured

    api_key = current_app.config.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AnthropicError(
            "ANTHROPIC_API_KEY is not configured, so the research agent is offline."
        )
    return AnthropicTextClient(
        api_key=str(api_key),
        model=str(
            current_app.config.get("ANTHROPIC_AGENT_MODEL")
            or current_app.config.get("ANTHROPIC_TEXT_MODEL")
            or DEFAULT_ANTHROPIC_MODEL
        ),
    )


def agent_run_options(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a chat request body into ``run_agent_stream`` keyword arguments."""
    client = get_agent_client()
    messages = normalize_history(payload.get("messages"))
    context = payload.get("context")
    tool_policy = payload.get("tool_policy")
    if "tool_policy" in payload and tool_policy != "exact":
        raise AgentError("tool_policy must be 'exact' when provided")
    exact_tool_policy = tool_policy == "exact"
    return {
        "client": client,
        "messages": messages,
        "model": getattr(client, "model", None),
        "tool_specs": select_tools(
            payload.get("tools"), exact=exact_tool_policy
        ),
        "suppress_refused_tool_events": exact_tool_policy,
        "system_extra": context if isinstance(context, str) else None,
    }


def collect_agent_turn(events: Iterator[dict[str, Any]]) -> dict[str, Any]:
    """Fold the agent event stream into one non-streaming response body."""
    model = ""
    tools: list[str] = []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    articles: list[dict[str, Any]] = []
    tool_trace: list[str] = []
    error: str | None = None
    stop_reason = "end_turn"

    for event in events:
        kind = event.get("type")
        if kind == "start":
            model = str(event.get("model") or "")
            event_tools = event.get("tools")
            if isinstance(event_tools, list):
                tools = [str(item) for item in event_tools]
        elif kind == "text":
            text_parts.append(str(event.get("text") or ""))
        elif kind == "tool_result":
            tool_calls.append(
                {
                    "name": event.get("name"),
                    "ok": event.get("ok"),
                    "duration_ms": event.get("duration_ms"),
                    "error": event.get("error"),
                }
            )
            for artifact in event.get("artifacts") or []:
                artifacts.append(artifact)
        elif kind == "article":
            articles.append(event.get("article") or {})
        elif kind == "error":
            error = str(event.get("message") or "Agent run failed")
        elif kind == "done":
            stop_reason = str(event.get("stop_reason") or "end_turn")
            trace = event.get("tool_trace")
            if isinstance(trace, list):
                tool_trace = [str(item) for item in trace]
            if not text_parts and event.get("text"):
                text_parts.append(str(event["text"]))

    if error:
        return {"error": error}
    return {
        "ok": True,
        "model": model,
        "tools": tools,
        "stop_reason": stop_reason,
        "text": "".join(text_parts).strip(),
        "tool_calls": tool_calls,
        "tool_trace": tool_trace,
        "artifacts": artifacts,
        "articles": articles,
    }


def build_news_response(
    exa_client: ExaClient, payload: dict[str, Any]
) -> dict[str, Any]:
    """Recent news for a ticker and/or free-text topic."""
    ticker = str(payload.get("ticker") or "").strip().upper()
    query = str(payload.get("query") or "").strip()
    if not query and not ticker:
        raise ValueError("Provide a query, a ticker, or both")

    days_back = _bounded_int(payload.get("days_back"), default=14, low=1, high=90)
    num_results = _bounded_int(payload.get("num_results"), default=6, low=1, high=12)

    search_terms = " ".join(part for part in [ticker, query] if part).strip()
    if not getattr(exa_client, "api_key", None):
        return {
            "ok": False,
            "status": "not configured",
            "provider": "Exa",
            "query": search_terms,
            "results": [],
            "error": "EXA_API_KEY is not configured, so news search is unavailable.",
        }

    start = (datetime.now(UTC) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    try:
        results = exa_client.search(
            search_terms,
            num_results=num_results,
            start_published_date=start,
            category="news",
        )
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "provider": "Exa",
            "query": search_terms,
            "results": [],
            "error": f"News search failed: {exc}",
        }

    return {
        "ok": True,
        "status": "ok",
        "provider": "Exa",
        "query": search_terms,
        "ticker": ticker or None,
        "days_back": days_back,
        "results": [
            {
                "title": result.title,
                "url": result.url,
                "published_date": result.published_date,
                "snippet": result.snippet,
                "author": result.author,
            }
            for result in results
        ],
    }


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def text_generation_options() -> dict[str, Any]:
    configured_generator = current_app.config.get("TEXT_GENERATOR")
    if configured_generator is not None:
        return {"text_generator": configured_generator}
    return {
        "api_key": current_app.config.get("ANTHROPIC_API_KEY"),
        "text_model": current_app.config.get("ANTHROPIC_TEXT_MODEL"),
    }


def vision_stream_events(
    report: dict[str, Any],
    options: dict[str, Any],
    charts: list[dict[str, Any]] | None = None,
    chart_errors: list[dict[str, str]] | None = None,
) -> Iterator[str]:
    memo_charts = charts or []
    memo_chart_errors = chart_errors or []
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
        image_files=memo_chart_files(memo_charts),
    )
    export["market_memo"] = memo_text
    export["report"] = report
    export["memo_charts"] = memo_charts
    export["chart_errors"] = memo_chart_errors
    export["text_provider"] = text_provider
    export["text_model"] = text_model
    yield ndjson(
        {
            "type": "done",
            "text": memo_text,
            "export": export,
            "Memo Charts": memo_charts,
            "Chart Errors": memo_chart_errors,
            **meta,
        }
    )


def vision_v2_phased_stream(
    *,
    ticker: str,
    market_client: MarketDataClient,
    sec_client: SecClient,
    exa_client: ExaClient,
    options: dict[str, Any],
) -> Iterator[str]:
    """Streaming Vision v2 generator that emits phase progress events as it
    walks the pipeline, then streams Claude tokens, then verifies citations.
    """
    started_at = time.monotonic()
    text_provider, text_model = text_generation_identity(options)

    def elapsed_ms() -> int:
        return int((time.monotonic() - started_at) * 1000)

    def phase(phase_id: str, label: str, progress: float) -> str:
        return ndjson(
            {
                "type": "phase",
                "phase_id": phase_id,
                "label": label,
                "progress": progress,
                "elapsed_ms": elapsed_ms(),
            }
        )

    # Initial meta with ticker + provider identity. Report not yet built.
    yield ndjson(
        {
            "type": "meta",
            "mode": "vision-v2",
            "ticker": ticker,
            "text_provider": text_provider,
            "text_model": text_model,
        }
    )

    try:
        yield phase("profile", "Fetching ticker profile", 0.05)
        # build_vision_v2_data internally pulls profile, SEC source pack, SEC trend,
        # Exa research, torque, and reclassification. We emit a phase event before
        # each major chunk by calling smaller pieces directly when possible.
        from app.tools import build_sec_source_pack, build_stock_fax_data

        yield phase("sec", "Pulling SEC filings", 0.18)
        report = build_stock_fax_data(market_client, ticker, sec_client=sec_client)

        yield phase("trend", "Mining 8 quarters of XBRL", 0.32)
        try:
            from app.sec_trend import build_sec_trend_pack

            sec_trend = build_sec_trend_pack(sec_client, ticker, quarters=8)
        except Exception as exc:
            sec_trend = {"Status": "error", "Errors": [str(exc)]}
        report["SEC Trend Pack"] = sec_trend

        yield phase("exa", "Searching Exa for language mutation", 0.48)
        try:
            from app.exa import build_research_pack

            profile = market_client.get_profile(ticker)
            exa_research = build_research_pack(
                exa_client,
                ticker,
                str(profile.get("longName") or profile.get("shortName") or ticker),
                industry=profile.get("industry"),
                sector=profile.get("sector"),
            )
        except Exception as exc:
            exa_research = {"Status": "error", "Errors": [str(exc)]}
        report["Exa Research Pack"] = exa_research

        yield phase("torque", "Computing Torque score", 0.62)
        try:
            from dataclasses import asdict as _asdict

            from app.torque import compute_torque_score

            torque_result = compute_torque_score(
                history=market_client.get_history(ticker, period="2y", interval="1d"),
                sec_trend=sec_trend,
                profile=market_client.get_profile(ticker),
            )
            report["Torque"] = _asdict(torque_result)
        except Exception as exc:
            report["Torque"] = {"Status": "error", "Errors": [str(exc)]}

        yield phase("reclass", "Scoring reclassification", 0.70)
        try:
            from dataclasses import asdict as _asdict

            from app.reclassification import score_reclassification

            reclass_result = score_reclassification(
                ticker=ticker,
                profile=market_client.get_profile(ticker),
                history=market_client.get_history(ticker, period="2y", interval="1d"),
                sec_trend=sec_trend,
                sec_source_pack=report.get("SEC Source Pack"),
                exa_research=exa_research,
                torque_result=report.get("Torque"),
            )
            report["Reclassification"] = _asdict(reclass_result)
        except Exception as exc:
            report["Reclassification"] = {"Status": "error", "Errors": [str(exc)]}

        # Charts
        charts, chart_errors = build_market_memo_charts(market_client, ticker)

        yield phase("memo", "Drafting analyst memo", 0.80)
    except (ValueError, MarketDataError, SecDataError) as exc:
        yield ndjson({"type": "error", "error": str(exc)})
        return
    except Exception as exc:
        current_app.logger.exception("Unexpected Vision v2 data error")
        yield ndjson({"type": "error", "error": f"Unexpected Vision v2 data error: {exc}"})
        return

    chunks: list[str] = []
    token_count = 0
    try:
        for chunk in stream_vision_v2_text(report, **options):
            chunks.append(chunk)
            token_count += 1
            yield ndjson({"type": "token", "text": chunk})
    except (AnthropicError, ValueError, MarketDataError) as exc:
        yield ndjson({"type": "error", "error": str(exc)})
        return
    except Exception as exc:
        current_app.logger.exception("Unexpected Vision v2 stream error")
        yield ndjson({"type": "error", "error": f"Unexpected Vision v2 stream error: {exc}"})
        return

    memo_text = "".join(chunks)
    sections = parse_memo_sections(memo_text)

    # Citation verification
    yield phase("verify", "Verifying citations", 0.95)
    citations_payload: dict[str, Any] = {"status": "skipped"}
    try:
        from dataclasses import asdict as _asdict

        from app.citation_verify import verify_citations

        citation_result = verify_citations(memo_text, report=report)
        citations_payload = _asdict(citation_result)
    except Exception as exc:
        current_app.logger.exception("Citation verification failed")
        citations_payload = {"status": "error", "error": str(exc)}

    meta_payload = {
        "mode": "vision-v2",
        "ticker": str(report.get("Ticker") or ticker),
        "text_provider": text_provider,
        "text_model": text_model,
    }
    export = export_payload(
        mode="vision-v2",
        provider=str(report.get("Provider") or "market data"),
        provider_note=str(report.get("Provider Note") or "Vision v2 reclassification memo"),
        tickers=[str(report.get("Ticker") or ticker)],
        meta=meta_payload,
        watchlist=None,
        image_files=memo_chart_files(charts),
    )
    export["market_memo"] = memo_text
    export["memo_sections"] = sections
    export["report"] = report
    export["memo_charts"] = charts
    export["chart_errors"] = chart_errors
    export["text_provider"] = text_provider
    export["text_model"] = text_model
    export["torque"] = report.get("Torque")
    export["reclassification"] = report.get("Reclassification")
    export["citations"] = citations_payload
    export["elapsed_ms"] = elapsed_ms()
    export["token_count"] = token_count

    yield ndjson(
        {
            "type": "done",
            "text": memo_text,
            "export": export,
            "Memo Charts": charts,
            "Chart Errors": chart_errors,
            "Memo Sections": sections,
            "citations": citations_payload,
            "elapsed_ms": elapsed_ms(),
            "token_count": token_count,
            **meta_payload,
        }
    )


def build_memo_pdf_payload(
    ticker: str,
    memo_text: str,
    report: dict[str, Any],
    charts_input: list[dict[str, Any]] | None,
) -> MemoPdfPayload:
    snapshot = report.get("Snapshot") if isinstance(report.get("Snapshot"), dict) else {}
    torque = report.get("Torque") if isinstance(report.get("Torque"), dict) else None
    reclass = (
        report.get("Reclassification")
        if isinstance(report.get("Reclassification"), dict)
        else None
    )
    sections = report.get("Memo Sections")
    if not isinstance(sections, dict):
        sections = parse_memo_sections(memo_text or "")

    recommendation = "Hold"
    if reclass:
        rec = reclass.get("recommendation") or reclass.get("Recommendation")
        if rec:
            recommendation = str(rec)
    if torque:
        torque_rec = torque.get("recommendation") or torque.get("Recommendation")
        if torque_rec and recommendation == "Hold":
            recommendation = str(torque_rec)
    final_rating_section = sections.get("Final Rating + Target Price Band") if isinstance(sections, dict) else None
    if isinstance(final_rating_section, str) and "Rating:" in final_rating_section:
        try:
            recommendation = final_rating_section.split("Rating:", 1)[1].split(".")[0].strip()
        except Exception:
            pass

    scenarios = None
    if reclass:
        targets = {
            "low": reclass.get("target_low"),
            "mid": reclass.get("target_mid"),
            "high": reclass.get("target_high"),
        }
        if any(v is not None for v in targets.values()):
            scenarios = [
                {"name": "Bear", "price": targets["low"], "notes": "Conservative scenario"},
                {"name": "Base", "price": targets["mid"], "notes": "Base case"},
                {"name": "Bull", "price": targets["high"], "notes": "Reclassification thesis plays out"},
            ]

    citations: list[dict[str, Any]] = []
    sec_pack = report.get("SEC Source Pack")
    if isinstance(sec_pack, dict):
        for c in (sec_pack.get("Citations") or [])[:25]:
            if isinstance(c, dict):
                citations.append(
                    {
                        "label": c.get("Label") or c.get("label") or "SEC citation",
                        "source": c.get("Source") or "SEC EDGAR",
                        "url": c.get("URL") or c.get("url"),
                        "filed_date": c.get("Filed") or c.get("Filed Date") or c.get("filed_date"),
                    }
                )
    exa_pack = report.get("Exa Research Pack")
    if isinstance(exa_pack, dict):
        for c in (exa_pack.get("Citations") or [])[:25]:
            if isinstance(c, dict):
                citations.append(
                    {
                        "label": c.get("title") or c.get("Title") or "Web citation",
                        "source": c.get("query_bucket") or "Exa",
                        "url": c.get("url") or c.get("URL"),
                        "filed_date": c.get("published_date") or c.get("Published"),
                    }
                )

    return MemoPdfPayload(
        ticker=str(ticker or report.get("Ticker") or "").upper(),
        company_name=str(report.get("Name") or ""),
        sector=report.get("Sector"),
        industry=report.get("Industry"),
        generated_at=datetime.now(UTC).isoformat(),
        recommendation=recommendation,
        target_low=(reclass or {}).get("target_low"),
        target_mid=(reclass or {}).get("target_mid"),
        target_high=(reclass or {}).get("target_high"),
        current_price=snapshot.get("Price") if isinstance(snapshot, dict) else None,
        market_cap=snapshot.get("Market Cap") if isinstance(snapshot, dict) else None,
        old_noun=(reclass or {}).get("old_noun"),
        new_verb=(reclass or {}).get("primary_new_verb"),
        hidden_bom_role=(reclass or {}).get("hidden_bom_role"),
        functional_layer=(reclass or {}).get("functional_layer"),
        proof_stage=(reclass or {}).get("proof_stage"),
        proof_stage_label=(reclass or {}).get("proof_stage_label"),
        reclassification_gap=(reclass or {}).get("reclassification_gap"),
        torque_score=(torque or {}).get("total_score"),
        torque_stage=(torque or {}).get("stage_label"),
        torque_components=(torque or {}).get("components"),
        memo_text=memo_text or "",
        memo_sections=sections if isinstance(sections, dict) else None,
        charts=charts_input if isinstance(charts_input, list) else None,
        scenarios=scenarios,
        citations=citations or None,
        catalysts=(reclass or {}).get("catalysts"),
        kill_criteria=(reclass or {}).get("kill_criteria"),
        diligence_gaps=(reclass or {}).get("diligence_gaps"),
    )


def memo_chart_files(charts: list[dict[str, Any]]) -> list[dict[str, str]]:
    files = []
    for chart in charts:
        image = chart.get("image")
        if not isinstance(image, dict):
            continue
        filename = image.get("filename")
        mime = image.get("mime")
        if isinstance(filename, str) and isinstance(mime, str):
            files.append({"filename": filename, "mime": mime})
    return files


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
        history_slots = fetch_history_slots(client, selection.tickers, period=period)
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                image, meta = render_auction_chart(slot, period=period)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            images.append(image)
            results.append(result_payload(slot.ticker, slot.provider, slot.note, meta))
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
        history_slots = fetch_history_slots(client, selection.tickers, period="10y")
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                image, meta = render_performance_chart(slot, month=month)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            images.append(image)
            results.append(result_payload(slot.ticker, slot.provider, slot.note, meta))
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
        history_slots = fetch_history_slots(
            client,
            selection.tickers,
            period=str(payload.get("period") or "1y"),
            start=payload.get("start_date"),
            end=payload.get("end_date"),
        )
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                image, meta = render_regression_chart(slot)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            images.append(image)
            results.append(result_payload(slot.ticker, slot.provider, slot.note, meta))
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

    if chart_key == "ridge-growth":
        selection = resolve_ticker_selection(payload, watchlist_client)
        images = []
        histories = []
        results = []
        errors = []
        windows: list[dict[str, Any]] = []
        # Fetch every (ticker, period) window concurrently, preserving nested order, then
        # render sequentially (matplotlib is not thread-safe). Collapses 3xN sequential
        # network round-trips into a single parallel batch.
        jobs = [
            (ticker, period)
            for ticker in selection.tickers
            for period in RIDGE_GROWTH_PERIODS
        ]
        job_slots: list[HistoryResult | Exception | None] = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=max(1, min(len(jobs), 8))) as executor:
            futures = {
                executor.submit(
                    client.get_history, ticker, period=period, interval="1d"
                ): index
                for index, (ticker, period) in enumerate(jobs)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    job_slots[index] = future.result()
                except (ValueError, MarketDataError) as exc:
                    job_slots[index] = exc
        job_index = 0
        for ticker in selection.tickers:
            ticker_windows: list[dict[str, Any]] = []
            for period in RIDGE_GROWTH_PERIODS:
                job_slot = job_slots[job_index]
                job_index += 1
                if isinstance(job_slot, Exception):
                    errors.append({"ticker": ticker, "error": f"{period}: {job_slot}"})
                    continue
                if job_slot is None:
                    continue
                try:
                    image, meta = render_ridge_growth_chart(job_slot, period=period)
                except (ValueError, MarketDataError) as exc:
                    errors.append({"ticker": ticker, "error": f"{period}: {exc}"})
                    continue
                histories.append(job_slot)
                images.append(image)
                ticker_windows.append(meta)
                windows.append(meta)
                results.append(
                    result_payload(job_slot.ticker, job_slot.provider, job_slot.note, meta)
                )
            if ticker_windows:
                ticker_windows[-1]["analysis_memo"] = build_ridge_growth_memo(
                    ticker, ticker_windows
                )
        require_results(results, errors)
        meta = {**batch_meta(results, errors, selection.watchlist), "windows": windows}
        first_memo = next(
            (
                str(window["analysis_memo"])
                for window in windows
                if window.get("analysis_memo")
            ),
            "",
        )
        if first_memo:
            meta["analysis_memo"] = first_memo
        if windows:
            primary = next(
                (window for window in windows if window.get("period") == "1y"),
                windows[-1],
            )
            meta.update(
                {
                    "state": primary.get("state"),
                    "recommendation": primary.get("recommendation"),
                    "ending_equity": primary.get("ending_equity"),
                    "total_return": primary.get("total_return"),
                    "max_drawdown": primary.get("max_drawdown"),
                    "flow_state": primary.get("flow_compass", {}).get("state"),
                    "flow_score": primary.get("flow_compass", {}).get("score"),
                    "auction_location": primary.get("auction", {}).get("location"),
                }
            )
        return response_payload(
            images,
            mixed_provider(histories),
            "Ridge Growth daily strategy render",
            meta,
            mode=chart_key,
            tickers=selection.tickers,
            watchlist=selection.watchlist,
        )

    if chart_key == "flow-compass":
        selection = resolve_ticker_selection(payload, watchlist_client)
        period = str(payload.get("period") or "1y")
        images = []
        histories = []
        results = []
        errors = []
        history_slots = fetch_history_slots(
            client, selection.tickers, period=period, interval="1d"
        )
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                image, meta = render_flow_compass_chart(slot, period=period)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            images.append(image)
            results.append(result_payload(slot.ticker, slot.provider, slot.note, meta))
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return response_payload(
            images,
            mixed_provider(histories),
            "Flow Compass daily indicator render",
            meta,
            mode=chart_key,
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "torque":
        selection = resolve_ticker_selection(payload, watchlist_client)
        period = str(payload.get("period") or "2y")
        sec_client = current_app.config.get("SEC_CLIENT")
        images = []
        histories = []
        results = []
        errors = []

        def _torque_bundle(
            tk: str,
        ) -> tuple[HistoryResult, dict[str, Any], dict[str, Any] | None]:
            history = client.get_history(tk, period=period, interval="1d")
            try:
                profile = client.get_profile(history.ticker)
            except Exception:
                profile = {}
            sec_trend_pack: dict[str, Any] | None = None
            try:
                from app.sec_trend import build_sec_trend_pack

                sec_trend_pack = build_sec_trend_pack(sec_client, history.ticker, quarters=8)
            except Exception:
                sec_trend_pack = None
            return history, profile, sec_trend_pack

        # Per-ticker Torque data pull (history + profile + SEC trend) runs concurrently,
        # matching the existing cockpit fan-out; render stays sequential (matplotlib).
        bundle_slots: list[Any] = [None] * len(selection.tickers)
        with ThreadPoolExecutor(max_workers=batch_worker_count(selection.tickers)) as executor:
            bundle_futures = {
                executor.submit(_torque_bundle, ticker): index
                for index, ticker in enumerate(selection.tickers)
            }
            for bundle_future in as_completed(bundle_futures):
                index = bundle_futures[bundle_future]
                try:
                    bundle_slots[index] = bundle_future.result()
                except (ValueError, MarketDataError) as exc:
                    bundle_slots[index] = exc
        for ticker, slot in zip(selection.tickers, bundle_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            history, profile, sec_trend_pack = slot
            try:
                image, meta = render_torque_chart(
                    history=history,
                    sec_trend=sec_trend_pack,
                    profile=profile,
                )
            except Exception as exc:
                errors.append({"ticker": ticker, "error": f"torque render failed: {exc}"})
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
            "Torque inflection indicator render",
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


def build_chart_data_response(
    client: MarketDataClient,
    watchlist_client: TradingViewWatchlistClient,
    chart_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Same inputs and market-data fan-out as build_chart_response, without PNG renders."""
    chart_key = chart_type.replace("_", "-")
    if chart_key == "auction":
        selection = resolve_ticker_selection(payload, watchlist_client)
        period = str(payload.get("period") or "1y")
        datasets: list[dict[str, Any]] = []
        histories = []
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        history_slots = fetch_history_slots(client, selection.tickers, period=period)
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                dataset = build_auction_chart_data(slot, period=period)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            datasets.append(dataset)
            results.append(
                result_payload(slot.ticker, slot.provider, slot.note, dataset["meta"])
            )
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return data_response_payload(
            datasets,
            mixed_provider(histories),
            "Batch auction chart data",
            meta,
            mode=f"{chart_key}-data",
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "performance":
        selection = resolve_ticker_selection(payload, watchlist_client)
        month = int(payload.get("month") or 1)
        datasets = []
        histories = []
        results = []
        errors = []
        history_slots = fetch_history_slots(client, selection.tickers, period="10y")
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                dataset = build_performance_chart_data(slot, month=month)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            datasets.append(dataset)
            results.append(
                result_payload(slot.ticker, slot.provider, slot.note, dataset["meta"])
            )
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return data_response_payload(
            datasets,
            mixed_provider(histories),
            "Batch monthly performance chart data",
            meta,
            mode=f"{chart_key}-data",
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "regression":
        selection = resolve_ticker_selection(payload, watchlist_client)
        datasets = []
        histories = []
        results = []
        errors = []
        history_slots = fetch_history_slots(
            client,
            selection.tickers,
            period=str(payload.get("period") or "1y"),
            start=payload.get("start_date"),
            end=payload.get("end_date"),
        )
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                dataset = build_regression_chart_data(slot)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            datasets.append(dataset)
            results.append(
                result_payload(slot.ticker, slot.provider, slot.note, dataset["meta"])
            )
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return data_response_payload(
            datasets,
            mixed_provider(histories),
            "Batch regression chart data",
            meta,
            mode=f"{chart_key}-data",
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "ridge-growth":
        selection = resolve_ticker_selection(payload, watchlist_client)
        datasets = []
        histories = []
        results = []
        errors = []
        windows: list[dict[str, Any]] = []
        jobs = [
            (ticker, period)
            for ticker in selection.tickers
            for period in RIDGE_GROWTH_PERIODS
        ]
        job_slots: list[HistoryResult | Exception | None] = [None] * len(jobs)
        with ThreadPoolExecutor(max_workers=max(1, min(len(jobs), 8))) as executor:
            futures = {
                executor.submit(
                    client.get_history, ticker, period=period, interval="1d"
                ): index
                for index, (ticker, period) in enumerate(jobs)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    job_slots[index] = future.result()
                except (ValueError, MarketDataError) as exc:
                    job_slots[index] = exc
        job_index = 0
        for ticker in selection.tickers:
            ticker_windows: list[dict[str, Any]] = []
            for period in RIDGE_GROWTH_PERIODS:
                job_slot = job_slots[job_index]
                job_index += 1
                if isinstance(job_slot, Exception):
                    errors.append({"ticker": ticker, "error": f"{period}: {job_slot}"})
                    continue
                if job_slot is None:
                    continue
                try:
                    dataset = build_ridge_growth_chart_data(job_slot, period=period)
                except (ValueError, MarketDataError) as exc:
                    errors.append({"ticker": ticker, "error": f"{period}: {exc}"})
                    continue
                histories.append(job_slot)
                datasets.append(dataset)
                ticker_windows.append(dataset["meta"])
                windows.append(dataset["meta"])
                results.append(
                    result_payload(
                        job_slot.ticker, job_slot.provider, job_slot.note, dataset["meta"]
                    )
                )
            if ticker_windows:
                memo = build_ridge_growth_memo(ticker, ticker_windows)
                ticker_windows[-1]["analysis_memo"] = memo
                datasets[-1]["meta"]["analysis_memo"] = memo
        require_results(results, errors)
        meta = {**batch_meta(results, errors, selection.watchlist), "windows": windows}
        first_memo = next(
            (
                str(window["analysis_memo"])
                for window in windows
                if window.get("analysis_memo")
            ),
            "",
        )
        if first_memo:
            meta["analysis_memo"] = first_memo
        if windows:
            primary = next(
                (window for window in windows if window.get("period") == "1y"),
                windows[-1],
            )
            meta.update(
                {
                    "state": primary.get("state"),
                    "recommendation": primary.get("recommendation"),
                    "ending_equity": primary.get("ending_equity"),
                    "total_return": primary.get("total_return"),
                    "max_drawdown": primary.get("max_drawdown"),
                    "flow_state": primary.get("flow_compass", {}).get("state"),
                    "flow_score": primary.get("flow_compass", {}).get("score"),
                    "auction_location": primary.get("auction", {}).get("location"),
                }
            )
        return data_response_payload(
            datasets,
            mixed_provider(histories),
            "Ridge Growth daily strategy chart data",
            meta,
            mode=f"{chart_key}-data",
            tickers=selection.tickers,
            watchlist=selection.watchlist,
        )

    if chart_key == "flow-compass":
        selection = resolve_ticker_selection(payload, watchlist_client)
        period = str(payload.get("period") or "1y")
        datasets = []
        histories = []
        results = []
        errors = []
        history_slots = fetch_history_slots(
            client, selection.tickers, period=period, interval="1d"
        )
        for ticker, slot in zip(selection.tickers, history_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            try:
                dataset = build_flow_compass_chart_data(slot, period=period)
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
                continue
            histories.append(slot)
            datasets.append(dataset)
            results.append(
                result_payload(slot.ticker, slot.provider, slot.note, dataset["meta"])
            )
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return data_response_payload(
            datasets,
            mixed_provider(histories),
            "Flow Compass daily indicator chart data",
            meta,
            mode=f"{chart_key}-data",
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "torque":
        selection = resolve_ticker_selection(payload, watchlist_client)
        period = str(payload.get("period") or "2y")
        sec_client = current_app.config.get("SEC_CLIENT")
        datasets = []
        histories = []
        results = []
        errors = []

        def _torque_bundle(
            tk: str,
        ) -> tuple[HistoryResult, dict[str, Any], dict[str, Any] | None]:
            history = client.get_history(tk, period=period, interval="1d")
            try:
                profile = client.get_profile(history.ticker)
            except Exception:
                profile = {}
            sec_trend_pack: dict[str, Any] | None = None
            try:
                from app.sec_trend import build_sec_trend_pack

                sec_trend_pack = build_sec_trend_pack(sec_client, history.ticker, quarters=8)
            except Exception:
                sec_trend_pack = None
            return history, profile, sec_trend_pack

        bundle_slots: list[Any] = [None] * len(selection.tickers)
        with ThreadPoolExecutor(max_workers=batch_worker_count(selection.tickers)) as executor:
            bundle_futures = {
                executor.submit(_torque_bundle, ticker): index
                for index, ticker in enumerate(selection.tickers)
            }
            for bundle_future in as_completed(bundle_futures):
                index = bundle_futures[bundle_future]
                try:
                    bundle_slots[index] = bundle_future.result()
                except (ValueError, MarketDataError) as exc:
                    bundle_slots[index] = exc
        for ticker, slot in zip(selection.tickers, bundle_slots, strict=False):
            if isinstance(slot, Exception):
                errors.append({"ticker": ticker, "error": str(slot)})
                continue
            history, profile, sec_trend_pack = slot
            try:
                dataset = build_torque_chart_data(
                    history=history,
                    sec_trend=sec_trend_pack,
                    profile=profile,
                )
            except Exception as exc:
                errors.append({"ticker": ticker, "error": f"torque data failed: {exc}"})
                continue
            histories.append(history)
            datasets.append(dataset)
            results.append(
                result_payload(history.ticker, history.provider, history.note, dataset["meta"])
            )
        require_results(results, errors)
        meta = batch_meta(results, errors, selection.watchlist)
        if len(results) == 1:
            meta = {**results[0]["meta"], **meta}
        return data_response_payload(
            datasets,
            mixed_provider(histories),
            "Torque inflection indicator chart data",
            meta,
            mode=f"{chart_key}-data",
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
        dataset = build_portfolio_chart_data(
            histories,
            investment_per_stock=float(payload.get("investment_per_stock") or 100),
            benchmark=benchmark,
        )
        results = [
            result_payload(
                history.ticker,
                history.provider,
                history.note,
                {"final_value": dataset["meta"]["final_values"][history.ticker]},
            )
            for history in histories
        ]
        meta = {
            **dataset["meta"],
            **batch_meta(results, errors, selection.watchlist),
        }
        return data_response_payload(
            [dataset],
            mixed_provider(histories),
            "Mixed provider portfolio chart data",
            meta,
            mode=f"{chart_key}-data",
            tickers=[history.ticker for history in histories],
            watchlist=selection.watchlist,
        )

    if chart_key == "volatility":
        selection = resolve_ticker_selection(payload, watchlist_client)
        histories, errors = collect_histories(client, selection.tickers, period="1y")
        require_histories(histories, errors)
        dataset = build_volatility_chart_data(histories)
        results = [
            result_payload(history.ticker, history.provider, history.note, row)
            for history, row in zip(histories, dataset["rows"], strict=False)
        ]
        meta = {**dataset["meta"], **batch_meta(results, errors, selection.watchlist)}
        return data_response_payload(
            [dataset],
            mixed_provider(histories),
            "Mixed provider volatility chart data",
            meta,
            mode=f"{chart_key}-data",
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


def build_cockpit_response(
    client: MarketDataClient,
    watchlist_client: TradingViewWatchlistClient,
    payload: dict[str, Any],
) -> dict[str, Any]:
    selection = resolve_ticker_selection(payload, watchlist_client)
    period = str(payload.get("period") or "1y")
    include_torque = bool(payload.get("include_torque"))
    sec_client = current_app.config.get("SEC_CLIENT") if include_torque else None
    exa_client = current_app.config.get("EXA_CLIENT") if include_torque else None
    rows, errors = collect_cockpit_rows(
        client,
        selection.tickers,
        period=period,
        include_torque=include_torque,
        sec_client=sec_client,
        exa_client=exa_client,
    )
    require_results(rows, errors)
    rows.sort(key=lambda row: float(row["score"]), reverse=True)
    for index, row in enumerate(rows, start=1):
        row["rank"] = index

    providers = sorted({str(row["provider"]) for row in rows})
    provider = "+".join(providers)
    meta = batch_meta(
        [
            result_payload(
                str(row["ticker"]),
                str(row["provider"]),
                str(row["provider_note"]),
                row,
            )
            for row in rows
        ],
        errors,
        selection.watchlist,
    )
    meta["period"] = period
    export = export_payload(
        mode="watchlist-cockpit",
        provider=provider,
        provider_note="Watchlist cockpit ranking",
        tickers=[str(row["ticker"]) for row in rows],
        meta=meta,
        watchlist=selection.watchlist,
        image_files=[],
    )
    export["rows"] = rows
    return {
        "rows": rows,
        "provider": provider,
        "provider_note": "Watchlist cockpit ranking",
        "meta": meta,
        "watchlist": watchlist_payload(selection.watchlist),
        "export": export,
    }


def build_alerts_response(
    client: MarketDataClient,
    watchlist_client: TradingViewWatchlistClient,
    payload: dict[str, Any],
) -> dict[str, Any]:
    cockpit = build_cockpit_response(client, watchlist_client, payload)
    alert_digest = build_alert_digest(
        cockpit["rows"],
        max_alerts=max_alerts(payload),
        volatility_threshold=alert_volatility_threshold(payload),
    )
    alerts = alert_digest["alerts"]
    digest = alert_digest["digest"]
    meta = {
        **cockpit["meta"],
        "alert_count": len(alerts),
        "high_alert_count": digest["severity_counts"].get("High", 0),
        "medium_alert_count": digest["severity_counts"].get("Medium", 0),
        "info_alert_count": digest["severity_counts"].get("Info", 0),
        "volatility_threshold": alert_volatility_threshold(payload),
    }
    export = {
        **cockpit["export"],
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "watchlist-alerts",
        "provider_note": "Watchlist alert digest",
        "tickers": [str(row["ticker"]) for row in cockpit["rows"]],
        "image_files": [],
        "meta": meta,
    }
    export["rows"] = cockpit["rows"]
    export["alerts"] = alerts
    export["digest"] = digest
    return {
        "alerts": alerts,
        "digest": digest,
        "rows": cockpit["rows"],
        "provider": cockpit["provider"],
        "provider_note": "Watchlist alert digest",
        "meta": meta,
        "watchlist": cockpit["watchlist"],
        "export": export,
    }


def run_scheduled_alert_rules(
    client: MarketDataClient,
    watchlist_client: TradingViewWatchlistClient,
    alert_store: SupabaseAlertStore,
    *,
    run_date: date,
    force: bool,
    limit: int,
) -> dict[str, Any]:
    rules = alert_store.list_due_rules(run_date=run_date, force=force)[:limit]
    results = []
    for rule in rules:
        result = run_scheduled_alert_rule(
            client,
            watchlist_client,
            alert_store,
            rule=rule,
            run_date=run_date,
        )
        results.append(result)
    return {
        "run_date": run_date.isoformat(),
        "force": force,
        "processed": len(results),
        "results": results,
    }


def run_scheduled_alert_rule(
    client: MarketDataClient,
    watchlist_client: TradingViewWatchlistClient,
    alert_store: SupabaseAlertStore,
    *,
    rule: ScheduledAlertRule,
    run_date: date,
) -> dict[str, Any]:
    try:
        payload = build_alerts_response(
            client,
            watchlist_client,
            alert_payload_from_rule(rule),
        )
    except Exception as exc:
        alert_store.insert_run(
            rule=rule,
            run_date=run_date,
            trigger="scheduled",
            status="failed",
            error=str(exc),
        )
        alert_store.mark_rule_ran(rule_id=rule.id, run_date=run_date)
        return {
            "rule_id": rule.id,
            "name": rule.name,
            "status": "failed",
            "error": str(exc),
        }

    run_id = alert_store.insert_run(
        rule=rule,
        run_date=run_date,
        trigger="scheduled",
        status="success",
        payload=payload,
    )
    delivery = run_alert_delivery(
        alert_store,
        rule=rule,
        run_date=run_date,
        alert_run_id=run_id,
        payload=payload,
    )
    alert_store.mark_rule_ran(rule_id=rule.id, run_date=run_date)
    result = {
        "rule_id": rule.id,
        "name": rule.name,
        "status": "success",
        "alert_count": payload["meta"]["alert_count"],
        "high_alert_count": payload["meta"]["high_alert_count"],
    }
    if delivery is not None:
        result["delivery_status"] = delivery.status
        result["delivery_channel"] = delivery.channel
    return result


def run_alert_delivery(
    alert_store: SupabaseAlertStore,
    *,
    rule: ScheduledAlertRule,
    run_date: date,
    alert_run_id: str | None,
    payload: dict[str, Any],
) -> AlertDeliveryResult | None:
    if rule.delivery_channel != "webhook":
        return None

    delivery = deliver_alert_webhook(
        rule=rule,
        run_date=run_date,
        alert_run_id=alert_run_id,
        payload=payload,
    )
    if alert_run_id:
        alert_store.insert_delivery(
            rule=rule,
            alert_run_id=alert_run_id,
            delivery=delivery,
            payload=payload,
        )
        alert_store.update_run_delivery_status(alert_run_id=alert_run_id, delivery=delivery)
    return delivery


def run_alert_webhook_test(
    alert_store: SupabaseAlertStore,
    *,
    user_id: str,
    rule_id: str,
    run_date: date,
) -> dict[str, Any]:
    if not rule_id:
        raise ValueError("rule_id is required")

    rule = alert_store.get_rule_for_user(rule_id=rule_id, user_id=user_id)
    if rule is None:
        raise ValueError("Alert rule was not found")
    if rule.delivery_channel != "webhook" or not rule.delivery_webhook_url:
        raise ValueError("Alert rule does not have webhook delivery configured")

    payload = alert_webhook_test_payload(rule=rule, run_date=run_date)
    run_id = alert_store.insert_run(
        rule=rule,
        run_date=run_date,
        trigger="manual",
        status="success",
        payload=payload,
    )
    delivery = deliver_alert_webhook(
        rule=rule,
        run_date=run_date,
        alert_run_id=run_id,
        payload=payload,
        require_alerts=False,
    )
    if run_id:
        alert_store.insert_delivery(
            rule=rule,
            alert_run_id=run_id,
            delivery=delivery,
            payload=payload,
        )
        alert_store.update_run_delivery_status(alert_run_id=run_id, delivery=delivery)
    return {
        "rule_id": rule.id,
        "run_id": run_id,
        "delivery": alert_delivery_response(delivery),
        "digest": payload["digest"],
        "meta": payload["meta"],
    }


def alert_webhook_test_payload(
    *,
    rule: ScheduledAlertRule,
    run_date: date,
) -> dict[str, Any]:
    ticker = rule.tickers[0] if rule.tickers else "TEST"
    return {
        "digest": {
            "headline": f"Webhook test: {rule.name}",
            "summary": "Test delivery from The Underlying Alert Monitor.",
            "severity_counts": {"High": 0, "Medium": 0, "Info": 1},
            "next_steps": ["Confirm this payload reached the expected destination."],
        },
        "alerts": [
            {
                "ticker": ticker,
                "severity": "Info",
                "signal": "Webhook test",
                "message": "This is a test alert delivery.",
                "action": "No market action required.",
            }
        ],
        "rows": [],
        "provider": "alert-monitor",
        "provider_note": "Webhook test delivery",
        "meta": {
            "alert_count": 1,
            "high_alert_count": 0,
            "medium_alert_count": 0,
            "info_alert_count": 1,
            "run_date": run_date.isoformat(),
            "test": True,
        },
        "export": {
            "mode": "alert-webhook-test",
            "generated_at": datetime.now(UTC).isoformat(),
            "tickers": rule.tickers,
        },
    }


def alert_delivery_response(delivery: AlertDeliveryResult) -> dict[str, Any]:
    return {
        "channel": delivery.channel,
        "status": delivery.status,
        "destination": delivery.destination,
        "response_status": delivery.response_status,
        "error": delivery.error,
    }


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


def data_response_payload(
    datasets: list[dict[str, Any]],
    provider: str,
    note: str,
    meta: dict[str, Any],
    *,
    mode: str,
    tickers: list[str],
    watchlist: WatchlistResult | None,
) -> dict[str, Any]:
    payload = {
        "datasets": datasets,
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
        image_files=[],
    )
    payload["export"]["datasets"] = datasets
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


def max_alerts(payload: dict[str, Any]) -> int:
    raw = payload.get("max_alerts") or DEFAULT_ALERT_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_ALERT_LIMIT
    return max(1, min(value, MAX_ALERT_LIMIT))


def scheduled_rule_limit(payload: dict[str, Any]) -> int:
    raw = payload.get("limit") or DEFAULT_SCHEDULED_RULE_LIMIT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_SCHEDULED_RULE_LIMIT
    return max(1, min(value, MAX_SCHEDULED_RULE_LIMIT))


def scheduled_run_date(payload: dict[str, Any]) -> date:
    raw = payload.get("run_date")
    if raw in (None, ""):
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError as exc:
        raise ValueError("run_date must be an ISO date") from exc


def alert_volatility_threshold(payload: dict[str, Any]) -> float:
    raw = payload.get("volatility_threshold")
    if raw in (None, ""):
        return DEFAULT_VOLATILITY_THRESHOLD
    if not isinstance(raw, int | float | str):
        return DEFAULT_VOLATILITY_THRESHOLD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_VOLATILITY_THRESHOLD
    return max(0.0, min(value, 2.0))


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


def fetch_history_slots(
    client: MarketDataClient, tickers: list[str], **history_options: Any
) -> list[HistoryResult | Exception]:
    """Fetch per-ticker histories concurrently, preserving ticker order.

    Returns a list aligned to ``tickers`` where each entry is the ``HistoryResult`` on
    success or the caught ``ValueError``/``MarketDataError`` on failure. Only the
    independent network fetches are parallelized here; callers keep chart rendering
    sequential because matplotlib is not thread-safe. Mirrors the established
    ``collect_histories`` fan-out pattern so error types and behavior are unchanged.
    """
    slots: list[HistoryResult | Exception | None] = [None] * len(tickers)
    with ThreadPoolExecutor(max_workers=batch_worker_count(tickers)) as executor:
        futures = {
            executor.submit(client.get_history, ticker, **history_options): index
            for index, ticker in enumerate(tickers)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                slots[index] = future.result()
            except (ValueError, MarketDataError) as exc:
                slots[index] = exc
    # Every ticker is submitted and resolves to a HistoryResult or a caught exception,
    # so no slot stays None; the filter only narrows the type and preserves ticker order
    # (and length) for aligned zip() in callers.
    return [slot for slot in slots if slot is not None]


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


def collect_cockpit_rows(
    client: MarketDataClient,
    tickers: list[str],
    *,
    period: str,
    include_torque: bool = False,
    sec_client: SecClient | None = None,
    exa_client: ExaClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    row_slots: list[dict[str, Any] | None] = [None] * len(tickers)
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=batch_worker_count(tickers)) as executor:
        futures = {
            executor.submit(
                build_cockpit_row,
                client,
                ticker,
                period=period,
                sec_client=sec_client,
                exa_client=exa_client,
                include_torque=include_torque,
            ): (index, ticker)
            for index, ticker in enumerate(tickers)
        }
        for future in as_completed(futures):
            index, ticker = futures[future]
            try:
                row_slots[index] = future.result()
            except (ValueError, MarketDataError) as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
    return [row for row in row_slots if row is not None], errors


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
                build_stock_fax(
                    get_market_client(),
                    ticker,
                    sec_client=get_sec_client(),
                    **text_generation_options(),
                )
            )
        except (ValueError, AnthropicError, MarketDataError) as exc:
            return jsonify({"Ticker": ticker.upper(), "Error": str(exc)}), 400

    @app.get("/micro_memo/<ticker>")
    def compat_micro_memo(ticker: str) -> Any:
        try:
            memo = build_market_memo(
                get_market_client(),
                ticker,
                sec_client=get_sec_client(),
                **text_generation_options(),
            )
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
