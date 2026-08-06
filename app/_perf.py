"""Shared performance primitives: pooled HTTP sessions and thread-safe TTL caching.

These helpers are intentionally dependency-free (stdlib + ``requests``, both already
required) and behavior-preserving. They exist so the API surface can reuse keep-alive
connections under fan-out concurrency and memoize idempotent, expensive lookups without
each module re-implementing the same pattern.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from functools import wraps
from typing import Any, TypeVar, cast

import requests
from requests.adapters import HTTPAdapter

try:  # urllib3 ships with requests; Retry lives here.
    from urllib3.util.retry import Retry
except Exception:  # pragma: no cover - defensive; requests always vendors urllib3
    Retry = None  # type: ignore[assignment, misc]

__all__ = ["tune_session", "pooled_session", "TTLCache", "ttl_cached"]

# Sized for gunicorn threads (4) plus in-request ThreadPoolExecutor fan-out. Generous
# but bounded so a single worker cannot exhaust the remote host's connection budget.
DEFAULT_POOL_MAXSIZE = 32
DEFAULT_POOL_CONNECTIONS = 32


def tune_session(
    session: requests.Session,
    *,
    pool_connections: int = DEFAULT_POOL_CONNECTIONS,
    pool_maxsize: int = DEFAULT_POOL_MAXSIZE,
    total_retries: int = 0,
    backoff_factor: float = 0.2,
) -> requests.Session:
    """Mount tuned HTTP/HTTPS adapters on an existing session, in place.

    Widens the connection pool so concurrent requests reuse keep-alive connections
    instead of churning new sockets. Retries default to 0 to preserve existing behavior
    (callers that want retry-on-transient can opt in via ``total_retries``).
    """
    retry: Any = None
    if total_retries and Retry is not None:
        retry = Retry(
            total=total_retries,
            backoff_factor=backoff_factor,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "OPTIONS"}),
            raise_on_status=False,
        )
    adapter = HTTPAdapter(
        pool_connections=pool_connections,
        pool_maxsize=pool_maxsize,
        max_retries=retry if retry is not None else 0,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def pooled_session(
    session: requests.Session | None = None,
    **kwargs: Any,
) -> requests.Session:
    """Return a session with tuned pooling, creating one if not provided."""
    return tune_session(session or requests.Session(), **kwargs)


@dataclass
class _Entry:
    value: Any
    expires_at: float


F = TypeVar("F", bound=Callable[..., Any])


class TTLCache:
    """A tiny thread-safe TTL cache. Monotonic clock; never serves stale entries."""

    def __init__(self, ttl_seconds: float, max_entries: int = 512) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._store: dict[Hashable, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        if self.ttl_seconds <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._store.pop(key, None)
                return None
            return entry.value

    def set(self, key: Hashable, value: Any) -> None:
        if self.ttl_seconds <= 0:
            return
        with self._lock:
            if len(self._store) >= self.max_entries:
                # Evict the soonest-to-expire entry to bound memory.
                oldest = min(self._store, key=lambda k: self._store[k].expires_at)
                self._store.pop(oldest, None)
            self._store[key] = _Entry(value=value, expires_at=time.monotonic() + self.ttl_seconds)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


def ttl_cached(ttl_seconds: float, max_entries: int = 512) -> Callable[[F], F]:
    """Decorator memoizing a function's return by its positional/keyword args for a TTL.

    Only use on pure, idempotent functions whose args are hashable. On a ``TypeError``
    from unhashable args the call falls through uncached, so it can never break a caller.
    """
    cache = TTLCache(ttl_seconds, max_entries=max_entries)

    def decorate(func: F) -> F:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                key: Hashable = (args, tuple(sorted(kwargs.items())))
            except TypeError:
                return func(*args, **kwargs)
            hit = cache.get(key)
            if hit is not None:
                return hit
            result = func(*args, **kwargs)
            if result is not None:
                cache.set(key, result)
            return result

        wrapper.cache = cache  # type: ignore[attr-defined]
        return cast(F, wrapper)

    return decorate
