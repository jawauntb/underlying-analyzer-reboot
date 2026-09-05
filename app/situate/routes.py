"""HTTP surface for Situate, as a Flask blueprint.

Mounted twice: under ``/api/situate`` and under ``/api/research`` (the
product-neutral alias). Both prefixes resolve to the same handlers.

A full build fans out to Massive, FRED, SEC EDGAR, Exa and Anthropic and can take
one to three minutes, so admission is bounded exactly like Prism's route: a
process-wide semaphore plus one in-flight build per calling client, with a
``Retry-After`` header on both refusals.
"""

from __future__ import annotations

import threading
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from app.situate.contract import ENGINE_ALIAS, ENGINE_NAME, ENGINE_VERSION

BUILD_CONCURRENCY_PER_PROCESS = 2
BUILD_CONCURRENCY_PER_CLIENT = 1
CHAT_CONCURRENCY_PER_PROCESS = 4
DEFAULT_RETRY_AFTER = "30"

MAX_TICKER_LENGTH = 16
MAX_MESSAGE_LENGTH = 4000
MAX_HISTORY_TURNS = 40
MAX_HISTORY_CONTENT = 4000
MAX_BODY_BYTES = 1 * 1024 * 1024

_build_slots = threading.BoundedSemaphore(BUILD_CONCURRENCY_PER_PROCESS)
_chat_slots = threading.BoundedSemaphore(CHAT_CONCURRENCY_PER_PROCESS)
_client_lock = threading.Lock()
_active_clients: dict[str, int] = {}

situate_blueprint = Blueprint("situate", __name__)


# --------------------------------------------------------------------------
# Admission control
# --------------------------------------------------------------------------


def client_key() -> str:
    """A bounded per-client key for admission control (rightmost XFF only)."""
    forwarded = (request.headers.get("X-Forwarded-For") or "").rsplit(",", 1)[-1]
    for candidate in (forwarded.strip(), (request.remote_addr or "").strip()):
        if not candidate:
            continue
        try:
            address = ip_address(candidate)
        except ValueError:
            continue
        return address.compressed
    return "unknown"


def try_acquire_client(key: str) -> bool:
    with _client_lock:
        active = _active_clients.get(key, 0)
        if active >= BUILD_CONCURRENCY_PER_CLIENT:
            return False
        _active_clients[key] = active + 1
        return True


def release_client(key: str) -> None:
    with _client_lock:
        active = _active_clients.get(key, 0)
        if active <= 1:
            _active_clients.pop(key, None)
        else:
            _active_clients[key] = active - 1


# --------------------------------------------------------------------------
# Request parsing
# --------------------------------------------------------------------------


def clean_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    if len(symbol) > MAX_TICKER_LENGTH:
        raise ValueError(f"ticker must be at most {MAX_TICKER_LENGTH} characters")
    if not all(character.isalnum() or character in {".", "-", ":", "^"} for character in symbol):
        raise ValueError("ticker contains unsupported characters")
    return symbol


