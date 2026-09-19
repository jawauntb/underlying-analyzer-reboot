"""HTTP surface for the tabular model, as a Flask blueprint under ``/api/tabular``.

Two routes:

* ``POST /api/tabular/predict`` — the GENERIC CONTRACT (:mod:`app.tabular`):
  a classification/regression in-context query over caller-supplied rows.
* ``GET /api/tabular/peer-forecast/<ticker>?horizon=3`` — the SHARED CONTRACT
  (:mod:`app.peer_forecast`): the ticker's forward-excess-return bucket vs its
  sector peers, cached per sector/horizon for 12 hours.

Both fail open with ``503 {"available": false, "reason": ...}`` whenever the model
cannot answer (not installed, no inference URL, too little data) and ``400`` on a
malformed request. Tests inject a fake predictor through
``app.config["TABULAR_PREDICTOR"]``; production resolves one from the
environment on every call (:func:`app.tabular.get_predictor`).
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, current_app, has_app_context, jsonify, request

from app.peer_forecast import PEER_FORECAST_HORIZONS, parse_horizon, peer_forecast_for_ticker
from app.tabular import (
    TabularPredictor,
    TabularRequestError,
    TabularUnavailable,
    get_predictor,
    run_prediction,
)

MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_TICKER_LENGTH = 16

tabular_blueprint = Blueprint("tabular", __name__)


def resolve_predictor() -> TabularPredictor:
    """The injected predictor when one is configured, else the environment's."""
    if has_app_context():
        configured = current_app.config.get("TABULAR_PREDICTOR")
        if configured is not None:
            return configured  # type: ignore[no-any-return]
    return get_predictor()


def unavailable(reason: str) -> tuple[Response, int]:
    return jsonify({"available": False, "reason": str(reason)}), 503


def _error(message: str, status: int) -> tuple[Response, int]:
    return jsonify({"error": message}), status


def _clean_ticker(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    if len(symbol) > MAX_TICKER_LENGTH:
        raise ValueError(f"ticker must be at most {MAX_TICKER_LENGTH} characters")
    if not all(ch.isalnum() or ch in {".", "-"} for ch in symbol):
        raise ValueError("ticker contains unsupported characters")
    return symbol


def _market_client() -> Any:
    from app.situate.routes import market_client

    return market_client()


@tabular_blueprint.get("/")
def tabular_info() -> Any:
    return jsonify(
        {
            "name": "tabular",
            "model": "TabICL v2 (in-context tabular learning)",
            "routes": {
                "predict": "POST /api/tabular/predict",
                "peer_forecast": "GET /api/tabular/peer-forecast/{ticker}?horizon=1|2|3|6|12",
            },
            "horizons": list(PEER_FORECAST_HORIZONS),
            "disclaimer": "Research only. Not investment advice.",
        }
    )


@tabular_blueprint.post("/predict")
def predict() -> Any:
    """Generic in-context tabular inference on caller-supplied rows."""
    length = request.content_length
    if length is not None and length > MAX_BODY_BYTES:
        return _error(f"request body must be at most {MAX_BODY_BYTES} bytes", 400)
    payload = request.get_json(silent=True)
    if payload is None:
        return _error("request body must be a JSON object", 400)
    try:
        predictor = resolve_predictor()
    except TabularUnavailable as exc:
        return unavailable(exc.reason)
    try:
        return jsonify(run_prediction(payload, predictor))
    except TabularRequestError as exc:
        return _error(str(exc), 400)
    except TabularUnavailable as exc:
        return unavailable(exc.reason)
    except Exception as exc:  # noqa: BLE001 - an optional model must never 500 a caller
        current_app.logger.exception("tabular predict failed")
        return unavailable(f"tabular inference failed: {exc}")


@tabular_blueprint.get("/peer-forecast/<ticker>")
def peer_forecast(ticker: str) -> Any:
    """The ticker's forward-excess-return bucket vs its sector peers."""
    try:
        symbol = _clean_ticker(ticker)
        horizon = parse_horizon(request.args.get("horizon"))
    except ValueError as exc:
        return _error(str(exc), 400)
    try:
        predictor = resolve_predictor()
    except TabularUnavailable as exc:
        return unavailable(exc.reason)
    try:
        payload = peer_forecast_for_ticker(
            symbol,
            horizon=horizon,
            predictor=predictor,
            client=_market_client(),
            as_of=request.args.get("as_of") or None,
        )
    except TabularRequestError as exc:
        return _error(str(exc), 400)
    except TabularUnavailable as exc:
        return unavailable(exc.reason)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:  # noqa: BLE001 - fail open, never 500 for an optional signal
        current_app.logger.exception("peer forecast failed")
        return unavailable(f"peer forecast failed: {exc}")
    return jsonify(payload)


def register_tabular_routes(app: Any) -> None:
    """Mount the blueprint under ``/api/tabular``."""
    app.register_blueprint(tabular_blueprint, url_prefix="/api/tabular")
