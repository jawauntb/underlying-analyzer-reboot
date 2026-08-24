"""The research agent: an Anthropic tool-use loop over the tool registry.

The agent has exactly the capabilities declared in :mod:`app.tool_registry`, and
runs them through :mod:`app.tool_executor`, so the chat experience, the MCP
endpoint, and the raw HTTP API can never disagree about what a tool does.

Events are streamed to the browser as NDJSON. The full vocabulary:

``start``      turn opened, carries model and available tool names
``text``       incremental assistant prose
``tool_call``  the model asked for a tool; arguments are final
``tool_result``the tool returned; carries compacted JSON plus image artifacts
``article``    a research article artifact was published
``error``      the turn failed; ``message`` is user-safe
``done``       turn complete; carries the assistant text for persistence
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from app.anthropic import AnthropicError, MessageStreamer
from app.tool_executor import execute_tool
from app.tool_registry import (
    ToolSpec,
    agent_tools,
    anthropic_tool_definitions,
)

DEFAULT_MAX_ITERATIONS = 8
MAX_TOOL_CALLS = 16
MAX_HISTORY_MESSAGES = 40
MAX_MESSAGE_CHARS = 12000

SYSTEM_PROMPT = """You are the research agent inside The Underlying Analyzer \
Terminal - a chart-led market research console.

Today is {today}.

## How you work

You have exactly the capabilities listed below for this turn. Use only these
tools, and never claim to have used or accessed a capability that is not listed.
Never estimate a price, a level, a filing detail, or a headline an available
tool can look up. If you have not called a tool, you do not know the number.

{tool_guidance}

Call independent tools in the same turn rather than one at a time.

## Writing

Lead with the answer, then the evidence. Short paragraphs, concrete numbers,
markdown headings only when the reply is genuinely long.

{visual_guidance}

Say what you do not know. If a tool fails or data is missing, state it plainly
and continue with what you have.

{artifact_guidance}

## Boundaries

This is research tooling, not advice. Frame recommendations as research stances
with their reasoning and what would falsify them. Never claim to place, route,
or manage an order - the terminal has no execution path.
"""


class AgentError(RuntimeError):
    """Raised when the agent turn cannot start."""


def build_system_prompt(
    extra: str | None = None,
    *,
    tool_specs: tuple[ToolSpec, ...] | None = None,
) -> str:
    """Build guidance that names only capabilities enabled for this turn."""
    specs = tool_specs if tool_specs is not None else agent_tools()
    if specs:
        tool_lines = [
            f"- `{spec.name}` ({spec.cost}): {spec.summary.rstrip('.')}." for spec in specs
        ]
        tool_guidance = "## Available tools\n\n" + "\n".join(tool_lines)
    else:
        tool_guidance = "## Available tools\n\nNo tools are enabled for this turn."

    if any(spec.produces_images for spec in specs):
        visual_guidance = (
            "Rendered charts are already shown to the user. Refer to them by name, "
            "never describe them pixel by pixel, and never claim to have drawn "
            "something you did not render."
        )
    else:
        visual_guidance = (
            "No rendered-image capability is enabled for this turn. Do not claim "
            "to have created an image."
        )

    article_enabled = any(spec.name == "compose_research_article" for spec in specs)
    if article_enabled:
        artifact_guidance = """## Articles

