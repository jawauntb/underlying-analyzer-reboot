"""The push ledger reaches a GitHub data branch and records what the old rule would have sent."""

from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app import ledger_sync as ls
from app import vol_watch as vw


class _Resp:
    def __init__(self, code: int, body: dict[str, Any] | None = None) -> None:
        self.status_code, self._body = code, body or {}

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGitHub:
    """Files by URL, like the contents API: GET 404 when missing, PUT needs the sha to update."""

    def __init__(self) -> None:
        self.files: dict[str, tuple[str, str]] = {}  # url -> (content, sha)
        self.puts: list[dict[str, Any]] = []

    def get(
        self,
        url: str,
        headers: dict[str, str],
        params: dict[str, str],
        timeout: int,  # noqa: ARG002
    ) -> _Resp:
        assert params == {"ref": "volwatch-ledger"} and headers["Authorization"] == "Bearer t"
        if url not in self.files:
            return _Resp(404)
        content, sha = self.files[url]
        return _Resp(200, {"sha": sha, "content": base64.b64encode(content.encode()).decode()})

    def put(
        self,
        url: str,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: int,  # noqa: ARG002
    ) -> _Resp:
        assert json["branch"] == "volwatch-ledger" and headers["Authorization"] == "Bearer t"
        if url in self.files and json.get("sha") != self.files[url][1]:
            return _Resp(409)
        self.puts.append({"url": url, **json})
        content = base64.b64decode(json["content"]).decode()
        self.files[url] = (content, f"sha{len(self.puts)}")
        return _Resp(201)


def _row(ts: str, title: str) -> str:
    return json.dumps({"ts": ts, "kind": "push", "title": title})


def test_split_days_groups_rows_by_utc_date() -> None:
    text = "\n".join(
        [
            _row("2026-11-05T04:36:10+00:00", "a"),
            _row("2026-11-05T14:00:00+00:00", "b"),
            _row("2026-11-06T01:00:00+00:00", "c"),
            "not json",
        ]
    )
    days = ls.split_days(text)
    assert sorted(days) == ["2026-11-05", "2026-11-06", "unknown"]
    assert days["2026-11-05"].count("\n") == 2


def test_sync_uploads_each_changed_day_once(tmp_path: Path) -> None:
    ledger, gh = tmp_path / "pushes.jsonl", FakeGitHub()
    ledger.write_text(_row("2026-11-05T04:36:10+00:00", "ES > 7720") + "\n")
    sync = ls.LedgerSync(ledger, "jawauntb/HVega", "volwatch-ledger", "volwatch", "t", gh)
    assert sync.sync() == ["2026-11-05"]
    url = "https://api.github.com/repos/jawauntb/HVega/contents/volwatch/2026-11-05.jsonl"
    assert gh.files[url][0].startswith('{"ts": "2026-11-05T04:36:10')
    assert sync.sync() == []  # unchanged: no request that writes
    with ledger.open("a") as fh:
        fh.write(_row("2026-11-05T15:00:00+00:00", "MU implied >> realized") + "\n")
    assert sync.sync() == ["2026-11-05"] and gh.puts[-1]["sha"] == "sha1"
    # a restart forgets what it sent: GitHub already has the same content, so nothing is written
    fresh = ls.LedgerSync(ledger, "jawauntb/HVega", "volwatch-ledger", "volwatch", "t", gh)
    assert fresh.sync() == [] and len(gh.puts) == 2


def test_sync_refuses_default_branches_and_is_off_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for branch in ("main", "vol-watch"):
        with pytest.raises(ValueError):
            ls.LedgerSync(tmp_path / "x", "jawauntb/HVega", branch, "volwatch", "t")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert ls.LedgerSync.from_env(tmp_path / "x") is None
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    s = ls.LedgerSync.from_env(tmp_path / "x")
    assert s is not None and (s.repo, s.branch, s.folder) == (
        "jawauntb/HVega",
        "volwatch-ledger",
        "volwatch",
    )


class _Data:
    def __init__(self, ivs: list[float]) -> None:
        self.ivs = list(ivs)

    def vix_level(self) -> float:
        return 17.0

    def daily_closes(self, _t: str, _p: str = "2y") -> pd.Series:
        return pd.Series([28 + (i % 5) * 0.3 for i in range(60)])

    def chain_metrics(self, _t: str, _s: float, _d: date) -> dict[str, Any]:
        return {"iv30": self.ivs.pop(0), "skew": None, "expiries": []}

    def zero_dte(self, *_a: Any) -> None:
        return None


def test_ledger_shows_kept_removed_and_held(tmp_path: Path) -> None:
    ledger = tmp_path / "pushes.jsonl"
    cfg = vw.Config(
        watchlist=["SDGR"],
        state_path=tmp_path / "s.json",
        push_ledger=ledger,
        confirm_passes=2,
        spread_rich_exit=6.0,
        spread_cheap_exit=-1.0,
        max_iv_jump=0.25,
    )
    state, notifier = vw.State(), vw.Notifier(cfg)
    # rv20 of the fixture is 34.17, so spread = iv30 - 34.17. Passes, 15 minutes apart:
    # 36 mid; 44 rich for one pass; 36 mid; 60 a 67% jump, held; 43.5 rich; 44 rich again
    data = _Data([36.0, 44.0, 36.0, 60.0, 43.5, 44.0])
    t0 = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
    for i in range(6):
        now = t0 + timedelta(minutes=15 * i)
        vw.run_chain_pass(cfg, state, data, notifier, date(2026, 11, 6), now)  # type: ignore[arg-type]
    rows = [json.loads(x) for x in ledger.read_text().splitlines()]
    held = [r for r in rows if r["kind"] == "held_iv"]
    assert len(held) == 1 and "jumped" in held[0]["reason"]
    pushes = [r for r in rows if r["kind"] == "push"]
    assert [p["title"] for p in pushes] == ["SDGR implied >> realized"]
    assert pushes[0]["payload"]["metrics"]["iv30"] == 44.0  # the sixth pass, confirmed
    # the old rule would have sent the one-pass spike (pass 2) and fired again on pass 5
    cands = [r for r in rows if r["kind"] == "candidate"]
    assert [(c["band"], c["pushed_now"], c["metrics"]["iv30"]) for c in cands] == [
        ("high", False, 44.0),
        ("high", False, 43.5),
    ]
