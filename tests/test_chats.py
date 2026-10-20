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
