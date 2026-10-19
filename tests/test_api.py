from fastapi.testclient import TestClient

from regorag.api import create_app
from regorag.app import build_app
from conftest import FakeClient


def test_ask_endpoint(workspace):
    fake = FakeClient(replies=["答案 [[reg-renewal]]\nSOURCES: reg-renewal"])
    client = TestClient(create_app(build_app(workspace, client=fake)))
    r = client.post("/api/ask", json={"question": "续期要什么？"})
    assert r.status_code == 200
    body = r.json()
    assert body["covered"] and body["sources"]["reg-renewal"]["title"] == "Registration renewal"
    assert client.get("/api/usage").json()["calls"] == 1
    assert "Rego 知识库助手" in client.get("/").text


def test_budget_exceeded_returns_429(workspace):
    from dataclasses import replace

    app = build_app(replace(workspace, monthly_budget_usd=0.0001), client=FakeClient(replies=["a\nSOURCES: NONE"] * 2))
    client = TestClient(create_app(app))
    assert client.post("/api/ask", json={"question": "q"}).status_code == 200
    assert client.post("/api/ask", json={"question": "q"}).status_code == 429
