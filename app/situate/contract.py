"""The ``SituatePacket`` contract.

Every Situate workstream codes against the names in this module rather than
against each other's implementations, so a section that is not built yet is still
present in the packet as ``None`` with a sibling ``<section>_error`` string. That
keeps the packet shape stable for the API proxy, the iOS dashboard and the agent
tools even when a data source is down or a module has not shipped.

Situate is the reformed engine that replaces Prism's *forecasting* posture with a
*situating* one: what a stock is exposed to, what the odds look like per horizon,
what the options market is pricing, and what the business is saying. It never
emits a point price target or buy/sell grammar (see the plan's NON-GOALS).

Conventions
-----------
* Returns are decimal fractions (``0.034`` is ``3.4%``), never percent.
* Dates are ISO-8601 strings; timestamps are ISO-8601 UTC.
* Absent numbers are ``None``, never ``0`` and never a placeholder — the engine
  must be able to say "we do not know" without fabricating a value.
* Horizons are integer months drawn from :data:`HORIZONS`.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal, TypedDict

ENGINE_NAME = "Situate"
ENGINE_ALIAS = "ubermemo"
ENGINE_VERSION = "1.0.0"

#: Module versions stamped into ``meta.versions`` and memo citations. Each module
#: owns its own entry; S1 owns exposure, state, panel and factors.
MODULE_VERSIONS: dict[str, str] = {
    "panel": "1.0.0",
    "factors_data": "1.0.0",
    "exposure": "1.0.0",
    "state": "1.0.0",
}

#: Forward horizons, in months. The single source of truth for every per-horizon
#: block (base_rates, implied, odds, scenarios).
HORIZONS: tuple[int, ...] = (1, 2, 3, 6, 12, 18)

#: Human-readable label for each horizon (``3`` -> ``"3m"``).
HORIZON_LABELS: dict[int, str] = {h: f"{h}m" for h in HORIZONS}

#: The empirical quantiles reported for every distribution.
QUANTILES: tuple[int, ...] = (5, 25, 50, 75, 95)

#: Quantile dict keys (``"q05"``, ``"q25"`` ...), in order.
QUANTILE_KEYS: tuple[str, ...] = tuple(f"q{q:02d}" for q in QUANTILES)

#: Trading days per calendar year, for annualising realised volatility.
TRADING_DAYS_PER_YEAR = 252
#: Calendar months per year, for annualising monthly statistics.
MONTHS_PER_YEAR = 12

#: Top-level keys of the packet, in the order documented in the build plan.
#: :func:`empty_packet` always returns exactly these keys (plus ``*_error``).
PACKET_KEYS: tuple[str, ...] = (
    "ticker",
    "as_of",
    "generated_at",
    "engine",
    "engine_version",
    "profile",
    "exposure",
    "state",
    "base_rates",
    "implied",
    "fundamentals",
    "text",
    "levels",
    "stack",
    "odds",
    "scenarios",
    "memo",
    "sources",
    "meta",
)

#: Sections that may be ``None`` and therefore carry a sibling ``*_error`` key.
NULLABLE_SECTIONS: tuple[str, ...] = (
    "profile",
    "exposure",
    "state",
    "base_rates",
    "implied",
    "fundamentals",
    "text",
    "levels",
    "stack",
    "odds",
    "scenarios",
    "memo",
)

PostureStance = Literal["odds_favorable", "balanced", "odds_unfavorable"]
VolState = Literal["high", "low"]
TrendState = Literal["up", "down"]


# --------------------------------------------------------------------------
# Typed section skeletons (documentation + light static typing)
# --------------------------------------------------------------------------


class Provenance(TypedDict, total=False):
    """One row of the packet's ``sources`` list."""

    provider: str
    url: str | None
    series_id: str | None
    symbol: str | None
    fetched_at: str
    confidence: float | None
    note: str | None


class QuantileBlock(TypedDict, total=False):
    """A 5/25/50/75/95 quantile block with a hit rate and effective sample size."""

    q05: float | None
    q25: float | None
    q50: float | None
    q75: float | None
    q95: float | None
    hit: float | None
    n_eff: float | None


