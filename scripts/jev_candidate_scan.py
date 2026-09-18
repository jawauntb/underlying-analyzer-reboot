#!/usr/bin/env python3
"""Scan hardcoded Jev candidate call sites and print a fit table.

This is a manual diagnostic, not part of the app or the test suite: it
hardcodes a short list of call sites in this codebase, asks Jev's 'choice'
primitive (in one batched ``systemone`` request) whether each one is a good
fit for a fast System 1 classification model, and prints the result next to
this codebase's own judgment call for that site.

Usage::

    python scripts/jev_candidate_scan.py

Requires ``JEV_API_KEY`` in the environment. If it is absent, the script
prints a short message and exits cleanly (status 0) rather than failing -
this is a developer tool, not something CI or a deploy should block on.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Allow running as `python scripts/jev_candidate_scan.py` from a checkout
# without installing the package first.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.jev import JevClient, JevError, parse_choice_answer  # noqa: E402

_CRITERIA: dict[str, str] = {
    "good_fit": (
        "A short, bounded classification or routing decision made over "
        "context that is already given - a calibrated yes/no, a single "
        "choice among a small known set, or a rubric score. Ideal for a "
        "fast, cheap System 1 model like Jev."
    ),
    "not_a_fit": (
        "Open-ended prose generation, multi-step reasoning, or math - needs "
        "a full reasoning/writing model, not a System 1 classifier."
    ),
}

#: Hardcoded per the task: known call sites in this codebase, whether they
#: are a good fit for Jev, and whether they are already wired up to it.
CANDIDATES: list[dict[str, str]] = [
    {
        "id": "agent_main_loop",
        "site": "app/agent.py: run_agent_stream main tool-calling loop",
        "description": (
            "The agent's turn-by-turn tool-use conversation loop: deciding "
            "what to say next and which tools to call, across a growing "
            "multi-step context, then writing the final answer in prose."
        ),
        "expected": "not_a_fit",
        "status": "not wired (out of scope)",
    },
    {
        "id": "tool_group_preclassify",
        "site": "app/agent.py: pre-loop tool-group classification",
        "description": (
            "Given only the user's question text, pick which capability "
            "group(s) (meta/charts/data/research/signals/watchlists/studio) "
            "the question is most likely about, before the tool-calling "
            "loop starts, so a smaller matching tool list can be passed in."
        ),
        "expected": "good_fit",
        "status": "wired (preclassify_tool_groups)",
    },
    {
        "id": "citation_regex_fallback",
        "site": "app/citation_verify.py: classify_citation Jev fallback",
        "description": (
            "Given a short parenthesized citation string that a regex pass "
            "already failed to classify, pick which known citation category "
            "(if any) it most likely belongs to."
        ),
        "expected": "good_fit",
        "status": "wired (_classify_citation_with_jev)",
    },
    {
        "id": "vision_v2_memo_writer",
        "site": "app/vision_v2.py: analyst memo writer",
        "description": (
            "Writing the full prose Vision v2 analyst memo - sections, "
            "narrative, and a rating - from a large evidence packet."
        ),
        "expected": "not_a_fit",
        "status": "not wired (out of scope)",
    },
    {
        "id": "prism_memo_writer",
        "site": "app/prism: full-stack investment memo writer",
        "description": (
            "Writing the full prose Prism investment memo - scenarios, "
            "levels, and a recommendation - from a large multi-source "
            "evidence packet."
        ),
        "expected": "not_a_fit",
        "status": "not wired (out of scope)",
    },
    {
        "id": "situate_memo_writer",
        "site": "app/situate: posture memo writer",
        "description": (
            "Writing the full prose Situate posture memo from factor "
            "exposure, return-distribution, and fundamentals data."
        ),
        "expected": "not_a_fit",
        "status": "not wired (out of scope)",
    },
    {
        "id": "summarize_filing_prose",
        "site": "summarize_filing (SEC filing prose summary)",
        "description": (
            "Summarizing a long SEC filing section into readable prose for "
            "a research packet."
        ),
        "expected": "not_a_fit",
        "status": "not wired (out of scope)",
    },
]


def _build_questions() -> dict[str, dict[str, object]]:
    return {
        candidate["id"]: {
            "type": "choice",
            "instructions": (
                "Is the following call site a good fit for a fast, cheap "
                "System 1 classification model, or does it need real "
                "open-ended reasoning/writing?\n\n"
                f"Call site: {candidate['site']}\n"
                f"What it does: {candidate['description']}"
            ),
            "criteria": _CRITERIA,
        }
        for candidate in CANDIDATES
    }


def _print_table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> None:
    all_rows = [headers, *rows]
    widths = [max(len(str(row[i])) for row in all_rows) for i in range(len(headers))]

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(str(cell).ljust(width) for cell, width in zip(row, widths, strict=True))

    print(fmt(headers))
    print(fmt(tuple("-" * w for w in widths)))
    for row in rows:
        print(fmt(row))


def main() -> int:
    if not os.getenv("JEV_API_KEY"):
        print("JEV_API_KEY is not set in the environment; skipping Jev candidate scan.")
        return 0

    client = JevClient()

    try:
        answers = client.ask(state="jev_candidate_scan", questions=_build_questions())
    except JevError as exc:
        print(f"Jev candidate scan failed: {exc}")
        return 1

    rows: list[tuple[str, ...]] = []
    for candidate in CANDIDATES:
        raw_answer = answers.get(candidate["id"])
        try:
            parsed = parse_choice_answer(raw_answer)
            verdict = parsed.choice
            confidence = f"{parsed.confidence:.2f}"
            agrees = "yes" if verdict == candidate["expected"] else "no"
        except JevError as exc:
            verdict = f"error: {exc}"
            confidence = "-"
            agrees = "-"
        rows.append(
            (
                candidate["id"],
                candidate["expected"],
                verdict,
                confidence,
                agrees,
                candidate["status"],
            )
        )

    headers = ("id", "expected", "jev verdict", "confidence", "agrees?", "status")
    _print_table(rows, headers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
