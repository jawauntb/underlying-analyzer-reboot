from __future__ import annotations

from dataclasses import asdict
from typing import Any
from unittest.mock import MagicMock

import pytest

import app.exa as exa_module
from app.exa import ExaClient, ExaError, ExaResult, build_research_pack

TEST_API_KEY = "test-exa-key"


class FakeResponse:
    def __init__(
        self,
        *,
        payload: Any | None = None,
        status_code: int = 200,
        text: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self) -> Any:
        return self._payload


class FakeSession:
    def __init__(self, *, responses: list[FakeResponse] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._responses = list(responses or [])

    def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
    ) -> FakeResponse:
        self.calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(payload={"results": []})


@pytest.fixture(autouse=True)
def _reset_request_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(exa_module, "_EXA_REQUEST_GATE", exa_module.ExaRequestGate())


def make_client(session: Any | None = None, **kwargs: Any) -> ExaClient:
    return ExaClient(
        api_key=kwargs.pop("api_key", TEST_API_KEY),
        session=session or FakeSession(),
        request_interval_seconds=0,
        max_retries=0,
        **kwargs,
    )


def sample_result_dict() -> dict[str, Any]:
    return {
        "title": "Acme Corp inks 1.6T optical deal with hyperscaler",
        "url": "https://example.com/acme-news",
        "publishedDate": "2026-05-01T12:00:00Z",
        "snippet": "Acme announced a major customer win for 1.6T optical modules.",
        "text": "Full article body describing the Acme 1.6T optical deal in detail.",
        "score": 0.91,
        "author": "Jane Reporter",
    }


def test_exa_result_parses_from_dict() -> None:
    result = ExaResult.from_dict(sample_result_dict())

    assert result.title.startswith("Acme")
    assert result.url == "https://example.com/acme-news"
    assert result.published_date == "2026-05-01T12:00:00Z"
    assert "1.6T optical" in result.snippet
    assert result.text is not None
    assert "Full article body" in result.text
    assert result.score == pytest.approx(0.91)
    assert result.author == "Jane Reporter"


def test_exa_result_handles_missing_fields() -> None:
    result = ExaResult.from_dict({"url": "https://example.com/x"})

    assert result.url == "https://example.com/x"
    assert result.title == ""
    assert result.published_date is None
    assert result.snippet == ""
    assert result.text is None
    assert result.score is None
    assert result.author is None


def test_exa_result_handles_highlights_list() -> None:
    result = ExaResult.from_dict(
        {
            "title": "T",
            "url": "https://example.com/y",
            "highlights": ["First highlight phrase.", "Second highlight phrase."],
        }
    )

    assert "First highlight phrase." in result.snippet
    assert "Second highlight phrase." in result.snippet


def test_search_posts_expected_body_and_headers() -> None:
    session = FakeSession(
        responses=[FakeResponse(payload={"results": [sample_result_dict()]})]
    )
    client = make_client(session=session)

    results = client.search(
        "Acme guidance",
        num_results=5,
        start_published_date="2026-03-01",
        include_domains=["bloomberg.com"],
        use_autoprompt=True,
    )

    assert len(results) == 1
    assert results[0].url == "https://example.com/acme-news"
    assert len(session.calls) == 1
    call = session.calls[0]
    assert call["url"] == exa_module.EXA_SEARCH_URL
    body = call["json"]
    assert body["query"] == "Acme guidance"
    assert body["numResults"] == 5
    assert body["useAutoprompt"] is True
    assert body["startPublishedDate"] == "2026-03-01"
    assert body["includeDomains"] == ["bloomberg.com"]
    assert call["headers"]["x-api-key"] == TEST_API_KEY
    assert call["headers"]["Content-Type"] == "application/json"


def test_search_returns_empty_for_blank_query() -> None:
    session = FakeSession()
    client = make_client(session=session)

    assert client.search("   ") == []
    assert session.calls == []