class FactorView(TypedDict, total=False):
    """The named-factor (Ken French) OLS view inside ``exposure``."""

    alpha_annual: float | None
    loadings: dict[str, float | None]
    t_stats: dict[str, float | None]
    r2: float | None
    n: int
    error: str | None


class ExposureSection(TypedDict, total=False):
    """``packet["exposure"]`` (SPEC 5.1)."""

    basket: list[str]
    betas: dict[str, float | None]
    se: dict[str, float | None]
    r2: float | None
    idiosyncratic_share: float | None
    residual_vol_annual: float | None
    factor: FactorView
    beta_path: dict[str, list[dict[str, Any]]]
    change_6m: dict[str, float | None]
    change_12m: dict[str, float | None]
    method: str
    lambda_: float | None
    n_months: int
    half_life_months: int
    notes: list[str]


class GridState(TypedDict, total=False):
    """One 2x2 vol x trend cell (``packet["state"]["spy"]`` / ``["ticker"]``)."""

    vol_state: VolState | None
    trend_state: TrendState | None
    cell: str | None
    realized_vol_21d: float | None
    vol_median_2y: float | None
    ret_12m_1m: float | None
    n_months: int
    error: str | None


class HmmOpinion(TypedDict, total=False):
    """Optional 3-state HMM second opinion (``packet["state"]["hmm"]``)."""

    probs: dict[str, float | None]
    label: str | None
    n_days: int
    converged: bool
    error: str | None


class StateContext(TypedDict, total=False):
    """VIX / HY / curve context percentiles (``packet["state"]["context"]``)."""

    vix_pct: float | None
    hy_oas_pct: float | None
    curve_10y_2y: float | None
    vix_level: float | None
    hy_oas_level: float | None
    error: str | None


class StateSection(TypedDict, total=False):
    """``packet["state"]`` (SPEC 5.2)."""

    spy: GridState
    ticker: GridState
    hmm: HmmOpinion | None
    context: StateContext


class PacketMeta(TypedDict, total=False):
    """``packet["meta"]`` — honest bookkeeping about what actually ran."""

    errors: list[dict[str, str]]
    unavailable: list[dict[str, str]]
    source_status: dict[str, str]
    timings_ms: dict[str, float]
    versions: dict[str, str]
    cache: dict[str, Any]
    notes: list[str]


# --------------------------------------------------------------------------
# Constructors
# --------------------------------------------------------------------------


