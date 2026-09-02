"""HTTP surface for Prism, as a Flask blueprint.

Routes are mounted twice: once under ``/api/prism`` and once under
``/api/ubermemo``, which is the working name the tooling was built against. Both
prefixes resolve to the same handlers, so a client that learned one keeps working.

A full build fans out to Massive, FRED, SEC EDGAR, Exa and Anthropic and can take
one to three minutes, so admission is bounded exactly like
``/api/data/ticker-research``: a process-wide semaphore plus one in-flight build
per calling client, and a ``Retry-After`` header on both refusals.
"""

from __future__ import annotations

import threading
from datetime import datetime
from ipaddress import ip_address
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from app.prism.contract import ENGINE_ALIAS, ENGINE_NAME, ENGINE_VERSION

BUILD_CONCURRENCY_PER_PROCESS = 2
BUILD_CONCURRENCY_PER_CLIENT = 1
CHAT_CONCURRENCY_PER_PROCESS = 4
DEFAULT_RETRY_AFTER = "30"

MAX_TICKER_LENGTH = 16
MAX_MESSAGE_LENGTH = 4000
MAX_HISTORY_TURNS = 40
#: Per-history-turn character cap. The turn *count* was capped but the content
#: was not, so two 5 MB entries produced a 10 MB Anthropic prompt from one
#: unauthenticated request.
MAX_HISTORY_CONTENT = 4000
#: Hard cap on the JSON body of any Prism route, enforced inside the blueprint
#: rather than app-wide so no other route's uploads change behaviour.
MAX_BODY_BYTES = 1 * 1024 * 1024

_build_slots = threading.BoundedSemaphore(BUILD_CONCURRENCY_PER_PROCESS)
_chat_slots = threading.BoundedSemaphore(CHAT_CONCURRENCY_PER_PROCESS)
_client_lock = threading.Lock()
_active_clients: dict[str, int] = {}

prism_blueprint = Blueprint("prism", __name__)


class PrismBusyError(RuntimeError):
    """Raised when the per-process build capacity is full."""


# --------------------------------------------------------------------------
# Admission control
# --------------------------------------------------------------------------


def client_key() -> str:
    """A bounded per-client key for admission control.

    ``X-Forwarded-For`` is written by the caller, so the *leftmost* entry is
    attacker-controlled: sending ``X-Forwarded-For: 127.0.0.1`` used to return
    ``None`` and skip admission entirely. Only the rightmost entry — the one the
    proxy in front of this process appended — is trusted, and an unusable value
    falls back to ``request.remote_addr`` rather than to "no client". Loopback is
    still a real key (a shared one) so an unkeyed caller can never be exempt.
    """
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
    """Admit one in-flight build per external client per process."""
    with _client_lock:
        active = _active_clients.get(key, 0)
        if active >= BUILD_CONCURRENCY_PER_CLIENT:
            return False
        _active_clients[key] = active + 1
        return True


def release_client(key: str) -> None:
    """Release a client's admission slot."""
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
    """Validate and normalise a ticker from a body or a path segment."""
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    if len(symbol) > MAX_TICKER_LENGTH:
        raise ValueError(f"ticker must be at most {MAX_TICKER_LENGTH} characters")
    if not all(character.isalnum() or character in {".", "-", ":", "^"} for character in symbol):
        raise ValueError("ticker contains unsupported characters")
    return symbol


def clean_as_of(value: Any) -> str | None:
    """Validate an ``as_of`` to a strict ``YYYY-MM-DD`` string, or ``None``.

    Unvalidated text used to reach ``engine._resolve_as_of``, which falls back to
    ``str(as_of)[:10]`` — so ``as_of`` became part of the stored packet's
    filename and of the export's ``Content-Disposition`` header. A value such as
    ``"9999-99-99"`` also sorted above every real date and shadowed the true
    packet.
    """
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
    """The market data client Prism builds against.

    Prism needs the facade's legacy (yfinance) fallback *off*: the benchmark
    universe deliberately contains symbols whose Massive coverage is short or
    stale (``X:BTCUSD`` ~2y, ``CYB`` last traded 2023-10-20), and the shared
    terminal client would try Yahoo for each of them and turn an honest short
    history into a hard ``MarketDataError``. The client is built once and kept on
    ``app.config`` so its HTTP session and caches are shared across builds.
    """
    configured = _config("PRISM_MARKET_CLIENT")
    if configured is not None:
        return configured
    try:
        from app.prism.data import build_prism_client

        client = build_prism_client()
    except Exception:  # noqa: BLE001 - fall back to the shared terminal client
        return _config("MARKET_DATA_CLIENT")
    current_app.config["PRISM_MARKET_CLIENT"] = client
    return client


def sec_client() -> Any:
    return _config("PRISM_SEC_CLIENT") or _config("SEC_CLIENT")


def exa_client() -> Any:
    return _config("PRISM_EXA_CLIENT") or _config("EXA_CLIENT")


def text_generator() -> Any:
    return _config("PRISM_TEXT_GENERATOR") or _config("TEXT_GENERATOR")


def prism_store() -> Any:
    configured = _config("PRISM_STORE")
    if configured is not None:
        return configured
    from app.prism.store import default_store

    return default_store()