def test_search_caches_repeated_queries() -> None:
    session = FakeSession(
        responses=[FakeResponse(payload={"results": [sample_result_dict()]})]
    )
    client = make_client(session=session)

    first = client.search("Acme guidance", num_results=5)
    second = client.search("Acme guidance", num_results=5)

    assert len(session.calls) == 1
    assert [r.url for r in first] == [r.url for r in second]
    assert first[0] is not second[0]


def test_search_raises_exa_error_on_non_retryable_status() -> None:
    session = FakeSession(
        responses=[FakeResponse(status_code=401, text="unauthorized")]
    )
    client = make_client(session=session)

    with pytest.raises(ExaError, match="401"):
        client.search("anything")


def test_search_retries_rate_limited_response(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(exa_module.time, "sleep", sleeps.append)
    session = FakeSession(
        responses=[
            FakeResponse(status_code=429, headers={"Retry-After": "0.25"}),
            FakeResponse(payload={"results": [sample_result_dict()]}),
        ]
    )
    client = ExaClient(
        api_key=TEST_API_KEY,
        session=session,
        request_interval_seconds=0,
        max_retries=1,
        backoff_max_seconds=2,
    )

    results = client.search("retry test")

    assert len(results) == 1
    assert len(session.calls) == 2
    assert sleeps == [0.25]


def test_get_contents_posts_to_contents_endpoint() -> None:
    session = FakeSession(
        responses=[FakeResponse(payload={"results": [sample_result_dict()]})]
    )
    client = make_client(session=session)

    results = client.get_contents(["https://example.com/acme-news"], text_max_chars=500)

    assert len(results) == 1
    call = session.calls[0]
    assert call["url"] == exa_module.EXA_CONTENTS_URL
    assert call["json"]["ids"] == ["https://example.com/acme-news"]
    assert call["json"]["text"] == {"maxCharacters": 500}


def test_get_contents_returns_empty_for_no_urls() -> None:
    session = FakeSession()
    client = make_client(session=session)

    assert client.get_contents([]) == []
    assert session.calls == []


def test_post_raises_when_api_key_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    client = ExaClient(
        api_key=None,
        session=FakeSession(),
        request_interval_seconds=0,
        max_retries=0,
    )

    with pytest.raises(ExaError, match="not configured"):
        client.search("hello")


def test_build_research_pack_returns_not_configured_when_no_client() -> None:
    pack = build_research_pack(None, "ACME", "Acme Corp", industry="Networking")

    assert pack["Status"] == "not configured"
    assert pack["Provider"] == "Exa"
    assert pack["Ticker"] == "ACME"
    assert pack["Company"] == "Acme Corp"
    expected_buckets = {
        "recent_news_90d",
        "product_and_customer",
        "language_mutation",
        "peer_and_reclassification",
        "sell_side_framing",
        "capex_cycle_context",
    }
    assert set(pack["Queries"].keys()) == expected_buckets
    assert all(v == [] for v in pack["Queries"].values())
    assert pack["Citations"] == []


def test_build_research_pack_returns_not_configured_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    client = ExaClient(
        api_key=None,
        session=FakeSession(),
        request_interval_seconds=0,
        max_retries=0,
    )

    pack = build_research_pack(client, "ACME", "Acme Corp")

    assert pack["Status"] == "not configured"


def test_build_research_pack_populates_all_buckets_with_stub_client() -> None:
    expected_buckets = [
        "recent_news_90d",
        "product_and_customer",
        "language_mutation",
        "peer_and_reclassification",
        "sell_side_framing",
        "capex_cycle_context",
    ]
    canned_result = ExaResult.from_dict(sample_result_dict())
    captured_calls: list[dict[str, Any]] = []

    class StubClient:
        api_key = TEST_API_KEY

        def search(self, query: str, **kwargs: Any) -> list[ExaResult]:
            captured_calls.append({"query": query, **kwargs})
            # Vary URL per bucket so dedupe doesn't collapse citations
            unique_url = f"https://example.com/{len(captured_calls)}"
            return [
                ExaResult(
                    title=canned_result.title,
                    url=unique_url,
                    published_date=canned_result.published_date,
                    snippet=canned_result.snippet,
                    text=canned_result.text,
                    score=canned_result.score,
                    author=canned_result.author,
                )
            ]

    pack = build_research_pack(
        StubClient(),  # type: ignore[arg-type]
        "ACME",
        "Acme Corp",
        industry="Networking",
        sector="Technology",
    )

    assert pack["Status"] == "available"
    assert pack["Provider"] == "Exa"
    for bucket in expected_buckets:
        assert bucket in pack["Queries"]
        assert len(pack["Queries"][bucket]) == 1
        entry = pack["Queries"][bucket][0]
        assert set(entry.keys()) >= set(asdict(canned_result).keys())
    assert len(pack["Citations"]) == len(expected_buckets)
    bucket_in_citations = {c["query_bucket"] for c in pack["Citations"]}
    assert bucket_in_citations == set(expected_buckets)
    # Validate curated query language
    queries_by_bucket = {call["query"]: call for call in captured_calls}
    assert any("earnings news guidance" in q for q in queries_by_bucket)
    assert any("optical" in q for q in queries_by_bucket)
    assert any("price target" in q for q in queries_by_bucket)
    assert any("capex" in q.lower() for q in queries_by_bucket)


def test_build_research_pack_collects_errors_on_failure() -> None:
    class FlakyClient:
        api_key = TEST_API_KEY

        def __init__(self) -> None:
            self.count = 0

        def search(self, query: str, **kwargs: Any) -> list[ExaResult]:
            self.count += 1
            if self.count % 2 == 0:
                raise ExaError("boom")
            return [ExaResult.from_dict(sample_result_dict())]

    pack = build_research_pack(FlakyClient(), "ACME", "Acme Corp")  # type: ignore[arg-type]

    assert pack["Status"] == "available"
    assert pack["Errors"]
    assert any("boom" in err for err in pack["Errors"])


def test_search_with_contents_merges_text(monkeypatch: pytest.MonkeyPatch) -> None:
    search_payload = {
        "results": [
            {
                "title": "Acme",
                "url": "https://example.com/a",
                "publishedDate": "2026-05-01",
                "snippet": "short snippet",
            }
        ]
    }
    contents_payload = {
        "results": [
            {
                "title": "Acme",
                "url": "https://example.com/a",
                "text": "FULL ARTICLE BODY",
            }
        ]
    }
    session = FakeSession(
        responses=[
            FakeResponse(payload=search_payload),
            FakeResponse(payload=contents_payload),
        ]
    )
    client = make_client(session=session)

    results = client.search_with_contents("Acme deal", num_results=3, text_max_chars=500)

    assert len(results) == 1
    assert results[0].text == "FULL ARTICLE BODY"
    assert results[0].snippet == "short snippet"
    assert len(session.calls) == 2
    assert session.calls[0]["url"] == exa_module.EXA_SEARCH_URL
    assert session.calls[1]["url"] == exa_module.EXA_CONTENTS_URL


def test_mock_session_records_x_api_key_header() -> None:
    # Spec asks for unittest.mock; verify the header survives via MagicMock too.
    session = MagicMock()
    session.post.return_value = FakeResponse(payload={"results": []})
    client = ExaClient(
        api_key=TEST_API_KEY,
        session=session,
        request_interval_seconds=0,
        max_retries=0,
    )

    client.search("hello")

    assert session.post.called
    kwargs = session.post.call_args.kwargs
    assert kwargs["headers"]["x-api-key"] == TEST_API_KEY
    assert kwargs["json"]["query"] == "hello"
    assert "numResults" in kwargs["json"]
    assert "useAutoprompt" in kwargs["json"]
