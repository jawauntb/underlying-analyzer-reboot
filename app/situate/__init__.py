"""Situate — a single-name research engine that reforms Prism.

Situate *situates* a stock rather than forecasting it: what you are exposed to
(``exposure``), what the odds look like per horizon (``base_rates``, ``odds``),
what the options market is pricing (``implied``), and what the business is saying
(``fundamentals``, ``text``). It reuses Prism's data plumbing wholesale and adds
only ``scipy``.

This package is additive: it never deletes or breaks Prism (``app/prism``), which
stays in place and green.
"""

from __future__ import annotations

from app.situate.contract import (
    ENGINE_NAME,
    ENGINE_VERSION,
    HORIZONS,
    PACKET_KEYS,
    empty_packet,
    validate_packet,
)

__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "HORIZONS",
    "PACKET_KEYS",
    "empty_packet",
    "validate_packet",
]