When the user asks for a write-up, recommendations, or a summary - or when the
work has produced something worth keeping - finish by calling
`compose_research_article`. Every claim in it must trace back to a tool you
actually ran this conversation, and every recommendation needs an explicit
invalidation condition. Do not repeat the article body in your reply
afterwards; a short handoff sentence is enough."""
    else:
        artifact_guidance = (
            "## Saved artifacts\n\nNo article-publishing capability is enabled for "
            "this turn. Deliver the result in the reply."
        )

    prompt = SYSTEM_PROMPT.format(
        today=datetime.now(UTC).strftime("%B %d, %Y"),
        tool_guidance=tool_guidance,
        visual_guidance=visual_guidance,
        artifact_guidance=artifact_guidance,
    )
    if extra and extra.strip():
        prompt = f"{prompt}\n\n## Session context\n\n{extra.strip()[:2000]}"
    return prompt


def normalize_history(messages: Any) -> list[dict[str, Any]]:
    """Turn browser-supplied history into a valid Messages array.

    The browser stores plain ``{role, content}`` records plus an optional
    ``tool_trace`` line per assistant turn. Prior tool calls are replayed as a
    compact trace rather than full tool blocks: it keeps replay cheap and means
    a saved conversation can always be resumed, even after the tool registry
    changes.
    """
    if not isinstance(messages, list):
        raise AgentError("messages must be a list")

    normalized: list[dict[str, Any]] = []
    for entry in messages[-MAX_HISTORY_MESSAGES:]:
        if not isinstance(entry, dict):
            continue
        role = str(entry.get("role") or "").strip()
        if role not in {"user", "assistant"}:
            continue
        content = entry.get("content")
        text = content if isinstance(content, str) else ""
        text = text.strip()[:MAX_MESSAGE_CHARS]

        trace = entry.get("tool_trace")
        if role == "assistant" and isinstance(trace, list) and trace:
            lines = [str(item).strip() for item in trace[:12] if str(item).strip()]
            if lines:
                joined = "\n".join(f"- {line}" for line in lines)
                text = f"{text}\n\n[tools run earlier this conversation]\n{joined}".strip()

        if not text:
            continue
        if normalized and normalized[-1]["role"] == role:
            normalized[-1]["content"] = f"{normalized[-1]['content']}\n\n{text}"
            continue
        normalized.append({"role": role, "content": text})

    while normalized and normalized[0]["role"] != "user":
        normalized.pop(0)

    if not normalized:
        raise AgentError("At least one user message is required")
    if normalized[-1]["role"] != "user":
        raise AgentError("The last message must come from the user")
    return normalized


def select_tools(names: Any, *, exact: bool = False) -> tuple[ToolSpec, ...]:
    """Resolve tool names using either the legacy fallback or exact policy."""
    available = agent_tools()
    if not exact:
        if not isinstance(names, list) or not names:
            return available
        wanted = {str(name).strip() for name in names if str(name).strip()}
        selected = tuple(spec for spec in available if spec.name in wanted)
        return selected or available

    error = "tools must be a non-empty list of recognized tool names"
    if not isinstance(names, list) or not names:
        raise AgentError(error)
    normalized = [name.strip() for name in names if isinstance(name, str)]
    if len(normalized) != len(names) or any(not name for name in normalized):
        raise AgentError(error)
    wanted = set(normalized)
    available_names = {spec.name for spec in available}
    if not wanted.issubset(available_names):
        raise AgentError(error)
    selected = tuple(spec for spec in available if spec.name in wanted)
    return selected


def run_agent_stream(
    client: MessageStreamer,
    messages: list[dict[str, Any]],
    *,
    model: str | None = None,
    tool_specs: tuple[ToolSpec, ...] | None = None,
    suppress_refused_tool_events: bool = False,
    required_first_tool: str | None = None,
    system_extra: str | None = None,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> Iterator[dict[str, Any]]:
    """Run one agent turn, yielding NDJSON-ready event dicts."""
    specs = tool_specs if tool_specs is not None else agent_tools()
    by_name = {spec.name: spec for spec in specs}
    definitions = anthropic_tool_definitions(specs)
    system = build_system_prompt(system_extra, tool_specs=specs)

    conversation: list[dict[str, Any]] = list(messages)
    assistant_text_parts: list[str] = []
    tool_trace: list[str] = []
    tool_budget = MAX_TOOL_CALLS
    stop_reason = "end_turn"
    first_tool_called = False
    required_first_result_pending = False

    yield {
        "type": "start",
        "model": model or getattr(client, "model", "anthropic"),
        "tools": [spec.name for spec in specs],
    }

    try:
        for _ in range(max(1, max_iterations)):
            turn_text: list[str] = []
            pending: list[dict[str, Any]] = []
            stop_reason = "end_turn"

            for event in client.stream_messages(
                system=system,
                messages=conversation,
                tools=definitions,
                model=model,
            ):
                kind = event.get("type")
                if kind == "text":
                    turn_text.append(str(event.get("text") or ""))
                    yield {"type": "text", "text": event.get("text")}
                elif kind == "tool_use":
                    pending.append(event)
                elif kind == "stop":
                    stop_reason = str(event.get("stop_reason") or "end_turn")

            joined = "".join(turn_text).strip()
            if joined:
                assistant_text_parts.append(joined)

            if not pending or stop_reason != "tool_use":
                if required_first_tool and not first_tool_called:
                    yield {
                        "type": "error",
                        "message": (f"Agent must call {required_first_tool} before answering."),
                    }
                    return
                break

            if required_first_tool and not first_tool_called:
                first_name = str(pending[0].get("name") or "")
                if first_name != required_first_tool:
                    yield {
                        "type": "error",
                        "message": (
                            f"Agent must call {required_first_tool} first; "
                            f"{first_name or 'an unnamed tool'} was refused."
                        ),
                    }
                    return
                first_tool_called = True
                required_first_result_pending = True

            assistant_blocks: list[dict[str, Any]] = []
            if joined:
                assistant_blocks.append({"type": "text", "text": joined})
            for call in pending:
                assistant_blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call["input"],
                    }
                )
            conversation.append({"role": "assistant", "content": assistant_blocks})

            result_blocks: list[dict[str, Any]] = []
            for call in pending:
                name = str(call.get("name") or "")
                spec = by_name.get(name)

                if tool_budget <= 0:
                    result_blocks.append(
                        _tool_result_block(
                            call["id"],
                            "Tool budget for this turn is exhausted. Answer with "
                            "what you already have.",
                            is_error=True,
                        )
                    )
                    continue
                tool_budget -= 1

                if spec is None:
                    error = f"Tool {name} is not available for this turn."
                    tool_trace.append(f"{name}({_trace_args(call.get('input'))}) -> error: {error}")
                    if not suppress_refused_tool_events:
                        yield {
                            "type": "tool_call",
                            "id": call["id"],
                            "name": name,
                            "title": name,
                            "group": "meta",
                            "cost": "fast",
                            "input": call.get("input") or {},
                        }
                        yield {
                            "type": "tool_result",
                            "id": call["id"],
                            "name": name,
                            "ok": False,
                            "status": 403,
                            "url": "",
                            "error": error,
                            "duration_ms": 0,
                            "result": None,
                            "artifacts": [],
                        }
                    result_blocks.append(_tool_result_block(call["id"], error, is_error=True))
                    continue

                yield {
                    "type": "tool_call",
                    "id": call["id"],
                    "name": name,
                    "title": spec.title,
                    "group": spec.group,
                    "cost": spec.cost,
                    "input": call.get("input") or {},
                }

                result = execute_tool(name, call.get("input") or {})
                trace_status = "ok" if result.ok else f"error: {result.error}"
                tool_trace.append(f"{name}({_trace_args(call.get('input'))}) -> {trace_status}")

                event_payload = result.to_event()
                event_payload["type"] = "tool_result"
                event_payload["id"] = call["id"]
                yield event_payload

                if required_first_result_pending and name == required_first_tool:
                    required_first_result_pending = False
                    if not result.ok:
                        yield {
                            "type": "error",
                            "message": (
                                f"Required first tool {required_first_tool} failed: "
                                f"{result.error or 'unknown error'}"
                            ),
                        }
                        return

                if result.ok and name == "compose_research_article":
                    article_event = _article_event(result.result)
                    if article_event:
                        yield article_event

                result_blocks.append(
                    _tool_result_block(call["id"], result.model_text(), is_error=not result.ok)
                )

            conversation.append({"role": "user", "content": result_blocks})
        else:
            stop_reason = "max_iterations"

    except AnthropicError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    except Exception as exc:  # pragma: no cover - defensive boundary
        yield {"type": "error", "message": f"Agent run failed: {exc}"}
        return

    yield {
        "type": "done",
        "stop_reason": stop_reason,
        "text": "\n\n".join(part for part in assistant_text_parts if part).strip(),
        "tool_trace": tool_trace,
    }


def _tool_result_block(tool_use_id: str, content: str, *, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
    }
    if is_error:
        block["is_error"] = True
    return block


def _trace_args(arguments: Any) -> str:
    if not isinstance(arguments, dict) or not arguments:
        return ""
    parts = []
    for key, value in list(arguments.items())[:3]:
        text = str(value)
        if len(text) > 40:
            text = text[:37] + "..."
        parts.append(f"{key}={text}")
    return ", ".join(parts)


def _article_event(result: Any) -> dict[str, Any] | None:
    if not isinstance(result, dict):
        return None
    article = result.get("article")
    if not isinstance(article, dict):
        return None
    return {
        "type": "article",
        "article": article,
        "markdown": result.get("markdown"),
    }
