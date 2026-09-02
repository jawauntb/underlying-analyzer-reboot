"""Persistence for built packets and memo chat turns.

Two tiers, exactly like :mod:`app.prism.cache`: a local JSON directory that
always works, and an optional Supabase tier (``prism_packets`` / ``prism_chats``,
created by ``supabase/migrations/20260901120000_create_prism_tables.sql``) that
turns the local copy into a shared one when the service-role key is present.

Supabase failures never propagate: a packet that reached the local tier is
stored, and the failure lands in the returned record's ``errors`` so the caller
can report it honestly rather than pretend the write succeeded.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests

PACKETS_TABLE = "prism_packets"
CHATS_TABLE = "prism_chats"
PACKETS_DIRNAME = "packets"
CHATS_DIRNAME = "chats"
DEFAULT_CACHE_DIR = ".prism-cache"
DEFAULT_TIMEOUT = 20.0
MAX_CHAT_TURNS = 200

_UNSAFE = re.compile(r"[^A-Za-z0-9._:-]+")
#: Only files whose stem is a bare ISO date are real packets. Anything else in
#: the directory (a stem produced by a malformed ``as_of``, an editor backup) is
#: skipped when picking "the latest packet", so a junk filename cannot sort above
#: today's date and shadow it.
_ISO_DATE_STEM = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PrismStoreError(RuntimeError):
    """Raised only for programmer errors (bad ticker, bad id), never for I/O."""


def store_dir_from_env() -> Path:
    """Resolve ``PRISM_CACHE_DIR`` (default ``.prism-cache``) to a path."""
    return Path(os.getenv("PRISM_CACHE_DIR") or DEFAULT_CACHE_DIR)


def safe_name(value: str) -> str:
    """Make a ticker or conversation id safe as a path segment."""
    cleaned = _UNSAFE.sub("_", str(value or "").strip()).lstrip(".")
    if not cleaned:
        raise PrismStoreError(f"unusable store key: {value!r}")
    return cleaned[:120]


def _as_of(value: date | str | None) -> str:
    if value is None:
        return datetime.now(UTC).date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text[:10] if text else datetime.now(UTC).date().isoformat()


class SupabasePrismStore:
    """PostgREST wrapper over ``prism_packets`` and ``prism_chats``."""

    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        session: requests.Session | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        if not supabase_url:
            raise PrismStoreError("SUPABASE_URL is required for the Supabase store tier")
        if not service_role_key:
            raise PrismStoreError(
                "SUPABASE_SERVICE_ROLE_KEY is required for the Supabase store tier"
            )
        self.supabase_url = supabase_url.rstrip("/")
        self.service_role_key = service_role_key
        self.session = session or requests.Session()
        self.timeout = timeout

    @classmethod
    def from_env(cls, *, session: requests.Session | None = None) -> SupabasePrismStore | None:
        """Build from env, or ``None`` when Supabase is not configured."""
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

    def _url(self, table: str) -> str:
        return urljoin(f"{self.supabase_url}/", f"rest/v1/{table}")

    def upsert_packet(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """Upsert one packet row on ``(ticker, as_of, user_id)``."""
        response = self.session.post(
            self._url(PACKETS_TABLE),
            params={"on_conflict": "ticker,as_of,user_id"},
            json=[row],
            headers=self._headers(
                {"Prefer": "resolution=merge-duplicates,return=representation"}
            ),
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise PrismStoreError(
                f"Supabase {PACKETS_TABLE} POST failed: {response.status_code}"
            )
        rows = response.json() if response.text else []
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None

    def latest_packet(self, ticker: str, *, as_of: str | None = None) -> dict[str, Any] | None:
        """Read the newest stored packet row for a ticker."""
        params: dict[str, str] = {
            "select": "*",
            "ticker": f"eq.{ticker}",
            "order": "as_of.desc,created_at.desc",
            "limit": "1",
        }
        if as_of:
            params["as_of"] = f"eq.{as_of}"
        response = self.session.get(
            self._url(PACKETS_TABLE),
            params=params,
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise PrismStoreError(f"Supabase {PACKETS_TABLE} GET failed: {response.status_code}")
        rows = response.json() if response.text else []
        return rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None

    def insert_chat(self, row: dict[str, Any]) -> None:
        response = self.session.post(
            self._url(CHATS_TABLE),
            json=[row],
            headers=self._headers({"Prefer": "return=minimal"}),
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise PrismStoreError(f"Supabase {CHATS_TABLE} POST failed: {response.status_code}")

    def chat_history(self, conversation_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
        response = self.session.get(
            self._url(CHATS_TABLE),
            params={
                "select": "*",
                "conversation_id": f"eq.{conversation_id}",
                "order": "created_at.asc",
                "limit": str(int(limit)),
            },
            headers=self._headers(),
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise PrismStoreError(f"Supabase {CHATS_TABLE} GET failed: {response.status_code}")
        rows = response.json() if response.text else []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


class PrismStore:
    """Local JSON packet/chat store with an optional shared Supabase tier."""

    def __init__(
        self,
        *,
        base_dir: Path | str | None = None,
        supabase: SupabasePrismStore | None = None,
        enabled: bool = True,
    ) -> None:
        self.base_dir = Path(base_dir) if base_dir is not None else store_dir_from_env()
        self.supabase = supabase
        self.enabled = enabled
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls,
        *,
        session: requests.Session | None = None,
        base_dir: Path | str | None = None,
    ) -> PrismStore:
        """Build from ``PRISM_CACHE_DIR`` / ``PRISM_STORE_ENABLED`` / Supabase env."""
        enabled = str(os.getenv("PRISM_STORE_ENABLED", "1")).strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        supabase: SupabasePrismStore | None = None
        try:
            supabase = SupabasePrismStore.from_env(session=session)
        except PrismStoreError:
            supabase = None
        return cls(base_dir=base_dir, supabase=supabase, enabled=enabled)

    # ---------------------------------------------------------------- paths

    def packet_dir(self, ticker: str) -> Path:
        return self.base_dir / PACKETS_DIRNAME / safe_name(ticker).upper()

    def packet_path(self, ticker: str, as_of: date | str | None = None) -> Path:
        return self.packet_dir(ticker) / f"{safe_name(_as_of(as_of))}.json"

    def chat_path(self, conversation_id: str) -> Path:
        return self.base_dir / CHATS_DIRNAME / f"{safe_name(conversation_id)}.json"

    # --------------------------------------------------------------- packets

    def save_packet(self, packet: dict[str, Any]) -> dict[str, Any]:
        """Persist one packet; returns a record describing where it landed."""
        ticker = str(packet.get("ticker") or "").strip().upper()
        if not ticker:
            raise PrismStoreError("packet has no ticker")
        as_of = _as_of(packet.get("as_of"))
        record: dict[str, Any] = {
            "ticker": ticker,
            "as_of": as_of,
            "local_path": None,
            "supabase_id": None,
            "stored_at": datetime.now(UTC).isoformat(),
            "errors": [],
        }
        if not self.enabled:
            record["errors"].append("store is disabled (PRISM_STORE_ENABLED=0)")
            return record

        path = self.packet_path(ticker, as_of)
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(".json.tmp")
                temporary.write_text(
                    json.dumps(packet, ensure_ascii=False, default=str), encoding="utf-8"
                )
                temporary.replace(path)
                (path.parent / "latest.json").write_text(
                    json.dumps({"as_of": as_of, "path": path.name}, ensure_ascii=False),
                    encoding="utf-8",
                )
            record["local_path"] = str(path)
        except OSError as exc:
            record["errors"].append(f"local write failed: {exc}")

        if self.supabase is not None:
            memo = packet.get("memo") if isinstance(packet.get("memo"), dict) else {}
            recommendation = (memo or {}).get("recommendation") or {}
            meta = packet.get("meta") if isinstance(packet.get("meta"), dict) else {}
            try:
                row = self.supabase.upsert_packet(
                    {
                        "ticker": ticker,
                        "as_of": as_of,
                        "engine_version": str(packet.get("engine_version") or "1.0.0"),
                        "recommendation": recommendation.get("action"),
                        "conviction": recommendation.get("conviction"),
                        "memo_text": str((memo or {}).get("text") or ""),
                        "packet": packet,
                        "build_errors": (meta or {}).get("errors") or [],
                    }
                )
                if row:
                    record["supabase_id"] = row.get("id")
            except (PrismStoreError, requests.RequestException, ValueError) as exc:
                record["errors"].append(f"supabase write failed: {exc}")
        return record

    @staticmethod
    def _latest_pointer(directory: Path) -> Path | None:
        """The packet file ``latest.json`` points at, when it names a real one."""
        pointer = directory / "latest.json"
        if not pointer.is_file():
            return None
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        name = payload.get("path") if isinstance(payload, dict) else None
        if not isinstance(name, str) or not _ISO_DATE_STEM.match(Path(name).stem):
            return None
        target = directory / Path(name).name
        return target if target.is_file() else None

    def load_packet(
        self, ticker: str, as_of: date | str | None = None
    ) -> dict[str, Any] | None:
        """Read a stored packet, preferring the local copy then Supabase."""
        symbol = str(ticker or "").strip().upper()
        if not symbol:
            raise PrismStoreError("ticker is required")
        directory = self.packet_dir(symbol)
        candidates: list[Path] = []
        if as_of is not None:
            candidates.append(self.packet_path(symbol, as_of))
        elif directory.is_dir():
            # Only files whose stem is a bare ISO date are packets. Picking "the
            # newest" by raw reverse glob order let any other stem — e.g. one
            # produced by a malformed ``as_of`` — sort above today's date and
            # shadow the real packet.
            dated = sorted(
                (item for item in directory.glob("*.json") if _ISO_DATE_STEM.match(item.stem)),
                key=lambda item: item.stem,
                reverse=True,
            )
            # ``latest.json`` is a pointer written by the last successful save;
            # follow it first, then fall back to the newest genuinely dated file.
            pointed = self._latest_pointer(directory)
            candidates = ([pointed] if pointed is not None else []) + dated
        for path in candidates:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(payload, dict):
                return payload

        if self.supabase is not None:
            try:
                row = self.supabase.latest_packet(
                    symbol, as_of=_as_of(as_of) if as_of is not None else None
                )
            except (PrismStoreError, requests.RequestException, ValueError):
                return None
            if row and isinstance(row.get("packet"), dict):
                return dict(row["packet"])
        return None

    def list_packets(self, ticker: str) -> list[str]:
        """Stored as-of dates for a ticker, newest first (local tier only)."""
        directory = self.packet_dir(ticker)
        if not directory.is_dir():
            return []
        return sorted(
            (item.stem for item in directory.glob("*.json") if item.name != "latest.json"),
            reverse=True,
        )

    # ----------------------------------------------------------------- chats

    def append_chat(
        self,
        *,
        conversation_id: str,
        ticker: str,
        role: str,
        content: str,
        citations: list[dict[str, Any]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append one chat turn; returns the stored turn plus any write errors."""
        if role not in {"user", "assistant"}:
            raise PrismStoreError("chat role must be 'user' or 'assistant'")
        turn: dict[str, Any] = {
            "conversation_id": str(conversation_id),
            "ticker": str(ticker or "").strip().upper(),
            "role": role,
            "content": str(content or ""),
            "citations": citations or [],
            "metadata": metadata or {},
            "created_at": datetime.now(UTC).isoformat(),
        }
        errors: list[str] = []
        if not self.enabled:
            return {**turn, "errors": ["store is disabled (PRISM_STORE_ENABLED=0)"]}

        path = self.chat_path(conversation_id)
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                existing: list[dict[str, Any]] = []
                if path.is_file():
                    try:
                        loaded = json.loads(path.read_text(encoding="utf-8"))
                        if isinstance(loaded, list):
                            existing = [row for row in loaded if isinstance(row, dict)]
                    except ValueError:
                        existing = []
                existing.append(turn)
                path.write_text(
                    json.dumps(existing[-MAX_CHAT_TURNS:], ensure_ascii=False, default=str),
                    encoding="utf-8",
                )
        except OSError as exc:
            errors.append(f"local chat write failed: {exc}")

        if self.supabase is not None:
            try:
                self.supabase.insert_chat(
                    {
                        "conversation_id": turn["conversation_id"],
                        "ticker": turn["ticker"],
                        "role": turn["role"],
                        "content": turn["content"],
                        "citations": turn["citations"],
                        "metadata": turn["metadata"],
                    }
                )
            except (PrismStoreError, requests.RequestException, ValueError) as exc:
                errors.append(f"supabase chat write failed: {exc}")
        return {**turn, "errors": errors}

    def chat_history(self, conversation_id: str, *, limit: int = 40) -> list[dict[str, Any]]:
        """Read a conversation's turns, oldest first."""
        path = self.chat_path(conversation_id)
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                loaded = []
            if isinstance(loaded, list):
                rows = [row for row in loaded if isinstance(row, dict)]
                if rows:
                    return rows[-int(limit) :]
        if self.supabase is not None:
            try:
                return self.supabase.chat_history(conversation_id, limit=limit)
            except (PrismStoreError, requests.RequestException, ValueError):
                return []
        return []


_DEFAULT_STORE: PrismStore | None = None
_DEFAULT_LOCK = threading.Lock()


def default_store() -> PrismStore:
    """A process-wide store built from the environment on first use."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        if _DEFAULT_STORE is None:
            _DEFAULT_STORE = PrismStore.from_env()
        return _DEFAULT_STORE


def reset_default_store() -> None:
    """Drop the memoised store (tests change ``PRISM_CACHE_DIR`` between cases)."""
    global _DEFAULT_STORE
    with _DEFAULT_LOCK:
        _DEFAULT_STORE = None


def save_packet(packet: dict[str, Any], *, store: PrismStore | None = None) -> dict[str, Any]:
    """Module-level convenience over :meth:`PrismStore.save_packet`."""
    return (store or default_store()).save_packet(packet)


def load_packet(
    ticker: str,
    as_of: date | str | None = None,
    *,
    store: PrismStore | None = None,
) -> dict[str, Any] | None:
    """Module-level convenience over :meth:`PrismStore.load_packet`."""
    return (store or default_store()).load_packet(ticker, as_of)
