from __future__ import annotations

from mcp_server.server import CHART_TYPES, _strip_heavy_fields, render_chart


def test_strip_heavy_fields_omits_base64() -> None:
    payload = {
        "images": [{"filename": "a.png", "mime": "image/png", "data": "x" * 500}],
        "meta": {"state": "LONG OK"},
        "nested": {"image": "y" * 300},
    }

    cleaned = _strip_heavy_fields(payload)

    assert cleaned["meta"]["state"] == "LONG OK"
    assert "omitted base64" in cleaned["images"][0]["data"]
    assert "omitted base64" in cleaned["nested"]["image"]


def test_render_chart_rejects_unknown_type() -> None:
    result = render_chart(chart_type="not-a-chart", ticker="AAPL")

    assert result["ok"] is False
    assert "Unsupported chart_type" in result["error"]
    assert "auction" in result["error"]
    assert "ridge-growth" in CHART_TYPES
