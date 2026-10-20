import pytest
from fastapi.testclient import TestClient

from regorag.api import create_app
from regorag.app import build_app
from regorag.ingest import BatchProposal, Topic, ProposedChange
from regorag.kb import KBError, load_kb, safe_file_name
from conftest import FakeClient, article

TEMPLATE = b"PK fake docx bytes"


def test_safe_file_name():
    assert safe_file_name("DEREGO LETTER - template.docx") == "derego-letter-template.docx"
    assert safe_file_name("注销 信.DOCX") == "注销-信.docx"


def test_template_is_stored_on_approval_and_offered_in_answers(workspace):
    proposal = BatchProposal(topics=[Topic(title_zh="测试", source_summary_zh="de-rego 流程和信件模板。", changes=[ProposedChange(
        action="new", article_id="reg-deregistration", title="De-registration", category="registration",
        summary_zh="新文章", evidence=["cancel the registration"],
        source_files=["process.txt", "DEREGO LETTER - template.docx"],
        attach_files=["DEREGO LETTER - template.docx", "not-uploaded.docx"],
        article_markdown="## Steps\n\n1. Send the de-registration letter template.")])])
    fake = FakeClient(parsed=proposal, replies=[
        "用这个模板 [[file:derego-letter-template.docx]]，别用 [[file:secret.docx]] [[reg-deregistration]]\n"
        "SOURCES: reg-deregistration"])
    api = create_app(build_app(workspace, client=fake))
    http = TestClient(api)
    http.post("/api/import", data={"text": "这两份都是 de-rego 用的", "collection": "rego"},
              files=[("files", ("process.txt", b"To cancel the registration...", "text/plain")),
                     ("files", ("DEREGO LETTER - template.docx", TEMPLATE, "application/octet-stream"))])
    api.state.import_queue.wait()
    (p,) = http.get("/api/proposals").json()["proposals"]
    draft = p["changes"][0]["draft"]
    assert "file: derego-letter-template.docx" in draft and "not-uploaded" not in draft

    # Not published yet: nothing is served.
    assert http.get("/files/rego/derego-letter-template.docx").status_code == 404

    r = http.post(f"/api/proposals/{p['name']}/apply", json={"reviewer": "Zeyu"})
    assert r.status_code == 200, r.text
    stored = workspace.kb_dir / "rego" / "files" / "derego-letter-template.docx"
    assert stored.read_bytes() == TEMPLATE
    load_kb(workspace.kb_dir)  # still valid

    body = http.post("/api/ask", json={"question": "de-rego 信件模板有吗？"}).json()
    assert body["files"] == {"derego-letter-template.docx": {
        "title": "DEREGO LETTER - template", "url": "/files/rego/derego-letter-template.docx"}}
    kb_text = fake.requests[-1]["system"][1]["text"]
    assert "attachment: [[file:derego-letter-template.docx]] DEREGO LETTER - template" in kb_text
    download = http.get(body["files"]["derego-letter-template.docx"]["url"])
    assert download.status_code == 200 and download.content == TEMPLATE
    assert http.get("/files/rego/secret.docx").status_code == 404
    assert http.get("/files/rego/..%2F..%2Fconfig%2Freviewers.yaml").status_code == 404


def test_missing_attachment_file_is_a_kb_error(workspace):
    (workspace.kb_dir / "rego" / "registration" / "x.md").write_text(article(
        "reg-x", "X", attachments="[{file: missing.pdf}]"), encoding="utf-8")
    with pytest.raises(KBError, match="missing.pdf not found"):
        load_kb(workspace.kb_dir)


def test_attachment_name_cannot_leave_files_dir(workspace):
    (workspace.kb_dir / "rego" / "registration" / "x.md").write_text(article(
        "reg-x", "X", attachments="[{file: ../../secret.txt}]"), encoding="utf-8")
    with pytest.raises(KBError, match="plain file name"):
        load_kb(workspace.kb_dir)


def test_reviewer_ticks_attachment_and_cited_article_offers_it(workspace):
    # The model did not mark the template; the reviewer ticks it on the page.
    proposal = BatchProposal(topics=[Topic(title_zh="注销", source_summary_zh="de-rego", changes=[ProposedChange(
        action="new", article_id="reg-deregistration", title="De-registration", category="registration",
        summary_zh="新文章", evidence=["x"], article_markdown="## Steps\n\n1. Send the letter.")])])
    # The answer cites the article but does not write a [[file:]] marker.
    fake = FakeClient(parsed=proposal, replies=["寄信给客户。\nSOURCES: reg-deregistration"])
    api = create_app(build_app(workspace, client=fake))
    http = TestClient(api)
    http.post("/api/import", data={"collection": "rego"},
              files=[("files", ("process.txt", b"De-rego steps", "text/plain")),
                     ("files", ("Letter Template.docx", TEMPLATE, "application/octet-stream"))])
    api.state.import_queue.wait()
    (p,) = http.get("/api/proposals").json()["proposals"]
    assert p["originals"] == ["Letter Template.docx", "process.txt"]
    assert p["changes"][0]["attached"] == []

    r = http.post(f"/api/proposals/{p['name']}/apply", json={
        "reviewer": "Zeyu", "attachments": {"reg-deregistration": ["Letter Template.docx"]}})
    assert r.status_code == 200, r.text
    assert (workspace.kb_dir / "rego" / "files" / "letter-template.docx").read_bytes() == TEMPLATE

    body = http.post("/api/ask", json={"question": "注销怎么做"}).json()
    assert body["files"] == {"letter-template.docx": {
        "title": "Letter Template", "url": "/files/rego/letter-template.docx"}}
