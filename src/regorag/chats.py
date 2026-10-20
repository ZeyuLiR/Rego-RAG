"""What the chat page keeps on the server: each clerk's conversations (so they see them from
any computer) and a log of every answer (what it read, its cost, the figures that could not be
checked, and the clerk's feedback), which feeds the reviewers' "needs checking" list.

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


class QALog(Protocol):
    def log_answer(self, rec: dict) -> None: ...  # see LOG_FIELDS
    def get_answer(self, answer_id: str) -> dict | None: ...
    def set_feedback(self, answer_id: str, helpful: bool, comment: str) -> None: ...
    def open_items(self) -> list[dict]: ...  # answers that need a reviewer, newest first
    def resolve(self, answer_id: str, by: str, note: str) -> None: ...


LOG_FIELDS = ("id", "ts", "user_name", "collection", "question", "answer", "mode", "search_query",
              "sources", "unverified", "kb_versions", "cost_usd", "latency_ms", "feedback",
              "comment", "status", "resolved_by", "resolved_at", "resolution")
_JSON_FIELDS = {"sources", "unverified", "kb_versions"}


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

    LOG_SCHEMA = ("CREATE TABLE IF NOT EXISTS qa_log (id TEXT PRIMARY KEY, ts DOUBLE PRECISION NOT NULL, "
                  "user_name TEXT, collection TEXT, question TEXT, answer TEXT, mode TEXT, "
                  "search_query TEXT, sources TEXT, unverified TEXT, kb_versions TEXT, "
                  "cost_usd DOUBLE PRECISION, latency_ms INTEGER, feedback TEXT, comment TEXT, "
                  "status TEXT NOT NULL, resolved_by TEXT, resolved_at DOUBLE PRECISION, resolution TEXT)")

    def _init(self) -> None:
        self._run(self.SCHEMA)
        self._run(self.LOG_SCHEMA)

    # ------------------------------------------------------------ answer log

    @staticmethod
    def _row(row) -> dict:
        rec = dict(zip(LOG_FIELDS, row))
        for k in _JSON_FIELDS:
            rec[k] = json.loads(rec[k]) if rec[k] else []
        return rec

    def log_answer(self, rec: dict) -> None:
        rec = {**rec, "ts": rec.get("ts") or time.time(),
               "status": "open" if rec.get("unverified") else "ok"}
        values = tuple(json.dumps(rec.get(k) or [], ensure_ascii=False) if k in _JSON_FIELDS
                       else rec.get(k) for k in LOG_FIELDS)
        self._run(f"INSERT INTO qa_log ({', '.join(LOG_FIELDS)}) VALUES "
                  f"({', '.join('?' * len(LOG_FIELDS))})", values)

    def get_answer(self, answer_id: str) -> dict | None:
        rows = self._run(f"SELECT {', '.join(LOG_FIELDS)} FROM qa_log WHERE id = ?", (answer_id,), fetch=True)
        return self._row(rows[0]) if rows else None

    def set_feedback(self, answer_id: str, helpful: bool, comment: str) -> None:
        """A clerk's verdict. "Wrong" puts the answer on the reviewers' list."""
        if helpful:
            self._run("UPDATE qa_log SET feedback = 'up', comment = ? WHERE id = ?", (comment, answer_id))
        else:
            self._run("UPDATE qa_log SET feedback = 'down', comment = ?, status = 'open' WHERE id = ?",
                      (comment, answer_id))

    def open_items(self) -> list[dict]:
        rows = self._run(f"SELECT {', '.join(LOG_FIELDS)} FROM qa_log WHERE status = 'open' "
                         "ORDER BY ts DESC LIMIT 200", fetch=True)
        return [self._row(r) for r in rows]

    def resolve(self, answer_id: str, by: str, note: str) -> None:
        self._run("UPDATE qa_log SET status = 'resolved', resolved_by = ?, resolved_at = ?, "
                  "resolution = ? WHERE id = ?", (by, time.time(), note, answer_id))

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


def open_chat_store(url: str, root: Path) -> _SQLStore:
    """The store behind REGORAG_CHAT_DB; it implements both `ChatStore` and `QALog`."""
    # url: sqlite:///relative/or/absolute/path.db or postgresql://...
    scheme = urlparse(url).scheme
    if scheme == "sqlite":
        path = Path(url.split("sqlite:///", 1)[1])
        return SQLiteChatStore(path if path.is_absolute() else root / path)
    if scheme in {"postgresql", "postgres"}:
        return PostgresChatStore(url)
    raise ValueError(f"REGORAG_CHAT_DB 不支持 {scheme or url}：请用 sqlite:/// 或 postgresql://")
