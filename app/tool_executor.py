"""In-process execution of registry tools against the app's own HTTP routes.

Every tool in :mod:`app.tool_registry` is a declarative binding over one public
HTTP route. Rather than re-implement those routes for MCP and for the agent, the
executor dispatches through Flask's test client: same code path, same
validation, same error messages - with no network hop.

Two things happen on the way back out:

``artifacts``
    Base64 image payloads are lifted out of the JSON and replaced with short
    refs. The model reads cheap refs; the browser receives the real bytes and
    renders them inline.

``compaction``
    Long strings and long arrays are trimmed so a single tool result cannot
    swamp the model's context.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

from flask import current_app

from app.tool_registry import ToolArgumentError, ToolSpec, build_request, get_tool

MAX_RESULT_CHARS = 14000
MAX_STRING_CHARS = 2400
MAX_ARRAY_ITEMS = 40
IMAGE_KEYS = ("data", "b64_json", "image_base64")


@dataclass
class ToolArtifact:
    """An image (or other binary) lifted out of a tool result."""

    id: str
    mime: str
    data: str
    filename: str | None = None
    title: str | None = None
    caption: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "mime": self.mime,
            "data": self.data,
            "filename": self.filename,
            "title": self.title,
            "caption": self.caption,
        }


@dataclass
class ToolResult:
    name: str
    ok: bool
    status: int
    url: str
    result: Any
    artifacts: list[ToolArtifact] = field(default_factory=list)
    error: str | None = None
    duration_ms: int = 0

    def model_text(self) -> str:
        """The string handed back to the model as tool output."""
        if not self.ok:
            return json.dumps({"error": self.error or "Tool call failed"})
        payload: dict[str, Any] = {"result": self.result}
        if self.artifacts:
            payload["artifacts"] = [
                {
                    "ref": artifact.id,
                    "title": artifact.title or artifact.filename,
                    "mime": artifact.mime,
                }
                for artifact in self.artifacts
            ]
            payload["note"] = (
                "Images are already displayed to the user. Reference them by "
                "title in prose; do not attempt to reproduce them."
            )
        text = json.dumps(payload, default=str)
        if len(text) > MAX_RESULT_CHARS:
            # Truncating raw JSON would hand the model a broken document, so
            # wrap the readable prefix in a valid envelope instead.
            return json.dumps(
                {
                    "truncated": True,
                    "total_chars": len(text),
                    "preview": text[:MAX_RESULT_CHARS],
                }
            )
        return text

    def to_event(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "url": self.url,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "result": self.result,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
        }


def execute_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    keep_images: bool = True,
) -> ToolResult:
    """Run one registry tool and return a normalized result.

    ``keep_images`` controls whether extracted artifacts carry their base64
    payload. The browser wants them; the stdio MCP client usually does not.
    """
    import time

    started = time.monotonic()
    try:
        spec = get_tool(name)
        method, path, body, query = build_request(spec, arguments or {})
    except ToolArgumentError as exc:
        return ToolResult(
            name=name, ok=False, status=400, url="", result=None, error=str(exc)
        )

    url = path + (f"?{urlencode(query)}" if query else "")
    try:
        payload, status = _dispatch(method, url, body)
    except Exception as exc:  # pragma: no cover - defensive boundary
        current_app.logger.exception("Tool %s failed", name)
        return ToolResult(
            name=name,
            ok=False,
            status=500,
            url=url,
            result=None,
            error=f"{spec.title} failed: {exc}",
            duration_ms=_elapsed_ms(started),
        )

    artifacts: list[ToolArtifact] = []
    stripped = _extract_artifacts(payload, artifacts, spec)
    if not keep_images:
        for artifact in artifacts:
            artifact.data = ""

    ok = 200 <= status < 300
    error = None
    if not ok:
        error = _error_text(payload) or f"{spec.title} failed with status {status}"

    return ToolResult(
        name=name,
        ok=ok,
        status=status,
        url=url,
        result=_compact(stripped) if ok else None,
        artifacts=artifacts,
        error=error,
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    import time

    return int((time.monotonic() - started) * 1000)


def _dispatch(method: str, url: str, body: dict[str, Any] | None) -> tuple[Any, int]:
    client = current_app.test_client()
    response = client.open(
        url,
        method=method,
        json=body if method != "GET" else None,
        headers={"Accept": "application/json"},
    )
    try:
        payload = response.get_json(silent=True)
    finally:
        response.close()
    if payload is None:
        payload = {"raw": response.get_data(as_text=True)[:2000]}
    return payload, response.status_code


def _error_text(payload: Any) -> str | None:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
    return None


def _extract_artifacts(
    value: Any,
    artifacts: list[ToolArtifact],
    spec: ToolSpec,
    *,
    title_hint: str | None = None,
) -> Any:
    """Replace embedded base64 images with refs, collecting them as artifacts."""
    if isinstance(value, list):
        return [
            _extract_artifacts(item, artifacts, spec, title_hint=title_hint)
            for item in value
        ]
    if not isinstance(value, dict):
        return value

    image_key = next(
        (
            key
            for key in IMAGE_KEYS
            if isinstance(value.get(key), str) and len(str(value.get(key))) > 512
        ),
        None,
    )
    if image_key:
        raw_meta = value.get("meta")
        meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
        artifact = ToolArtifact(
            id=f"img_{len(artifacts) + 1}",
            mime=str(value.get("mime") or value.get("mime_type") or "image/png"),
            data=str(value[image_key]),
            filename=_optional_str(value.get("filename")),
            title=_optional_str(value.get("title"))
            or _optional_str(meta.get("title"))
            or title_hint
            or spec.title,
            caption=_optional_str(value.get("caption")) or _optional_str(meta.get("caption")),
        )
        artifacts.append(artifact)
        rest = {
            key: item
            for key, item in value.items()
            if key not in IMAGE_KEYS and key != "mime"
        }
        return {
            **_extract_artifacts(rest, artifacts, spec, title_hint=artifact.title),
            "image_ref": artifact.id,
        }

    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        hint = _optional_str(item) if key == "title" else title_hint
        cleaned[key] = _extract_artifacts(item, artifacts, spec, title_hint=hint)
    return cleaned


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _compact(value: Any, depth: int = 0) -> Any:
    """Trim oversized strings and arrays so one result cannot flood context."""
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            return value[:MAX_STRING_CHARS] + f"... [{len(value)} chars total]"
        return value
    if isinstance(value, list):
        trimmed = value[:MAX_ARRAY_ITEMS]
        compacted = [_compact(item, depth + 1) for item in trimmed]
        if len(value) > MAX_ARRAY_ITEMS:
            compacted.append(f"... {len(value) - MAX_ARRAY_ITEMS} more items omitted")
        return compacted
    if isinstance(value, dict):
        return {key: _compact(item, depth + 1) for key, item in value.items()}
    return value
