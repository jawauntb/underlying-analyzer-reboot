"""Modal host for the tabular model (TabICL v2) behind the GENERIC CONTRACT.

A separate app from ``modal_app.py`` so the Flask terminal keeps its small image;
this one pip-installs ``tabicl`` with CPU-only ``torch`` and pre-downloads the v2
checkpoints at image build, so the first request on a fresh container does not
pay the Hugging Face download.

Deploy (not done by CI)::

    python -m pip install -e ".[deploy]"
    modal deploy modal_tabular.py

Then set ``TABULAR_INFERENCE_URL`` to the printed endpoint URL in the service's
environment (Railway / Modal secret for the terminal) and, if you created the
``jawaun-tabular-icl-token`` secret with ``TABULAR_INFERENCE_TOKEN``, the same
value as ``TABULAR_INFERENCE_TOKEN`` on the caller so requests carry the bearer.
:class:`app.tabular.RemotePredictor` speaks exactly this contract.

The endpoint is::

    POST <url>
    {"task": "classification"|"regression", "columns": [...], "categorical": [...],
     "context": {"rows": [[...]], "target": [...]}, "query": {"rows": [[...]]},
     "options": {"max_context_rows": 8000, "n_estimators": 4}}
    200 -> {"method": "tabicl_v2", "context_rows_used": n, "predictions": [...],
            "classes": [...], "probabilities": [[...]]}   (classes/probabilities: classification)
    400 -> {"error": "..."}            malformed input or a cap breach
    401 -> {"error": "unauthorized"}   bearer token configured and missing/wrong
    503 -> {"available": false, "reason": "..."}   model could not answer
"""

from __future__ import annotations

import os
from typing import Any

import modal

CHECKPOINTS = (
    "tabicl-classifier-v2-20260212.ckpt",
    "tabicl-regressor-v2-20260212.ckpt",
)
#: Hugging Face cache lives here inside the image so the checkpoints are baked in.
HF_HOME = "/root/.cache/huggingface"


def _warm_checkpoints() -> None:
    """Fit tiny problems once so both v2 checkpoints land in the image cache."""
    import numpy as np
    import pandas as pd
    from tabicl import TabICLClassifier, TabICLRegressor

    rng = np.random.default_rng(0)
    x = pd.DataFrame({"a": rng.normal(size=64), "b": rng.normal(size=64)})
    TabICLClassifier(n_estimators=1, device="cpu").fit(x, np.where(x["a"] > 0, "up", "down"))
    TabICLRegressor(n_estimators=1, device="cpu").fit(x, x["a"].to_numpy())


image = (
    modal.Image.debian_slim(python_version="3.12")
    .pip_install(
        "torch>=2.2",
        extra_index_url="https://download.pytorch.org/whl/cpu",
    )
    .pip_install(
        "tabicl>=2.2,<3",
        "numpy>=2.0,<3",
        "pandas>=2.2,<3",
        "requests>=2.32,<3",
        "fastapi[standard]>=0.115",
    )
    .env({"HF_HOME": HF_HOME, "TABULAR_DEVICE": "cpu"})
    .run_function(_warm_checkpoints)
    .add_local_file("app/tabular.py", remote_path="/root/app/tabular.py", copy=True)
    .add_local_file("app/_perf.py", remote_path="/root/app/_perf.py", copy=True)
    .add_local_file("app/__init__.py", remote_path="/root/app/__init__.py", copy=True)
)

app = modal.App("jawaun-tabular-icl", image=image)

# Optional bearer token: create with
#   modal secret create jawaun-tabular-icl-token TABULAR_INFERENCE_TOKEN=<random>
# and the endpoint refuses requests that do not carry it. Without the secret the
# endpoint is open (the caller's TABULAR_INFERENCE_TOKEN is then simply ignored).
secrets: list[modal.Secret] = []
try:  # pragma: no cover - resolved at deploy time
    secrets.append(modal.Secret.from_name("jawaun-tabular-icl-token"))
except Exception:  # noqa: BLE001 - the secret is optional
    secrets = []


@app.function(
    cpu=4.0,
    memory=8192,
    timeout=600,
    scaledown_window=300,
    secrets=secrets,
)
@modal.concurrent(max_inputs=2)
@modal.fastapi_endpoint(method="POST", label="jawaun-tabular-icl")
def predict(payload: dict[str, Any], request: Any = None) -> Any:  # noqa: ARG001
    """The GENERIC CONTRACT over HTTP; see the module docstring."""
    import sys

    from fastapi import Request
    from fastapi.responses import JSONResponse

    sys.path.insert(0, "/root")
    from app.tabular import (
        TabICLPredictor,
        TabularRequestError,
        TabularUnavailable,
        run_prediction,
    )

    expected = os.getenv("TABULAR_INFERENCE_TOKEN", "").strip()
    if expected:
        header = ""
        if isinstance(request, Request):
            header = request.headers.get("authorization", "")
        if header != f"Bearer {expected}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        return run_prediction(payload, TabICLPredictor(device="cpu"))
    except TabularRequestError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except TabularUnavailable as exc:
        return JSONResponse({"available": False, "reason": exc.reason}, status_code=503)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback; the caller fails open
        return JSONResponse(
            {"available": False, "reason": f"tabular inference failed: {exc}"}, status_code=503
        )
