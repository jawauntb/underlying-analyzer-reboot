"""Consolidated utility functions used across multiple modules.

This module contains shared utility functions that were previously
duplicated across prism, situate, and other modules.
"""

from __future__ import annotations

import math
from typing import Any


def finite(value: Any) -> float | None:
    """Convert a value to float, returning None if not finite.
    
    Args:
        value: Value to convert.
        
    Returns:
        Float value if finite, None otherwise.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def pct(value: Any, *, digits: int = 1, sign: bool = True) -> str:
    """Format a value as percentage string.
    
    Args:
        value: Value to format.
        digits: Number of decimal digits.
        sign: Whether to include +/- sign.
        
    Returns:
        Formatted percentage string or "n/a" if not finite.
    """
    number = finite(value)
    if number is None:
        return "n/a"
    return f"{number * 100:{'+' if sign else ''}.{digits}f}%"


def pct2(value: Any, *, digits: int = 2) -> str:
    """Format a value as percentage string with 2 decimal places.
    
    Args:
        value: Value to format.
        digits: Number of decimal digits.
        
    Returns:
        Formatted percentage string with dash if not finite.
    """
    number = finite(value)
    return "-" if number is None else f"{number * 100:+.{digits}f}%"


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Compute CDF of normal distribution at x.
    
    Args:
        x: Point at which to evaluate.
        mu: Mean.
        sigma: Standard deviation.
        
    Returns:
        CDF value in [0, 1].
    """
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def normal_pdf(x: float, loc: float, scale: float) -> float:
    """Compute PDF of normal distribution at x.
    
    Args:
        x: Point at which to evaluate.
        loc: Mean.
        scale: Standard deviation.
        
    Returns:
        PDF value.
    """
    return float(math.exp(-0.5 * ((x - loc) / scale) ** 2) / (scale * math.sqrt(2.0 * math.pi)))


def cache_key(*parts: str) -> str:
    """Create a cache key from parts.
    
    Args:
        *parts: Parts to combine into key.
        
    Returns:
        Cache key string.
    """
    return "|".join(str(p) for p in parts)


def error_response(message: str, status: int = 400) -> tuple[dict[str, Any], int]:
    """Create a standardized error response.
    
    Args:
        message: Error message.
        status: HTTP status code.
        
    Returns:
        Tuple of (response dict, status code).
    """
    return {"error": message}, status


__all__ = [
    "finite",
    "pct",
    "pct2",
    "normal_cdf",
    "normal_pdf",
    "cache_key",
    "error_response",
]
