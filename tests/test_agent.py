from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.agent import AgentError, normalize_history, select_tools
from app.articles import ArticleError, article_markdown, normalize_article
from app.main import create_app


class StubAgentClient:
    """Replays scripted Anthropic stream events, one script per turn."""

    model = "stub-agent"

    def __init__(self, scripts: list[list[dict[str, Any]]]) -> None:
        self.scripts = scripts
        self.turns: list[dict[str, Any]] = []

    def stream_messages(self, **kwargs: Any) -> Iterator[dict[str, Any]]:
        self.turns.append(kwargs)
        script = self.scripts[min(len(self.turns) - 1, len(self.scripts) - 1)]
        yield from script


def build_client(
    scripts: list[list[dict[str, Any]]],
) -> tuple[Flask, FlaskClient]:
    app = create_app()
    app.config["AGENT_CLIENT"] = StubAgentClient(scripts)
    return app, app.test_client()


def stream_events(client: FlaskClient, body: dict[str, Any]) -> list[dict[str, Any]]:
    response = client.post("/api/agent/chat/stream", json=body)
    assert response.status_code == 200
    return [
        json.loads(line)
        for line in response.get_data(as_text=True).splitlines()
        if line.strip()
    ]


ARTICLE_ARGS = {
    "title": "NVDA into the print",
    "thesis": "Trend intact, positioning crowded.",
    "sections": [{"heading": "Setup", "body": "Holding above value."}],
    "recommendations": [
        {
            "ticker": "NVDA",
            "stance": "constructive",
            "action": "Watch for acceptance above VAH",
            "invalidation": "Loses VAL on a closing basis",
        }
    ],
}


def test_agent_streams_text_only_turn() -> None:
    _, client = build_client(
        [
            [
                {"type": "text", "text": "Hello."},
                {"type": "stop", "stop_reason": "end_turn"},
            ]
        ]
    )

    events = stream_events(client, {"messages": [{"role": "user", "content": "hi"}]})
    kinds = [event["type"] for event in events]

    assert kinds[0] == "start"
    assert kinds[-1] == "done"
    assert events[-1]["text"] == "Hello."
    assert events[0]["tools"]


def test_agent_runs_a_tool_then_answers() -> None:
    app, client = build_client(
        [
            [
                {"type": "text", "text": "Checking. "},
                {"type": "tool_use", "id": "t1", "name": "list_capabilities", "input": {}},
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text", "text": "There are chart and research tools."},
                {"type": "stop", "stop_reason": "end_turn"},
            ],
        ]
    )

    events = stream_events(
        client, {"messages": [{"role": "user", "content": "what can you do?"}]}
    )
    by_type = {event["type"]: event for event in events}

    assert by_type["tool_call"]["name"] == "list_capabilities"
    assert by_type["tool_result"]["ok"] is True
    assert by_type["tool_result"]["result"]["tool_count"] > 0
    assert by_type["done"]["tool_trace"] == ["list_capabilities() -> ok"]

    # The second turn must see the tool result as a real tool_result block.
    second_turn = app.config["AGENT_CLIENT"].turns[1]
    roles = [message["role"] for message in second_turn["messages"]]
    assert roles == ["user", "assistant", "user"]
    assert second_turn["messages"][2]["content"][0]["type"] == "tool_result"


def test_agent_emits_article_event_for_composed_brief() -> None:
    _, client = build_client(
        [
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "compose_research_article",
                    "input": ARTICLE_ARGS,
                },
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text", "text": "Brief is above."},
                {"type": "stop", "stop_reason": "end_turn"},
            ],
        ]
    )

    events = stream_events(
        client, {"messages": [{"role": "user", "content": "write it up"}]}
    )
    article = next(event for event in events if event["type"] == "article")

    assert article["article"]["title"] == "NVDA into the print"
    assert article["article"]["recommendations"][0]["stance"] == "constructive"
    assert "## Recommendations" in article["markdown"]


def test_failed_tool_is_reported_without_killing_the_turn() -> None:
    _, client = build_client(
        [
            [
                {"type": "tool_use", "id": "t1", "name": "stock_fax", "input": {}},
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text", "text": "I could not read that."},
                {"type": "stop", "stop_reason": "end_turn"},
            ],
        ]
    )

    events = stream_events(client, {"messages": [{"role": "user", "content": "fax"}]})
    result = next(event for event in events if event["type"] == "tool_result")

    assert result["ok"] is False
    assert "missing required" in result["error"]
    assert events[-1]["type"] == "done"


