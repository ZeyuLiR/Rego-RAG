import datetime as dt
import json
import subprocess
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from regorag import cli
from regorag.api import create_app
from regorag.app import build_app
from regorag.kb import load_kb
from regorag.retrieval import BM25Retriever, HybridRetriever, VectorRetriever, build_retriever
from regorag.vectors import VectorSearchUnavailable
from conftest import FakeClient, FakeEmbedder


def live(workspace):
    return load_kb(workspace.kb_dir).live("rego", today=dt.date(2026, 10, 19))


def test_vector_search_matches_chinese_question(workspace):
    r = VectorRetriever(live(workspace), FakeEmbedder())
    ctx = r.retrieve("外州车需要什么材料")
    assert ctx.mode == "vector"
    assert ctx.cite_ids[0].startswith("reg-interstate-transfer")
    assert len(ctx.scores) == len(ctx.cite_ids) and ctx.scores[0] >= ctx.scores[-1]
    # Keyword search cannot match Chinese text against English articles.
    assert BM25Retriever(live(workspace)).retrieve("外州车需要什么材料").cite_ids == ()


def test_vectors_are_cached_and_pruned(workspace, tmp_path):
    cache = tmp_path / "v.json"
    emb = FakeEmbedder()
    first = VectorRetriever(live(workspace), emb, cache)
    n = len(first.sections)
    assert emb.embedded == n
    VectorRetriever(live(workspace), emb, cache)
    assert emb.embedded == n  # nothing re-embedded
    VectorRetriever([a for a in live(workspace) if a.id == "reg-renewal"], emb, cache)
    assert len(json.loads(cache.read_text())["vectors"]) < n  # removed sections dropped

    other = FakeEmbedder()
    other.name = "another-model"
    VectorRetriever(live(workspace), other, cache)
    assert other.embedded == n  # a different model never reuses old vectors


def test_hybrid_and_modes(workspace):
    arts = live(workspace)
    h = build_retriever(arts, 150_000, "hybrid", FakeEmbedder())
    assert isinstance(h, HybridRetriever)
    assert h.retrieve("blue slip interstate").cite_ids[0] == "reg-interstate-transfer#what-you-need"
    assert build_retriever(arts, 150_000, "bm25").mode == "bm25"
    assert build_retriever(arts, 150_000, "full").mode == "full"
    with pytest.raises(ValueError):
        build_retriever(arts, 150_000, "nope")
    with pytest.raises(VectorSearchUnavailable):
        build_retriever(arts, 150_000, "vector", None)


def test_ask_with_vector_mode_rewrites_then_searches(workspace):
    fake = FakeClient(replies=["interstate transfer documents",
                               "需要 Blue Slip [[reg-interstate-transfer#what-you-need]]\n"
                               "SOURCES: reg-interstate-transfer#what-you-need"])
    app = build_app(workspace, client=fake, embedder=FakeEmbedder())
    ans = app.qa.ask("外州车要什么材料？", mode="vector")
    assert ans.mode == "vector" and ans.covered
    assert ans.search_query == "interstate transfer documents"
    assert "<section id=\"reg-interstate-transfer#what-you-need\"" in fake.requests[1]["system"][1]["text"]
    assert (workspace.vector_dir / "rego.json").is_file()
    # The default mode is unchanged: small KB, whole-KB context, no rewrite.
    assert app.qa.retriever("rego").mode == "full"


def test_api_retrieval_option(workspace):
    fake = FakeClient(replies=["renewal", "答案\nSOURCES: reg-renewal#steps"])
    client = TestClient(create_app(build_app(workspace, client=fake, embedder=FakeEmbedder())))
    body = client.post("/api/ask", json={"question": "续期", "retrieval": "hybrid"}).json()
    assert body["mode"] == "hybrid" and body["sources"] == {
        "reg-renewal#steps": {"title": "Registration renewal", "path": "rego/registration/renewal.md"}}
    assert client.post("/api/ask", json={"question": "q", "retrieval": "magic"}).status_code == 400
    assert "对比全部四种" in client.get("/").text


def test_api_vector_unavailable_is_503(workspace):
    class Broken:
        name = "broken"

        def embed(self, texts):
            raise VectorSearchUnavailable("向量检索需要先安装")

    fake = FakeClient(replies=["q"])
    client = TestClient(create_app(build_app(workspace, client=fake, embedder=Broken())))
    r = client.post("/api/ask", json={"question": "q", "retrieval": "vector"})
    assert r.status_code == 503 and "安装" in r.json()["detail"]


def test_cli_compare_and_eval_without_model_calls(workspace, tmp_path, monkeypatch, capsys):
    fake = FakeClient()
    monkeypatch.setattr(cli, "build_app", lambda s: build_app(workspace, client=fake, embedder=FakeEmbedder()))
    assert cli.main(["compare", "外州车要什么", "--query", "interstate blue slip"]) == 0
    out = capsys.readouterr().out
    assert "== vector ==" in out and "reg-interstate-transfer#what-you-need" in out

    qs = tmp_path / "q.yaml"
    qs.write_text("- question: 外州车需要什么材料\n  expect: [reg-interstate-transfer]\n"
                  "- question: blue slip interstate\n  expect: [reg-interstate-transfer]\n", encoding="utf-8")
    assert cli.main(["eval-retrieval", str(qs), "--no-rewrite"]) == 0
    out = capsys.readouterr().out
    table = [l.split() for l in out.splitlines() if l.split() and l.split()[0] in cli.SECTION_MODES]
    assert {row[0]: row[1] for row in table[:3]} == {"bm25": "50%", "vector": "100%", "hybrid": "100%"}
    assert "bm25 没找到：\n  - 外州车需要什么材料" in out
    assert fake.requests == []  # nothing called the model
