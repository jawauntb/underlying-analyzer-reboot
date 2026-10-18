"""Anonymous Parallel Search MCP adapter for the research agent's news tool."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation, TextContent

from app.exa import ExaResult

MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "underlying-analyzer-reboot/0.1.0"


class ParallelSearchError(RuntimeError):
    pass


class ParallelSearchClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    def search(self, query: str, *, num_results: int, start_published_date: str) -> list[ExaResult]:
        """Keep the existing news result shape and enforce its publication window."""
        payload = asyncio.run(self._search(query, start_published_date))
        raw_results = payload.get("results")
        if not isinstance(raw_results, list):
            raise ParallelSearchError("Parallel search returned invalid results")
        start = date.fromisoformat(start_published_date)
        results = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not isinstance(url, str) or not _public_web_url(url):
                continue
            # Anonymous MCP has freshness guidance, not a hard date-filter argument.
            # Undated results cannot satisfy search_news's published-within contract.
            published = item.get("publish_date")
            if not isinstance(published, str):
                continue
            try:
                published_date = date.fromisoformat(published)
            except (ValueError, TypeError):
                continue
            if published_date < start or published_date > datetime.now(UTC).date():
                continue
            excerpts = item.get("excerpts")
            if not isinstance(excerpts, list) or not all(isinstance(x, str) for x in excerpts):
                raise ParallelSearchError("Parallel search returned invalid excerpts")
            results.append(
                ExaResult(
                    title=str(item.get("title") or url),
                    url=url,
                    published_date=published_date.isoformat(),
                    snippet="\n\n".join(excerpts),
                    text=None,
                    score=None,
                    author=None,
                )
            )
            if len(results) >= num_results:
                break
        return results

    async def _search(self, query: str, start: str) -> dict[str, Any]:
        # Bound discovery, search, and cleanup together. CancelledError propagates
        # through the SDK context managers so an async caller can cancel cleanly.
        try:
            async with (
                asyncio.timeout(self.timeout_seconds),
                httpx.AsyncClient(
                    headers={"User-Agent": USER_AGENT},
                    timeout=self.timeout_seconds,
                    transport=self.transport,
                    follow_redirects=False,
                ) as client,
                streamable_http_client(MCP_URL, http_client=client) as streams,
                ClientSession(
                    streams[0],
                    streams[1],
                    client_info=Implementation(name="underlying-analyzer-reboot", version="0.1.0"),
                ) as session,
            ):
                await session.initialize()
                result = await session.call_tool(
                    "web_search",
                    {
                        "objective": (
                            f"Find recent news about {query}, published on or after "
                            f"{start}. Return dated articles with source URLs."
                        ),
                        "search_queries": [f"{query} news after:{start}"],
                    },
                )
                if result.isError:
                    raise ParallelSearchError("Parallel Search MCP returned an error")
                payload = result.structuredContent
                if payload is None:
                    text = "\n".join(
                        block.text for block in result.content if isinstance(block, TextContent)
                    )
                    payload = json.loads(text)
                if not isinstance(payload, dict):
                    raise ParallelSearchError("Parallel search returned invalid data")
                return payload
        except TimeoutError as exc:
            raise ParallelSearchError("Parallel Search MCP timed out") from exc
        except ParallelSearchError:
            raise
        except Exception as exc:
            raise ParallelSearchError("Parallel Search MCP request failed") from exc


def _public_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False