def clean_as_of(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("as_of must be an ISO date (YYYY-MM-DD)") from exc
    return parsed.isoformat()


def _bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _json_body() -> dict[str, Any]:
    length = request.content_length
    if length is not None and length > MAX_BODY_BYTES:
        raise ValueError(f"request body must be at most {MAX_BODY_BYTES} bytes")
    payload = request.get_json(silent=True)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _error(message: str, status: int, *, retry_after: str | None = None) -> tuple[Response, int]:
    response = jsonify({"error": message})
    if retry_after:
        response.headers["Retry-After"] = retry_after
    return response, status


# --------------------------------------------------------------------------
# Dependency resolution (overridable from app.config for tests)
# --------------------------------------------------------------------------


def _config(key: str, default: Any = None) -> Any:
    return current_app.config.get(key, default)


def market_client() -> Any:
    configured = _config("SITUATE_MARKET_CLIENT") or _config("PRISM_MARKET_CLIENT")
    if configured is not None:
        return configured
    try:
        from app.prism.data import build_prism_client

        client = build_prism_client()
    except Exception:  # noqa: BLE001
        return _config("MARKET_DATA_CLIENT")
    current_app.config["SITUATE_MARKET_CLIENT"] = client
    return client


def sec_client() -> Any:
    return _config("SITUATE_SEC_CLIENT") or _config("PRISM_SEC_CLIENT") or _config("SEC_CLIENT")


def exa_client() -> Any:
    return _config("SITUATE_EXA_CLIENT") or _config("PRISM_EXA_CLIENT") or _config("EXA_CLIENT")


def text_generator() -> Any:
    return (
        _config("SITUATE_TEXT_GENERATOR")
        or _config("PRISM_TEXT_GENERATOR")
        or _config("TEXT_GENERATOR")
    )


def fred_client() -> Any:
    return _config("SITUATE_FRED_CLIENT") or _config("PRISM_FRED_CLIENT") or _config("FRED_CLIENT")


def situate_store() -> Any:
    configured = _config("SITUATE_STORE")
    if configured is not None:
        return configured
    from app.situate.engine import _situate_store

    return _situate_store()


def _generation_options() -> dict[str, Any]:
    return {
        "text_generator": text_generator(),
        "api_key": _config("ANTHROPIC_API_KEY"),
        "text_model": _config("SITUATE_TEXT_MODEL")
        or _config("PRISM_TEXT_MODEL")
        or _config("ANTHROPIC_TEXT_MODEL"),
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@situate_blueprint.get("/")
def situate_info() -> Any:
    """What this engine is and which routes it serves."""
    return jsonify(
        {
            "name": ENGINE_NAME,
            "alias": ENGINE_ALIAS,
            "engine_version": ENGINE_VERSION,
            "summary": (
                "Situates one ticker: what you are exposed to (factor basket), the "
                "current state, the odds per horizon (historical + option-implied), "
                "what the business is saying, cheap/rich zones and a posture memo — "
                "distributions, never point price targets, never buy/sell."
            ),
            "routes": {
                "build": "POST /api/situate",
                "get": "GET /api/situate/{ticker}",
                "summary": "GET /api/situate/{ticker}/summary",
                "export": "GET /api/situate/{ticker}/export?format=md|json|pdf",
                "chat": "POST /api/situate/{ticker}/chat",
            },
            "aliases": ["/api/research"],
            "disclaimer": "Research only. Not investment advice; no price target.",
        }
    )


@situate_blueprint.post("")
@situate_blueprint.post("/")
def build() -> Any:
    """Build (or return today's stored) packet for one ticker."""
    from app.situate.engine import build_situate_packet

    try:
        payload = _json_body()
        symbol = clean_symbol(payload.get("ticker"))
        as_of = clean_as_of(payload.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)

    force = _bool(payload.get("force"))
    include_memo = _bool(payload.get("include_memo"), default=True)

    key = client_key()
    if not try_acquire_client(key):
        return _error(
            "A Situate build is already running for this client.",
            429,
            retry_after=DEFAULT_RETRY_AFTER,
        )
    if not _build_slots.acquire(blocking=False):
        release_client(key)
        return _error(
            "Situate is at capacity; try again shortly.", 503, retry_after=DEFAULT_RETRY_AFTER
        )
    try:
        packet = build_situate_packet(
            market_client(),
            symbol,
            sec_client=sec_client(),
            exa_client=exa_client(),
            as_of=as_of,
            include_memo=include_memo,
            force=force,
            store=situate_store(),
            fred_client=fred_client(),
            **_generation_options(),
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Situate build failed")
        return _error(f"Situate build failed: {exc}", 500)
    finally:
        _build_slots.release()
        release_client(key)
    return jsonify(packet)


@situate_blueprint.get("/<ticker>")
def read_packet(ticker: str) -> Any:
    """Return the latest stored packet, or 404 when nothing has been built."""
    from app.situate.engine import get_situate_packet

    try:
        symbol = clean_symbol(ticker)
        as_of = clean_as_of(request.args.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)
    packet = get_situate_packet(symbol, as_of, store=situate_store())
    if packet is None:
        return _error(
            f"No stored Situate packet for {symbol}. POST /api/situate to build one.", 404
        )
    return jsonify(packet)


@situate_blueprint.get("/<ticker>/summary")
def read_summary(ticker: str) -> Any:
    """The bounded agent projection of the latest stored packet."""
    from app.situate.engine import get_situate_packet, situate_summary

    try:
        symbol = clean_symbol(ticker)
        as_of = clean_as_of(request.args.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)
    packet = get_situate_packet(symbol, as_of, store=situate_store())
    if packet is None:
        return _error(
            f"No stored Situate packet for {symbol}. POST /api/situate to build one.", 404
        )
    return jsonify(situate_summary(packet))


@situate_blueprint.get("/<ticker>/export")
def export(ticker: str) -> Any:
    """Download the stored packet as ``md``, ``json`` or ``pdf``."""
    from app.situate.engine import get_situate_packet
    from app.situate.export import SituateExportError, export_packet

    try:
        symbol = clean_symbol(ticker)
        as_of = clean_as_of(request.args.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)
    packet = get_situate_packet(symbol, as_of, store=situate_store())
    if packet is None:
        return _error(
            f"No stored Situate packet for {symbol}. POST /api/situate to build one.", 404
        )
    try:
        body, content_type, filename = export_packet(packet, request.args.get("format", "md"))
    except SituateExportError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Situate export failed")
        return _error(f"Situate export failed: {exc}", 500)
    response = current_app.response_class(body, content_type=content_type)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Content-Length"] = str(len(body))
    return response


@situate_blueprint.post("/<ticker>/chat")
def chat(ticker: str) -> Any:
    """Ask one question about a stored packet."""
    from app.situate.chat import chat_turn
    from app.situate.engine import get_situate_packet

    try:
        symbol = clean_symbol(ticker)
        payload = _json_body()
        as_of = clean_as_of(payload.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)

    message = str(payload.get("message") or "").strip()
    if not message:
        return _error("message is required", 400)
    if len(message) > MAX_MESSAGE_LENGTH:
        return _error(f"message must be at most {MAX_MESSAGE_LENGTH} characters", 400)

    history = payload.get("history")
    if history is not None and not isinstance(history, list):
        return _error("history must be a list of {role, content} objects", 400)
    for turn in history or []:
        content = turn.get("content") if isinstance(turn, dict) else turn
        if content is not None and len(str(content)) > MAX_HISTORY_CONTENT:
            return _error(
                f"each history turn must be at most {MAX_HISTORY_CONTENT} characters", 400
            )

    key = client_key()
    if not try_acquire_client(key):
        return _error(
            "A Situate request is already running for this client.",
            429,
            retry_after=DEFAULT_RETRY_AFTER,
        )
    if not _chat_slots.acquire(blocking=False):
        release_client(key)
        return _error(
            "Situate is at capacity; try again shortly.", 503, retry_after=DEFAULT_RETRY_AFTER
        )
    try:
        store = situate_store()
        packet = get_situate_packet(symbol, as_of, store=store)
        if packet is None:
            return _error(
                f"No stored Situate packet for {symbol}. POST /api/situate to build one.", 404
            )
        result = chat_turn(
            packet,
            (history or [])[-MAX_HISTORY_TURNS:],
            message,
            store=store,
            conversation_id=payload.get("conversation_id"),
            **_generation_options(),
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Situate chat failed")
        return _error(f"Situate chat failed: {exc}", 500)
    finally:
        _chat_slots.release()
        release_client(key)
    return jsonify(result)


def register_situate_routes(app: Any) -> None:
    """Mount the blueprint under ``/api/situate`` and its ``/api/research`` alias."""
    app.register_blueprint(situate_blueprint, url_prefix="/api/situate")
    app.register_blueprint(situate_blueprint, url_prefix="/api/research", name="research")
