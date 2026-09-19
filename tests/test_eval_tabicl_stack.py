"""Harness smoke for ``scripts/eval_tabicl_stack.py`` with an injected fake predictor.

The script is a developer tool (never run in CI against a real model), but its
walk-forward loop must honour the same purge/embargo rule as the ridge and its
report must be writable. No ``tabicl``, no network, no checkpoint download.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import numpy as np
import pandas as pd
import pytest

from app.situate.stack import StackConfig, _month_idx, eligible_train_mask
from app.situate.validate import synthetic_panel
from app.tabular import TabularPrediction

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_tabicl_stack.py"


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_tabicl_stack", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RecordingRegressor:
    name = "recording"

    def __init__(self) -> None:
        self.calls: list[tuple[pd.DataFrame, pd.DataFrame]] = []

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: object = (),
        options: dict[str, object] | None = None,
    ) -> TabularPrediction:
        del y_context, categorical, options
        self.calls.append((x_context.copy(), x_query.copy()))
        return TabularPrediction(
            task=task,
            predictions=[float(v) for v in x_query["mom_12_1"]],
            context_rows_used=int(x_context.shape[0]),
        )


_CFG = StackConfig(
    horizons=(1, 3), min_train_months=24, min_train_rows=120, min_cross_section=8, n_bootstrap=100
)


def test_walk_forward_icl_purges_like_the_ridge(script: ModuleType) -> None:
    frame = synthetic_panel(n_symbols=20, n_months=100, horizons=(1, 3), signal=1.0, seed=4)
    frame["_m"] = _month_idx(frame["date"])
    predictor = RecordingRegressor()
    result = script.walk_forward_icl(
        frame, ["mom_12_1", "rev_1m"], horizon=3, cfg=_CFG, target_col="target_h3",
        predictor=predictor, max_context_rows=150,
    )
    assert result["n_oos_rows"] > 0 and result["model_calls"] == len(predictor.calls)
    assert result["mean_ic"] > 0.3  # the fake ranks by the true signal
    # Every call's context is at most the cap, and every context row's label
    # window closed at least one embargo month before the earliest query month.
    oos = result["oos"]
    for context, query in predictor.calls:
        assert context.shape[0] <= 150
        assert query.shape[0] > 0
    # The context handed over is a strict subset of rows eligible for that year.
    first_query_month = int(oos["_m"].min())
    eligible = eligible_train_mask(frame["_m"].to_numpy(), first_query_month, 3, 1)
    assert predictor.calls[0][0].shape[0] <= int(eligible.sum())


def test_compare_and_report(script: ModuleType, tmp_path: Path) -> None:
    frame = synthetic_panel(n_symbols=20, n_months=100, horizons=(1, 3), signal=1.0, seed=2)
    report = script.compare(
        frame, cfg=_CFG, predictor=script.FakeRegressor(), max_context_rows=400
    )
    assert set(report["by_horizon"]) == {"1", "3"}
    block = report["by_horizon"]["1"]
    for name in ("ridge", "tabicl"):
        gates = block[name]
        assert gates["oos_ic"] is not None
        assert isinstance(gates["passed_gates"], bool)
        assert len(gates["oos_ic_ci"]) == 2
    assert block["ic_delta_tabicl_minus_ridge"] is not None
    assert block["tabicl"]["model_calls"] >= 1 and block["ridge"]["model_calls"] == 0
    assert report["gates"]["n_trials"] == 4  # two models x two horizons
    report.update({"generated_at": "t", "source": "synthetic", "predictor": "fake"})
    json_path, md_path = script.write_report(report, tmp_path / "tabicl-eval")
    assert json.loads(json_path.read_text())["by_horizon"]["3"]["tabicl"]["oos_ic"] is not None
    markdown = md_path.read_text()
    assert "TabICL v2 vs ridge stack" in markdown
    assert "| 1m | tabicl |" in markdown and "| 3m | ridge |" in markdown


def test_main_smoke_with_fake_predictor(
    script: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = script.main([
        "--fake-predictor", "--horizons", "1", "--n-symbols", "16", "--n-months", "90",
        "--out-dir", str(tmp_path),
    ])
    assert code == 0
    written = sorted(tmp_path.glob("*.md"))
    assert len(written) == 1
    assert "[written]" in capsys.readouterr().out


def test_main_exits_2_without_a_model(script: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    from app import tabular as tabular_module

    monkeypatch.delenv("TABULAR_INFERENCE_URL", raising=False)
    monkeypatch.setattr(tabular_module, "tabicl_installed", lambda: False)
    assert script.main(["--horizons", "1"]) == 2


def test_fake_regressor_is_deterministic(script: ModuleType) -> None:
    x = pd.DataFrame({"mom_12_1": [0.5, -0.2], "rev_1m": [0.0, 0.0]})
    out = script.FakeRegressor().predict("regression", x, pd.Series([1.0, 2.0]), x)
    assert out.predictions == [0.5, -0.2]
    assert np.isfinite(out.predictions).all()
