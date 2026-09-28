"""Conformance: the constellation's shared vectors, run against a fixture built with ``create_mcp``.

``app/mcp_lite.py`` is lattice-animal's ``lib/mcp_lite.py``, copied verbatim, and
``tests/fixtures/constellation-vectors.json`` is lattice-animal's file of the same name. Every
member of the constellation builds the fixture the vectors describe and must answer each case
the same way; this file is this repo's half of that (lattice-animal's is
``tests/test_mcp_lite.py``). Underlying's own tools are tested in ``test_constellation_mcp.py``.

A case's ``expect`` maps a dotted path into ``{status, headers, json, outbound}`` to an exact
value, to ``{"startsWith": s}``, or to ``{"absent": true}``. ``outbound`` is what the site sent
to its peer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app import mcp_lite
from app.mcp_lite import Mcp, create_mcp, relay_result

ROOT = Path(__file__).resolve().parent.parent
VECTORS_PATH = ROOT / "tests" / "fixtures" / "constellation-vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text())
_MISSING = object()

# Both files are vendored: compared byte for byte with lattice-animal's, so not reformatted here.
# When lattice-animal changes either one, copy it again and update the digest.
VENDORED_SHA256 = {
    "app/mcp_lite.py": "bde4462ad32e0b03087e979351e81d35a56986f2d99645fb28ad1744951e4e6c",
    "tests/fixtures/constellation-vectors.json": (
        "b3c1be73321a194c801e2e451f119fcc8294944d35e7a1912630bc0ddc1462fa"
    ),
}


def build(fixture: dict[str, Any], post: Any = None, **extra: Any) -> Mcp:
    tools: list[dict[str, Any]] = []
    for spec in fixture["tools"]:
        if spec["kind"] == "echo":
            tools.append(
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
                    "run": lambda args, _ctx: str(args.get("text") or ""),
                }
            )
        else:

            def run(args: dict[str, Any], ctx: Any, spec: dict[str, Any] = spec) -> Any:
                async def go() -> Any:
                    return relay_result(
                        await ctx.call(spec["peer"], spec["tool"], {"text": args.get("text")})
                    )

                return go()

            tools.append(
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "relay": True,
                    "run": run,
                }
            )
    return create_mcp(
        name=fixture["name"],
        title=fixture["title"],
        origin=fixture["origin"],
        tools=tools,
        peers=fixture["peers"],
        post=post,
        **extra,
    )


def get(obj: Any, path: str) -> Any:
    value = obj
    for key in path.split("."):
        if value is _MISSING or value is None:
            return _MISSING
        if isinstance(value, list):
            if key == "length":
                value = len(value)
            else:
                try:
                    value = value[int(key)]
                except (ValueError, IndexError):
                    return _MISSING
        elif isinstance(value, dict):
            value = value.get(key, _MISSING)
        else:
            return _MISSING
    return value


def test_the_library_and_the_vectors_are_the_vendored_bytes() -> None:
    for relative, digest in VENDORED_SHA256.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == digest, (
            f"{relative} is vendored from lattice-animal; copy it again instead of editing it"
        )


def test_the_vectors_are_present() -> None:
    assert VECTORS["cases"], "no conformance cases were loaded"
    assert VECTORS["fixture"]["name"] == "fixture"
    assert mcp_lite.MAX_HOP == 2


@pytest.mark.parametrize("case", VECTORS["cases"], ids=[case["name"] for case in VECTORS["cases"]])
def test_vector(case: dict[str, Any]) -> None:
    outbound: list[dict[str, Any]] = []

    async def post(url: str, *, body: str, headers: dict[str, str], timeout_ms: int) -> Any:
        del timeout_ms
        message = json.loads(body)
        outbound.append({"url": url, "headers": headers, "body": message})
        if "id" not in message:
            return {"ok": True, "status": 202, "headers": {}, "text": ""}
        result = VECTORS["fixture"]["peerReplies"].get(message["method"])
        reply = (
            {"jsonrpc": "2.0", "id": message["id"], "result": result}
            if result
            else {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "no"},
            }
        )
        return {
            "ok": True,
            "status": 200,
            "headers": {"content-type": "application/json"},
            "text": json.dumps(reply),
        }

    mcp = build(VECTORS["fixture"], post)
    request = case["request"]
    out = asyncio.run(
        mcp.handle(
            request["method"],
            request["path"],
            request.get("headers") or {},
            request.get("body"),
            "203.0.113.9",
        )
    )
    seen = {
        "status": out["status"],
        "headers": out.get("headers") or {},
        "json": out["json"],
        "outbound": outbound,
    }
    for path, want in case["expect"].items():
        got = get(seen, path)
        if isinstance(want, dict) and "absent" in want:
            assert got is _MISSING or got is None, f"{path} should be absent, got {got!r}"
        elif isinstance(want, dict) and "startsWith" in want:
            assert str(got).startswith(want["startsWith"]), (
                f"{path} = {got!r} should start with {want['startsWith']!r}"
            )
        else:
            assert got == want, f"{path}: {got!r} != {want!r}"
