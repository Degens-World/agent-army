from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass
class HuntRecord:
    id: int
    repo: str
    issue_number: int
    issue_title: str
    issue_url: str
    bounty_amount: str
    status: str
    pr_url: str
    worked_at: str


class BountyDB:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS bounty_hunts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    repo TEXT NOT NULL,
                    issue_number INTEGER NOT NULL,
                    issue_title TEXT NOT NULL,
                    issue_url TEXT NOT NULL,
                    bounty_amount TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'pending',
                    pr_url TEXT NOT NULL DEFAULT '',
                    worked_at TEXT NOT NULL
                )
            """)
            conn.commit()

    def log(
        self,
        *,
        repo: str,
        issue_number: int,
        issue_title: str,
        issue_url: str,
        bounty_amount: str,
        status: str,
        pr_url: str = "",
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO bounty_hunts
                    (repo, issue_number, issue_title, issue_url, bounty_amount, status, pr_url, worked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (repo, issue_number, issue_title, issue_url, bounty_amount, status, pr_url, _utcnow()),
            )
            conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def update(self, record_id: int, *, status: str, pr_url: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE bounty_hunts SET status = ?, pr_url = ? WHERE id = ?",
                (status, pr_url, record_id),
            )
            conn.commit()

    def list_all(self) -> list[HuntRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM bounty_hunts ORDER BY worked_at DESC"
            ).fetchall()
        return [
            HuntRecord(
                id=r["id"],
                repo=r["repo"],
                issue_number=r["issue_number"],
                issue_title=r["issue_title"],
                issue_url=r["issue_url"],
                bounty_amount=r["bounty_amount"],
                status=r["status"],
                pr_url=r["pr_url"],
                worked_at=r["worked_at"],
            )
            for r in rows
        ]
