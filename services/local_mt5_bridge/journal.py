"""Small local journal that prevents blind replay after a worker crash."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class CommandJournal:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS command_journal (
                command_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                response_json TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        self._connection.commit()

    def begin(self, command_id: str) -> tuple[str, dict[str, Any] | None]:
        row = self._connection.execute(
            "SELECT status,response_json FROM command_journal WHERE command_id=?",
            (command_id,),
        ).fetchone()
        if row is not None:
            response = json.loads(row[1]) if row[1] else None
            return str(row[0]), response
        self._connection.execute(
            "INSERT INTO command_journal(command_id,status) VALUES (?, 'started')",
            (command_id,),
        )
        self._connection.commit()
        return "new", None

    def finish(self, command_id: str, response: dict[str, Any]) -> None:
        self._connection.execute(
            """
            UPDATE command_journal
            SET status='completed',response_json=?,updated_at=CURRENT_TIMESTAMP
            WHERE command_id=?
            """,
            (json.dumps(response, separators=(",", ":")), command_id),
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
