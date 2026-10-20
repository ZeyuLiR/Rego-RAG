"""Chat history kept on the server, so a clerk sees their conversations from any computer.

Conversations are filed under the name the clerk enters on the chat page; there are no logins
yet, so this keeps history apart but does not protect it (phase 3 adds logins).

The database is chosen by REGORAG_CHAT_DB:
  sqlite:///data/chats.db              (default) one file next to the usage log
  postgresql://user:pass@host/dbname   needs: pip install -e ".[postgres]"
Anything else only has to implement `ChatStore`; `open_chat_store` picks it from the URL.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

MAX_CHATS_PER_USER = 500


class ChatStore(Protocol):
    def list(self, user: str) -> list[dict]: ...  # [{id, title, updated}], newest first
    def get(self, user: str, chat_id: str) -> dict | None: ...  # {id, title, updated, turns}
    def save(self, user: str, chat_id: str, title: str, turns: list) -> None: ...
    def delete(self, user: str, chat_id: str) -> None: ...


class _SQLStore:
    """Shared SQL for databases with a DB-API driver; subclasses supply `_db` and `PARAM`."""

    PARAM = "?"
    SCHEMA = ("CREATE TABLE IF NOT EXISTS chats (user_name TEXT NOT NULL, id TEXT NOT NULL, "
              "title TEXT NOT NULL, updated DOUBLE PRECISION NOT NULL, turns TEXT NOT NULL, "
              "PRIMARY KEY (user_name, id))")

    def _db(self):
        raise NotImplementedError

    def _sql(self, sql: str) -> str:
        return sql.replace("?", self.PARAM)

    def _run(self, sql: str, args: tuple = (), fetch: bool = False):
        with closing(self._db()) as db:
            cur = db.cursor()
            cur.execute(self._sql(sql), args)
            rows = cur.fetchall() if fetch else None
            db.commit()
            return rows

    def _init(self) -> None:
        self._run(self.SCHEMA)

    def list(self, user: str) -> list[dict]:
        rows = self._run("SELECT id, title, updated FROM chats WHERE user_name = ? "
                         "ORDER BY updated DESC LIMIT ?", (user, MAX_CHATS_PER_USER), fetch=True)
        return [{"id": i, "title": t, "updated": u} for i, t, u in rows]

    def get(self, user: str, chat_id: str) -> dict | None:
        rows = self._run("SELECT id, title, updated, turns FROM chats WHERE user_name = ? AND id = ?",
                         (user, chat_id), fetch=True)
        if not rows:
            return None
        i, t, u, turns = rows[0]
        return {"id": i, "title": t, "updated": u, "turns": json.loads(turns)}

    def save(self, user: str, chat_id: str, title: str, turns: list) -> None:
        self._run("INSERT INTO chats (user_name, id, title, updated, turns) VALUES (?, ?, ?, ?, ?) "
                  "ON CONFLICT (user_name, id) DO UPDATE SET title = excluded.title, "
                  "updated = excluded.updated, turns = excluded.turns",
                  (user, chat_id, title, time.time(), json.dumps(turns, ensure_ascii=False)))

    def delete(self, user: str, chat_id: str) -> None:
        self._run("DELETE FROM chats WHERE user_name = ? AND id = ?", (user, chat_id))


class SQLiteChatStore(_SQLStore):
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._db()) as db:  # the first version called the column "user"
            cols = [r[1] for r in db.execute("PRAGMA table_info(chats)")]
            if "user" in cols:
                db.execute("ALTER TABLE chats RENAME COLUMN user TO user_name")
                db.commit()
        self._init()

    def _db(self):
        # One short connection per call: requests run on several threads.
        return sqlite3.connect(self.path, timeout=10)


class PostgresChatStore(_SQLStore):
    PARAM = "%s"

    def __init__(self, url: str):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError('PostgreSQL 需要先安装：.venv/bin/pip install -e ".[postgres]"') from exc
        self._connect = lambda: psycopg.connect(url)
        self._init()

    def _db(self):
        return self._connect()


def open_chat_store(url: str, root: Path) -> ChatStore:
    """`sqlite:///relative/or/absolute/path.db` or `postgresql://...`."""
    scheme = urlparse(url).scheme
    if scheme == "sqlite":
        path = Path(url.split("sqlite:///", 1)[1])
        return SQLiteChatStore(path if path.is_absolute() else root / path)
    if scheme in {"postgresql", "postgres"}:
        return PostgresChatStore(url)
    raise ValueError(f"REGORAG_CHAT_DB 不支持 {scheme or url}：请用 sqlite:/// 或 postgresql://")
