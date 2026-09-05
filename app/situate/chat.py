"""Chat over a built Situate packet.

The system prompt is the same bounded briefing the memo was written from, so any
question about any number in the packet is answered from the same evidence. Turns
are persisted through a store (Supabase ``prism_chats`` when configured, local
JSON otherwise) so a conversation survives the process that started it.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.prism.memo import citation_glosses, mismatched_citation_ids, strip_model_citations
from app.situate.memo import (
    DISCLAIMER,
    build_citations,
    derive_posture,
    project_packet,
)

DEFAULT_MAX_TOKENS = 1_600
DEFAULT_HISTORY_TURNS = 12
DEFAULT_PROJECTION_CHARS = 22_000
DEFAULT_TRANSCRIPT_CHARS = 40_000
MAX_TURN_CHARS = 4_000

CHAT_SYSTEM_HEADER = (
    "You are Situate, answering questions about one research memo you have already "
    "written. The briefing below is the complete evidence base: every number the "
    "engine computed, with its source.\n\n"
    "Rules:\n"
    "1. Answer only from the briefing. If it does not contain the answer, say which "
    "section would have held it and that it is unavailable - never estimate.\n"
    "2. Never say 'buy' or 'sell', never give a price target. Talk in postures "
    "(odds_favorable / balanced / odds_unfavorable) and distributions (quantiles), "
    "with the base rate beside every conditional number.\n"
    "3. Cite by citation id in square brackets, e.g. [C4], using only ids listed in "
    "the briefing's '## Citations' section. Do not write your own citation list and "
    "do not renumber an id.\n"
    "4. Be concrete and short: lead with the number, then the one sentence that "
    "makes it matter.\n"
    "5. Research only. Never give personalised investment advice and never describe "
    "placing an order.\n"
)


def build_system_prompt(
    packet: Mapping[str, Any], *, projection_chars: int = DEFAULT_PROJECTION_CHARS
) -> str:
    """System prompt: the chat rules followed by the packet briefing."""
    briefing = project_packet(packet, max_chars=projection_chars)
    return "\n".join([CHAT_SYSTEM_HEADER, "", briefing])


def normalize_history(
    history: Sequence[Mapping[str, Any]] | None,
    *,
    limit: int = DEFAULT_HISTORY_TURNS,
    max_turn_chars: int = MAX_TURN_CHARS,
    max_total_chars: int = DEFAULT_TRANSCRIPT_CHARS,
) -> list[dict[str, str]]:
    """Keep the last ``limit`` well-formed user/assistant turns, under budget."""
    rows: list[dict[str, str]] = []
    for entry in history or []:
        if not isinstance(entry, Mapping):
            continue
        role = str(entry.get("role") or "").strip().lower()
        content = str(entry.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if len(content) > max_turn_chars:
            content = f"{content[:max_turn_chars].rstrip()} […truncated]"
        rows.append({"role": role, "content": content})
    rows = rows[-max(1, int(limit)) :]
    total = sum(len(row["content"]) + 16 for row in rows)
    while rows and total > max_total_chars:
        total -= len(rows[0]["content"]) + 16
        rows.pop(0)
    return rows


def render_transcript(history: Sequence[Mapping[str, str]], message: str) -> str:
    """Flatten the thread into one prompt for the single-turn text API."""
    lines: list[str] = []
    for turn in history:
        speaker = "Analyst" if turn["role"] == "user" else "Situate"
        lines.append(f"{speaker}: {turn['content']}")
    lines.append(f"Analyst: {message.strip()}")
    lines.append("Situate:")
    return "\n\n".join(lines)


def fallback_reply(packet: Mapping[str, Any], message: str, *, reason: str) -> str:
    """A useful, honest answer when no model is available."""
    ticker = str(packet.get("ticker") or "this name")
    posture = derive_posture(packet)
    return (
        f"The chat model is not available ({reason}), so this is the stored posture "
        f"rather than a new answer to “{message.strip()[:200]}”.\n\n"
        f"Situate's standing read on {ticker} is **{posture['stance'].replace('_', ' ')}** at "
        f"{posture['horizon']} months (conviction {posture['conviction']}). "
        f"{posture['one_line']}\n\n"
        f"Every number behind that is in the packet; ask again once a model key is "
        f"configured.\n\n{DISCLAIMER}"
    )


def chat_turn(
    packet: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]] | None,
    message: str,
    *,
    text_generator: Any | None = None,
    api_key: str | None = None,
    text_model: str | None = None,
    store: Any | None = None,
    conversation_id: str | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.2,
    history_turns: int = DEFAULT_HISTORY_TURNS,
    persist: bool = True,
) -> dict[str, Any]:
    """Answer one question about ``packet`` and persist both sides of the turn."""
    text = str(message or "").strip()
    if not text:
        raise ValueError("message is required")

    ticker = str(packet.get("ticker") or "").strip().upper()
    conversation = str(conversation_id or "").strip() or str(uuid.uuid4())
    citations = build_citations(packet)

    turns = normalize_history(history, limit=history_turns)
    if not turns and store is not None and conversation_id:
        try:
            stored = store.chat_history(conversation, limit=history_turns * 2)
        except Exception:  # noqa: BLE001 - a cold store must not block the answer
            stored = []
        turns = normalize_history(stored, limit=history_turns)

    generator = text_generator
    reason: str | None = None
    if generator is None:
        if api_key:
            from app.anthropic import AnthropicTextClient

            generator = AnthropicTextClient(api_key=api_key, model=text_model)
        else:
            reason = "no text generator and no ANTHROPIC_API_KEY"

    model: str | None = None
    if generator is None:
        reply = fallback_reply(packet, text, reason=reason or "no model configured")
        method = "deterministic"
    else:
        try:
            generated = generator.generate_text(
                system=build_system_prompt(packet),
                prompt=render_transcript(turns, text),
                max_tokens=int(max_tokens),
                temperature=float(temperature),
            )
            reply = str(getattr(generated, "text", "") or "").strip()
            model = getattr(generated, "model", None)
            method = "model"
            if not reply:
                reply = fallback_reply(packet, text, reason="model returned no text")
                method = "deterministic"
                reason = "model returned no text"
        except Exception as exc:  # noqa: BLE001
            reason = f"text generation failed: {exc}"
            reply = fallback_reply(packet, text, reason=reason)
            method = "deterministic"

    reply, model_block = strip_model_citations(reply)
    mismatched = set(mismatched_citation_ids(citation_glosses(model_block), citations))
    cited = [
        citation
        for citation in citations
        if f"[{citation['id']}]" in reply and citation["id"] not in mismatched
    ]

    store_errors: list[str] = []
    if persist and store is not None:
        for role, content, payload in (
            ("user", text, []),
            ("assistant", reply, cited),
        ):
            try:
                stored_turn = store.append_chat(
                    conversation_id=conversation,
                    ticker=ticker,
                    role=role,
                    content=content,
                    citations=payload,
                    metadata={"model": model, "method": method, "engine": "Situate"},
                )
                store_errors.extend(stored_turn.get("errors") or [])
            except Exception as exc:  # noqa: BLE001
                store_errors.append(f"could not store {role} turn: {exc}")

    return {
        "conversation_id": conversation,
        "ticker": ticker,
        "reply": reply,
        "citations": cited,
        "available_citations": citations,
        "model": model,
        "method": method,
        "reason": reason,
        "history": [
            *turns,
            {"role": "user", "content": text},
            {"role": "assistant", "content": reply},
        ],
        "store_errors": store_errors,
        "generated_at": datetime.now(UTC).isoformat(),
    }
