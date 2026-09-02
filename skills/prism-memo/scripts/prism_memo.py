#!/usr/bin/env python3
"""Prism (alias "ubermemo") memo runner — Python standard library only.

One entry point for the full-stack investment memo engine, usable by any agent
that holds nothing but a Doppler service-account token.

    prism_memo.py health
    prism_memo.py build NVDA --format txt
    prism_memo.py build NVDA --format pdf --out ./out
    prism_memo.py get NVDA --summary
    prism_memo.py chat NVDA --message "what would break the bull case?"
    prism_memo.py export NVDA --format json --out ./out

Two execution modes, selected with ``--remote`` / ``--local`` (default: remote):

* ``--remote`` talks HTTP to the deployed Underlying API
  (``https://underlying-terminal-production.up.railway.app`` unless overridden).
  Nothing but the origin is needed; the server holds its own keys.
* ``--local`` runs the engine in-process in a checkout of
  ``underlying-analyzer-reboot`` via ``python -m app.prism.cli``, with the
  Doppler secrets injected into the subprocess environment.

Secrets come from Doppler's config download endpoint for ``shared/prd`` and
``underlying-terminal/prd``. Only the variables the engine actually reads
(``ENGINE_SECRET_KEYS``) are kept; everything else in those configs is dropped
immediately, so an unrelated production credential is never handed to the child
process. What is kept is held in memory, passed to the child environment, and
never printed, logged, or written to disk — only counts are reported. Every
message this script relays (packet gaps, child stderr) goes through
``redact_secrets`` first, so a credentialed URL echoed back by an upstream
error cannot reach the terminal or an agent transcript.

Research only. Nothing here places, stages, or simulates a broker order.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DOPPLER_URL = "https://api.doppler.com/v3/configs/config/secrets/download"
DOPPLER_PROJECTS: tuple[tuple[str, str], ...] = (
    ("shared", "prd"),
    ("underlying-terminal", "prd"),
)
DEFAULT_ORIGIN = "https://underlying-terminal-production.up.railway.app"

#: The only variables the Prism engine (and this script's origin resolution)
#: reads. ``shared/prd`` alone carries ~70 more secrets for unrelated services;
#: handing those to a subprocess would widen the blast radius for nothing, so
#: the Doppler overlay is filtered down to exactly this set.
ENGINE_SECRET_KEYS: frozenset[str] = frozenset(
    {
        # Market, macro, filings, search and narrative providers.
        "MASSIVE_API_KEY",
        "MASSIVE_REST_BASE_URL",
        "MASSIVE_TIMEOUT_SECONDS",
        "FRED_API_KEY",
        "FRED_BASE_URL",
        "SEC_USER_AGENT",
        "EXA_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TEXT_MODEL",
        "PRISM_TEXT_MODEL",
        # Packet persistence.
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "PRISM_STORE_ENABLED",
        "PRISM_CACHE_DIR",
        "PRISM_CACHE_ENABLED",
        "PRISM_CACHE_TTL_DAYS",
        # Origin resolution for --remote.
        "PRISM_ORIGIN",
        "APP_URL",
    }
)

#: Credentials leak into error text as query parameters far more often than as
#: bare tokens — an upstream ``requests`` error carries the whole URL. Redact
#: the value of anything that names itself a credential, plus ``Bearer <tok>``.
_CREDENTIAL_PARAM = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|secret|password|passwd|signature|key)"
    r"(=|%3D|\"?\s*:\s*\"?)([^&\s\"',;)]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._\-+/=]{8,})")

#: Below this length a Doppler value is a flag or a short label, not a
#: credential, and blanking it would only make relayed text unreadable.
REDACT_MIN_VALUE_LEN = 8

#: A cold build fans out to Massive, FRED, SEC EDGAR, Exa and Anthropic.
BUILD_TIMEOUT_S = 420
READ_TIMEOUT_S = 90
HEALTH_TIMEOUT_S = 20

FORMATS = ("txt", "json", "pdf")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ENGINE_MISSING = 3
EXIT_FAILED = 4

USER_AGENT = "prism-memo-skill/1.0"


class PrismSkillError(RuntimeError):
    """Anything the caller should see as a one-line message, not a traceback."""


def redact_secrets(text: str, values: object = ()) -> str:
    """Strip credentials out of anything this script relays to stderr.

    Two passes. First the literal values we know we are holding (the Doppler
    overlay) are blanked, which catches a key echoed back in a shape no pattern
    anticipates. Then the pattern pass covers the common case — a provider
    error carrying its own request URL, e.g.
    ``...&api_key=abcd1234&file_type=json`` — including values this process
    never held, such as a key configured server-side.
    """
    if not text:
        return text
    for value in values or ():
        candidate = str(value)
        if len(candidate) >= REDACT_MIN_VALUE_LEN and candidate in text:
            text = text.replace(candidate, "***")
    text = _CREDENTIAL_PARAM.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _BEARER.sub(lambda m: f"{m.group(1)} ***", text)


def note(message: str) -> None:
    """Progress goes to stderr so stdout stays a clean artifact."""
    print(message, file=sys.stderr)


# ---------------------------------------------------------------------------
# Doppler
# ---------------------------------------------------------------------------


def resolve_token(explicit: str | None) -> str | None:
    """``--doppler-token`` beats ``DOPPLER_TOKEN`` beats the service-account var."""
    for candidate in (
        explicit,
        os.getenv("DOPPLER_TOKEN"),
        os.getenv("DOPPLER_SERVICE_ACCOUNT_API_TOKEN"),
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return None


def fetch_doppler_config(token: str, project: str, config: str) -> dict[str, str]:
    """Download one Doppler config as ``{NAME: value}``.

    Errors name the project and the HTTP status only — never the token and never
    a value.
    """
    query = urllib.parse.urlencode({"project": project, "config": config, "format": "json"})
    request = urllib.request.Request(
        f"{DOPPLER_URL}?{query}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PrismSkillError(
            f"Doppler refused {project}/{config}: HTTP {exc.code}. "
            "Check that the service-account token can read that config."
        ) from None
    except urllib.error.URLError as exc:
        raise PrismSkillError(f"Doppler unreachable for {project}/{config}: {exc.reason}") from None
    except json.JSONDecodeError:
        raise PrismSkillError(f"Doppler returned a non-JSON body for {project}/{config}") from None
    if not isinstance(payload, dict):
        raise PrismSkillError(f"Doppler returned an unexpected shape for {project}/{config}")
    return {str(key): str(value) for key, value in payload.items() if value is not None}


def load_secrets(token: str | None, *, override_env: bool = False) -> dict[str, str]:
    """Merge ``shared/prd`` then ``underlying-terminal/prd`` into an env overlay.

    Later configs win over earlier ones. The ambient environment wins over
    Doppler unless ``--override-env`` is passed, so a caller who has already
    exported a key keeps it.
    """
    if not token:
        note(
            "note: no Doppler token supplied (--doppler-token, DOPPLER_TOKEN, or "
            "DOPPLER_SERVICE_ACCOUNT_API_TOKEN); using the ambient environment."
        )
        return {}
    merged: dict[str, str] = {}
    for project, config in DOPPLER_PROJECTS:
        secrets = fetch_doppler_config(token, project, config)
        kept = {key: value for key, value in secrets.items() if key in ENGINE_SECRET_KEYS}
        merged.update(kept)
        note(
            f"note: read {len(secrets)} secrets from Doppler {project}/{config}, "
            f"kept {len(kept)} the engine reads"
        )
    if not override_env:
        merged = {key: value for key, value in merged.items() if key not in os.environ}
    note(f"note: {len(merged)} secrets applied to the child environment")
    return merged


# ---------------------------------------------------------------------------
# HTTP against the deployed engine
# ---------------------------------------------------------------------------


def http(
    origin: str,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: int = READ_TIMEOUT_S,
) -> tuple[int, bytes, str]:
    """Return ``(status, body, content_type)``; HTTP errors are returned, not raised."""
    url = f"{origin.rstrip('/')}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json, text/plain, application/pdf", "User-Agent": USER_AGENT}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "") if exc.headers else ""
    except urllib.error.URLError as exc:
        raise PrismSkillError(f"{method} {url} failed: {exc.reason}") from None
    except TimeoutError:
        raise PrismSkillError(f"{method} {url} timed out after {timeout}s") from None


def http_error_message(origin: str, path: str, status: int, payload: bytes) -> str:
    """Turn one failure into a sentence a caller can act on."""
    detail = ""
    try:
        parsed = json.loads(payload.decode("utf-8"))
        if isinstance(parsed, dict) and parsed.get("error"):
            detail = str(parsed["error"])
    except Exception:  # noqa: BLE001 - a non-JSON body is normal for a 404 page
        detail = payload.decode("utf-8", "replace")[:200].strip()
    detail = redact_secrets(detail)
    if status == 404 and path.startswith("/api/prism"):
        return (
            f"{origin} answered 404 for {path}. Either Prism is not deployed at this "
            "origin yet, or no packet has been built for that ticker — run "
            "`build TICKER` first, or use --local against a checkout of "
            "underlying-analyzer-reboot."
        )
    if status in (429, 503):
        return (
            f"{origin} is at capacity for {path} (HTTP {status}). Builds are "
            f"serialized per client; retry shortly. {detail}".strip()
        )
    return f"{origin} answered HTTP {status} for {path}. {detail}".strip()


def remote_json(origin: str, path: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                timeout: int = READ_TIMEOUT_S) -> Any:
    status, payload, _ = http(origin, path, method=method, body=body, timeout=timeout)
    if status >= 400:
        raise PrismSkillError(http_error_message(origin, path, status, payload))
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        raise PrismSkillError(f"{origin}{path} returned a non-JSON body") from None


# ---------------------------------------------------------------------------
# Local engine
# ---------------------------------------------------------------------------


def resolve_origin(explicit: str | None, secrets: dict[str, str]) -> str:
    """Explicit flag, then the environment, then Doppler's APP_URL, then the default.

    A caller who holds nothing but a Doppler token still lands on the right
    deployment, because ``underlying-terminal/prd`` carries ``APP_URL``.
    """
    for candidate in (
        explicit,
        os.getenv("PRISM_ORIGIN"),
        os.getenv("UNDERLYING_ORIGIN"),
        secrets.get("PRISM_ORIGIN"),
        secrets.get("APP_URL"),
        DEFAULT_ORIGIN,
    ):
        if candidate and str(candidate).strip():
            origin = str(candidate).strip().rstrip("/")
            return origin if "://" in origin else f"https://{origin}"
    return DEFAULT_ORIGIN


def resolve_repo(explicit: str | None) -> Path:
    """Find the ``underlying-analyzer-reboot`` checkout for ``--local``."""
    candidates: list[Path] = []
    for value in (explicit, os.getenv("UNDERLYING_REPO"), os.getenv("UNDERLYING_ANALYZER_REPO")):
        if value:
            candidates.append(Path(value).expanduser())
    # The skill lives either inside option_derivation/.agents/skills or inside the
    # engine repo itself, so walk the ancestors rather than counting levels.
    for anchor in (Path(__file__).resolve(), Path.cwd().resolve() / "_"):
        for parent in anchor.parents:
            candidates.append(parent)
            candidates.append(parent / "underlying-analyzer-reboot")
    candidates.append(Path.home() / "underlying-analyzer-reboot")
    for candidate in candidates:
        if (candidate / "app" / "prism" / "cli.py").is_file():
            return candidate.resolve()
    for candidate in candidates:
        if (candidate / "app" / "main.py").is_file():
            return candidate.resolve()
    raise PrismSkillError(
        "cannot find an underlying-analyzer-reboot checkout for --local. Pass "
        "--repo PATH or set UNDERLYING_REPO, or drop --local to use the deployed API."
    )


def python_for(repo: Path) -> str:
    """Prefer the repo's virtualenv interpreter; fall back to this one."""
    venv = repo / ".venv" / "bin" / "python"
    if venv.is_file():
        return str(venv)
    return shutil.which("python3") or sys.executable