def test_non_streaming_endpoint_folds_the_turn() -> None:
    _, client = build_client(
        [
            [
                {"type": "tool_use", "id": "t1", "name": "health_check", "input": {}},
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text", "text": "Service is up."},
                {"type": "stop", "stop_reason": "end_turn"},
            ],
        ]
    )

    payload = client.post(
        "/api/agent/chat", json={"messages": [{"role": "user", "content": "status?"}]}
    ).get_json()

    assert payload["ok"] is True
    assert payload["text"] == "Service is up."
    assert payload["tool_calls"][0]["name"] == "health_check"


def test_agent_offline_without_api_key() -> None:
    app = create_app()
    app.config["ANTHROPIC_API_KEY"] = None
    app.config["AGENT_CLIENT"] = None
    response = app.test_client().post(
        "/api/agent/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in response.get_json()["error"]


def test_tool_catalog_reports_readiness() -> None:
    app = create_app()
    app.config["ANTHROPIC_API_KEY"] = "sk-test"
    payload = app.test_client().get("/api/agent/tools").get_json()
    assert payload["agent_ready"] is True
    assert payload["tool_count"] > 0


def test_history_normalization_merges_and_validates() -> None:
    history = normalize_history(
        [
            {"role": "system", "content": "ignored"},
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
            {"role": "assistant", "content": "ok", "tool_trace": ["render_chart -> ok"]},
            {"role": "user", "content": "three"},
        ]
    )

    assert [message["role"] for message in history] == ["user", "assistant", "user"]
    assert history[0]["content"] == "one\n\ntwo"
    assert "render_chart -> ok" in history[1]["content"]


def test_history_requires_a_trailing_user_message() -> None:
    with pytest.raises(AgentError):
        normalize_history([{"role": "assistant", "content": "hi"}])
    with pytest.raises(AgentError):
        normalize_history([])


def test_select_tools_falls_back_to_everything() -> None:
    assert len(select_tools(["render_chart"])) == 1
    assert len(select_tools(["not-a-tool"])) > 1
    assert len(select_tools(None)) > 1


def test_article_normalization_cleans_input() -> None:
    article = normalize_article(
        {
            "title": "  Spaced   title  ",
            "thesis": "A thesis.",
            "tickers": "nvda, amd, nvda",
            "sections": [
                {"heading": "One", "body": "Body."},
                {"heading": "", "body": "dropped"},
            ],
            "recommendations": [
                {"stance": "nonsense", "action": "Do a thing"},
                {"stance": "avoid", "action": ""},
            ],
            "sources": [{"label": "SEC 10-Q", "url": "ftp://bad", "kind": "filing"}],
        }
    )

    assert article["title"] == "Spaced title"
    assert article["tickers"] == ["NVDA", "AMD"]
    assert len(article["sections"]) == 1
    assert len(article["recommendations"]) == 1
    assert article["recommendations"][0]["stance"] == "watch"
    assert article["sources"][0]["url"] is None
    assert article["disclaimer"]


def test_article_requires_title_thesis_and_sections() -> None:
    for payload in (
        {"thesis": "t", "sections": [{"heading": "h", "body": "b"}]},
        {"title": "t", "sections": [{"heading": "h", "body": "b"}]},
        {"title": "t", "thesis": "t"},
    ):
        with pytest.raises(ArticleError):
            normalize_article(payload)


def test_article_markdown_renders_tables_and_sources() -> None:
    markdown = article_markdown(normalize_article(ARTICLE_ARGS))
    assert markdown.startswith("# NVDA into the print")
    assert "| Ticker | Stance | Action | Confidence |" in markdown
    assert "Invalidated if: Loses VAL on a closing basis" in markdown


def test_article_route_returns_markdown_and_summary() -> None:
    app = create_app()
    payload = app.test_client().post("/api/agent/article", json=ARTICLE_ARGS).get_json()
    assert payload["article"]["title"] == "NVDA into the print"
    assert payload["summary"] == "Trend intact, positioning crowded."
    assert payload["markdown"]


def test_article_route_rejects_empty_payload() -> None:
    app = create_app()
    response = app.test_client().post("/api/agent/article", json={})
    assert response.status_code == 400
    assert "title" in response.get_json()["error"]
