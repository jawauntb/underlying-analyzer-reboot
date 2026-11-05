"""Copy vol-watch's push ledger to a GitHub data branch, so it outlives Railway's log.

Railway kept about 6 days of vol-watch's deploy log; the 2026-11-04 alert backtest (HVega
reports/alerts/2026-11-04-volwatch-backtest.md) lost 3 of 8 sessions to that. The ledger
(``/data/vol_watch_pushes.jsonl``, one JSON row per push, held vol band and shadow candidate)
is uploaded here as one file per UTC day, ``<folder>/<YYYY-MM-DD>.jsonl``, on ``<branch>``
of ``<repo>``, through the GitHub contents API. A day whose content did not change is not
uploaded again, so the branch gets one commit per changed day per sync.

Environment: ``GITHUB_TOKEN`` turns it on (unset = off, nothing is sent).
``VOL_WATCH_LEDGER_REPO`` (jawauntb/HVega), ``VOL_WATCH_LEDGER_BRANCH`` (volwatch-ledger),
``VOL_WATCH_LEDGER_DIR`` (volwatch), ``VOL_WATCH_LEDGER_SYNC_MINUTES`` (30).
It writes only to that one folder on that one branch, and never to a default branch.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("ledger_sync")

API = "https://api.github.com"
PROTECTED = {"main", "master", "vol-watch"}


def split_days(text: str) -> dict[str, str]:
    """Group ledger lines by their row's UTC date (ts[:10]); unparseable lines go to 'unknown'."""
    days: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            day = str(json.loads(line).get("ts", ""))[:10] or "unknown"
        except ValueError:
            day = "unknown"
        days.setdefault(day, []).append(line)
    return {d: "\n".join(lines) + "\n" for d, lines in days.items()}


class LedgerSync:
    def __init__(
        self,
        path: Path,
        repo: str,
        branch: str,
        folder: str,
        token: str,
        session: Any = None,
    ) -> None:
        if branch in PROTECTED:
            raise ValueError(f"refusing to write the ledger to branch {branch!r}")
        self.path, self.repo, self.branch, self.folder = path, repo, branch, folder.strip("/")
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        self.sent: dict[str, str] = {}  # day -> sha256 of the content GitHub holds

    @classmethod
    def from_env(cls, path: Path | None) -> LedgerSync | None:
        token = os.environ.get("GITHUB_TOKEN") or ""
        if path is None or not token.strip():
            return None
        return cls(
            path,
            os.environ.get("VOL_WATCH_LEDGER_REPO") or "jawauntb/HVega",
            os.environ.get("VOL_WATCH_LEDGER_BRANCH") or "volwatch-ledger",
            os.environ.get("VOL_WATCH_LEDGER_DIR") or "volwatch",
            token.strip(),
        )

    def _url(self, day: str) -> str:
        return f"{API}/repos/{self.repo}/contents/{self.folder}/{day}.jsonl"

    def sync(self) -> list[str]:
        """Upload every day whose content changed; return the days uploaded."""
        try:
            text = self.path.read_text()
        except OSError:
            return []
        uploaded = []
        for day, content in sorted(split_days(text).items()):
            digest = hashlib.sha256(content.encode()).hexdigest()
            if self.sent.get(day) == digest:
                continue
            url = self._url(day)
            got = self.session.get(
                url, headers=self.headers, params={"ref": self.branch}, timeout=20
            )
            sha = None
            if got.status_code == 200:
                body = got.json()
                sha = body.get("sha")
                remote = base64.b64decode(body.get("content") or "").decode("utf-8", "replace")
                if remote == content:
                    self.sent[day] = digest
                    continue
            elif got.status_code != 404:
                got.raise_for_status()
            put = self.session.put(
                url,
                headers=self.headers,
                json={
                    "message": f"vol-watch ledger {day}",
                    "content": base64.b64encode(content.encode()).decode(),
                    "branch": self.branch,
                    **({"sha": sha} if sha else {}),
                },
                timeout=30,
            )
            put.raise_for_status()
            self.sent[day] = digest
            uploaded.append(day)
        return uploaded

    def run_forever(self, minutes: float) -> None:
        while True:
            try:
                days = self.sync()
                if days:
                    log.info("ledger sync: uploaded %s", ", ".join(days))
            except Exception as exc:  # a failed sync retries next cycle; it never stops the watcher
                log.warning("ledger sync failed: %s", exc)
            time.sleep(max(60.0, minutes * 60.0))


def start_thread(path: Path | None) -> threading.Thread | None:
    sync = LedgerSync.from_env(path)
    if sync is None:
        log.info("ledger sync off: no GITHUB_TOKEN or no ledger path")
        return None
    minutes = float(os.environ.get("VOL_WATCH_LEDGER_SYNC_MINUTES") or 30)
    log.info("ledger sync on: %s@%s/%s every %g min", sync.repo, sync.branch, sync.folder, minutes)
    thread = threading.Thread(target=sync.run_forever, args=(minutes,), daemon=True, name="ledger")
    thread.start()
    return thread