def _generation_options() -> dict[str, Any]:
    return {
        "text_generator": text_generator(),
        "api_key": _config("ANTHROPIC_API_KEY"),
        "text_model": _config("PRISM_TEXT_MODEL") or _config("ANTHROPIC_TEXT_MODEL"),
    }


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@prism_blueprint.get("/")
def prism_info() -> Any:
    """What this engine is and which routes it serves."""
    return jsonify(
        {
            "name": ENGINE_NAME,
            "alias": ENGINE_ALIAS,
            "engine_version": ENGINE_VERSION,
            "summary": (
                "Splits one ticker into macro, factor, regime, spectral, entropy, "
                "fundamental and filing components and recombines them into scenarios, "
                "a recommendation and a chat-able memo."
            ),
            "routes": {
                "build": "POST /api/prism",
                "get": "GET /api/prism/{ticker}",
                "summary": "GET /api/prism/{ticker}/summary",
                "export": "GET /api/prism/{ticker}/export?format=txt|json|pdf",
                "chat": "POST /api/prism/chat",
            },
            "aliases": ["/api/ubermemo"],
            "disclaimer": "Research only. Not investment advice.",
        }
    )


@prism_blueprint.post("")
@prism_blueprint.post("/")
def build_packet() -> Any:
    """Build (or return today's stored) packet for one ticker."""
    from app.prism.engine import build_prism_packet

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
            "A Prism build is already running for this client.",
            429,
            retry_after=DEFAULT_RETRY_AFTER,
        )
    if not _build_slots.acquire(blocking=False):
        release_client(key)
        return _error(
            "Prism is at capacity; try again shortly.", 503, retry_after=DEFAULT_RETRY_AFTER
        )
    try:
        packet = build_prism_packet(
            market_client(),
            symbol,
            sec_client=sec_client(),
            exa_client=exa_client(),
            as_of=as_of,
            include_memo=include_memo,
            force=force,
            store=prism_store(),
            **_generation_options(),
        )
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Prism build failed")
        return _error(f"Prism build failed: {exc}", 500)
    finally:
        _build_slots.release()
        release_client(key)
    return jsonify(packet)


@prism_blueprint.get("/<ticker>")
def read_packet(ticker: str) -> Any:
    """Return the latest stored packet, or 404 when nothing has been built."""
    from app.prism.engine import get_prism_packet

    try:
        symbol = clean_symbol(ticker)
        as_of = clean_as_of(request.args.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)
    packet = get_prism_packet(symbol, as_of, store=prism_store())
    if packet is None:
        return _error(
            f"No stored Prism packet for {symbol}. POST /api/prism to build one.", 404
        )
    return jsonify(packet)


@prism_blueprint.get("/<ticker>/summary")
def read_summary(ticker: str) -> Any:
    """The bounded agent projection of the latest stored packet."""
    from app.prism.engine import get_prism_packet, prism_summary

    try:
        symbol = clean_symbol(ticker)
        as_of = clean_as_of(request.args.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)
    packet = get_prism_packet(symbol, as_of, store=prism_store())
    if packet is None:
        return _error(
            f"No stored Prism packet for {symbol}. POST /api/prism to build one.", 404
        )
    return jsonify(prism_summary(packet))


@prism_blueprint.get("/<ticker>/export")
def export(ticker: str) -> Any:
    """Download the stored packet as ``txt``, ``json`` or ``pdf``."""
    from app.prism.engine import get_prism_packet
    from app.prism.export import PrismExportError, export_packet

    try:
        symbol = clean_symbol(ticker)
        as_of = clean_as_of(request.args.get("as_of"))
    except ValueError as exc:
        return _error(str(exc), 400)
    packet = get_prism_packet(symbol, as_of, store=prism_store())
    if packet is None:
        return _error(
            f"No stored Prism packet for {symbol}. POST /api/prism to build one.", 404
        )
    try:
        body, content_type, filename = export_packet(packet, request.args.get("format", "txt"))
    except PrismExportError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("Prism export failed")
        return _error(f"Prism export failed: {exc}", 500)
    # ``content_type`` rather than ``mimetype``: the text export already declares
    # its charset, and Flask appends one to a mimetype, producing a doubled
    # "text/plain; charset=utf-8; charset=utf-8".
    response = current_app.response_class(body, content_type=content_type)
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    response.headers["Content-Length"] = str(len(body))
    return response


@prism_blueprint.post("/chat")
def chat() -> Any:
    """Ask one question about a stored packet."""
    from app.prism.chat import chat_turn
    from app.prism.engine import get_prism_packet

    try:
        payload = _json_body()
        symbol = clean_symbol(payload.get("ticker"))
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
    # The turn count was capped but each turn's content was not, so an
    # unauthenticated caller could drive an arbitrarily large Anthropic prompt.
    for turn in history or []:
        content = turn.get("content") if isinstance(turn, dict) else turn
        if content is not None and len(str(content)) > MAX_HISTORY_CONTENT:
            return _error(
                f"each history turn must be at most {MAX_HISTORY_CONTENT} characters",
                400,
            )

    # Chat calls the model on every request, so it takes admission control too.
    key = client_key()
    if not try_acquire_client(key):
        return _error(
            "A Prism request is already running for this client.",
            429,
            retry_after=DEFAULT_RETRY_AFTER,
        )
    if not _chat_slots.acquire(blocking=False):
        release_client(key)
        return _error(
            "Prism is at capacity; try again shortly.", 503, retry_after=DEFAULT_RETRY_AFTER
        )
    try:
        store = prism_store()
        packet = get_prism_packet(symbol, as_of, store=store)
        if packet is None:
            return _error(
                f"No stored Prism packet for {symbol}. POST /api/prism to build one.", 404
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
        current_app.logger.exception("Prism chat failed")
        return _error(f"Prism chat failed: {exc}", 500)
    finally:
        _chat_slots.release()
        release_client(key)
    return jsonify(result)


def register_prism_routes(app: Any) -> None:
    """Mount the blueprint under ``/api/prism`` and its ``/api/ubermemo`` alias."""
    app.register_blueprint(prism_blueprint, url_prefix="/api/prism")
    app.register_blueprint(
        prism_blueprint, url_prefix="/api/ubermemo", name="ubermemo"
    )
