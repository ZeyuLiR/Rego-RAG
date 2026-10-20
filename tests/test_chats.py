from fastapi.testclient import TestClient

from regorag.api import create_app
from regorag.app import build_app
from conftest import FakeClient


def test_chat_history_is_kept_on_the_server_per_name(workspace):
    http = TestClient(create_app(build_app(workspace, client=FakeClient())))
    turns = [{"role": "user", "content": "外州车？"}, {"role": "assistant", "data": {"answer": "Blue Slip"}}]
    assert http.put("/api/chats/c1", json={"user": "Amy", "title": "外州车？", "turns": turns}).status_code == 200
    http.put("/api/chats/c2", json={"user": "Amy", "title": "续期", "turns": []})
    http.put("/api/chats/c1", json={"user": "Bob", "title": "Bob's", "turns": []})

    assert [c["id"] for c in http.get("/api/chats", params={"user": "Amy"}).json()["chats"]] == ["c2", "c1"]
    assert http.get("/api/chats/c1", params={"user": "Amy"}).json()["turns"] == turns
    assert http.get("/api/chats/c1", params={"user": "Bob"}).json()["title"] == "Bob's"

    # Saving again updates in place and moves it to the top.
    http.put("/api/chats/c1", json={"user": "Amy", "title": "外州车？", "turns": turns * 2})
    amy = http.get("/api/chats", params={"user": "Amy"}).json()["chats"]
    assert [c["id"] for c in amy] == ["c1", "c2"]

    assert http.delete("/api/chats/c1", params={"user": "Amy"}).status_code == 200
    assert http.get("/api/chats/c1", params={"user": "Amy"}).status_code == 404
    assert http.get("/api/chats/c1", params={"user": "Bob"}).status_code == 200
    assert http.get("/api/chats", params={"user": " "}).status_code == 400
    assert (workspace.usage_log.parent / "chats.db").is_file()


import os
import sqlite3

import pytest

from regorag.chats import SQLiteChatStore, open_chat_store

PG_URL = os.environ.get("REGORAG_TEST_POSTGRES_URL")


@pytest.fixture(params=["sqlite", "postgresql"])
def store(request, tmp_path):
    if request.param == "sqlite":
        return open_chat_store("sqlite:///data/chats.db", tmp_path)
    if not PG_URL:
        pytest.skip("set REGORAG_TEST_POSTGRES_URL to test PostgreSQL")
    s = open_chat_store(PG_URL, tmp_path)
    s._run("DELETE FROM chats")
    s._run("DELETE FROM qa_log")
    return s


def test_every_store_behaves_the_same(store):
    store.save("Amy", "c1", "一", [{"role": "user", "content": "外州车？"}])
    store.save("Amy", "c2", "二", [])
    store.save("Bob", "c1", "Bob", [])
    assert [c["id"] for c in store.list("Amy")] == ["c2", "c1"]
    assert store.get("Amy", "c1")["turns"] == [{"role": "user", "content": "外州车？"}]
    store.save("Amy", "c1", "一改", [])
    assert [(c["id"], c["title"]) for c in store.list("Amy")] == [("c1", "一改"), ("c2", "二")]
    store.delete("Amy", "c1")
    assert store.get("Amy", "c1") is None and store.get("Bob", "c1")["title"] == "Bob"


def test_every_store_keeps_the_answer_log(store):
    store.log_answer({"id": "a1", "question": "q1", "sources": ["reg-renewal"], "unverified": [],
                      "kb_versions": {"reg-renewal": 2}, "cost_usd": 0.01, "latency_ms": 900})
    store.log_answer({"id": "a2", "question": "q2", "unverified": ["$99"]})
    assert store.get_answer("a1")["kb_versions"] == {"reg-renewal": 2}
    assert [i["id"] for i in store.open_items()] == ["a2"]
    store.set_feedback("a1", False, "wrong fee")
    assert {i["id"] for i in store.open_items()} == {"a1", "a2"}
    store.resolve("a2", "Zeyu", "updated the article")
    assert [i["id"] for i in store.open_items()] == ["a1"]
    assert store.get_answer("a2")["resolved_by"] == "Zeyu"


def test_unknown_database_url_is_refused(tmp_path):
    with pytest.raises(ValueError, match="REGORAG_CHAT_DB"):
        open_chat_store("mysql://x/y", tmp_path)


def test_first_version_sqlite_file_is_upgraded(tmp_path):
    path = tmp_path / "chats.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE chats (user TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, "
                   "updated REAL NOT NULL, turns TEXT NOT NULL, PRIMARY KEY (user, id))")
        db.execute("INSERT INTO chats VALUES ('Amy', 'c1', 'old', 1, '[]')")
    assert SQLiteChatStore(path).list("Amy")[0]["title"] == "old"
