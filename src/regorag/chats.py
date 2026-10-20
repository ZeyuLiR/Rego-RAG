"""Chat history kept on the server (data/chats.db), so a clerk sees their conversations from
any computer. Conversations are filed under the name the clerk enters on the chat page; there
are no logins yet, so this keeps history apart but does not protect it (phase 3 adds logins).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

MAX_CHATS_PER_USER = 500


class ChatStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS chats (user TEXT NOT NULL, id TEXT NOT NULL, "
                       "title TEXT NOT NULL, updated REAL NOT NULL, turns TEXT NOT NULL, "
                       "PRIMARY KEY (user, id))")

    def _db(self) -> sqlite3.Connection:
        # One short connection per call: requests run on several threads.
        return sqlite3.connect(self.path, timeout=10)

    def list(self, user: str) -> list[dict]:
        with closing(self._db()) as db:
            rows = db.execute("SELECT id, title, updated FROM chats WHERE user = ? "
                              "ORDER BY updated DESC LIMIT ?", (user, MAX_CHATS_PER_USER)).fetchall()
        return [{"id": i, "title": t, "updated": u} for i, t, u in rows]

    def get(self, user: str, chat_id: str) -> dict | None:
        with closing(self._db()) as db:
            row = db.execute("SELECT id, title, updated, turns FROM chats WHERE user = ? AND id = ?",
                             (user, chat_id)).fetchone()
        if not row:
            return None
        return {"id": row[0], "title": row[1], "updated": row[2], "turns": json.loads(row[3])}

    def save(self, user: str, chat_id: str, title: str, turns: list) -> None:
        with closing(self._db()) as db, db:
            db.execute("INSERT INTO chats (user, id, title, updated, turns) VALUES (?, ?, ?, ?, ?) "
                       "ON CONFLICT (user, id) DO UPDATE SET title = excluded.title, "
                       "updated = excluded.updated, turns = excluded.turns",
                       (user, chat_id, title, time.time(), json.dumps(turns, ensure_ascii=False)))

    def delete(self, user: str, chat_id: str) -> None:
        with closing(self._db()) as db, db:
            db.execute("DELETE FROM chats WHERE user = ? AND id = ?", (user, chat_id))
