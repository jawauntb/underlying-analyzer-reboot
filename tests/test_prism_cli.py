"""Tests for ``python -m app.prism.cli``.

The CLI is the ``--local`` half of the ``prism-memo`` skill, so what matters is
that it degrades honestly: a checkout without the engine says so instead of
raising, a missing stored packet is a plain message, and every export format
lands where the caller asked for it. Nothing here touches the network — the
engine functions are monkeypatched with a small fake packet.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.prism import cli


@pytest.fixture
def fake_packet() -> dict[str, Any]:
    return {
        "ticker": "NVDA",
        "as_of": "2026-09-01",
        "generated_at": "2026-09-01T22:10:00+00:00",
        "engine_version": "1.0.0",
        "name": "Prism",
        "memo": {"recommendation": {"action": "buy"}, "text": "# NVDA\n"},
        "meta": {"errors": [{"source": "news", "error": "exa key missing"}]},
    }


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let a developer's local ``.env`` change a test outcome."""
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")


def test_ticker_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["   "]) == cli.EXIT_USAGE
    assert "ticker is required" in capsys.readouterr().err


def test_build_prints_text_and_reports_gaps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_packet: dict[str, Any],
) -> None:
    calls: dict[str, Any] = {}

    def fake_build(_client: Any, ticker: str, **kwargs: Any) -> dict[str, Any]:
        calls["ticker"] = ticker
        calls["kwargs"] = kwargs
        return fake_packet

    monkeypatch.setattr("app.prism.engine.build_prism_packet", fake_build)
    monkeypatch.setattr(cli, "build_clients", lambda: dict.fromkeys(
        ("client", "sec_client", "exa_client", "text_generator", "api_key", "text_model")
    ))

    assert cli.main(["nvda", "--format", "txt"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert calls["ticker"] == "NVDA"
    assert calls["kwargs"]["include_memo"] is True
    assert calls["kwargs"]["force"] is False
    assert "PRISM MEMO" in captured.out
    # meta.errors are relayed on stderr so a caller sees what was not computed.
    assert "news: exa key missing" in captured.err


def test_no_memo_and_force_reach_the_engine(
    monkeypatch: pytest.MonkeyPatch,
    fake_packet: dict[str, Any],
) -> None:
    seen: dict[str, Any] = {}

    def fake_build(_client: Any, _ticker: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return fake_packet

    monkeypatch.setattr("app.prism.engine.build_prism_packet", fake_build)
    monkeypatch.setattr(cli, "build_clients", lambda: dict.fromkeys(
        ("client", "sec_client", "exa_client", "text_generator", "api_key", "text_model")
    ))

    assert cli.main(["NVDA", "--force", "--no-memo", "--as-of", "2026-08-31"]) == cli.EXIT_OK
    assert seen["force"] is True
    assert seen["include_memo"] is False
    assert seen["as_of"] == "2026-08-31"


def test_json_and_summary_are_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_packet: dict[str, Any],
) -> None:
    monkeypatch.setattr("app.prism.engine.get_prism_packet", lambda *_a, **_k: fake_packet)

    assert cli.main(["NVDA", "--stored", "--format", "json"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["ticker"] == "NVDA"

    assert cli.main(["NVDA", "--stored", "--summary"]) == cli.EXIT_OK
    summary = json.loads(capsys.readouterr().out)
    assert summary["ticker"] == "NVDA"
    assert summary["as_of"] == "2026-09-01"
    # The projection is bounded; it must not be the whole packet.
    assert "raw" not in summary


def test_pdf_is_always_written_to_a_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
    fake_packet: dict[str, Any],
) -> None:
    monkeypatch.setattr("app.prism.engine.get_prism_packet", lambda *_a, **_k: fake_packet)

    assert cli.main(["NVDA", "--stored", "--format", "pdf", "--out", str(tmp_path)]) == cli.EXIT_OK
    printed = capsys.readouterr().out.strip()
    written = tmp_path / "prism-NVDA-2026-09-01.pdf"
    assert printed == str(written.resolve())
    assert written.read_bytes().startswith(b"%PDF")


def test_out_directory_is_created_for_text(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
    fake_packet: dict[str, Any],
) -> None:
    monkeypatch.setattr("app.prism.engine.get_prism_packet", lambda *_a, **_k: fake_packet)
    target = tmp_path / "nested" / "out"

    assert cli.main(["NVDA", "--stored", "--format", "txt", "--out", str(target)]) == cli.EXIT_OK
    path = capsys.readouterr().out.strip()
    assert path.endswith("prism-NVDA-2026-09-01.txt")
    assert (target / "prism-NVDA-2026-09-01.txt").is_file()


def test_missing_stored_packet_is_a_plain_message(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("app.prism.engine.get_prism_packet", lambda *_a, **_k: None)

    assert cli.main(["NVDA", "--stored"]) == cli.EXIT_FAILED
    error = capsys.readouterr().err
    assert "no stored Prism packet for NVDA" in error
    assert "Traceback" not in error


def test_engine_import_failure_reports_exit_3(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(module: str, *_names: str) -> tuple[Any, ...]:
        raise cli.EngineUnavailable(f"cannot import {module}: no module named {module}")

    monkeypatch.setattr(cli, "_import", refuse)

    assert cli.main(["NVDA"]) == cli.EXIT_ENGINE_MISSING
    error = capsys.readouterr().err
    assert "cannot import app.prism.engine" in error
    assert "Traceback" not in error


def test_incomplete_engine_module_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """An importable module that lacks the function is still 'engine missing'."""
    import app.prism.engine as engine_module

    monkeypatch.delattr(engine_module, "build_prism_packet", raising=False)
    with pytest.raises(cli.EngineUnavailable, match="does not define build_prism_packet"):
        cli._import("app.prism.engine", "build_prism_packet")


def test_chat_prints_the_reply(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_packet: dict[str, Any],
) -> None:
    monkeypatch.setattr("app.prism.engine.get_prism_packet", lambda *_a, **_k: fake_packet)
    monkeypatch.setattr(
        "app.prism.chat.chat_turn",
        lambda _packet, _history, message, **_kwargs: {
            "conversation_id": "conv-1",
            "reply": f"answering: {message}",
        },
    )
    monkeypatch.setattr(cli, "build_clients", lambda: dict.fromkeys(
        ("client", "sec_client", "exa_client", "text_generator", "api_key", "text_model")
    ))

    assert cli.main(["NVDA", "--stored", "--chat", "why?"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert "answering: why?" in captured.out
    assert "conv-1" in captured.err


def test_build_clients_never_raises_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing key degrades to ``None``; the engine records the gap itself."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    clients = cli.build_clients()
    assert set(clients) >= {"client", "sec_client", "exa_client", "text_generator"}
    assert clients["text_generator"] is None


def test_load_env_file_does_not_override_existing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("PRISM_TEST_ONLY=from_file\nPRISM_TEST_KEPT=from_file\n", encoding="utf-8")
    monkeypatch.delenv("UNDERLYING_SKIP_DOTENV", raising=False)
    monkeypatch.delenv("PRISM_TEST_ONLY", raising=False)
    monkeypatch.setenv("PRISM_TEST_KEPT", "from_env")

    cli.load_env_file(env_file)

    import os

    assert os.environ["PRISM_TEST_ONLY"] == "from_file"
    assert os.environ["PRISM_TEST_KEPT"] == "from_env"
    monkeypatch.delenv("PRISM_TEST_ONLY", raising=False)
