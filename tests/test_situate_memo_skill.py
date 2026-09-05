"""The situate-memo skill script must keep the promise its docstring makes.

Two properties are load-bearing for an operator who runs this inside a logged
agent session:

1. Nothing the script relays to stderr may carry a credential. The engine's
   ``meta.errors`` entries and the local child's stderr are upstream exception
   strings, and an upstream error routinely quotes the request URL that failed
   — query string, API key and all.
2. The child process gets only the variables the engine reads, not every
   secret in ``shared/prd``.

The script is standard-library-only and lives outside the package, so it is
loaded by path. It is also mirrored into the option_derivation console repo, and
the two copies must stay byte-identical.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_SCRIPT = REPO_ROOT / "skills" / "situate-memo" / "scripts" / "situate_memo.py"
SKILL_DOC = REPO_ROOT / "skills" / "situate-memo" / "SKILL.md"
#: The console repo carries the same script and SKILL.md; the copies must not drift.
MIRROR_ROOT = REPO_ROOT.parent / "option_derivation" / ".agents" / "skills" / "situate-memo"
MIRROR_SCRIPT = MIRROR_ROOT / "scripts" / "situate_memo.py"
MIRROR_DOC = MIRROR_ROOT / "SKILL.md"

FAKE_FRED_KEY = "abcdef0123456789abcdef0123456789"
FAKE_URL_ERROR = (
    "HTTPError: 400 for url: https://api.stlouisfed.org/fred/series/observations"
    f"?series_id=DGS10&api_key={FAKE_FRED_KEY}&file_type=json"
)


def load_skill() -> ModuleType:
    spec = importlib.util.spec_from_file_location("situate_memo_skill", SKILL_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def skill() -> ModuleType:
    return load_skill()


def test_the_two_copies_of_the_script_are_byte_identical() -> None:
    if not MIRROR_SCRIPT.is_file():
        pytest.skip("the option_derivation checkout is not present next to this repo")
    assert SKILL_SCRIPT.read_bytes() == MIRROR_SCRIPT.read_bytes()


def test_the_two_copies_of_the_skill_doc_are_byte_identical() -> None:
    if not MIRROR_DOC.is_file():
        pytest.skip("the option_derivation checkout is not present next to this repo")
    assert SKILL_DOC.read_bytes() == MIRROR_DOC.read_bytes()


def test_redaction_strips_a_credentialed_url(skill: ModuleType) -> None:
    redacted = skill.redact_secrets(FAKE_URL_ERROR)
    assert FAKE_FRED_KEY not in redacted
    assert "api_key=***" in redacted
    # The part an operator needs — which series, which host, which status — survives.
    assert "series_id=DGS10" in redacted
    assert "400" in redacted


@pytest.mark.parametrize(
    "text",
    [
        "token=sk-live-9f8e7d6c5b4a",
        "apiKey=9f8e7d6c5b4a3210",
        "Authorization: Bearer sk-ant-0123456789abcdef",
        '{"secret": "9f8e7d6c5b4a3210"}',
    ],
)
def test_redaction_covers_the_common_credential_shapes(skill: ModuleType, text: str) -> None:
    redacted = skill.redact_secrets(text)
    for token in ("sk-live-9f8e7d6c5b4a", "9f8e7d6c5b4a3210", "sk-ant-0123456789abcdef"):
        assert token not in redacted


def test_redaction_blanks_literal_values_it_is_holding(skill: ModuleType) -> None:
    held = {"MASSIVE_API_KEY": "zzzz-not-url-shaped-zzzz", "PRISM_CACHE_ENABLED": "1"}
    redacted = skill.redact_secrets(
        "engine said: zzzz-not-url-shaped-zzzz failed while cache=1",
        held.values(),
    )
    assert "zzzz-not-url-shaped-zzzz" not in redacted
    assert "***" in redacted
    # A one-character flag value is not a credential and must not be blanked,
    # or every relayed line turns into asterisks.
    assert "cache=1" in redacted


def test_redaction_leaves_ordinary_diagnostics_alone(skill: ModuleType) -> None:
    text = "note: massive returned 0 rows for X:BTCUSD between 2016-01-01 and 2026-09-01"
    assert skill.redact_secrets(text) == text
    assert skill.redact_secrets("") == ""


def test_report_packet_gaps_redacts_before_printing(
    skill: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    packet = {
        "ticker": "NVDA",
        "meta": {
            "errors": [
                {"source": "implied", "error": FAKE_URL_ERROR},
                {"source": "text", "error": "SEC EDGAR 403 for CIK 0001045810"},
            ]
        },
    }

    skill.report_packet_gaps(packet, {"FRED_API_KEY": FAKE_FRED_KEY})

    captured = capsys.readouterr()
    assert captured.out == ""
    assert FAKE_FRED_KEY not in captured.err
    # The gap is still reported honestly, source and all.
    assert "implied" in captured.err
    assert "text" in captured.err
    assert "SEC EDGAR 403" in captured.err


def test_report_packet_gaps_ignores_a_non_packet(
    skill: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    skill.report_packet_gaps("not a packet")
    skill.report_packet_gaps({"ticker": "NVDA", "meta": {}})
    assert capsys.readouterr().err == ""


def test_emit_local_redacts_child_stderr_and_passes_stdout_through(
    skill: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    result: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess(
        args=["python", "-m", "app.situate.cli", "NVDA"],
        returncode=0,
        stdout=b"SITUATE NVDA memo body\n",
        stderr=FAKE_URL_ERROR.encode("utf-8"),
    )

    code = skill.emit_local(result, {"FRED_API_KEY": FAKE_FRED_KEY})

    captured = capsys.readouterr()
    assert code == skill.EXIT_OK
    assert captured.out == "SITUATE NVDA memo body\n"
    assert FAKE_FRED_KEY not in captured.err
    assert "api_key=***" in captured.err


def test_emit_local_translates_a_missing_engine(
    skill: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    result: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess(
        args=["python"], returncode=skill.EXIT_ENGINE_MISSING, stdout=b"", stderr=b""
    )
    assert skill.emit_local(result) == skill.EXIT_ENGINE_MISSING
    assert "not importable" in capsys.readouterr().err


def test_load_secrets_keeps_only_what_the_engine_reads(
    skill: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    shared = {
        "MASSIVE_API_KEY": "massive-value-0123456789",
        "FRED_API_KEY": FAKE_FRED_KEY,
        "ANTHROPIC_API_KEY": "anthropic-value-0123456789",
        "EXA_API_KEY": "exa-value-0123456789",
        "STRIPE_SECRET_KEY": "stripe-value-0123456789",
        "TWILIO_AUTH_TOKEN": "twilio-value-0123456789",
        "DATABASE_URL": "postgres://user:pass@host/db",
    }
    underlying = {
        "SUPABASE_URL": "https://project.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "supabase-value-0123456789",
        "SEC_USER_AGENT": "situate test contact@example.com",
        "SITUATE_TEXT_MODEL": "claude-opus-4-8",
        "APP_URL": "https://underlying-terminal-production.up.railway.app",
        "SENTRY_DSN": "https://sentry-value-0123456789@o1.ingest.sentry.io/2",
    }
    by_project = {"shared": shared, "underlying-terminal": underlying}
    monkeypatch.setattr(
        skill,
        "fetch_doppler_config",
        lambda _token, project, _config: dict(by_project[project]),
    )
    for name in (*shared, *underlying):
        monkeypatch.delenv(name, raising=False)

    merged = skill.load_secrets("dp.st.fake")

    assert set(merged) == {
        "MASSIVE_API_KEY",
        "FRED_API_KEY",
        "ANTHROPIC_API_KEY",
        "EXA_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
        "SEC_USER_AGENT",
        "SITUATE_TEXT_MODEL",
        "APP_URL",
    }
    assert set(merged) <= skill.ENGINE_SECRET_KEYS
    for dropped in ("STRIPE_SECRET_KEY", "TWILIO_AUTH_TOKEN", "DATABASE_URL", "SENTRY_DSN"):
        assert dropped not in merged

    # Only counts are reported, never a name's value.
    err = capsys.readouterr().err
    for value in (*shared.values(), *underlying.values()):
        assert value not in err
    assert "kept 4" in err  # from shared/prd
    assert "kept 5" in err  # from underlying-terminal/prd


def test_load_secrets_lets_the_ambient_environment_win_by_default(
    skill: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        skill,
        "fetch_doppler_config",
        lambda _token, project, _config: (
            {"MASSIVE_API_KEY": "from-doppler-0123456789"} if project == "shared" else {}
        ),
    )
    monkeypatch.setenv("MASSIVE_API_KEY", "from-environment-0123456789")

    assert skill.load_secrets("dp.st.fake") == {}
    assert skill.load_secrets("dp.st.fake", override_env=True) == {
        "MASSIVE_API_KEY": "from-doppler-0123456789"
    }


def test_engine_secret_keys_cover_origin_resolution(skill: ModuleType) -> None:
    # resolve_origin reads these out of the Doppler overlay, so filtering them
    # out would silently send --remote at the default origin.
    assert {"SITUATE_ORIGIN", "PRISM_ORIGIN", "APP_URL"} <= skill.ENGINE_SECRET_KEYS
    assert skill.resolve_origin(None, {"APP_URL": "https://situate.example.test"}) == (
        "https://situate.example.test"
    )
    # SITUATE_ORIGIN in the overlay wins over APP_URL.
    assert skill.resolve_origin(
        None, {"SITUATE_ORIGIN": "https://s.example.test", "APP_URL": "https://a.example.test"}
    ) == "https://s.example.test"


def test_the_error_path_redacts_too(
    skill: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    message = skill.http_error_message(
        "https://situate.example.test",
        "/api/situate",
        500,
        f'{{"error": "{FAKE_URL_ERROR}"}}'.encode(),
    )
    assert FAKE_FRED_KEY not in message
    assert "HTTP 500" in message

    def boom(_args: object, _secrets: object) -> int:
        raise skill.SituateSkillError(FAKE_URL_ERROR)

    monkeypatch.setitem(skill.COMMANDS, "health", boom)
    assert skill.main(["health", "--no-secrets"]) == skill.EXIT_FAILED
    err = capsys.readouterr().err
    assert FAKE_FRED_KEY not in err
    assert "api_key=***" in err


def test_a_missing_situate_route_reads_as_not_deployed(skill: ModuleType) -> None:
    # Situate is additive and may not be deployed at an origin yet; a 404 on the
    # situate route must be explained as "not deployed", not as a ticker error.
    message = skill.http_error_message(
        "https://situate.example.test", "/api/situate/NVDA", 404, b"not found"
    )
    assert "not deployed" in message
    assert "--local" in message


def test_the_script_stays_standard_library_only() -> None:
    source = SKILL_SCRIPT.read_text(encoding="utf-8")
    for forbidden in ("import requests", "import httpx", "from requests", "import pandas"):
        assert forbidden not in source
    assert sys.version_info >= (3, 11)
