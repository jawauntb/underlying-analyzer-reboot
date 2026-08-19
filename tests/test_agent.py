from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from flask import Flask
from flask.testing import FlaskClient

from app.agent import AgentError, build_system_prompt, normalize_history, select_tools
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
    app, client = build_client(
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
        "/api/agent/chat",
        json={
            "messages": [{"role": "user", "content": "status?"}],
            "tools": ["health_check"],
            "tool_policy": "exact",
        },
    ).get_json()

    assert payload["ok"] is True
    assert payload["model"] == "stub-agent"
    assert payload["tools"] == ["health_check"]
    assert payload["text"] == "Service is up."
    assert payload["tool_calls"][0]["name"] == "health_check"
    system = app.config["AGENT_CLIENT"].turns[0]["system"]
    assert "`health_check`" in system
    assert "`render_chart`" not in system


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
    assert len(select_tools(None)) > 1
    assert select_tools([]) == select_tools(None)
    assert select_tools("health_check") == select_tools(None)
    assert select_tools(["not-a-tool"]) == select_tools(None)
    assert [spec.name for spec in select_tools(["health_check", "not-a-tool"])] == [
        "health_check"
    ]


def test_select_tools_rejects_a_nonempty_unknown_allowlist() -> None:
    with pytest.raises(AgentError, match="recognized tool"):
        select_tools(["not-a-tool"], exact=True)


@pytest.mark.parametrize(
    "names",
    [
        [],
        "health_check",
        ["health_check", "not-a-tool"],
        ["health_check", ""],
    ],
)
def test_select_tools_rejects_any_invalid_explicit_allowlist(names: Any) -> None:
    with pytest.raises(AgentError, match="tools must be a non-empty list of recognized tool names"):
        select_tools(names, exact=True)


def test_exact_tool_policy_requires_a_supplied_allowlist() -> None:
    with pytest.raises(AgentError, match="recognized tool"):
        select_tools(None, exact=True)


def test_system_prompt_names_only_selected_tools() -> None:
    specs = select_tools(["health_check", "provider_status"], exact=True)

    prompt = build_system_prompt(tool_specs=specs)

    assert "`health_check`" in prompt
    assert "`provider_status`" in prompt
    assert "render_chart" not in prompt
    assert "vision_memo" not in prompt
    assert "compose_research_article" not in prompt
    assert "Rendered charts" not in prompt


def test_system_prompt_preserves_selected_chart_and_article_guidance() -> None:
    specs = select_tools(
        ["render_chart", "compose_research_article"], exact=True
    )

    prompt = build_system_prompt(tool_specs=specs)

    assert "Rendered charts are already shown" in prompt
    assert "finish by calling\n`compose_research_article`" in prompt


@pytest.mark.parametrize(
    ("tools", "expected"),
    [
        ("health_check", None),
        ([], None),
        (["not-a-tool"], None),
        (["health_check", "not-a-tool"], ["health_check"]),
    ],
)
def test_legacy_endpoint_preserves_tool_fallbacks(
    tools: Any, expected: list[str] | None
) -> None:
    _, client = build_client(
        [[{"type": "text", "text": "Ready."}, {"type": "stop", "stop_reason": "end_turn"}]]
    )

    events = stream_events(
        client,
        {"messages": [{"role": "user", "content": "status?"}], "tools": tools},
    )

    if expected is None:
        assert len(events[0]["tools"]) > 1
    else:
        assert events[0]["tools"] == expected


@pytest.mark.parametrize("tools", [None, [], ["not-a-tool"], ["health_check", "bad"]])
def test_exact_endpoint_rejects_invalid_allowlists(tools: Any) -> None:
    _, client = build_client(
        [[{"type": "text", "text": "unused"}, {"type": "stop", "stop_reason": "end_turn"}]]
    )

    response = client.post(
        "/api/agent/chat",
        json={
            "messages": [{"role": "user", "content": "status?"}],
            "tools": tools,
            "tool_policy": "exact",
        },
    )

    assert response.status_code == 400
    assert "recognized tool names" in response.get_json()["error"]


@pytest.mark.parametrize("endpoint", ["/api/agent/chat", "/api/agent/chat/stream"])
@pytest.mark.parametrize("tool_policy", [None, "best_effort", "", False, ["exact"]])
def test_endpoint_rejects_unknown_tool_policy(
    endpoint: str, tool_policy: Any
) -> None:
    _, client = build_client(
        [[{"type": "text", "text": "unused"}, {"type": "stop", "stop_reason": "end_turn"}]]
    )

    response = client.post(
        endpoint,
        json={
            "messages": [{"role": "user", "content": "status?"}],
            "tools": ["health_check"],
            "tool_policy": tool_policy,
        },
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "tool_policy must be 'exact' when provided"


def test_legacy_stream_reports_a_refused_unselected_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, client = build_client(
        [
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "list_capabilities",
                    "input": {},
                },
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text", "text": "That tool is unavailable for this turn."},
                {"type": "stop", "stop_reason": "end_turn"},
            ],
        ]
    )

    def fail_if_executed(name: str, arguments: dict[str, Any]) -> Any:
        raise AssertionError(f"unexpected tool execution: {name} {arguments}")

    monkeypatch.setattr("app.agent.execute_tool", fail_if_executed)
    events = stream_events(
        client,
        {
            "messages": [{"role": "user", "content": "what can you do?"}],
            "tools": ["health_check"],
        },
    )

    refused = [
        event
        for event in events
        if event["type"] in {"tool_call", "tool_result"}
    ]
    assert [event["type"] for event in refused] == ["tool_call", "tool_result"]
    assert all(event["name"] == "list_capabilities" for event in refused)
    assert refused[1]["status"] == 403


def test_agent_does_not_execute_a_tool_outside_the_selected_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = build_client(
        [
            [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "list_capabilities",
                    "input": {},
                },
                {"type": "stop", "stop_reason": "tool_use"},
            ],
            [
                {"type": "text", "text": "That tool is unavailable for this turn."},
                {"type": "stop", "stop_reason": "end_turn"},
            ],
        ]
    )

    def fail_if_executed(name: str, arguments: dict[str, Any]) -> Any:
        raise AssertionError(f"unexpected tool execution: {name} {arguments}")

    monkeypatch.setattr("app.agent.execute_tool", fail_if_executed)
    events = stream_events(
        client,
        {
            "messages": [{"role": "user", "content": "what can you do?"}],
            "tools": ["health_check"],
            "tool_policy": "exact",
        },
    )

    assert events[0]["tools"] == ["health_check"]
    assert not any(event["type"] in {"tool_call", "tool_result"} for event in events)
    assert events[-1]["type"] == "done"
    assert events[-1]["tool_trace"] == [
        "list_capabilities() -> error: Tool list_capabilities is not available for this turn."
    ]

    recovery_turn = app.config["AGENT_CLIENT"].turns[1]
    refusal = recovery_turn["messages"][2]["content"][0]
    assert refusal == {
        "type": "tool_result",
        "tool_use_id": "t1",
        "content": "Tool list_capabilities is not available for this turn.",
        "is_error": True,
    }


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
