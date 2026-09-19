"""Tabular in-context inference (TabICL v2) behind one small, optional seam.

This service hosts the tabular model for the whole stack. The model is TabICL v2
(``pip install tabicl``, BSD-3-Clause code *and* checkpoints, so commercial use is
fine; Google's TabFM weights are non-commercial and are deliberately not used).
It is a zero-shot in-context learner: ``fit(X_context, y_context)`` then
``predict``/``predict_proba(X_query)`` in one forward pass, no gradient steps.

``tabicl`` pulls ``torch`` and is therefore an **optional** dependency
(``pip install -e '.[tabular]'`` or ``requirements-tabular.txt``), never part of
the production build. Everything here degrades honestly when it is absent:

* :class:`TabICLPredictor` imports ``tabicl`` lazily inside the call and raises
  :class:`TabularUnavailable` when the import fails;
* :class:`RemotePredictor` forwards the same request to an inference service
  (``TABULAR_INFERENCE_URL``, optional ``TABULAR_INFERENCE_TOKEN`` bearer token,
  e.g. the Modal function in ``modal_tabular.py``) and turns any failure into
  :class:`TabularUnavailable`;
* :func:`get_predictor` resolves remote -> local -> :class:`TabularUnavailable`.

Callers turn :class:`TabularUnavailable` into a ``503``, an omitted field or a
skipped step. Malformed input is :class:`TabularRequestError` (a ``ValueError``,
so existing ``400`` handlers already cover it). The generic contract enforced by
:func:`validate_request` / :func:`run_prediction`::

    POST /api/tabular/predict
    {"task": "classification"|"regression", "columns": [...], "categorical": [...],
     "context": {"rows": [[...], ...], "target": [...]}, "query": {"rows": [[...], ...]},
     "options": {"max_context_rows": 8000, "n_estimators": 4}}
    200 -> {"method": "tabicl_v2", "context_rows_used": n, "predictions": [...],
            "classes": [...] (classification), "probabilities": [[...]] (classification)}

Hard caps: 100 features, 20 000 context rows, 2 000 query rows, 10 classes.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import numpy as np
import pandas as pd
import requests

from app._perf import tune_session

__all__ = [
    "MAX_FEATURES",
    "MAX_CONTEXT_ROWS",
    "MAX_QUERY_ROWS",
    "MAX_CLASSES",
    "DEFAULT_MAX_CONTEXT_ROWS",
    "DEFAULT_N_ESTIMATORS",
    "METHOD",
    "TabularTask",
    "TabularUnavailable",
    "TabularRequestError",
    "TabularPrediction",
    "TabularPredictor",
    "TabICLPredictor",
    "RemotePredictor",
    "get_predictor",
    "tabicl_installed",
    "validate_request",
    "run_prediction",
    "prediction_payload",
    "frames_from_request",
]

#: Contract caps (also enforced by the Modal function).
MAX_FEATURES = 100
MAX_CONTEXT_ROWS = 20_000
MAX_QUERY_ROWS = 2_000
MAX_CLASSES = 10
#: Contract defaults for ``options``.
DEFAULT_MAX_CONTEXT_ROWS = 8_000
DEFAULT_N_ESTIMATORS = 4
MAX_N_ESTIMATORS = 32
#: Method tag every response carries.
METHOD = "tabicl_v2"
#: Remote inference budget; a cold Modal container can take a while to warm.
DEFAULT_REMOTE_TIMEOUT_SECONDS = 60.0

TabularTask = Literal["classification", "regression"]
_TASKS: tuple[str, ...] = ("classification", "regression")


class TabularUnavailable(RuntimeError):
    """The model cannot answer right now (not installed, no service, bad reply).

    Callers must fail open: return ``503``, omit the optional field, or skip the
    step. Never let it propagate into a request failure for a feature that was
    only ever optional.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TabularRequestError(ValueError):
    """The request violates the contract (malformed input or a cap breach)."""


@dataclass(frozen=True)
class TabularPrediction:
    """One inference result on the generic contract's shape."""

    task: str
    predictions: list[Any]
    context_rows_used: int
    classes: list[Any] | None = None
    probabilities: list[list[float]] | None = None
    method: str = METHOD
    extra: dict[str, Any] = field(default_factory=dict)


class TabularPredictor(Protocol):
    """Anything that can run one in-context tabular fit+predict."""

    name: str

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: Sequence[str] = (),
        options: dict[str, Any] | None = None,
    ) -> TabularPrediction: ...


