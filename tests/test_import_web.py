from dataclasses import replace

from fastapi.testclient import TestClient

from regorag.api import create_app
from regorag.app import build_app
from regorag.ingest import Proposal, ProposedChange, pasted_source_name
from conftest import FakeClient

PROPOSAL = Proposal(source_summary_zh="新增地址证明要求。", changes=[
    ProposedChange(action="new", article_id="reg-proof-of-address", title="Proof of address",
                   category="registration", summary_zh="新文章", evidence=["proof of address"],
                   article_markdown="## What you need\n\nA utility bill."),
    ProposedChange(action="duplicate", article_id="reg-renewal", title="Registration renewal",
                   category="registration", summary_zh="没有新内容", evidence=["renewal"]),
])


def client_for(settings, proposal=PROPOSAL):
    api = create_app(build_app(settings, client=FakeClient(parsed=proposal)))
    return TestClient(api), api.state.import_queue


def test_drag_files_and_paste_text_then_approve(workspace, tmp_path):
    http, queue = client_for(workspace)
    r = http.post("/api/import",
                  files=[("files", ("notice.txt", b"Interstate vehicles need proof of address.", "text/plain")),
                         ("files", ("memo.md", b"# Memo\nSame thing.", "text/markdown"))],
                  data={"text": "Subject: Address rule\n\nCall 0412 345 678 about proof of address.",
                        "collection": "rego"})
    assert r.status_code == 200 and len(r.json()["jobs"]) == 3
    queue.wait()
    jobs = http.get("/api/import/jobs").json()["jobs"]
    assert {j["status"] for j in jobs} == {"done"}, jobs
    assert {j["name"] for j in jobs} == {"notice.txt", "memo.md", "address-rule.txt"}

    props = http.get("/api/proposals").json()["proposals"]
    assert len(props) == 3
    pasted = next(p for p in props if p["source"] == "address-rule.txt")
    assert "0412" not in (workspace.inbox_dir / pasted["name"] / "source.md").read_text()
    notice = next(p for p in props if p["source"] == "notice.txt")
    assert (workspace.inbox_dir / notice["name"] / "original.txt").is_file()
    draft = notice["changes"][0]["draft"]
    assert "status: draft" in draft

    edited = draft.replace("A utility bill.", "A utility bill or bank statement.")
    assert http.put(f"/api/proposals/{notice['name']}/drafts/reg-proof-of-address",
                    json={"content": edited}).status_code == 200

    r = http.post(f"/api/proposals/{notice['name']}/apply", json={"reviewer": "Zeyu"})
    assert r.status_code == 200, r.text
    assert r.json()["published"] == ["rego/registration/reg-proof-of-address.md"]
    published = (workspace.kb_dir / "rego/registration/reg-proof-of-address.md").read_text()
    assert "bank statement" in published and "reviewed_by: Zeyu" in published
    assert (workspace.inbox_dir / "_applied" / notice["name"]).is_dir()
    assert len(http.get("/api/proposals").json()["proposals"]) == 2
    assert http.get("/api/health").json()["collections"]["rego"] == 3  # KB reloaded


def test_reject_archives_proposal(workspace):
    http, queue = client_for(workspace)
    http.post("/api/import", data={"text": "Some email", "collection": "rego"})
    queue.wait()
    name = http.get("/api/proposals").json()["proposals"][0]["name"]
    assert http.post(f"/api/proposals/{name}/reject").status_code == 200
    assert http.get("/api/proposals").json()["proposals"] == []
    assert (workspace.inbox_dir / "_rejected" / name / "proposal.json").is_file()


def test_unknown_reviewer_publishes_nothing(workspace):
    http, queue = client_for(workspace)
    http.post("/api/import", data={"text": "Some email", "collection": "rego"})
    queue.wait()
    name = http.get("/api/proposals").json()["proposals"][0]["name"]
    r = http.post(f"/api/proposals/{name}/apply", json={"reviewer": "Mallory"})
    assert r.status_code == 403
    assert not (workspace.kb_dir / "rego/registration/reg-proof-of-address.md").exists()


def test_rejects_unsupported_files_and_empty_requests(workspace):
    http, _ = client_for(workspace)
    assert http.post("/api/import", files=[("files", ("x.exe", b"MZ", "application/octet-stream"))]).status_code == 400
    assert http.post("/api/import", data={"text": "  "}).status_code == 400


def test_path_traversal_is_refused(workspace):
    http, _ = client_for(workspace)
    assert http.post("/api/proposals/..%2Fkb/reject").status_code == 404
    assert http.post("/api/proposals/_applied/reject").status_code == 404


def test_admin_token(workspace):
    http, _ = client_for(replace(workspace, admin_token="s3cret"))
    assert http.get("/api/proposals").status_code == 401
    assert http.get("/api/proposals", headers={"X-Admin-Token": "wrong"}).status_code == 401
    assert http.get("/api/proposals", headers={"X-Admin-Token": "s3cret"}).status_code == 200
    assert http.get("/import").status_code == 200  # the page itself loads and asks for the token
    assert http.post("/api/ask", json={"question": "q"}).status_code != 401  # clerks are unaffected


def test_pasted_source_name():
    assert pasted_source_name("From: x\nSubject: New CTP rules 2026\n\nbody") == "new-ctp-rules-2026.txt"
    assert pasted_source_name("主题：外州转入\n正文").endswith(".txt")
    assert pasted_source_name("no subject here").startswith("pasted-")
