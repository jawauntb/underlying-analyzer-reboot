"""Offline tests for the tabular (TabICL v2) seam: contract, caps, remote, fail-open.

Nothing here imports ``tabicl`` or touches the network: the predictor is an
injected fake, the remote predictor talks to a fake HTTP session, and the only
real-model test is skip-guarded on the optional import.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest
from flask import Flask

from app import tabular as tabular_module
from app.main import create_app
from app.tabular import (
    MAX_CLASSES,
    MAX_CONTEXT_ROWS,
    MAX_FEATURES,
    MAX_QUERY_ROWS,
    RemotePredictor,
    TabICLPredictor,
    TabularPrediction,
    TabularRequestError,
    TabularUnavailable,
    frames_from_request,
    get_predictor,
    run_prediction,
    validate_request,
)


class FakePredictor:
    """Deterministic in-context stand-in: rank by the first feature."""

    name = "fake"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: Any = (),
        options: dict[str, Any] | None = None,
    ) -> TabularPrediction:
        self.calls.append(
            {
                "task": task,
                "x_context": x_context.copy(),
                "y_context": y_context.copy(),
                "x_query": x_query.copy(),
                "categorical": list(categorical),
                "options": dict(options or {}),
            }
        )
        first = x_query.columns[0]
        if task == "regression":
            return TabularPrediction(
                task=task,
                predictions=[float(v) * 2.0 for v in x_query[first].fillna(0.0)],
                context_rows_used=int(x_context.shape[0]),
            )
        classes = sorted({str(v) for v in y_context.tolist()})
        probabilities: list[list[float]] = []
        for value in x_query[first].fillna(0.0):
            weights = np.array([1.0 + (i * float(value)) for i in range(len(classes))])
            weights = np.clip(weights, 0.05, None)
            probabilities.append((weights / weights.sum()).tolist())
        predictions = [classes[int(np.argmax(row))] for row in probabilities]
        return TabularPrediction(
            task=task,
            predictions=predictions,
            context_rows_used=int(x_context.shape[0]),
            classes=classes,
            probabilities=probabilities,
        )


def _request(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task": "classification",
        "columns": ["f1", "f2"],
        "categorical": ["f2"],
        "context": {
            "rows": [[0.1, "a"], [0.9, "b"], [0.2, "a"], [0.8, "b"]],
            "target": ["low", "high", "low", "high"],
        },
        "query": {"rows": [[0.15, "a"], [0.85, None]]},
        "options": {"max_context_rows": 3, "n_estimators": 2},
    }
    payload.update(overrides)
    return payload


@pytest.fixture()
def app() -> Flask:
    application = create_app()
    application.config["TABULAR_PREDICTOR"] = FakePredictor()
    return application


# ---------------------------------------------------------------------------
# Contract validation
# ---------------------------------------------------------------------------


def test_validate_request_normalises_and_keeps_options() -> None:
    normalised = validate_request(_request())
    assert normalised["task"] == "classification"
    assert normalised["columns"] == ["f1", "f2"]
    assert normalised["categorical"] == ["f2"]
    assert normalised["options"] == {"max_context_rows": 3, "n_estimators": 2}


def test_validate_request_defaults_options() -> None:
    normalised = validate_request(_request(options=None))
    assert normalised["options"] == {"max_context_rows": 8000, "n_estimators": 4}


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"task": "ranking"}, "task must be"),
        ({"columns": []}, "columns must be"),
        ({"columns": ["f1", "f1"]}, "unique"),
        ({"categorical": ["nope"]}, "not in columns"),
        ({"context": {"rows": [[1.0]], "target": ["x"]}}, "exactly 2 values"),
        ({"context": {"rows": [[1.0, "a"]], "target": []}}, "one value per context row"),
        ({"query": {"rows": []}}, "at least one row"),
        ({"query": {"rows": [[float("inf"), "a"]]}}, "unsupported value"),
        ({"options": {"n_estimators": 0}}, "between 1 and"),
        ({"options": {"max_context_rows": "many"}}, "must be an integer"),
    ],
)
def test_validate_request_rejects_malformed_input(overrides: dict[str, Any], fragment: str) -> None:
    with pytest.raises(TabularRequestError, match=fragment):
        validate_request(_request(**overrides))


def test_validate_request_enforces_hard_caps() -> None:
    too_many_features = _request(
        columns=[f"f{i}" for i in range(MAX_FEATURES + 1)],
        context={"rows": [[0.0] * (MAX_FEATURES + 1)], "target": ["a"]},
        query={"rows": [[0.0] * (MAX_FEATURES + 1)]},
    )
    with pytest.raises(TabularRequestError, match="features"):
        validate_request(too_many_features)

    too_many_context = _request(
        context={
            "rows": [[0.0, "a"]] * (MAX_CONTEXT_ROWS + 1),
            "target": ["a"] * (MAX_CONTEXT_ROWS + 1),
        }
    )
    with pytest.raises(TabularRequestError, match=f"{MAX_CONTEXT_ROWS}"):
        validate_request(too_many_context)

    too_many_query = _request(query={"rows": [[0.0, "a"]] * (MAX_QUERY_ROWS + 1)})
    with pytest.raises(TabularRequestError, match=f"{MAX_QUERY_ROWS}"):
        validate_request(too_many_query)

    too_many_classes = _request(
        context={
            "rows": [[float(i), "a"] for i in range(MAX_CLASSES + 1)],
            "target": [f"c{i}" for i in range(MAX_CLASSES + 1)],
        }
    )
    with pytest.raises(TabularRequestError, match=f"{MAX_CLASSES} classes"):
        validate_request(too_many_classes)


def test_regression_target_must_be_finite_numbers() -> None:
    with pytest.raises(TabularRequestError, match="finite numbers"):
        validate_request(
            _request(task="regression", context={"rows": [[1.0, "a"]], "target": ["x"]})
        )


def test_frames_truncate_to_the_most_recent_context_rows_and_type_columns() -> None:
    x_context, y_context, x_query = frames_from_request(validate_request(_request()))
    # max_context_rows=3 keeps the LAST three rows.
    assert x_context.shape == (3, 2)
    assert y_context.tolist() == ["high", "low", "high"]
    assert str(x_context["f2"].dtype) == "category"
    assert x_context["f1"].dtype == float
    assert x_query.shape == (2, 2)
    assert pd.isna(x_query["f2"].iloc[1])


# ---------------------------------------------------------------------------
# run_prediction + HTTP route with an injected fake
# ---------------------------------------------------------------------------


def test_run_prediction_serialises_the_generic_contract() -> None:
    predictor = FakePredictor()
    payload = run_prediction(_request(), predictor)
    assert payload["method"] == "tabicl_v2"
    assert payload["task"] == "classification"
    assert payload["context_rows_used"] == 3
    assert payload["classes"] == ["high", "low"]
    assert len(payload["predictions"]) == 2
    assert len(payload["probabilities"]) == 2
    assert all(abs(sum(row) - 1.0) < 1e-9 for row in payload["probabilities"])
    assert predictor.calls[0]["categorical"] == ["f2"]
    assert predictor.calls[0]["options"] == {"max_context_rows": 3, "n_estimators": 2}


def test_run_prediction_regression_has_no_class_fields() -> None:
    payload = run_prediction(
        _request(
            task="regression",
            context={"rows": [[0.1, "a"], [0.9, "b"]], "target": [1.0, 2.0]},
            options={},
        ),
        FakePredictor(),
    )
    assert payload["predictions"] == pytest.approx([0.3, 1.7])
    assert "classes" not in payload and "probabilities" not in payload


def test_predict_route_returns_200_with_injected_predictor(app: Flask) -> None:
    response = app.test_client().post("/api/tabular/predict", json=_request())
    assert response.status_code == 200
    body = response.get_json()
    assert body["method"] == "tabicl_v2"
    assert body["classes"] == ["high", "low"]
    assert len(body["probabilities"]) == 2


def test_predict_route_returns_400_on_malformed_input(app: Flask) -> None:
    client = app.test_client()
    response = client.post("/api/tabular/predict", json=_request(task="nope"))
    assert response.status_code == 400
    assert "task must be" in response.get_json()["error"]
    response = client.post("/api/tabular/predict", data="not json", content_type="text/plain")
    assert response.status_code == 400


def test_predict_route_returns_503_when_no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TABULAR_INFERENCE_URL", raising=False)
    monkeypatch.setattr(tabular_module, "tabicl_installed", lambda: False)
    application = create_app()
    application.config["TABULAR_PREDICTOR"] = None
    response = application.test_client().post("/api/tabular/predict", json=_request())
    assert response.status_code == 503
    body = response.get_json()
    assert body["available"] is False
    assert "TABULAR_INFERENCE_URL" in body["reason"]


def test_predict_route_returns_503_when_predictor_raises_unavailable(app: Flask) -> None:
    class Broken:
        name = "broken"

        def predict(self, *_: Any, **__: Any) -> TabularPrediction:
            raise TabularUnavailable("model exploded")

    app.config["TABULAR_PREDICTOR"] = Broken()
    response = app.test_client().post("/api/tabular/predict", json=_request())
    assert response.status_code == 503
    assert response.get_json() == {"available": False, "reason": "model exploded"}


def test_tabular_info_route(app: Flask) -> None:
    body = app.test_client().get("/api/tabular/").get_json()
    assert body["routes"]["predict"] == "POST /api/tabular/predict"
    assert body["horizons"] == [1, 2, 3, 6, 12]


def test_docs_and_openapi_register_the_tabular_surface(app: Flask) -> None:
    client = app.test_client()
    catalog = client.get("/api/docs").get_json()
    paths = {item["path"] for item in catalog["endpoints"]}
    assert "/api/tabular/predict" in paths
    assert "/api/tabular/peer-forecast/{ticker}" in paths
    chart = next(
        item for item in catalog["endpoints"] if item["path"] == "/api/charts/{chart_type}"
    )
    assert "peer-forecast" in chart["path_params"]["chart_type"]
    openapi = client.get("/api/openapi").get_json()
    assert "post" in openapi["paths"]["/api/tabular/predict"]
    forecast = openapi["paths"]["/api/tabular/peer-forecast/{ticker}"]["get"]
    assert "503" in forecast["responses"]
    schema = openapi["paths"]["/api/charts/{chart_type}"]["post"]["parameters"][0]["schema"]
    assert "peer-forecast" in schema["enum"]


# ---------------------------------------------------------------------------
# Remote predictor (mocked HTTP)
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, status_code: int, body: Any, *, raw: bool = False) -> None:
        self.status_code = status_code
        self._body = body
        self._raw = raw

    def json(self) -> Any:
        if self._raw:
            raise ValueError("not json")
        return self._body


class _Session:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def post(self, url: str, *, headers: dict[str, str], json: Any, timeout: float) -> Any:
        self.requests.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _frames() -> tuple[pd.DataFrame, pd.Series, pd.DataFrame]:
    x_context = pd.DataFrame({"f1": [0.1, 0.9, float("nan")], "f2": ["a", "b", "a"]})
    y_context = pd.Series(["low", "high", "low"])
    x_query = pd.DataFrame({"f1": [0.2], "f2": ["b"]})
    return x_context, y_context, x_query


def test_remote_predictor_posts_the_generic_contract_and_parses_classification() -> None:
    session = _Session(
        [
            _Response(
                200,
                {
                    "method": "tabicl_v2",
                    "context_rows_used": 3,
                    "predictions": ["low"],
                    "classes": ["high", "low"],
                    "probabilities": [[0.3, 0.7]],
                },
            )
        ]
    )
    predictor = RemotePredictor("https://tabular.example/predict", token="secret", session=session)  # type: ignore[arg-type]
    x_context, y_context, x_query = _frames()
    result = predictor.predict(
        "classification", x_context, y_context, x_query, categorical=["f2"],
        options={"max_context_rows": 100, "n_estimators": 2},
    )
    assert result.classes == ["high", "low"]
    assert result.probabilities == [[0.3, 0.7]]
    assert result.predictions == ["low"]
    assert result.context_rows_used == 3
    sent = session.requests[0]
    assert sent["headers"]["Authorization"] == "Bearer secret"
    assert sent["timeout"] == 60.0
    body = sent["json"]
    assert body["task"] == "classification"
    assert body["columns"] == ["f1", "f2"]
    assert body["categorical"] == ["f2"]
    # NaN travels as JSON null, never as a bare NaN.
    assert body["context"]["rows"][2] == [None, "a"]
    assert body["context"]["target"] == ["low", "high", "low"]
    assert body["query"]["rows"] == [[0.2, "b"]]
    assert body["options"] == {"max_context_rows": 100, "n_estimators": 2}


def test_remote_predictor_regression() -> None:
    session = _Session([_Response(200, {"context_rows_used": 2, "predictions": [0.5]})])
    predictor = RemotePredictor("https://tabular.example/predict", session=session)  # type: ignore[arg-type]
    x_context, _, x_query = _frames()
    result = predictor.predict("regression", x_context, pd.Series([1.0, 2.0, 3.0]), x_query)
    assert result.predictions == [0.5]
    assert "Authorization" not in session.requests[0]["headers"]


@pytest.mark.parametrize(
    "response",
    [
        _Response(503, {"available": False, "reason": "cold"}),
        _Response(500, {"error": "boom"}),
        _Response(200, "<html>", raw=True),
        _Response(
            200, {"predictions": ["low", "high"], "classes": ["low"], "probabilities": [[1]]}
        ),
        _Response(200, {"predictions": ["low"]}),
        __import__("requests").ConnectionError("down"),
    ],
)
def test_remote_predictor_turns_every_failure_into_unavailable(response: Any) -> None:
    session = _Session([response])
    predictor = RemotePredictor("https://tabular.example/predict", session=session)  # type: ignore[arg-type]
    x_context, y_context, x_query = _frames()
    with pytest.raises(TabularUnavailable):
        predictor.predict("classification", x_context, y_context, x_query)


def test_remote_predictor_relays_a_400_as_a_request_error() -> None:
    session = _Session([_Response(400, {"error": "columns exceeds the cap"})])
    predictor = RemotePredictor("https://tabular.example/predict", session=session)  # type: ignore[arg-type]
    x_context, y_context, x_query = _frames()
    with pytest.raises(TabularRequestError, match="columns exceeds"):
        predictor.predict("classification", x_context, y_context, x_query)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_get_predictor_prefers_the_remote_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABULAR_INFERENCE_URL", "https://tabular.example/predict")
    monkeypatch.setenv("TABULAR_INFERENCE_TOKEN", "tok")
    predictor = get_predictor()
    assert isinstance(predictor, RemotePredictor)
    assert predictor.url == "https://tabular.example/predict"
    assert predictor.token == "tok"


def test_get_predictor_falls_back_to_local_then_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TABULAR_INFERENCE_URL", raising=False)
    monkeypatch.setattr(tabular_module, "tabicl_installed", lambda: True)
    assert isinstance(get_predictor(), TabICLPredictor)
    monkeypatch.setattr(tabular_module, "tabicl_installed", lambda: False)
    with pytest.raises(TabularUnavailable):
        get_predictor()
    # allow_local=False never returns the in-process model.
    monkeypatch.setattr(tabular_module, "tabicl_installed", lambda: True)
    with pytest.raises(TabularUnavailable):
        get_predictor(allow_local=False)


def test_local_predictor_is_unavailable_without_tabicl(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def _no_tabicl(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "tabicl" or name.startswith("tabicl."):
            raise ImportError("no tabicl here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_tabicl)
    x_context, y_context, x_query = _frames()
    with pytest.raises(TabularUnavailable, match="tabicl is not installed"):
        TabICLPredictor().predict("classification", x_context, y_context, x_query)


# ---------------------------------------------------------------------------
# Real model smoke (only when the optional extra is installed; downloads a
# checkpoint on first use, so it is skipped in CI and offline sandboxes).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not tabular_module.tabicl_installed(), reason="tabicl not installed")
def test_real_tabicl_classifier_smoke() -> None:  # pragma: no cover - optional dependency
    rng = np.random.default_rng(0)
    x_context = pd.DataFrame({"f1": rng.normal(size=120), "f2": rng.normal(size=120)})
    y_context = pd.Series(np.where(x_context["f1"] > 0, "up", "down"))
    x_query = pd.DataFrame({"f1": [2.0, -2.0], "f2": [0.0, 0.0]})
    result = TabICLPredictor(device="cpu", n_estimators=1).predict(
        "classification", x_context, y_context, x_query
    )
    assert result.classes is not None and set(result.classes) == {"down", "up"}
    assert result.probabilities is not None and len(result.probabilities) == 2
