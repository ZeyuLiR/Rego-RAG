import json

from regorag.app import build_app
from regorag.qa import Turn
from conftest import FakeClient


def test_full_context_answer_with_sources_and_cache(workspace):
    client = FakeClient(replies=["需要 Blue Slip 和身份证明 [[reg-interstate-transfer]]。\nSOURCES: reg-interstate-transfer"])
    app = build_app(workspace, client=client)
    ans = app.qa.ask("外州车转入需要什么材料？")
    assert ans.covered and ans.sources == ["reg-interstate-transfer"]
    assert "SOURCES" not in ans.text
    req = client.requests[0]
    assert req["fallbacks"] == "default"
    assert req["system"][-1]["cache_control"]["type"] == "ephemeral"
    assert "reg-renewal" in req["system"][-1]["text"]
    assert "reg-future-rule" not in req["system"][-1]["text"]
    assert ans.cost_usd > 0


def test_uncovered_question_is_logged_as_gap(workspace):
    client = FakeClient(replies=["知识库没有覆盖这个问题。\nSOURCES: NONE"])
    app = build_app(workspace, client=client)
    ans = app.qa.ask("船的注册怎么办？")
    assert not ans.covered
    gaps = (workspace.usage_log.parent / "gaps.jsonl").read_text(encoding="utf-8")
    assert json.loads(gaps)["question"] == "船的注册怎么办？"


def test_invented_source_ids_are_dropped(workspace):
    client = FakeClient(replies=["...\nSOURCES: reg-made-up, reg-renewal"])
    ans = build_app(workspace, client=client).qa.ask("q")
    assert ans.sources == ["reg-renewal"]


def test_bm25_mode_rewrites_question_first(workspace):
    from dataclasses import replace

    settings = replace(workspace, full_context_token_limit=10)
    client = FakeClient(replies=["blue slip interstate transfer",
                                 "答案 [[reg-interstate-transfer#what-you-need]]\nSOURCES: reg-interstate-transfer#what-you-need"])
    app = build_app(settings, client=client)
    ans = app.qa.ask("外州车要蓝单吗", history=[Turn("user", "你好"), Turn("assistant", "你好！")])
    assert ans.mode == "bm25" and ans.search_query == "blue slip interstate transfer"
    assert ans.sources == ["reg-interstate-transfer#what-you-need"]
    assert "cache_control" not in client.requests[1]["system"][-1]
    assert [m["role"] for m in client.requests[1]["messages"]] == ["user", "assistant", "user"]
