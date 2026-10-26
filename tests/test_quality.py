"""Number check, answer log and feedback, and the eval set."""

import time

from fastapi.testclient import TestClient

from regorag import evals
from regorag.api import create_app
from regorag.app import build_app
from regorag.numbers import figures, unverified
from regorag.qa import Answer
from conftest import FakeClient

SOURCE = ("The fee is $45.00 from 1 July 2026. Apply within fourteen days using Form 1234. "
          "A 10% surcharge applies.")


def keys(text):
    return {f.key for f in figures(text)}


def test_figures_match_in_any_written_form():
    assert keys("费用 45 澳元，2026年7月1日起，14 天内，表格 1234，加收 10%") == keys(SOURCE)
    assert "duration:14:days" in keys("within 14 days")
    assert "duration:5:years" in keys("车龄超过五年")
    assert "date:2026-07-01" in keys("01/07/2026")
    assert "duration:14:days" in keys("a 14-day free trial")
    assert keys("每月 8 美元") == keys("US$8 per month") == keys("USD 8") == keys("8元") == {"money:8"}
    assert keys("€12.50") == keys("12.5 欧元") == {"money:12.5"}


def test_unverified_lists_figures_missing_from_the_sources():
    answer = "费用是 $45，从 2026年7月1日 起；30 天内办理，2026-08-01 截止，另收 $12.50。"
    assert unverified(answer, [SOURCE]) == ["2026-08-01", "$12.50", "30 天"]
    # A figure the clerk gave in the question is not the answer's invention.
    assert unverified("按你说的 30 天算", [SOURCE], "30 天够吗？") == []
    assert unverified("July 2026 起", [SOURCE]) == []  # the month of a known date


def test_answer_with_unknown_figure_is_flagged_and_logged(workspace):
    fake = FakeClient(replies=["续期费 $99 [[reg-renewal]]\nSOURCES: reg-renewal"])
    client = TestClient(create_app(build_app(workspace, client=fake)))
    body = client.post("/api/ask", json={"question": "续期多少钱？", "user": "Amy"}).json()
    assert body["unverified"] == ["$99"] and body["answer_id"]
    items = client.get("/api/review/items").json()["items"]
    assert [i["question"] for i in items] == ["续期多少钱？"]
    assert items[0]["user_name"] == "Amy" and items[0]["kb_versions"] == {"reg-renewal": 1}
    assert items[0]["source_links"][0]["title"] == "Registration renewal"

    r = client.post(f"/api/review/items/{body['answer_id']}/resolve", json={"reviewer": "Zeyu", "note": "fixed"})
    assert r.status_code == 200 and client.get("/api/review/items").json()["items"] == []


def test_clerk_feedback(workspace):
    fake = FakeClient(replies=["Check the Green Slip [[reg-renewal]]\nSOURCES: reg-renewal"] * 2)
    client = TestClient(create_app(build_app(workspace, client=fake)))
    ok = client.post("/api/ask", json={"question": "续期要什么？"}).json()
    bad = client.post("/api/ask", json={"question": "续期要什么？"}).json()
    assert ok["unverified"] == [] and client.get("/api/review/items").json()["items"] == []

    assert client.post("/api/feedback", json={"answer_id": ok["answer_id"], "helpful": True}).status_code == 200
    client.post("/api/feedback", json={"answer_id": bad["answer_id"], "helpful": False, "comment": "还要 Pink Slip"})
    items = client.get("/api/review/items").json()["items"]
    assert [(i["id"], i["feedback"], i["comment"]) for i in items] == [(bad["answer_id"], "down", "还要 Pink Slip")]
    assert client.post("/api/feedback", json={"answer_id": "nope", "helpful": True}).status_code == 404

    r = client.post(f"/api/review/items/{bad['answer_id']}/to-eval",
                    json={"question": "续期要什么？", "must_include": ["Pink Slip"], "expect": ["reg-renewal"],
                          "added_by": "Zeyu"})
    assert r.status_code == 200 and r.json()["item"]["id"] == "q-001"
    assert evals.load_items(workspace.eval_file)[0].must_include == ["Pink Slip"]


def test_review_and_eval_pages_need_the_admin_token(workspace):
    from dataclasses import replace

    client = TestClient(create_app(build_app(replace(workspace, admin_token="s3cret"), client=FakeClient())))
    assert client.get("/api/review/items").status_code == 401
    assert client.get("/api/eval/items").status_code == 401
    assert client.get("/api/eval/items", headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert "评测集" in client.get("/eval").text


def test_eval_items_are_numbered_and_edited(tmp_path):
    path = tmp_path / "eval" / "questions.yaml"
    a = evals.upsert(path, {"question": "Q1", "added_by": "Zeyu"})
    b = evals.upsert(path, {"question": "Q2", "must_include": "Blue Slip", "added_by": "Amy"})
    assert (a.id, b.id) == ("q-001", "q-002")
    evals.upsert(path, {"id": "q-001", "question": "Q1 edited", "added_by": "Zeyu"})
    evals.remove(path, "q-002")
    assert [(i.id, i.question) for i in evals.load_items(path)] == [("q-001", "Q1 edited")]
    assert evals.upsert(path, {"question": "Q3"}).id == "q-002"


def test_scoring():
    item = evals.EvalItem("q-001", "费用？", expect=["reg-renewal"], must_include=["$45", "Green Slip"])
    good = Answer("费用 45.00 澳元，需要 green slip", ["reg-renewal#steps"], True, "full")
    assert evals.score(item, good)["passed"]
    wrong = Answer("费用 $50", ["reg-renewal"], True, "full", unverified=["$50"])
    s = evals.score(item, wrong)
    assert s["missing"] == ["$45", "Green Slip"] and not s["checks"]["figures"] and not s["passed"]
    refuse = evals.EvalItem("q-002", "火星车?", should_refuse=True)
    assert evals.score(refuse, Answer("知识库没有这个内容", [], False, "full"))["passed"]
    assert not evals.score(refuse, good)["passed"]


def test_eval_run_from_the_web_page(workspace):
    evals.upsert(workspace.eval_file, {"question": "续期要什么？", "expect": ["reg-renewal"],
                                       "must_include": ["Green Slip"], "added_by": "Zeyu"})
    evals.upsert(workspace.eval_file, {"question": "外州转入？", "expect": ["reg-interstate-transfer"],
                                       "added_by": "Amy"})
    fake = FakeClient(replies=["Check the Green Slip\nSOURCES: reg-renewal", "不知道\nSOURCES: NONE",
                               '{"faithful": true, "answers_question": true, "note": "ok"}'])
    client = TestClient(create_app(build_app(workspace, client=fake)))
    assert client.post("/api/eval/run", json={"retrieval": "full"}).status_code == 200
    for _ in range(100):
        s = client.get("/api/eval/status").json()
        if not s["running"] and s["report"]:
            break
        time.sleep(0.05)
    report = s["report"]
    assert (report["total"], report["passed"], report["mode"]) == (2, 1, "full")
    assert report["rates"]["cited"] == 0.5 and report["rates"]["facts"] == 1.0
    assert list(workspace.eval_reports.glob("*.json"))


def test_judge_adds_its_verdict(workspace):
    app = build_app(workspace, client=FakeClient(replies=[
        "Check the Green Slip\nSOURCES: reg-renewal",
        'Here: {"faithful": false, "answers_question": true, "note": "adds a fee"}']))
    report = evals.run(app.qa, [evals.EvalItem("q-001", "续期？")], judge=True)
    r = report["results"][0]
    assert r["judge"]["note"] == "adds a fee" and r["checks"]["faithful"] is False and not r["passed"]
