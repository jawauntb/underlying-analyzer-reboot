"""Two-tier cache for the ticker-independent parts of a Prism build.

Benchmark closes and macro series do not depend on which ticker is being
analysed, so a second build in the same month should not re-download 50 symbols
of ten-year history. Entries are keyed by ``(namespace, key, as_of_month)`` and
stored:

1. as JSON files under ``PRISM_CACHE_DIR`` (default ``.prism-cache/``, already
   gitignored), which always works, including offline; and
2. optionally in the Supabase table ``prism_series_cache`` when both
   ``SUPABASE_URL`` and ``SUPABASE_SERVICE_ROLE_KEY`` are set, so a Railway
   worker and a local run share one cache.

The Supabase tier is strictly best effort: any REST failure degrades to the
local tier and is recorded on the cache's ``errors`` list rather than raised,
because a cache outage must never fail a build.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pandas as pd
import requests

DEFAULT_CACHE_DIR = ".prism-cache"
DEFAULT_TTL_DAYS = 31
SERIES_NAMESPACE = "series"
MACRO_NAMESPACE = "macro"
SUPABASE_TABLE = "prism_series_cache"
_UNSAFE_KEY = re.compile(r"[^A-Za-z0-9._-]+")


class PrismCacheError(RuntimeError):
    """Raised only for programmer errors (bad namespace/key), never for I/O."""


def cache_dir_from_env() -> Path:
    """Resolve ``PRISM_CACHE_DIR`` (default ``.prism-cache``) to a path."""
    return Path(os.getenv("PRISM_CACHE_DIR") or DEFAULT_CACHE_DIR)


def as_of_month(value: date | str | None = None) -> str:
    """Normalise a date to the ``YYYY-MM`` bucket used as the cache generation."""
    if value is None:
        return datetime.now(UTC).date().strftime("%Y-%m")
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    text = str(value).strip()
    if len(text) >= 7 and text[4] == "-":
        return text[:7]
    raise PrismCacheError(f"cannot derive an as-of month from {value!r}")


def safe_key(value: str) -> str:
    """Make a symbol or series id safe as a file name (``X:BTCUSD`` -> ``X_BTCUSD``)."""
    cleaned = _UNSAFE_KEY.sub("_", str(value or "").strip()).lstrip(".")
    if not cleaned:
        raise PrismCacheError(f"unusable cache key: {value!r}")
    return cleaned[:120]


def series_to_payload(series: pd.Series) -> dict[str, Any]:
    """Serialise a date-indexed float series to plain JSON."""
    clean = series.dropna()
    index = pd.to_datetime(clean.index)
    return {
        "dates": [stamp.date().isoformat() for stamp in index],
        "values": [float(value) for value in clean.to_numpy()],
    }


def payload_to_series(payload: dict[str, Any], *, name: str | None = None) -> pd.Series:
    """Rebuild a date-indexed float series from :func:`series_to_payload`."""
    dates = payload.get("dates") or []
    values = payload.get("values") or []
    if len(dates) != len(values):
        raise PrismCacheError("cached series payload has mismatched dates and values")
    index = pd.to_datetime(pd.Index(dates))
    return pd.Series([float(value) for value in values], index=index, name=name, dtype="float64")


class SupabaseSeriesCache:
    """Thin PostgREST wrapper over ``prism_series_cache``.

    Mirrors the request shape used by :class:`app.alert_scheduler.SupabaseAlertStore`
    (service-role key in both ``apikey`` and ``Authorization``) so there is one
    Supabase access pattern in this codebase.
    """

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        session: requests.Session | None = None,
        timeout: float = 15.0,
        table: str = SUPABASE_TABLE,
    ) -> None:
        if not supabase_url:
            raise PrismCacheError("SUPABASE_URL is required for the Supabase cache tier")
        if not service_role_key:
            raise PrismCacheError(
                "SUPABASE_SERVICE_ROLE_KEY is required for the Supabase cache tier"
            )
        self.supabase_url = supabase_url.rstrip("/")
        self.service_role_key = service_role_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self.table = table

    @classmethod
    def from_env(
        cls, *, session: requests.Session | None = None
    ) -> SupabaseSeriesCache | None:
        """Build from env, or return ``None`` when Supabase is not configured."""
        url = os.getenv("SUPABASE_URL", "").strip()
        key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        if not url or not key:
            return None
        return cls(supabase_url=url, service_role_key=key, session=session)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        headers.update(extra or {})
        return headers

    def fetch(self, cache_key: str) -> dict[str, Any] | None:
        """Return the stored row for ``cache_key``, or ``None``."""
        response = self.session.get(
            urljoin(f"{self.supabase_url}/", f"rest/v1/{self.table}"),
            params={"select": "*", "cache_key": f"eq.{cache_key}", "limit": "1"},
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise PrismCacheError(f"Supabase {self.table} GET failed: {response.status_code}")
        rows = response.json() if response.text else []
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        return None

    def store(self, row: dict[str, Any]) -> None:
        """Upsert one cache row on the ``cache_key`` primary key."""
        response = self.session.post(
            urljoin(f"{self.supabase_url}/", f"rest/v1/{self.table}"),
            params={"on_conflict": "cache_key"},
            json=[row],
            headers=self._headers(
                {"Prefer": "resolution=merge-duplicates,return=minimal"}
            ),
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise PrismCacheError(f"Supabase {self.table} POST failed: {response.status_code}")


class PrismCache:
    """Local-JSON cache with an optional shared Supabase tier."""

    def __init__(
        self,
        *,
        base_dir: Path | str | None = None,
        supabase: SupabaseSeriesCache | None = None,
        ttl_days: int = DEFAULT_TTL_DAYS,
        enabled: bool = True,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else cache_dir_from_env()
        self.supabase = supabase
        self.ttl = timedelta(days=max(1, int(ttl_days)))
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.errors: list[dict[str, str]] = []
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        *,
        session: requests.Session | None = None,
        base_dir: Path | str | None = None,
    ) -> PrismCache:
        """Build a cache from ``PRISM_CACHE_DIR`` / ``PRISM_CACHE_ENABLED`` / Supabase env."""
        enabled = str(os.getenv("PRISM_CACHE_ENABLED", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        try:
            ttl_days = int(os.getenv("PRISM_CACHE_TTL_DAYS", str(DEFAULT_TTL_DAYS)))
        except ValueError:
            ttl_days = DEFAULT_TTL_DAYS
        supabase: SupabaseSeriesCache | None = None
        try:
            supabase = SupabaseSeriesCache.from_env(session=session)
        except PrismCacheError:
            supabase = None
        return cls(
            base_dir=base_dir,
            supabase=supabase,
            ttl_days=ttl_days,
            enabled=enabled,
        )

    # ------------------------------------------------------------------ keys

    def cache_key(self, namespace: str, key: str, *, generation: str) -> str:
        """The stable identifier shared by both tiers."""
        return f"{safe_key(namespace)}:{safe_key(key)}:{safe_key(generation)}"

    def _path(self, namespace: str, key: str, *, generation: str) -> Path:
        return self.base_dir / safe_key(namespace) / safe_key(generation) / f"{safe_key(key)}.json"

    # ------------------------------------------------------------------ core

    def get(
        self,
        namespace: str,
        key: str,
        *,
        generation: date | str | None = None,
    ) -> dict[str, Any] | None:
        """Read one entry, checking the local tier then Supabase."""
        if not self.enabled:
            self._count(hit=False)
            return None
        month = as_of_month(generation)
        local = self._read_local(namespace, key, generation=month)
        if local is not None:
            self._count(hit=True)
            return local
        remote = self._read_supabase(namespace, key, generation=month)
        if remote is not None:
            # Warm the local tier so the next process-local read stays offline.
            self._write_local(namespace, key, remote, generation=month)
            self._count(hit=True)
            return remote
        self._count(hit=False)
        return None

    def set(
        self,
        namespace: str,
        key: str,
        payload: dict[str, Any],
        *,
        generation: date | str | None = None,
    ) -> None:
        """Write one entry to both tiers (Supabase failures are swallowed)."""
        if not self.enabled:
            return
        month = as_of_month(generation)
        body = dict(payload)
        body.setdefault("cached_at", datetime.now(UTC).isoformat())
        self._write_local(namespace, key, body, generation=month)
        self._write_supabase(namespace, key, body, generation=month)
        with self._lock:
            self.writes += 1

    def get_series(
        self,
        symbol: str,
        *,
        generation: date | str | None = None,
        namespace: str = SERIES_NAMESPACE,
    ) -> tuple[pd.Series, dict[str, Any]] | None:
        """Read a cached price/level series plus its stored metadata."""
        entry = self.get(namespace, symbol, generation=generation)
        if entry is None:
            return None
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            series = payload_to_series(payload, name=symbol)
        except PrismCacheError:
            return None
        meta = {key: value for key, value in entry.items() if key != "payload"}
        return series, meta

    def set_series(
        self,
        symbol: str,
        series: pd.Series,
        *,
        meta: dict[str, Any] | None = None,
        generation: date | str | None = None,
        namespace: str = SERIES_NAMESPACE,
    ) -> None:
        """Write a price/level series with its provenance metadata."""
        entry: dict[str, Any] = dict(meta or {})
        entry["payload"] = series_to_payload(series)
        entry["symbol"] = symbol
        self.set(namespace, symbol, entry, generation=generation)

    def status(self) -> str:
        """``"hit"`` when anything was served from cache, else ``"miss"``."""
        if not self.enabled:
            return "disabled"
        return "hit" if self.hits else "miss"

    def stats(self) -> dict[str, Any]:
        """Counters for ``packet["meta"]["cache"]``."""
        return {
            "enabled": self.enabled,
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "supabase": self.supabase is not None,
            "dir": str(self.base_dir),
            "errors": list(self.errors),
        }

    def clear(self, *, namespaces: Iterable[str] | None = None) -> int:
        """Delete local cache files; returns how many were removed."""
        removed = 0
        roots = (
            [self.base_dir / safe_key(name) for name in namespaces]
            if namespaces is not None
            else [self.base_dir]
        )
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.json"):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    # ------------------------------------------------------------------ tiers

    def _count(self, *, hit: bool) -> None:
        with self._lock:
            if hit:
                self.hits += 1
            else:
                self.misses += 1

    def _note_error(self, stage: str, error: Exception) -> None:
        entry = {"stage": stage, "error": str(error)[:200]}
        with self._lock:
            if entry not in self.errors:
                self.errors.append(entry)

    def _is_fresh(self, entry: dict[str, Any]) -> bool:
        stamp = entry.get("cached_at") or entry.get("fetched_at")
        if not stamp:
            return True
        try:
            cached_at = datetime.fromisoformat(str(stamp))
        except ValueError:
            return True
        if cached_at.tzinfo is None:
            cached_at = cached_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - cached_at <= self.ttl

    def _read_local(self, namespace: str, key: str, *, generation: str) -> dict[str, Any] | None:
        path = self._path(namespace, key, generation=generation)
        try:
            if not path.exists():
                return None
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._note_error("local_read", exc)
            return None
        if not isinstance(entry, dict) or not self._is_fresh(entry):
            return None
        return entry

    def _write_local(
        self, namespace: str, key: str, entry: dict[str, Any], *, generation: str
    ) -> None:
        path = self._path(namespace, key, generation=generation)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(entry, separators=(",", ":")), encoding="utf-8")
            temporary.replace(path)
        except (OSError, TypeError, ValueError) as exc:
            self._note_error("local_write", exc)

    def _read_supabase(
        self, namespace: str, key: str, *, generation: str
    ) -> dict[str, Any] | None:
        if self.supabase is None:
            return None
        try:
            row = self.supabase.fetch(self.cache_key(namespace, key, generation=generation))
        except (PrismCacheError, requests.RequestException, ValueError) as exc:
            self._note_error("supabase_read", exc)
            return None
        if not row:
            return None
        entry = row.get("entry")
        if not isinstance(entry, dict):
            return None
        entry.setdefault("cached_at", row.get("fetched_at"))
        return entry if self._is_fresh(entry) else None

    def _write_supabase(
        self, namespace: str, key: str, entry: dict[str, Any], *, generation: str
    ) -> None:
        if self.supabase is None:
            return
        row = {
            "cache_key": self.cache_key(namespace, key, generation=generation),
            "namespace": safe_key(namespace),
            "symbol": str(key)[:64],
            "as_of_month": generation,
            "provider": str(entry.get("provider") or "")[:32] or None,
            "entry": entry,
            "fetched_at": entry.get("cached_at") or datetime.now(UTC).isoformat(),
        }
        try:
            self.supabase.store(row)
        except (PrismCacheError, requests.RequestException, ValueError) as exc:
            self._note_error("supabase_write", exc)


def null_cache() -> PrismCache:
    """A disabled cache — every ``get`` misses and every ``set`` is a no-op."""
    return PrismCache(base_dir=cache_dir_from_env(), supabase=None, enabled=False)
