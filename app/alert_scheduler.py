from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urljoin

import requests

DEFAULT_SCHEDULED_RULE_LIMIT = 25
MAX_SCHEDULED_RULE_LIMIT = 100


class AlertStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScheduledAlertRule:
    id: str
    user_id: str
    name: str
    source_url: str | None
    tickers: list[str]
    period: str
    max_results: int
    max_alerts: int
    volatility_threshold: float
    metadata: dict[str, Any]
    last_run_date: date | None


class SupabaseAlertStore:
    def __init__(
        self,
        *,
        supabase_url: str,
        service_role_key: str,
        session: requests.Session | None = None,
    ) -> None:
        if not supabase_url:
            raise AlertStoreError("SUPABASE_URL is required for scheduled alerts")
        if not service_role_key:
            raise AlertStoreError("SUPABASE_SERVICE_ROLE_KEY is required for scheduled alerts")
        self.supabase_url = supabase_url.rstrip("/")
        self.service_role_key = service_role_key
        self.session = session or requests.Session()

    def list_due_rules(self, *, run_date: date, force: bool = False) -> list[ScheduledAlertRule]:
        rows = self._request(
            "GET",
            "alert_rules",
            params={
                "select": "*",
                "active": "eq.true",
                "schedule": "eq.daily",
                "order": "updated_at.asc",
            },
        )
        rules = [scheduled_rule_from_row(row) for row in rows]
        if force:
            return rules
        return [
            rule
            for rule in rules
            if rule.last_run_date is None or rule.last_run_date < run_date
        ]

    def insert_run(
        self,
        *,
        rule: ScheduledAlertRule,
        run_date: date,
        trigger: str,
        status: str,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        payload_data = payload or {}
        meta_value = payload_data.get("meta")
        meta = meta_value if isinstance(meta_value, dict) else {}
        self._request(
            "POST",
            "alert_runs",
            json=[
                {
                    "alert_rule_id": rule.id,
                    "user_id": rule.user_id,
                    "trigger": trigger,
                    "status": status,
                    "run_date": run_date.isoformat(),
                    "alert_count": int(meta.get("alert_count") or 0),
                    "high_alert_count": int(meta.get("high_alert_count") or 0),
                    "digest": payload_data.get("digest") or {},
                    "alerts": payload_data.get("alerts") or [],
                    "rows": payload_data.get("rows") or [],
                    "payload": payload_data,
                    "error": error,
                }
            ],
            headers={"Prefer": "return=minimal"},
        )

    def mark_rule_ran(self, *, rule_id: str, run_date: date) -> None:
        self._request(
            "PATCH",
            "alert_rules",
            params={"id": f"eq.{rule_id}"},
            json={
                "last_run_at": datetime.now(UTC).isoformat(),
                "last_run_date": run_date.isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            },
            headers={"Prefer": "return=minimal"},
        )

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, str] | None = None,
        json: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = {
            "apikey": self.service_role_key,
            "Authorization": f"Bearer {self.service_role_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        response = self.session.request(
            method,
            urljoin(f"{self.supabase_url}/", f"rest/v1/{table}"),
            params=params,
            json=json,
            headers=request_headers,
            timeout=30,
        )
        if response.status_code >= 400:
            raise AlertStoreError(f"Supabase {table} {method} failed: {response.text}")
        if not response.text:
            return None
        return response.json()


def scheduled_rule_from_row(row: dict[str, Any]) -> ScheduledAlertRule:
    metadata_value = row.get("metadata")
    metadata = (
        {str(key): value for key, value in metadata_value.items()}
        if isinstance(metadata_value, dict)
        else {}
    )
    return ScheduledAlertRule(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        name=str(row.get("name") or "Alert rule"),
        source_url=string_or_none(row.get("source_url")),
        tickers=normalize_tickers(row.get("tickers")),
        period=str(row.get("period") or "1y"),
        max_results=bounded_int(row.get("max_results"), default=10, minimum=1, maximum=50),
        max_alerts=bounded_int(row.get("max_alerts"), default=12, minimum=1, maximum=50),
        volatility_threshold=bounded_float(
            row.get("volatility_threshold"), default=0.55, minimum=0.0, maximum=2.0
        ),
        metadata=metadata,
        last_run_date=parse_date(row.get("last_run_date")),
    )


def alert_payload_from_rule(rule: ScheduledAlertRule) -> dict[str, Any]:
    return {
        "ticker": rule.tickers[0] if rule.tickers else "AAPL",
        "tickers": rule.tickers,
        "watchlist_url": rule.source_url,
        "max_results": rule.max_results,
        "period": rule.period,
        "max_alerts": rule.max_alerts,
        "volatility_threshold": rule.volatility_threshold,
    }


def bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def normalize_tickers(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(ticker).strip().upper() for ticker in value if str(ticker).strip()]
    if isinstance(value, str):
        return [part.strip().upper() for part in value.split(",") if part.strip()]
    return []


def string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
