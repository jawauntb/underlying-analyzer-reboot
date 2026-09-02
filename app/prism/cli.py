"""Command line entry point for Prism (working alias ``ubermemo``).

    python -m app.prism.cli NVDA --format txt
    python -m app.prism.cli NVDA --format pdf --out ./out
    python -m app.prism.cli NVDA --stored --format json
    python -m app.prism.cli NVDA --chat "what would break the bull case?"

This is the ``--local`` half of the ``prism-memo`` skill: the skill's
``scripts/prism_memo.py`` shells out to it when the underlying repository is on
disk, and talks HTTP to the deployed API otherwise. Both paths produce the same
packet, so a memo built locally and one built remotely are comparable.

Clients are constructed from the environment exactly the way
``app.main.create_app`` does — ``MASSIVE_API_KEY`` for market data,
``SEC_USER_AGENT`` for EDGAR, ``EXA_API_KEY`` for news, ``ANTHROPIC_API_KEY`` +
``ANTHROPIC_TEXT_MODEL`` (or ``PRISM_TEXT_MODEL``) for the memo, ``FRED_API_KEY``
for macro series, ``PRISM_CACHE_DIR`` for the packet store — so `doppler run --`
or a sourced env file is all the configuration this needs.

The engine package is imported lazily and every import failure is reported as a
plain message rather than a traceback: a checkout without the engine modules
should still be able to run ``--help`` and be told exactly what is missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENGINE_MISSING = 3
EXIT_FAILED = 4

FORMATS = ("txt", "json", "pdf")


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def load_env_file(path: Path | None = None) -> None:
    """Populate ``os.environ`` from a ``.env`` beside the repo root, if present.

    Mirrors ``app.main.load_env_file``: existing environment variables win, so a
    ``doppler run`` wrapper or an explicit export is never overridden by a stale
    file on disk.
    """
    if os.getenv("UNDERLYING_SKIP_DOTENV") == "1":
        return
    env_path = path or Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_clients() -> dict[str, Any]:
    """The same client set ``create_app`` puts on ``app.config``.

    A client whose dependency is missing is returned as ``None`` rather than
    raised: the engine records the gap in ``meta.errors`` and still builds every
    section it can, which is the whole point of the packet contract.
    """
    clients: dict[str, Any] = {
        "client": None,
        "sec_client": None,
        "exa_client": None,
        "text_generator": None,
    }
    try:
        # build_prism_client disables the legacy (yfinance) fallback: several
        # universe symbols legitimately have short or stale Massive coverage and
        # the fallback would turn those into hard errors.
        from app.prism.data import build_prism_client

        clients["client"] = build_prism_client()
    except Exception as exc:  # noqa: BLE001
        print(f"warning: market data client unavailable: {exc}", file=sys.stderr)
    try:
        from app.sec import SecClient

        clients["sec_client"] = SecClient(user_agent=os.getenv("SEC_USER_AGENT"))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: SEC client unavailable: {exc}", file=sys.stderr)
    try:
        from app.exa import ExaClient

        clients["exa_client"] = ExaClient(api_key=os.getenv("EXA_API_KEY"))
    except Exception as exc:  # noqa: BLE001
        print(f"warning: Exa client unavailable: {exc}", file=sys.stderr)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("PRISM_TEXT_MODEL") or os.getenv("ANTHROPIC_TEXT_MODEL")
    if api_key:
        try:
            from app.anthropic import AnthropicTextClient

            clients["text_generator"] = AnthropicTextClient(api_key=api_key, model=model)
        except Exception as exc:  # noqa: BLE001
            print(f"warning: Anthropic client unavailable: {exc}", file=sys.stderr)
    else:
        print(
            "warning: ANTHROPIC_API_KEY is not set — the memo falls back to the "
            "deterministic template.",
            file=sys.stderr,
        )
    clients["api_key"] = api_key
    clients["text_model"] = model
    return clients


# ---------------------------------------------------------------------------
# Lazy engine imports
# ---------------------------------------------------------------------------


class EngineUnavailable(RuntimeError):
    """The engine package is not importable in this checkout."""


def _import(module: str, *names: str) -> tuple[Any, ...]:
    try:
        imported = __import__(module, fromlist=list(names))
    except Exception as exc:  # noqa: BLE001 - ImportError or a failing import-time side effect
        raise EngineUnavailable(
            f"cannot import {module}: {exc}. The Prism engine is not present in this "
            "checkout yet — use the deployed API instead "
            "(prism_memo.py --remote), or install the engine dependencies with "
            "`pip install -r requirements.txt`."
        ) from exc
    missing = [name for name in names if not hasattr(imported, name)]
    if missing:
        raise EngineUnavailable(
            f"{module} is importable but does not define {', '.join(missing)}. "
            "The Prism engine in this checkout is incomplete."
        )
    return tuple(getattr(imported, name) for name in names)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def load_or_build(args: argparse.Namespace) -> dict[str, Any]:
    """Return the packet the requested action should operate on."""
    ticker = str(args.ticker).strip().upper()
    if args.stored:
        (get_prism_packet,) = _import("app.prism.engine", "get_prism_packet")
        packet = get_prism_packet(ticker, args.as_of)
        if packet is None:
            raise RuntimeError(
                f"no stored Prism packet for {ticker}"
                f"{f' at {args.as_of}' if args.as_of else ''}. "
                "Drop --stored to build one."
            )
        return packet

    (build_prism_packet,) = _import("app.prism.engine", "build_prism_packet")
    clients = build_clients()
    return build_prism_packet(
        clients["client"],
        ticker,
        sec_client=clients["sec_client"],
        exa_client=clients["exa_client"],
        text_generator=clients["text_generator"],
        api_key=clients["api_key"],
        text_model=clients["text_model"],
        as_of=args.as_of,
        include_memo=not args.no_memo,
        force=args.force,
    )


def render(packet: dict[str, Any], fmt: str) -> tuple[bytes, str]:
    """Return ``(body, suggested_filename)`` for one export format."""
    (export_packet,) = _import("app.prism.export", "export_packet")
    body, _content_type, filename = export_packet(packet, fmt)
    return body, filename


def emit(body: bytes, filename: str, fmt: str, out: str | None) -> str | None:
    """Write or print one export; return the path when a file was written.

    A PDF is always written to disk even without ``--out`` — printing binary to a
    terminal is never what the caller meant.
    """
    if out is None and fmt != "pdf":
        sys.stdout.write(body.decode("utf-8"))
        if not body.endswith(b"\n"):
            sys.stdout.write("\n")
        return None
    directory = Path(out or ".").expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(body)
    return str(path.resolve())


def run_chat(packet: dict[str, Any], message: str, args: argparse.Namespace) -> int:
    """One question against a built packet."""
    (chat_turn,) = _import("app.prism.chat", "chat_turn")
    clients = build_clients()
    result = chat_turn(
        packet,
        None,
        message,
        text_generator=clients["text_generator"],
        api_key=clients["api_key"],
        text_model=clients["text_model"],
        conversation_id=args.conversation_id,
    )
    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
        return EXIT_OK
    reply = result.get("reply") if isinstance(result, dict) else None
    print(str(reply if reply is not None else result))
    conversation_id = result.get("conversation_id") if isinstance(result, dict) else None
    if conversation_id:
        print(f"\nconversation_id: {conversation_id}", file=sys.stderr)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.prism.cli",
        description=(
            "Build a Prism full-stack investment memo packet for one ticker and "
            "print it as txt, json or a written pdf path. Research only — not "
            "investment advice."
        ),
    )
    parser.add_argument("ticker", help="Ticker symbol, e.g. NVDA")
    parser.add_argument(
        "--format",
        "-f",
        choices=FORMATS,
        default="txt",
        help="Output format (default: txt). pdf is always written to a file.",
    )
    parser.add_argument(
        "--out",
        "-o",
        default=None,
        help="Directory to write the export into; prints the path instead of the body.",
    )
    parser.add_argument("--as-of", default=None, help="ISO date to build or read (default: today)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rebuild from the sources instead of reusing today's stored packet",
    )
    parser.add_argument(
        "--no-memo",
        action="store_true",
        help="Skip the narrative memo (numbers only, much cheaper)",
    )
    parser.add_argument(
        "--stored",
        action="store_true",
        help="Read the latest stored packet instead of building a new one",
    )
    parser.add_argument(
        "--chat",
        metavar="MESSAGE",
        default=None,
        help="Ask one question about the packet instead of exporting it",
    )
    parser.add_argument(
        "--conversation-id",
        default=None,
        help="Continue an existing --chat thread",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print only the bounded agent summary projection as JSON",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not str(args.ticker).strip():
        print("error: ticker is required", file=sys.stderr)
        return EXIT_USAGE

    load_env_file()

    try:
        packet = load_or_build(args)
    except EngineUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ENGINE_MISSING
    except Exception as exc:  # noqa: BLE001
        print(f"error: Prism build failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    errors = (packet.get("meta") or {}).get("errors") or []
    for entry in errors:
        if isinstance(entry, dict):
            print(f"note: {entry.get('source')}: {entry.get('error')}", file=sys.stderr)

    try:
        if args.chat:
            return run_chat(packet, args.chat, args)
        if args.summary:
            (prism_summary,) = _import("app.prism.engine", "prism_summary")
            print(json.dumps(prism_summary(packet), indent=2, default=str))
            return EXIT_OK
        body, filename = render(packet, args.format)
    except EngineUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ENGINE_MISSING
    except Exception as exc:  # noqa: BLE001
        print(f"error: Prism export failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    path = emit(body, filename, args.format, args.out)
    if path:
        print(path)
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
