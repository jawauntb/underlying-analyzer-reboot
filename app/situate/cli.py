"""Command line entry point for Situate.

    python -m app.situate.cli NVDA
    python -m app.situate.cli NVDA --export md,json,pdf --out ./out
    python -m app.situate.cli NVDA --as-of 2026-06-30 --export json
    python -m app.situate.cli NVDA --stored --export md
    python -m app.situate.cli NVDA --chat "what would prove the read wrong?"
    python -m app.situate.cli NVDA --summary

Clients are constructed from the environment exactly the way
``app.main.create_app`` does — ``MASSIVE_API_KEY`` for market data,
``SEC_USER_AGENT`` for EDGAR, ``EXA_API_KEY`` for news, ``ANTHROPIC_API_KEY`` +
``ANTHROPIC_TEXT_MODEL`` for the memo, ``FRED_API_KEY`` for macro series,
``PRISM_CACHE_DIR`` for the packet store — so ``doppler run --`` or a sourced env
file is all the configuration this needs.

The engine package is imported lazily and every import failure is reported as a
plain message rather than a traceback: a checkout missing an engine module still
runs ``--help`` and degrades clearly.
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

FORMATS = ("md", "json", "pdf")


def load_env_file(path: Path | None = None) -> None:
    """Populate ``os.environ`` from a ``.env`` beside the repo root, if present."""
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
    """The same client set ``create_app`` puts on ``app.config`` (best effort)."""
    clients: dict[str, Any] = {
        "client": None,
        "sec_client": None,
        "exa_client": None,
        "text_generator": None,
        "fred_client": None,
    }
    try:
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
    try:
        from app.prism.macro import fred_client_from_env

        clients["fred_client"] = fred_client_from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"warning: FRED client unavailable: {exc}", file=sys.stderr)

    api_key = os.getenv("ANTHROPIC_API_KEY")
    model = os.getenv("SITUATE_TEXT_MODEL") or os.getenv("ANTHROPIC_TEXT_MODEL")
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


class EngineUnavailable(RuntimeError):
    """The engine package is not importable in this checkout."""


def _import(module: str, *names: str) -> tuple[Any, ...]:
    try:
        imported = __import__(module, fromlist=list(names))
    except Exception as exc:  # noqa: BLE001
        raise EngineUnavailable(
            f"cannot import {module}: {exc}. The Situate engine is not present in this "
            "checkout, or a dependency is missing — install with "
            "`pip install -r requirements.txt`."
        ) from exc
    missing = [name for name in names if not hasattr(imported, name)]
    if missing:
        raise EngineUnavailable(
            f"{module} is importable but does not define {', '.join(missing)}. "
            "The Situate engine in this checkout is incomplete."
        )
    return tuple(getattr(imported, name) for name in names)


def load_or_build(args: argparse.Namespace) -> dict[str, Any]:
    """Return the packet the requested action should operate on."""
    ticker = str(args.ticker).strip().upper()
    if args.stored:
        (get_situate_packet,) = _import("app.situate.engine", "get_situate_packet")
        packet = get_situate_packet(ticker, args.as_of)
        if packet is None:
            raise RuntimeError(
                f"no stored Situate packet for {ticker}"
                f"{f' at {args.as_of}' if args.as_of else ''}. Drop --stored to build one."
            )
        return packet

    (build_situate_packet,) = _import("app.situate.engine", "build_situate_packet")
    clients = build_clients()
    return build_situate_packet(
        clients["client"],
        ticker,
        sec_client=clients["sec_client"],
        exa_client=clients["exa_client"],
        text_generator=clients["text_generator"],
        fred_client=clients["fred_client"],
        api_key=clients["api_key"],
        text_model=clients["text_model"],
        as_of=args.as_of,
        include_memo=not args.no_memo,
        force=args.force,
        include_stack=not args.no_stack,
    )


def render(packet: dict[str, Any], fmt: str) -> tuple[bytes, str]:
    """Return ``(body, suggested_filename)`` for one export format."""
    (export_packet,) = _import("app.situate.export", "export_packet")
    body, _content_type, filename = export_packet(packet, fmt)
    return body, filename


def emit(body: bytes, filename: str, fmt: str, out: str | None, *, force_file: bool) -> str | None:
    """Write or print one export; return the path when a file was written."""
    if out is None and fmt != "pdf" and not force_file:
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
    (chat_turn,) = _import("app.situate.chat", "chat_turn")
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
    reply = result.get("reply") if isinstance(result, dict) else None
    print(str(reply if reply is not None else result))
    conversation_id = result.get("conversation_id") if isinstance(result, dict) else None
    if conversation_id:
        print(f"\nconversation_id: {conversation_id}", file=sys.stderr)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.situate.cli",
        description=(
            "Situate one ticker: exposure, state, odds per horizon, what options "
            "are pricing, the business, zones and a posture memo. Research only — "
            "not investment advice, no price target."
        ),
    )
    parser.add_argument("ticker", help="Ticker symbol, e.g. NVDA")
    parser.add_argument(
        "--export",
        default=None,
        help="Comma-separated formats to write: md,json,pdf (implies --out unless one format)",
    )
    parser.add_argument(
        "--out", "-o", default=None, help="Directory to write exports into"
    )
    parser.add_argument("--as-of", default=None, help="ISO date to build or read (default: today)")
    parser.add_argument(
        "--force", action="store_true", help="Rebuild instead of reusing a stored packet"
    )
    parser.add_argument(
        "--no-memo", action="store_true", help="Skip the narrative memo (numbers only)"
    )
    parser.add_argument(
        "--no-stack", action="store_true", help="Skip the cross-sectional stack (faster)"
    )
    parser.add_argument("--stored", action="store_true", help="Read the latest stored packet")
    parser.add_argument(
        "--chat", metavar="MESSAGE", default=None, help="Ask one question about the packet"
    )
    parser.add_argument(
        "--conversation-id", default=None, help="Continue an existing --chat thread"
    )
    parser.add_argument(
        "--summary", action="store_true", help="Print the bounded agent summary as JSON"
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
        print(f"error: Situate build failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    for entry in (packet.get("meta") or {}).get("errors") or []:
        if isinstance(entry, dict):
            print(f"note: {entry.get('source')}: {entry.get('error')}", file=sys.stderr)

    try:
        if args.chat:
            return run_chat(packet, args.chat, args)
        if args.summary:
            (situate_summary,) = _import("app.situate.engine", "situate_summary")
            print(json.dumps(situate_summary(packet), indent=2, default=str))
            return EXIT_OK

        formats = [f.strip().lower() for f in (args.export or "md").split(",") if f.strip()]
        unknown = [f for f in formats if f not in FORMATS]
        if unknown:
            print(f"error: unknown export format(s): {', '.join(unknown)}", file=sys.stderr)
            return EXIT_USAGE
        force_file = len(formats) > 1
        for fmt in formats:
            body, filename = render(packet, fmt)
            path = emit(body, filename, fmt, args.out, force_file=force_file)
            if path:
                print(path)
    except EngineUnavailable as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ENGINE_MISSING
    except Exception as exc:  # noqa: BLE001
        print(f"error: Situate export failed: {exc}", file=sys.stderr)
        return EXIT_FAILED

    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