# --------------------------------------------------------------------------
# Contract validation
# --------------------------------------------------------------------------


def _as_int(value: Any, *, default: int, lo: int, hi: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise TabularRequestError(f"options.{label} must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise TabularRequestError(f"options.{label} must be an integer") from exc
    if number < lo or number > hi:
        raise TabularRequestError(f"options.{label} must be between {lo} and {hi}")
    return number


def _cell_ok(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        return not (isinstance(value, float) and math.isinf(value))
    return False


def _rows(value: Any, *, width: int, cap: int, label: str) -> list[list[Any]]:
    if not isinstance(value, list):
        raise TabularRequestError(f"{label}.rows must be a list of rows")
    if len(value) > cap:
        raise TabularRequestError(f"{label}.rows exceeds the cap of {cap} rows")
    rows: list[list[Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, list) or len(row) != width:
            raise TabularRequestError(
                f"{label}.rows[{index}] must be a list of exactly {width} values"
            )
        if not all(_cell_ok(cell) for cell in row):
            raise TabularRequestError(
                f"{label}.rows[{index}] contains an unsupported value "
                "(only numbers, strings, booleans and null are allowed)"
            )
        rows.append(list(row))
    return rows


def validate_request(payload: Any) -> dict[str, Any]:
    """Validate a generic-contract request and return its normalised form.

    Raises :class:`TabularRequestError` on any malformed field or cap breach so
    the route can answer ``400`` with the reason.
    """
    if not isinstance(payload, dict):
        raise TabularRequestError("request body must be a JSON object")

    task = payload.get("task")
    if task not in _TASKS:
        raise TabularRequestError("task must be 'classification' or 'regression'")

    columns = payload.get("columns")
    if not isinstance(columns, list) or not columns:
        raise TabularRequestError("columns must be a non-empty list of feature names")
    if len(columns) > MAX_FEATURES:
        raise TabularRequestError(f"columns exceeds the cap of {MAX_FEATURES} features")
    names: list[str] = []
    for column in columns:
        if not isinstance(column, str) or not column.strip():
            raise TabularRequestError("every column name must be a non-empty string")
        names.append(column)
    if len(set(names)) != len(names):
        raise TabularRequestError("column names must be unique")

    categorical_raw = payload.get("categorical") or []
    if not isinstance(categorical_raw, list):
        raise TabularRequestError("categorical must be a list of column names")
    categorical: list[str] = []
    for column in categorical_raw:
        if column not in names:
            raise TabularRequestError(f"categorical column {column!r} is not in columns")
        if column not in categorical:
            categorical.append(str(column))

    context = payload.get("context")
    if not isinstance(context, dict):
        raise TabularRequestError("context must be an object with rows and target")
    context_rows = _rows(
        context.get("rows"), width=len(names), cap=MAX_CONTEXT_ROWS, label="context"
    )
    if not context_rows:
        raise TabularRequestError("context.rows must contain at least one row")
    target = context.get("target")
    if not isinstance(target, list) or len(target) != len(context_rows):
        raise TabularRequestError("context.target must be a list with one value per context row")
    if task == "regression":
        cleaned_target: list[Any] = []
        for value in target:
            bad = isinstance(value, bool) or not isinstance(value, (int, float))
            if bad or not math.isfinite(value):
                raise TabularRequestError("context.target must be finite numbers for regression")
            cleaned_target.append(float(value))
    else:
        cleaned_target = []
        for value in target:
            if value is None or isinstance(value, (list, dict)):
                raise TabularRequestError("context.target must be class labels for classification")
            cleaned_target.append(value)
        if len({str(v) for v in cleaned_target}) > MAX_CLASSES:
            raise TabularRequestError(f"classification supports at most {MAX_CLASSES} classes")

    query = payload.get("query")
    if not isinstance(query, dict):
        raise TabularRequestError("query must be an object with rows")
    query_rows = _rows(query.get("rows"), width=len(names), cap=MAX_QUERY_ROWS, label="query")
    if not query_rows:
        raise TabularRequestError("query.rows must contain at least one row")

    options_raw = payload.get("options") or {}
    if not isinstance(options_raw, dict):
        raise TabularRequestError("options must be an object")
    options = {
        "max_context_rows": _as_int(
            options_raw.get("max_context_rows"),
            default=DEFAULT_MAX_CONTEXT_ROWS,
            lo=1,
            hi=MAX_CONTEXT_ROWS,
            label="max_context_rows",
        ),
        "n_estimators": _as_int(
            options_raw.get("n_estimators"),
            default=DEFAULT_N_ESTIMATORS,
            lo=1,
            hi=MAX_N_ESTIMATORS,
            label="n_estimators",
        ),
    }

    return {
        "task": task,
        "columns": names,
        "categorical": categorical,
        "context": {"rows": context_rows, "target": cleaned_target},
        "query": {"rows": query_rows},
        "options": options,
    }


def frames_from_request(request: dict[str, Any]) -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    """Build ``(x_context, y_context, x_query)`` from a validated request.

    The context is truncated to its **last** ``options.max_context_rows`` rows,
    so a caller that orders rows oldest-first keeps the most recent ones.
    """
    columns = list(request["columns"])
    categorical = list(request["categorical"])
    cap = int(request["options"]["max_context_rows"])
    context_rows = request["context"]["rows"][-cap:]
    target = request["context"]["target"][-cap:]
    x_context = _frame(context_rows, columns, categorical)
    x_query = _frame(request["query"]["rows"], columns, categorical)
    y_context = pd.Series(target, name="target")
    if request["task"] == "regression":
        y_context = pd.to_numeric(y_context, errors="coerce").astype(float)
    return x_context, y_context, x_query


def _frame(rows: list[list[Any]], columns: list[str], categorical: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=columns)
    for column in columns:
        if column in categorical:
            labels = frame[column].map(lambda v: None if v is None or pd.isna(v) else str(v))
            frame[column] = labels.astype("category")
        else:
            frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    return frame


def _json_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def prediction_payload(prediction: TabularPrediction) -> dict[str, Any]:
    """Serialise a prediction on the generic contract's response shape."""
    payload: dict[str, Any] = {
        "method": prediction.method,
        "task": prediction.task,
        "context_rows_used": int(prediction.context_rows_used),
        "predictions": [
            _json_number(v) if prediction.task == "regression" else _plain(v)
            for v in prediction.predictions
        ],
    }
    if prediction.task == "classification":
        payload["classes"] = [_plain(c) for c in (prediction.classes or [])]
        payload["probabilities"] = [
            [_json_number(p) or 0.0 for p in row] for row in (prediction.probabilities or [])
        ]
    if prediction.extra:
        payload["extra"] = dict(prediction.extra)
    return payload


def _plain(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return _json_number(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, np.str_):
        return str(value)
    return value


def run_prediction(payload: Any, predictor: TabularPredictor) -> dict[str, Any]:
    """Validate ``payload``, run it through ``predictor`` and serialise the reply."""
    request = validate_request(payload)
    x_context, y_context, x_query = frames_from_request(request)
    prediction = predictor.predict(
        request["task"],
        x_context,
        y_context,
        x_query,
        categorical=request["categorical"],
        options=request["options"],
    )
    return prediction_payload(prediction)


# --------------------------------------------------------------------------
# Local predictor (lazy tabicl import)
# --------------------------------------------------------------------------


def tabicl_installed() -> bool:
    """Whether ``tabicl`` can be imported in this process (no download involved)."""
    try:
        import importlib.util

        return importlib.util.find_spec("tabicl") is not None
    except Exception:  # noqa: BLE001 - any import machinery failure means "no"
        return False


def _cap_classes(y_context: pd.Series) -> None:
    n_classes = int(y_context.astype(str).nunique())
    if n_classes > MAX_CLASSES:
        raise TabularRequestError(f"classification supports at most {MAX_CLASSES} classes")
    if n_classes < 2:
        raise TabularRequestError("classification needs at least two classes in the context")


def _cap_frames(x_context: pd.DataFrame, x_query: pd.DataFrame) -> None:
    if x_context.shape[1] > MAX_FEATURES:
        raise TabularRequestError(f"at most {MAX_FEATURES} features are supported")
    if x_context.shape[0] > MAX_CONTEXT_ROWS:
        raise TabularRequestError(f"at most {MAX_CONTEXT_ROWS} context rows are supported")
    if x_query.shape[0] > MAX_QUERY_ROWS:
        raise TabularRequestError(f"at most {MAX_QUERY_ROWS} query rows are supported")
    if x_context.empty or x_query.empty:
        raise TabularRequestError("context and query must both be non-empty")
    if list(x_context.columns) != list(x_query.columns):
        raise TabularRequestError("context and query must share the same columns")


class TabICLPredictor:
    """Run TabICL v2 in-process. ``tabicl`` is imported lazily on first use.

    Checkpoints auto-download from Hugging Face the first time a model is fit
    (``tabicl-classifier-v2-20260212.ckpt`` / ``tabicl-regressor-v2-20260212.ckpt``),
    so the first call on a cold host is slow; ``device="cpu"`` works everywhere.
    """

    name = "tabicl_local"

    def __init__(
        self,
        *,
        device: str | None = None,
        n_estimators: int | None = None,
        classifier_kwargs: dict[str, Any] | None = None,
        regressor_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self.device = device if device is not None else os.getenv("TABULAR_DEVICE") or "cpu"
        self.n_estimators = n_estimators
        self.classifier_kwargs = dict(classifier_kwargs or {})
        self.regressor_kwargs = dict(regressor_kwargs or {})

    @staticmethod
    def _import() -> tuple[Any, Any]:
        try:
            from tabicl import TabICLClassifier, TabICLRegressor
        except Exception as exc:  # noqa: BLE001 - ImportError or a broken torch install
            raise TabularUnavailable(
                "tabicl is not installed (pip install -e '.[tabular]' or requirements-tabular.txt)"
            ) from exc
        return TabICLClassifier, TabICLRegressor

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: Sequence[str] = (),
        options: dict[str, Any] | None = None,
    ) -> TabularPrediction:
        del categorical  # pandas dtypes already carry the categorical columns
        if task not in _TASKS:
            raise TabularRequestError("task must be 'classification' or 'regression'")
        _cap_frames(x_context, x_query)
        opts = dict(options or {})
        n_estimators = int(
            opts.get("n_estimators") or self.n_estimators or DEFAULT_N_ESTIMATORS
        )
        classifier_cls, regressor_cls = self._import()
        try:
            if task == "classification":
                _cap_classes(y_context)
                model = classifier_cls(
                    n_estimators=n_estimators, device=self.device, **self.classifier_kwargs
                )
                model.fit(x_context, y_context.astype(str).to_numpy())
                probabilities = np.asarray(model.predict_proba(x_query), dtype=float)
                classes = [str(c) for c in model.classes_]
                predictions = [classes[int(i)] for i in probabilities.argmax(axis=1)]
                return TabularPrediction(
                    task=task,
                    predictions=predictions,
                    context_rows_used=int(x_context.shape[0]),
                    classes=classes,
                    probabilities=[[float(p) for p in row] for row in probabilities],
                    extra={"predictor": self.name, "n_estimators": n_estimators},
                )
            model = regressor_cls(
                n_estimators=n_estimators, device=self.device, **self.regressor_kwargs
            )
            model.fit(x_context, y_context.to_numpy(dtype=float))
            values = np.asarray(model.predict(x_query), dtype=float)
            return TabularPrediction(
                task=task,
                predictions=[float(v) for v in values],
                context_rows_used=int(x_context.shape[0]),
                extra={"predictor": self.name, "n_estimators": n_estimators},
            )
        except (TabularRequestError, TabularUnavailable):
            raise
        except Exception as exc:  # noqa: BLE001 - torch/model failures are "unavailable"
            raise TabularUnavailable(f"tabicl inference failed: {exc}") from exc


# --------------------------------------------------------------------------
# Remote predictor (the generic contract over HTTP)
# --------------------------------------------------------------------------


def _cell_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(float(value)) else None
    if pd.isna(value):
        return None
    return str(value)


def _frame_rows(frame: pd.DataFrame) -> list[list[Any]]:
    return [[_cell_json(v) for v in row] for row in frame.itertuples(index=False, name=None)]


class RemotePredictor:
    """POST the generic contract to ``TABULAR_INFERENCE_URL``.

    Any transport error, non-2xx status (including the service's own ``503``) or
    malformed reply becomes :class:`TabularUnavailable`; a ``400`` from the
    service is re-raised as :class:`TabularRequestError` so the caller answers
    ``400`` too.
    """

    name = "tabicl_remote"

    def __init__(
        self,
        url: str,
        *,
        token: str | None = None,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_REMOTE_TIMEOUT_SECONDS,
    ) -> None:
        self.url = str(url).strip()
        self.token = token
        self.session = session if session is not None else tune_session(requests.Session())
        self.timeout = timeout

    def headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: Sequence[str] = (),
        options: dict[str, Any] | None = None,
    ) -> TabularPrediction:
        if task not in _TASKS:
            raise TabularRequestError("task must be 'classification' or 'regression'")
        _cap_frames(x_context, x_query)
        target: list[Any]
        if task == "classification":
            _cap_classes(y_context)
            target = [str(v) for v in y_context.tolist()]
        else:
            target = [float(v) for v in y_context.tolist()]
        opts = dict(options or {})
        payload = {
            "task": task,
            "columns": [str(c) for c in x_context.columns],
            "categorical": [str(c) for c in categorical if c in x_context.columns],
            "context": {"rows": _frame_rows(x_context), "target": target},
            "query": {"rows": _frame_rows(x_query)},
            "options": {
                "max_context_rows": int(opts.get("max_context_rows") or DEFAULT_MAX_CONTEXT_ROWS),
                "n_estimators": int(opts.get("n_estimators") or DEFAULT_N_ESTIMATORS),
            },
        }
        try:
            response = self.session.post(
                self.url, headers=self.headers(), json=payload, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise TabularUnavailable(f"tabular inference request failed: {exc}") from exc

        status = int(getattr(response, "status_code", 0) or 0)
        try:
            body = response.json()
        except ValueError as exc:
            raise TabularUnavailable(
                f"tabular inference service returned invalid JSON (HTTP {status})"
            ) from exc
        if status == 400:
            reason = body.get("error") if isinstance(body, dict) else None
            raise TabularRequestError(
                str(reason or "tabular inference service rejected the request")
            )
        if status >= 400 or not isinstance(body, dict):
            reason = body.get("reason") or body.get("error") if isinstance(body, dict) else None
            raise TabularUnavailable(
                f"tabular inference service unavailable (HTTP {status}): {reason or 'no reason'}"
            )
        return _parse_remote(task, body, x_query.shape[0], self.name)


def _parse_remote(task: str, body: dict[str, Any], n_query: int, name: str) -> TabularPrediction:
    predictions = body.get("predictions")
    if not isinstance(predictions, list) or len(predictions) != n_query:
        raise TabularUnavailable("tabular inference service returned a malformed predictions list")
    context_rows_used = body.get("context_rows_used")
    used = int(context_rows_used) if isinstance(context_rows_used, (int, float)) else 0
    extra = {"predictor": name}
    if isinstance(body.get("extra"), dict):
        extra.update(body["extra"])
    if task == "regression":
        values: list[Any] = []
        for value in predictions:
            number = _json_number(value)
            if number is None:
                raise TabularUnavailable(
                    "tabular inference service returned a non-numeric prediction"
                )
            values.append(number)
        return TabularPrediction(
            task=task,
            predictions=values,
            context_rows_used=used,
            method=str(body.get("method") or METHOD),
            extra=extra,
        )
    classes = body.get("classes")
    probabilities = body.get("probabilities")
    if not isinstance(classes, list) or not classes or not isinstance(probabilities, list):
        raise TabularUnavailable("tabular inference service returned no classes/probabilities")
    if len(probabilities) != n_query:
        raise TabularUnavailable(
            "tabular inference service returned a malformed probability matrix"
        )
    rows: list[list[float]] = []
    for row in probabilities:
        if not isinstance(row, list) or len(row) != len(classes):
            raise TabularUnavailable(
                "tabular inference service returned a malformed probability row"
            )
        rows.append([_json_number(p) or 0.0 for p in row])
    return TabularPrediction(
        task=task,
        predictions=[str(p) for p in predictions],
        context_rows_used=used,
        classes=[str(c) for c in classes],
        probabilities=rows,
        method=str(body.get("method") or METHOD),
        extra=extra,
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


def get_predictor(
    *,
    remote_url: str | None = None,
    token: str | None = None,
    allow_local: bool = True,
    session: requests.Session | None = None,
) -> TabularPredictor:
    """Resolve a predictor: remote service -> local ``tabicl`` -> unavailable.

    ``remote_url``/``token`` default to ``TABULAR_INFERENCE_URL`` /
    ``TABULAR_INFERENCE_TOKEN`` read at call time so production can be wired
    without a restart of anything but the service. Raises
    :class:`TabularUnavailable` when neither path can serve.
    """
    configured = remote_url if remote_url is not None else os.getenv("TABULAR_INFERENCE_URL")
    url = (configured or "").strip()
    if url:
        return RemotePredictor(
            url,
            token=token if token is not None else os.getenv("TABULAR_INFERENCE_TOKEN") or None,
            session=session,
        )
    if allow_local and tabicl_installed():
        return TabICLPredictor()
    raise TabularUnavailable(
        "no tabular model: set TABULAR_INFERENCE_URL or install the optional 'tabular' extra"
    )