def local_cli(
    repo: Path,
    secrets: dict[str, str],
    args: list[str],
    *,
    timeout: int = BUILD_TIMEOUT_S,
    capture: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m app.prism.cli`` in the checkout with the secret overlay."""
    env = {**os.environ, **secrets}
    env.setdefault("PYTHONPATH", str(repo))
    command = [python_for(repo), "-m", "app.prism.cli", *args]
    note(f"note: running {' '.join(['python', '-m', 'app.prism.cli', *args])} in {repo}")
    try:
        return subprocess.run(
            command,
            cwd=str(repo),
            env=env,
            capture_output=capture,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise PrismSkillError(f"cannot launch the local engine: {exc}") from None
    except subprocess.TimeoutExpired:
        raise PrismSkillError(f"the local Prism build exceeded {timeout}s") from None


def emit_local(
    result: subprocess.CompletedProcess[bytes],
    secrets: dict[str, str] | None = None,
) -> int:
    """Relay a child run and translate its exit code.

    The child's stderr is engine diagnostics, and a provider error there can
    carry the credentialed URL that produced it, so it is redacted before it
    reaches this process's stderr (and any agent transcript capturing it).
    stdout is the artifact and is passed through untouched.
    """
    if result.stderr:
        sys.stderr.write(
            redact_secrets(
                result.stderr.decode("utf-8", "replace"),
                (secrets or {}).values(),
            )
        )
    if result.returncode == EXIT_ENGINE_MISSING:
        note(
            "error: the Prism engine is not importable in that checkout. Use "
            "--remote against the deployed API, or install the engine requirements."
        )
        return EXIT_ENGINE_MISSING
    if result.returncode != 0:
        return EXIT_FAILED
    sys.stdout.write(result.stdout.decode("utf-8", "replace"))
    return EXIT_OK


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def write_or_print(body: bytes, filename: str, fmt: str, out: str | None) -> int:
    """Print text formats, always write a PDF, and print the path when written."""
    if out is None and fmt != "pdf":
        sys.stdout.write(body.decode("utf-8", "replace"))
        if not body.endswith(b"\n"):
            sys.stdout.write("\n")
        return EXIT_OK
    directory = Path(out or ".").expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_bytes(body)
    print(str(path.resolve()))
    return EXIT_OK


def print_json(value: Any) -> int:
    print(json.dumps(value, indent=2, default=str))
    return EXIT_OK


def report_packet_gaps(packet: Any, secrets: dict[str, str] | None = None) -> None:
    """Say honestly which sections the engine could not compute.

    ``meta.errors[].error`` is an upstream exception string, which routinely
    contains the failing request URL — and therefore its API key. Redact before
    printing, so the honesty of the gap report does not cost a credential.
    """
    if not isinstance(packet, dict):
        return
    values = (secrets or {}).values()
    errors = (packet.get("meta") or {}).get("errors") or []
    for entry in errors:
        if isinstance(entry, dict):
            source = redact_secrets(str(entry.get("source")), values)
            detail = redact_secrets(str(entry.get("error")), values)
            note(f"note: {source}: {detail}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_health(args: argparse.Namespace, _secrets: dict[str, str]) -> int:
    """Positional signature matches every other command; health needs no secrets."""
    if args.local:
        repo = resolve_repo(args.repo)
        engine = repo / "app" / "prism" / "engine.py"
        print(
            json.dumps(
                {
                    "mode": "local",
                    "repo": str(repo),
                    "engine_present": engine.is_file(),
                    "cli_present": (repo / "app" / "prism" / "cli.py").is_file(),
                },
                indent=2,
            )
        )
        return EXIT_OK if engine.is_file() else EXIT_ENGINE_MISSING
    payload = remote_json(args.origin, "/api/health", timeout=HEALTH_TIMEOUT_S)
    status, _, _ = http(args.origin, "/api/prism/", timeout=HEALTH_TIMEOUT_S)
    print(
        json.dumps(
            {
                "mode": "remote",
                "origin": args.origin,
                "health": payload,
                "prism_route_status": status,
                "prism_deployed": status < 400,
            },
            indent=2,
        )
    )
    return EXIT_OK


def cmd_build(args: argparse.Namespace, secrets: dict[str, str]) -> int:
    ticker = args.ticker.strip().upper()
    if args.local:
        repo = resolve_repo(args.repo)
        cli_args = [ticker, "--format", args.format]
        if args.out:
            cli_args += ["--out", args.out]
        if args.force:
            cli_args.append("--force")
        if args.no_memo:
            cli_args.append("--no-memo")
        if args.as_of:
            cli_args += ["--as-of", args.as_of]
        return emit_local(local_cli(repo, secrets, cli_args), secrets)

    body: dict[str, Any] = {"ticker": ticker}
    if args.force:
        body["force"] = True
    if args.no_memo:
        body["include_memo"] = False
    if args.as_of:
        body["as_of"] = args.as_of
    note(f"note: POST {args.origin}/api/prism {{'ticker': '{ticker}'}} — this can take 1-3 minutes")
    packet = remote_json(args.origin, "/api/prism", method="POST", body=body,
                         timeout=BUILD_TIMEOUT_S)
    report_packet_gaps(packet, secrets)
    if args.format == "json" and not args.out:
        return print_json(packet)
    return export_remote(args, ticker)


def cmd_get(args: argparse.Namespace, secrets: dict[str, str]) -> int:
    ticker = args.ticker.strip().upper()
    if args.local:
        repo = resolve_repo(args.repo)
        cli_args = [ticker, "--stored"]
        cli_args += ["--summary"] if args.summary else ["--format", "json"]
        if args.as_of:
            cli_args += ["--as-of", args.as_of]
        return emit_local(local_cli(repo, secrets, cli_args, timeout=READ_TIMEOUT_S), secrets)

    suffix = "/summary" if args.summary else ""
    query = f"?as_of={urllib.parse.quote(args.as_of)}" if args.as_of else ""
    path = f"/api/prism/{urllib.parse.quote(ticker)}{suffix}{query}"
    payload = remote_json(args.origin, path)
    if not args.summary:
        report_packet_gaps(payload, secrets)
    return print_json(payload)


def cmd_chat(args: argparse.Namespace, secrets: dict[str, str]) -> int:
    ticker = args.ticker.strip().upper()
    message = args.message.strip()
    if not message:
        raise PrismSkillError("--message is required and must not be empty")
    if args.local:
        repo = resolve_repo(args.repo)
        cli_args = [ticker, "--stored", "--chat", message]
        if args.conversation_id:
            cli_args += ["--conversation-id", args.conversation_id]
        if args.json:
            cli_args += ["--format", "json"]
        return emit_local(local_cli(repo, secrets, cli_args, timeout=READ_TIMEOUT_S), secrets)

    body: dict[str, Any] = {"ticker": ticker, "message": message}
    if args.conversation_id:
        body["conversation_id"] = args.conversation_id
    result = remote_json(args.origin, "/api/prism/chat", method="POST", body=body)
    if args.json:
        return print_json(result)
    print(str((result or {}).get("reply", "")))
    conversation_id = (result or {}).get("conversation_id")
    if conversation_id:
        note(f"note: conversation_id {conversation_id}")
    return EXIT_OK


def export_remote(args: argparse.Namespace, ticker: str) -> int:
    """Download one stored export and write or print it."""
    query = urllib.parse.urlencode(
        {"format": args.format, **({"as_of": args.as_of} if args.as_of else {})}
    )
    path = f"/api/prism/{urllib.parse.quote(ticker)}/export?{query}"
    status, payload, _ = http(args.origin, path, timeout=READ_TIMEOUT_S)
    if status >= 400:
        raise PrismSkillError(http_error_message(args.origin, path, status, payload))
    as_of = args.as_of or "latest"
    return write_or_print(payload, f"prism-{ticker}-{as_of}.{args.format}", args.format, args.out)


def cmd_export(args: argparse.Namespace, secrets: dict[str, str]) -> int:
    ticker = args.ticker.strip().upper()
    if args.local:
        repo = resolve_repo(args.repo)
        cli_args = [ticker, "--stored", "--format", args.format]
        if args.out:
            cli_args += ["--out", args.out]
        if args.as_of:
            cli_args += ["--as-of", args.as_of]
        return emit_local(local_cli(repo, secrets, cli_args, timeout=READ_TIMEOUT_S), secrets)
    return export_remote(args, ticker)


COMMANDS = {
    "health": cmd_health,
    "build": cmd_build,
    "get": cmd_get,
    "chat": cmd_chat,
    "export": cmd_export,
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--doppler-token", default=None, help="Doppler service-account token")
    parser.add_argument(
        "--override-env",
        action="store_true",
        help="Let Doppler values replace variables already set in this environment",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--remote", action="store_true", help="Use the deployed API (default)")
    mode.add_argument("--local", action="store_true", help="Run the engine from a local checkout")
    parser.add_argument(
        "--origin",
        default=None,
        help=(
            "Underlying API origin for --remote. Falls back to PRISM_ORIGIN, "
            f"UNDERLYING_ORIGIN, the Doppler APP_URL, then {DEFAULT_ORIGIN}."
        ),
    )
    parser.add_argument(
        "--no-secrets",
        action="store_true",
        help="Skip the Doppler download entirely and use only the ambient environment",
    )
    parser.add_argument(
        "--repo", default=None, help="Path to underlying-analyzer-reboot for --local"
    )
    parser.add_argument("--as-of", default=None, help="ISO date to build or read")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prism_memo.py",
        description=(
            "Build, read, chat with, and export Prism full-stack investment memo "
            "packets. Research only — not investment advice, and no order is ever placed."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    health = sub.add_parser("health", help="Check the deployed API or the local checkout")
    add_common(health)

    build = sub.add_parser("build", help="Build a packet for one ticker")
    build.add_argument("ticker")
    build.add_argument("--format", "-f", choices=FORMATS, default="txt")
    build.add_argument("--out", "-o", default=None, help="Directory to write the export into")
    build.add_argument("--force", action="store_true", help="Bypass today's stored packet")
    build.add_argument("--no-memo", action="store_true", help="Numbers only, no narrative memo")
    add_common(build)

    get = sub.add_parser("get", help="Read the latest stored packet")
    get.add_argument("ticker")
    get.add_argument("--summary", action="store_true", help="Bounded agent projection only")
    add_common(get)

    chat = sub.add_parser("chat", help="Ask one question about a stored packet")
    chat.add_argument("ticker")
    chat.add_argument("--message", "-m", required=True)
    chat.add_argument("--conversation-id", default=None)
    chat.add_argument("--json", action="store_true", help="Print the full chat payload")
    add_common(chat)

    export = sub.add_parser("export", help="Download a stored packet as txt, json or pdf")
    export.add_argument("ticker")
    export.add_argument("--format", "-f", choices=FORMATS, default="txt")
    export.add_argument("--out", "-o", default=None)
    add_common(export)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse already enforces the choice
        print(f"error: unknown command {args.command}", file=sys.stderr)
        return EXIT_USAGE
    try:
        token = None if args.no_secrets else resolve_token(args.doppler_token)
        secrets = load_secrets(token, override_env=args.override_env) if token else {}
        args.origin = resolve_origin(args.origin, secrets)
        return handler(args, secrets)
    except PrismSkillError as exc:
        print(f"error: {redact_secrets(str(exc))}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:  # pragma: no cover - interactive
        return EXIT_FAILED


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
