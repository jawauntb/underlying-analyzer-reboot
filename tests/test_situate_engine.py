"""Offline tests for the Situate assembly layer: engine, odds, memo, export.

Nothing here touches the network. The market client is a deterministic synthetic
generator (correlated daily closes), there is no options chain (so ``implied``
degrades per horizon with a stated reason), no SEC/Exa/text model (so the memo is
the deterministic template), and the Ken French factor download is stubbed off.
This exercises the guaranteed ship state: exposure + state + base_rates + odds +
scenarios + a posture memo, with the packet still validating.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.situate import odds as odds_module
from app.situate.contract import HORIZONS, empty_packet, validate_packet
from app.situate.engine import build_situate_packet, get_situate_packet, situate_summary
from app.situate.export import export_packet
from app.situate.memo import (
    DISCLAIMER,
    build_citations,
    derive_posture,
    fallback_memo,
    falsifiers,
)

_SYMBOLS = ["NVDA", "SPY", "IWM", "UUP", "FXY", "USO", "GLD", "XLK", "SOXX"]


class _FakeHistory:
    def __init__(self, frame: pd.DataFrame) -> None:
        self.data = frame
        self.dataframe = frame


class _FakeClient:
    """Deterministic correlated daily closes; honours start/end like Massive."""

    def __init__(self, symbols: list[str], *, seed: int = 7, days: int = 2800) -> None:
        rng = np.random.default_rng(seed)
        index = pd.date_range(end="2026-08-31", periods=days, freq="B")
        market = rng.normal(0.0004, 0.011, days)
        betas = {"SPY": 1.0, "IWM": 1.1, "UUP": -0.2, "FXY": 0.1, "USO": 0.4,
                 "GLD": 0.2, "XLK": 1.15, "SOXX": 1.4, "NVDA": 1.6}
        self._series: dict[str, pd.Series] = {}
        for sym in symbols:
            beta = betas.get(sym, rng.uniform(0.8, 1.5))
            idio = rng.normal(0.0, 0.008, days)
            prices = 100.0 * np.exp(np.cumsum(beta * market + idio))
            self._series[sym] = pd.Series(prices, index=index, name=sym)

    def get_history(
        self, ticker: str, *, start: Any, end: Any, interval: str = "1d"
    ) -> _FakeHistory:
        del interval
        sym = str(ticker).upper()
        if sym not in self._series:
            raise ValueError(f"unknown symbol {sym}")
        series = self._series[sym]
        lo, hi = pd.Timestamp(start), pd.Timestamp(end)
        windowed = series[(series.index >= lo) & (series.index <= hi)]
        return _FakeHistory(pd.DataFrame({"Close": windowed}))

    def get_profile(self, ticker: str) -> dict[str, Any]:
        return {
            "longName": f"{ticker} Corp",
            "sector": "Technology",
            "industry": "Semiconductors",
            "marketCap": 1_500_000_000_000.0,
            "longBusinessSummary": "Designs semiconductors and accelerated computing.",
        }

    def get_financials(self, _ticker: str, **_: Any) -> dict[str, Any]:
        # No statements: fundamentals degrades to null with a stated reason.
        return {}


@pytest.fixture()
def no_factor_download(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.situate.factors_data as fd

    def _stub(**_: Any) -> tuple[pd.DataFrame, dict[str, Any]]:
        return pd.DataFrame(), {"error": "download disabled in tests"}

    monkeypatch.setattr(fd, "load_ken_french_monthly", _stub)


def _build(tmp_path: Path, **kwargs: Any) -> dict[str, Any]:
    from app.prism.store import PrismStore

    store = PrismStore(base_dir=tmp_path / "situate", supabase=None)
    client = _FakeClient(_SYMBOLS)
    defaults: dict[str, Any] = {
        "as_of": date(2026, 6, 30),
        "include_stack": False,
        "years": 10,
        "store": store,
        "cache": None,
        "fred_client": None,
    }
    defaults.update(kwargs)
    return build_situate_packet(client, "NVDA", **defaults)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------


def test_full_build_validates_and_ships_core_sections(
    tmp_path: Path, no_factor_download: None  # noqa: ARG001
) -> None:
    packet = _build(tmp_path)
    assert validate_packet(packet) == []
    assert packet["engine"] == "Situate"
    # Guaranteed ship state.
    assert packet["exposure"] is not None
    assert "SPY" in packet["exposure"]["betas"]
    assert packet["state"] is not None
    assert packet["state"]["spy"]["cell"] is not None
    assert packet["base_rates"] is not None
    assert packet["odds"] is not None
    assert packet["scenarios"] is not None
    assert packet["memo"] is not None


def test_implied_degrades_without_a_chain(tmp_path: Path, no_factor_download: None) -> None:  # noqa: ARG001, E501
    packet = _build(tmp_path)
    # No options provider on the fake client: every horizon is null with a reason.
    implied = packet["implied"]
    assert implied is not None  # the section is present...
    assert all(implied["by_horizon"][str(h)] is None for h in HORIZONS)  # ...but empty
    assert implied["unavailable"], "the reason must be recorded, never faked"


def test_odds_fall_back_to_base_rates_when_no_stack_or_implied(
    tmp_path: Path, no_factor_download: None  # noqa: ARG001
) -> None:
    packet = _build(tmp_path)
    by_h = packet["odds"]["by_horizon"]
    sources = {block.get("source") for block in by_h.values() if block.get("quantiles")}
    assert sources <= {"base_rates", "base_rates+implied"}
    assert packet["odds"]["stack_published"] is False


def test_memo_is_posture_not_buy_sell_and_carries_disclaimer(
    tmp_path: Path, no_factor_download: None  # noqa: ARG001
) -> None:
    packet = _build(tmp_path)
    memo = packet["memo"]
    assert memo["posture"]["stance"] in {"odds_favorable", "balanced", "odds_unfavorable"}
    assert memo["method"] == "deterministic"
    assert DISCLAIMER in memo["text"]
    # Check the body only; the disclaimer legitimately says "no buy or sell".
    body = memo["text"].replace(DISCLAIMER, "").lower()
    assert "buy" not in body.replace("buying", "")  # 'what you're buying' is allowed
    assert "sell" not in body
    assert "price target" not in body
    assert len(memo["falsifiers"]) == 3


def test_persist_and_reload(tmp_path: Path, no_factor_download: None) -> None:  # noqa: ARG001
    from app.prism.store import PrismStore

    store = PrismStore(base_dir=tmp_path / "situate", supabase=None)
    _build(tmp_path, store=store)
    reloaded = get_situate_packet("NVDA", "2026-06-30", store=store)
    assert reloaded is not None
    assert reloaded["ticker"] == "NVDA"
    assert reloaded["odds"] is not None


def test_summary_projection_shape(tmp_path: Path, no_factor_download: None) -> None:  # noqa: ARG001
    packet = _build(tmp_path)
    summary = situate_summary(packet)
    assert summary["ticker"] == "NVDA"
    assert summary["posture"]["stance"] in {"odds_favorable", "balanced", "odds_unfavorable"}
    assert "disclaimer" in summary
    assert isinstance(summary["odds"], dict)


def test_lookahead_masking_is_identical(tmp_path: Path, no_factor_download: None) -> None:  # noqa: ARG001, E501
    """Deleting data after t must not change exposure/base_rates/odds."""
    from app.prism.store import PrismStore

    as_of = date(2025, 6, 30)

    def _packet(masked: bool) -> dict[str, Any]:
        client = _FakeClient(_SYMBOLS, seed=7)
        if masked:
            cutoff = pd.Timestamp(as_of)
            client._series = {s: v[v.index <= cutoff] for s, v in client._series.items()}
        store = PrismStore(base_dir=tmp_path / ("m" if masked else "f"), supabase=None)
        return build_situate_packet(
            client, "NVDA", as_of=as_of, include_stack=False, include_memo=False,
            years=10, store=store, persist=False, fred_client=None,
        )

    full = _packet(False)
    masked = _packet(True)
    assert full["exposure"]["betas"] == masked["exposure"]["betas"]
    f_shrunk = full["base_rates"]["by_horizon"]["3"]["shrunk"]
    m_shrunk = masked["base_rates"]["by_horizon"]["3"]["shrunk"]
    assert f_shrunk == m_shrunk
    assert full["odds"]["by_horizon"] == masked["odds"]["by_horizon"]


def test_export_md_json_pdf(tmp_path: Path, no_factor_download: None) -> None:  # noqa: ARG001
    packet = _build(tmp_path)
    body_md, ct_md, name_md = export_packet(packet, "md")
    assert b"Situate" in body_md and name_md.endswith(".md") and "markdown" in ct_md
    body_json, _ct, name_json = export_packet(packet, "json")
    assert name_json.endswith(".json") and b'"engine": "Situate"' in body_json
    body_pdf, ct_pdf, name_pdf = export_packet(packet, "pdf")
    assert body_pdf[:4] == b"%PDF" and ct_pdf == "application/pdf" and name_pdf.endswith(".pdf")


# --------------------------------------------------------------------------
# odds / scenarios (offline, from a hand-built packet)
# --------------------------------------------------------------------------


def _packet_with_distributions() -> dict[str, Any]:
    packet = empty_packet("TST", as_of="2026-06-30")
    packet["exposure"] = {
        "betas": {"SPY": 1.4, "SOXX": 0.9, "USO": -0.3, "GLD": 0.1},
        "r2": 0.72,
        "version": "1.0.0",
    }
    packet["exposure_error"] = None
    packet["base_rates"] = {
        "by_horizon": {
            str(h): {
                "uncond": {"q05": -0.2, "q25": -0.05, "q50": 0.02, "q75": 0.09,
                           "q95": 0.25, "n_eff": 40},
                "shrunk": {"q05": -0.18, "q25": -0.04, "q50": 0.03, "q75": 0.10,
                           "q95": 0.26, "n_eff": 30, "w": 0.55},
                "industry": {"shrunk": {"q50": 0.01}},
            }
            for h in HORIZONS
        },
        "version": "1.0.0",
    }
    packet["base_rates_error"] = None
    return packet


def test_build_odds_blends_base_and_implied() -> None:
    packet = _packet_with_distributions()
    packet["implied"] = {
        "by_horizon": {
            "6": {
                "rw_quantiles": {"q05": -0.16, "q25": -0.02, "q50": 0.05, "q75": 0.12, "q95": 0.30},
                "iv_atm": 0.45,
            }
        },
        "version": "1.0.0",
    }
    result = odds_module.build_odds(packet)
    six = result["by_horizon"]["6"]
    assert six["source"] == "base_rates+implied"
    # q50 is the average of shrunk 0.03 and implied 0.05.
    assert six["quantiles"]["q50"] == pytest.approx(0.04, abs=1e-6)
    assert 0.0 <= six["p_up"] <= 1.0
    assert six["shrink_w"] == 0.55
    # Horizons with no implied fall back to base rates alone.
    assert result["by_horizon"]["1"]["source"] == "base_rates"


def test_stack_excess_is_lifted_to_total_return() -> None:
    packet = _packet_with_distributions()
    packet["stack"] = {
        "published": True,
        "by_horizon": {
            "6": {
                "expected_excess": 0.04,
                "quantiles": {"q05": -0.10, "q25": -0.02, "q50": 0.04, "q75": 0.10, "q95": 0.22},
                "passed_gates": True,
            }
        },
        "version": "1.0.0",
    }
    result = odds_module.build_odds(packet)
    six = result["by_horizon"]["6"]
    assert six["source"] == "stack"
    # Excess q50 0.04 lifted by the industry shrunk median 0.01 -> 0.05.
    assert six["quantiles"]["q50"] == pytest.approx(0.05, abs=1e-6)


def test_p_up_interpolation_edges() -> None:
    all_up = {"q05": 0.01, "q25": 0.03, "q50": 0.05, "q75": 0.08, "q95": 0.2}
    all_down = {"q05": -0.2, "q25": -0.1, "q50": -0.05, "q75": -0.02, "q95": -0.01}
    assert odds_module._p_up_from_quantiles(all_up) == 1.0
    assert odds_module._p_up_from_quantiles(all_down) == 0.0


def test_scenarios_read_the_odds_and_carry_drivers() -> None:
    packet = _packet_with_distributions()
    odds = odds_module.build_odds(packet)
    packet["odds"] = odds
    scenarios = odds_module.build_scenarios(packet, odds)
    assert set(scenarios) >= {"bull", "neutral", "bear"}
    bull_6m = scenarios["bull"]["horizons"]["6"]
    assert bull_6m["quantile_key"] == "q75"
    assert bull_6m["quantile"] == odds["by_horizon"]["6"]["quantiles"]["q75"]
    # Top-2 drivers are the largest |beta| legs: SPY (1.4) and SOXX (0.9).
    driver_names = [d["name"] for d in bull_6m["drivers"]]
    assert driver_names == ["SPY", "SOXX"]


# --------------------------------------------------------------------------
# memo unit behaviour
# --------------------------------------------------------------------------


def test_derive_posture_favorable_when_odds_lean_up() -> None:
    packet = _packet_with_distributions()
    packet["odds"] = {
        "by_horizon": {
            "6": {"quantiles": {"q05": -0.05, "q25": 0.02, "q50": 0.08, "q75": 0.15, "q95": 0.3},
                  "p_up": 0.72, "shrink_w": 0.6, "source": "base_rates"}
        }
    }
    posture = derive_posture(packet)
    assert posture["stance"] == "odds_favorable"
    assert posture["horizon"] == 6
    assert 0.0 <= posture["conviction"] <= 1.0
    assert "the data suggests" in posture["one_line"].lower()


def test_citations_bind_module_and_version() -> None:
    packet = _packet_with_distributions()
    citations = build_citations(packet)
    assert citations
    exposure_cites = [c for c in citations if c["module"] == "exposure"]
    assert exposure_cites and exposure_cites[0]["version"] == "1.0.0"
    assert all(c["id"].startswith("C") for c in citations)


def test_fallback_memo_has_no_buy_sell_and_three_falsifiers() -> None:
    packet = _packet_with_distributions()
    packet["odds"] = odds_module.build_odds(packet)
    memo = fallback_memo(packet, reason="no key")
    assert memo["method"] == "deterministic"
    assert memo["posture"]["stance"] in {"odds_favorable", "balanced", "odds_unfavorable"}
    assert len(memo["falsifiers"]) == 3
    assert len(falsifiers(packet)) == 3
    assert DISCLAIMER in memo["text"]
