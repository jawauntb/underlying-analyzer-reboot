"""Prism — the full-stack investment memo engine (working alias ``ubermemo``).

A prism splits one beam into its spectrum. This package splits one ticker's
price history into its macro, factor, regime, spectral, entropy, fundamental and
filing components, keeps every intermediate number with provenance, and hands
the whole packet to the narrative layer.

Layout
------
``contract``   packet skeleton, ``empty_packet()``, ``PACKET_KEYS`` and the
               ``MacroSeries``/``SeasonalStats`` shapes every workstream codes to.
``universe``   benchmark resolver (indices, sectors, industry, commodities, FX,
               credit, rates, vol, crypto) plus the sector -> ETF map.
``data``       Massive daily closes, alignment helpers and per-symbol provenance.
``macro``      FRED client, ``MacroSeries`` builder, yield-curve shape.
``cache``      two-tier (local JSON + optional Supabase) series cache.
``seasonality``calendar-month statistics and forward-return distributions.
``relational`` gauge-fixed cross-asset beta/correlation/kinematics/impact weights.

Only the modules owned by workstream W1 are imported here; the quant (W2) and
narrative (W3) modules are imported lazily by ``app.prism.engine`` so a partial
checkout still imports cleanly.
"""

from __future__ import annotations

from app.prism.contract import (
    ENGINE_NAME,
    ENGINE_VERSION,
    PACKET_KEYS,
    empty_macro_series,
    empty_packet,
    empty_seasonal_stats,
    record_error,
    set_section,
)

__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "PACKET_KEYS",
    "empty_macro_series",
    "empty_packet",
    "empty_seasonal_stats",
    "record_error",
    "set_section",
]
