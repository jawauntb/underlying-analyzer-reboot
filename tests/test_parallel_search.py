from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from flask import Flask

from app.main import create_app
from app.parallel_search import MCP_URL, USER_AGENT, ParallelSearchClient, ParallelSearchError

ARTICLE_URL = "https://example.com/news/earnings?source=research"


def article(**overrides: Any) -> dict[str, Any]:
    return {
        "url": ARTICLE_URL,
        "title": "Earnings outlook",
        "publish_date": datetime.now(UTC).date().isoformat(),
        "excerpts": ["Revenue guidance increased."],
        **overrides,
    }


class MCPFixture:
    """Exercise the real MCP SDK over an observed, deterministic HTTP transport."""

    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.result = result if result is not None else {"results": [article()]}
        self.requests: list[httpx.Request] = []
        self.messages: list[dict[str, Any]] = []
        self.error = False
        self.text_only = False

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        message = json.loads(request.content)
        self.messages.append(message)
        method = message["method"]
        if method.startswith("notifications/"):
            return httpx.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "parallel-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": "web_search", "inputSchema": {"type": "object"}}]}
        else:
            assert method == "tools/call"
            assert message["params"]["name"] == "web_search"
            result = {
                "isError": self.error,
                "content": [{"type": "text", "text": json.dumps(self.result)}],
            }
            if not self.text_only:
                result["structuredContent"] = self.result
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    def client(self, **kwargs: Any) -> ParallelSearchClient:
        return ParallelSearchClient(transport=httpx.MockTransport(self.handle), **kwargs)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Flask:
    # Isolate local .env files and ambient provider credentials as well as keys.
    monkeypatch.setattr("app.main.load_env_file", lambda: None)
    for key in ("EXA_API_KEY", "NEWS_SEARCH_PROVIDER", "ANTHROPIC_API_KEY", "PARALLEL_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return create_app()


class ScriptedAgent:
    """Script model decisions, but read the real tool result on the second turn."""

    model = "scripted-test-model"

    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def stream_messages(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.turns.append(kwargs)
        if len(self.turns) == 1:
            yield {
                "type": "tool_use",
                "id": "news-1",
                "name": "search_news",
                "input": {"ticker": "NVDA", "query": "earnings", "num_results": 1},
            }
            yield {"type": "stop", "stop_reason": "tool_use"}
        else:
            yield {"type": "text", "text": "Research complete."}
            yield {"type": "stop", "stop_reason": "end_turn"}


def chat(app: Flask, tools: list[str] | None = None) -> dict[str, Any]:
    app.config["AGENT_CLIENT"] = ScriptedAgent()
    response = app.test_client().post(
        "/api/agent/chat/stream",
        json={
            "messages": [{"role": "user", "content": "What changed in NVDA earnings?"}],
            "tools": tools or ["search_news"],
            "tool_policy": "exact",
        },
    )
    assert response.status_code == 200
    events = [
        json.loads(line) for line in response.get_data(as_text=True).splitlines() if line.strip()
    ]
    return {"tool_calls": [event for event in events if event["type"] == "tool_result"]}


def test_fresh_agent_uses_anonymous_mcp_and_consumes_citations(app: Flask) -> None:
    fixture = MCPFixture()
    app.config["PARALLEL_SEARCH_CLIENT"] = fixture.client()
    response = chat(app)
    tool = response["tool_calls"][0]
    assert tool["result"]["provider"] == "Parallel"
    assert tool["result"]["results"][0] == {
        "title": "Earnings outlook",
        "url": ARTICLE_URL,
        "published_date": datetime.now(UTC).date().isoformat(),
        "snippet": "Revenue guidance increased.",
        "author": None,
    }
    second_turn = app.config["AGENT_CLIENT"].turns[1]
    block = second_turn["messages"][-1]["content"][0]
    consumed = json.loads(block["content"])["result"]["results"][0]
    assert consumed["url"] == ARTICLE_URL
    assert consumed["snippet"] == "Revenue guidance increased."
    assert {m["method"] for m in fixture.messages} >= {"initialize", "tools/call"}
    call = next(m for m in fixture.messages if m["method"] == "tools/call")
    args = call["params"]["arguments"]
    assert "NVDA earnings" in args["objective"]
    assert "after:" in args["search_queries"][0]
    for request in fixture.requests:
        assert str(request.url) == MCP_URL
        assert request.headers["user-agent"] == USER_AGENT
        assert "authorization" not in request.headers


@pytest.mark.parametrize("text_only", [True, False])
def test_dates_limits_and_safe_citation_urls(app: Flask, text_only: bool) -> None:
    fixture = MCPFixture(
        {
            "results": [
                article(publish_date="2000-01-01"),
                article(publish_date=None),
                article(publish_date="invalid"),
                article(publish_date="2999-01-01"),
                article(url="javascript:alert(1)"),
                article(url="https://user:secret@example.com/private"),
                article(),
                article(url="https://example.org/second"),
            ]
        }
    )
    fixture.text_only = text_only
    app.config["PARALLEL_SEARCH_CLIENT"] = fixture.client()
    result = (
        app.test_client()
        .post("/api/news", json={"query": "earnings", "days_back": 1, "num_results": 1})
        .get_json()
    )
    assert result["ok"] is True
    assert [r["url"] for r in result["results"]] == [ARTICLE_URL]


class ExaFixture:
    def __init__(self, key: str | None) -> None:
        self.api_key = key
        self.calls: list[dict[str, Any]] = []
        self.error = False

    def search(self, query: str, **kwargs: Any) -> list[Any]:
        self.calls.append({"query": query, **kwargs})
        if self.error:
            raise RuntimeError("Exa unavailable")
        return []


@pytest.mark.parametrize(
    ("selection", "key", "expected"),
    [
        ("auto", "configured", "Exa"),
        ("exa", "configured", "Exa"),
        ("parallel", "configured", "Parallel"),
        ("disabled", "configured", "disabled"),
        ("disabled", None, "disabled"),
        ("exa", None, "Exa"),
    ],
)
def test_provider_choices(app: Flask, selection: str, key: str | None, expected: str) -> None:
    exa = ExaFixture(key)
    fixture = MCPFixture()
    app.config.update(
        NEWS_SEARCH_PROVIDER=selection, EXA_CLIENT=exa, PARALLEL_SEARCH_CLIENT=fixture.client()
    )
    result = app.test_client().post("/api/news", json={"query": "earnings"}).get_json()
    assert result["provider"] == expected
    assert bool(fixture.requests) == (expected == "Parallel")
    assert bool(exa.calls) == (expected == "Exa" and bool(key))
    if exa.calls:
        assert exa.calls[0]["category"] == "news"
        assert (
            exa.calls[0]["start_published_date"]
            == (datetime.now(UTC) - timedelta(days=14)).date().isoformat()
        )
    if selection == "disabled" or (selection == "exa" and not key):
        assert result["ok"] is False


def test_explicit_exa_error_never_falls_back(app: Flask) -> None:
    exa = ExaFixture("configured")
    exa.error = True
    fixture = MCPFixture()
    app.config.update(EXA_CLIENT=exa, PARALLEL_SEARCH_CLIENT=fixture.client())
    result = app.test_client().post("/api/news", json={"query": "earnings"}).get_json()
    assert result["ok"] is False
    assert result["provider"] == "Exa"
    assert not fixture.requests


def test_tool_allowlist_prevents_outbound_search(app: Flask) -> None:
    fixture = MCPFixture()
    app.config["PARALLEL_SEARCH_CLIENT"] = fixture.client()
    chat(app, tools=["health_check"])
    assert not fixture.requests
    block = app.config["AGENT_CLIENT"].turns[1]["messages"][-1]["content"][0]
    assert block["is_error"] is True


@pytest.mark.parametrize("failure", ["tool", "http", "quota", "malformed", "network", "timeout"])
def test_errors_reach_agent_without_provider_switch(app: Flask, failure: str) -> None:
    fixture = MCPFixture()
    fixture.error = failure == "tool"
    if failure == "malformed":
        fixture.result = {"unexpected": []}

    async def handler(request: httpx.Request) -> httpx.Response:
        if failure == "http":
            return httpx.Response(503)
        if failure == "quota":
            return httpx.Response(429, headers={"Retry-After": "60"})
        if failure == "network":
            raise httpx.ConnectError("offline", request=request)
        if failure == "timeout":
            await asyncio.Event().wait()
        return await fixture.handle(request)

    exa = ExaFixture("configured")
    app.config.update(EXA_CLIENT=exa, NEWS_SEARCH_PROVIDER="parallel")
    app.config["PARALLEL_SEARCH_CLIENT"] = ParallelSearchClient(
        transport=httpx.MockTransport(handler), timeout_seconds=0.1
    )
    result = chat(app)["tool_calls"][0]["result"]
    assert result["ok"] is False
    assert result["provider"] == "Parallel"
    assert result["results"] == []
    assert not exa.calls
    if failure == "timeout":
        assert "timed out" in result["error"]
    block = app.config["AGENT_CLIENT"].turns[1]["messages"][-1]["content"][0]
    assert json.loads(block["content"])["result"]["ok"] is False


def test_timeout_and_cancellation_close_http_transport() -> None:
    closed: list[bool] = []

    class BlockingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def aclose(self) -> None:
            closed.append(True)

    client = ParallelSearchClient(timeout_seconds=0.05, transport=BlockingTransport())
    with pytest.raises(ParallelSearchError, match="timed out"):
        client.search("earnings", num_results=1, start_published_date="2026-01-01")
    assert closed
    closed.clear()

    async def cancel() -> None:
        task = asyncio.create_task(
            ParallelSearchClient(transport=BlockingTransport())._search("earnings", "2026-01-01")
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert closed


def test_invalid_selection_and_empty_query_make_no_request(app: Flask) -> None:
    fixture = MCPFixture()
    app.config.update(NEWS_SEARCH_PROVIDER="typo", PARALLEL_SEARCH_CLIENT=fixture.client())
    client = app.test_client()
    assert client.post("/api/news", json={"query": "earnings"}).status_code == 400
    assert client.post("/api/news", json={}).status_code == 400
    assert not fixture.requests


def test_deployment_selection_loaded_at_startup(
    app: Flask, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert app.config["NEWS_SEARCH_PROVIDER"] == "auto"
    monkeypatch.setenv("NEWS_SEARCH_PROVIDER", "disabled")
    monkeypatch.setenv("EXA_API_KEY", "saved-key")
    configured_app = create_app()
    assert configured_app.config["NEWS_SEARCH_PROVIDER"] == "disabled"
    assert configured_app.config["EXA_CLIENT"].api_key == "saved-key"
    result = configured_app.test_client().post("/api/news", json={"query": "earnings"}).get_json()
    assert result["provider"] == "disabled"
    assert result["ok"] is False