def utc_now_iso() -> str:
    """Current UTC timestamp as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


def empty_quantile_block() -> QuantileBlock:
    """A quantile block with every key present and no fabricated values."""
    block: QuantileBlock = dict.fromkeys(QUANTILE_KEYS)  # type: ignore[assignment]
    block["hit"] = None
    block["n_eff"] = None
    return block


def empty_exposure() -> ExposureSection:
    """A fully-shaped exposure section with no numbers filled in."""
    return ExposureSection(
        basket=[],
        betas={},
        se={},
        r2=None,
        idiosyncratic_share=None,
        residual_vol_annual=None,
        factor=FactorView(
            alpha_annual=None, loadings={}, t_stats={}, r2=None, n=0, error=None
        ),
        beta_path={},
        change_6m={},
        change_12m={},
        method="ewma_ridge",
        lambda_=None,
        n_months=0,
        half_life_months=24,
        notes=[],
    )


def empty_grid_state(error: str | None = None) -> GridState:
    """A 2x2 grid cell with no observations."""
    return GridState(
        vol_state=None,
        trend_state=None,
        cell=None,
        realized_vol_21d=None,
        vol_median_2y=None,
        ret_12m_1m=None,
        n_months=0,
        error=error,
    )


def empty_state() -> StateSection:
    """A fully-shaped state section with no numbers filled in."""
    return StateSection(
        spy=empty_grid_state(),
        ticker=empty_grid_state(),
        hmm=None,
        context=StateContext(
            vix_pct=None,
            hy_oas_pct=None,
            curve_10y_2y=None,
            vix_level=None,
            hy_oas_level=None,
            error=None,
        ),
    )


def empty_meta() -> PacketMeta:
    """A fresh ``meta`` block, seeded with the module version table."""
    return PacketMeta(
        errors=[],
        unavailable=[],
        source_status={},
        timings_ms={},
        versions=dict(MODULE_VERSIONS),
        cache={},
        notes=[],
    )


def empty_packet(ticker: str, *, as_of: date | str | None = None) -> dict[str, Any]:
    """Return a packet with every contract key present and no invented numbers.

    Sections start as ``None``; :func:`set_section` fills them in and records a
    ``<section>_error`` when a builder fails, so consumers can always index the
    same keys regardless of which modules ran.
    """
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    resolved_as_of = as_of.isoformat() if isinstance(as_of, date) else (as_of or None)
    packet: dict[str, Any] = {
        "ticker": symbol,
        "as_of": resolved_as_of,
        "generated_at": utc_now_iso(),
        "engine": ENGINE_NAME,
        "engine_version": ENGINE_VERSION,
        "sources": [],
        "meta": empty_meta(),
    }
    for section in NULLABLE_SECTIONS:
        packet[section] = None
        packet[f"{section}_error"] = None
    ordered = {key: packet[key] for key in PACKET_KEYS}
    for section in NULLABLE_SECTIONS:
        ordered[f"{section}_error"] = packet[f"{section}_error"]
    return ordered


# --------------------------------------------------------------------------
# Mutators
# --------------------------------------------------------------------------


def set_section(
    packet: dict[str, Any],
    section: str,
    value: Any,
    *,
    error: str | None = None,
) -> dict[str, Any]:
    """Write one section (and its ``*_error`` sibling) into ``packet``.

    Passing ``error`` leaves the section ``None`` and records the reason both
    beside the section and in ``meta.errors``, which is how the engine keeps a
    single failing source from failing the whole build.
    """
    packet[section] = None if error else value
    if section in NULLABLE_SECTIONS:
        packet[f"{section}_error"] = error
    if error:
        record_error(packet, section, error)
    return packet


def record_error(packet: dict[str, Any], source: str, error: str) -> dict[str, Any]:
    """Append one honest failure to ``meta.errors`` and mark the source status."""
    meta = packet.setdefault("meta", empty_meta())
    errors = meta.setdefault("errors", [])
    entry = {"source": str(source), "error": str(error)[:500]}
    if entry not in errors:
        errors.append(entry)
    meta.setdefault("source_status", {})[str(source)] = "error"
    return packet


def record_unavailable(packet: dict[str, Any], source: str, reason: str) -> dict[str, Any]:
    """Record a source that is genuinely unavailable rather than broken."""
    meta = packet.setdefault("meta", empty_meta())
    unavailable = meta.setdefault("unavailable", [])
    entry = {"source": str(source), "reason": str(reason)[:500]}
    if entry not in unavailable:
        unavailable.append(entry)
    meta.setdefault("source_status", {})[str(source)] = "unavailable"
    return packet


def record_source(packet: dict[str, Any], source: Provenance | dict[str, Any]) -> dict[str, Any]:
    """Append a provenance row to ``packet["sources"]`` (deduplicated)."""
    sources = packet.setdefault("sources", [])
    row = dict(source)
    if row not in sources:
        sources.append(row)
    return packet


def record_timing(packet: dict[str, Any], name: str, milliseconds: float) -> dict[str, Any]:
    """Record how long one section took, for the ``meta.timings_ms`` block."""
    meta = packet.setdefault("meta", empty_meta())
    meta.setdefault("timings_ms", {})[str(name)] = round(float(milliseconds), 3)
    return packet


def record_version(packet: dict[str, Any], module: str, version: str) -> dict[str, Any]:
    """Stamp a module version into ``meta.versions`` for memo citations."""
    meta = packet.setdefault("meta", empty_meta())
    meta.setdefault("versions", {})[str(module)] = str(version)
    return packet


def validate_packet(packet: dict[str, Any]) -> list[str]:
    """Return a list of contract violations (empty when the packet is valid)."""
    problems: list[str] = []
    for key in PACKET_KEYS:
        if key not in packet:
            problems.append(f"missing key: {key}")
    for section in NULLABLE_SECTIONS:
        error_key = f"{section}_error"
        if error_key not in packet:
            problems.append(f"missing key: {error_key}")
            continue
        if packet.get(section) is None and packet.get(error_key) is None:
            continue
        if packet.get(section) is not None and packet.get(error_key) is not None:
            problems.append(f"{section} is populated but {error_key} is also set")
    meta = packet.get("meta")
    if not isinstance(meta, dict):
        problems.append("meta must be an object")
    elif not isinstance(meta.get("errors"), list):
        problems.append("meta.errors must be a list")
    if not isinstance(packet.get("sources"), list):
        problems.append("sources must be a list")
    if packet.get("engine") != ENGINE_NAME:
        problems.append(f"engine must be {ENGINE_NAME!r}")
    problems.extend(_density_integrity_problems(packet))
    return problems


def _trapz(y: list[float], x: list[float]) -> float:
    """Trapezoidal integral of ``y`` over ``x`` (kept dependency-free here)."""
    return sum(0.5 * (y[i] + y[i - 1]) * (x[i] - x[i - 1]) for i in range(1, len(x)))


def _median_from_density(ks: list[float], pdfs: list[float]) -> float | None:
    """The 0.5-mass point of a density sampled at ``ks`` with values ``pdfs``."""
    total = _trapz(pdfs, ks)
    if total <= 0.0:
        return None
    target = 0.5 * total
    cum = 0.0
    for i in range(1, len(ks)):
        seg = 0.5 * (pdfs[i] + pdfs[i - 1]) * (ks[i] - ks[i - 1])
        if cum + seg >= target:
            frac = (target - cum) / seg if seg > 0.0 else 0.0
            return ks[i - 1] + frac * (ks[i] - ks[i - 1])
        cum += seg
    return ks[-1]


def _density_integrity_problems(packet: dict[str, Any]) -> list[str]:
    """Cross-check each published implied density.

    The serialized ``rn_density`` and the reported risk-neutral quantiles are two
    views of one distribution, so a published density must integrate to ~1 and its
    median must agree with the reported ``q50``. A coarse point-sampling of the
    density (rather than a mass-preserving resample) breaks both, so this catches
    that regression at the packet level.
    """
    problems: list[str] = []
    implied = packet.get("implied")
    if not isinstance(implied, dict):
        return problems
    by_horizon = implied.get("by_horizon")
    if not isinstance(by_horizon, dict):
        return problems
    spot = implied.get("underlying_price")
    for label, block in by_horizon.items():
        if not isinstance(block, dict):
            continue
        density = block.get("rn_density")
        if not isinstance(density, list) or len(density) < 3:
            continue
        try:
            ks = [float(point["k"]) for point in density]
            pdfs = [float(point["pdf"]) for point in density]
        except (KeyError, TypeError, ValueError):
            problems.append(f"implied[{label}].rn_density has malformed points")
            continue
        area = _trapz(pdfs, ks)
        if not 0.95 <= area <= 1.05:
            problems.append(f"implied[{label}].rn_density integrates to {area:.3f}, not ~1")
        reported = (block.get("quantiles") or {}).get("q50")
        if (
            isinstance(spot, (int, float))
            and not isinstance(spot, bool)
            and spot > 0
            and isinstance(reported, (int, float))
            and not isinstance(reported, bool)
        ):
            median_k = _median_from_density(ks, pdfs)
            if median_k is not None:
                derived = median_k / float(spot) - 1.0
                if abs(derived - float(reported)) > 0.06:
                    problems.append(
                        f"implied[{label}].rn_density median {derived:.3f} disagrees "
                        f"with reported q50 {float(reported):.3f}"
                    )
    return problems


# --------------------------------------------------------------------------
# Window helpers
# --------------------------------------------------------------------------


def horizon_label(months: int) -> str:
    """``3`` -> ``"3m"``; raises for a horizon outside :data:`HORIZONS`."""
    if int(months) not in HORIZON_LABELS:
        raise ValueError(f"unknown horizon: {months}")
    return HORIZON_LABELS[int(months)]


def quantile_key(quantile: int | float) -> str:
    """``25`` -> ``"q25"``; the canonical key for a percentile."""
    return f"q{int(round(float(quantile))):02d}"
