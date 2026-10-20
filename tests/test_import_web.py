from dataclasses import replace

from fastapi.testclient import TestClient

from regorag.api import create_app
from regorag.app import build_app
from regorag.ingest import BatchProposal, Topic, ProposedChange, pasted_source_name
from conftest import FakeClient

PROPOSAL = BatchProposal(topics=[Topic(title_zh="测试", source_summary_zh="新增地址证明要求。", changes=[
    ProposedChange(action="new", article_id="reg-proof-of-address", title="Proof of address",
                   category="registration", summary_zh="新文章", evidence=["proof of address"],
                   article_markdown="## What you need\n\nA utility bill."),
    ProposedChange(action="duplicate", article_id="reg-renewal", title="Registration renewal",
                   category="registration", summary_zh="没有新内容", evidence=["renewal"]),
])])


def client_for(settings, proposal=PROPOSAL):
    fake = FakeClient(parsed=proposal)
    api = create_app(build_app(settings, client=fake))
    http = TestClient(api)
    http.fake = fake
    return http, api.state.import_queue


FILES = [("files", ("notice.txt", b"Interstate vehicles need proof of address.", "text/plain")),
         ("files", ("memo.md", b"# Memo\nSame thing.", "text/markdown"))]


def test_files_and_note_are_read_together_then_approved(workspace, tmp_path):
    http, queue = client_for(workspace)
    r = http.post("/api/import", files=FILES,
                  data={"text": "Subject: Address rule\n\nBoth files are about the new address rule. "
                                "Call 0412 345 678.", "collection": "rego"})
    assert r.status_code == 200 and len(r.json()["jobs"]) == 1
    queue.wait()
    jobs = http.get("/api/import/jobs").json()["jobs"]
    assert [(j["name"], j["status"]) for j in jobs] == [("notice.txt + memo.md + 说明", "done")], jobs

    # One request carrying both files and the note, with personal data removed.
    (req,) = http.fake.requests
    content = req["messages"][0]["content"]
    assert content.startswith("<reviewer_note>") and "Both files are about" in content
    assert '<source name="notice.txt">' in content and '<source name="memo.md">' in content
    assert "0412" not in content

    (notice,) = http.get("/api/proposals").json()["proposals"]
    assert notice["source"] == "notice.txt + memo.md" and notice["sources"] == ["notice.txt", "memo.md"]
    assert notice["name"].endswith("-address-rule")
    folder = workspace.inbox_dir / notice["name"]
    assert (folder / "originals" / "notice.txt").is_file() and (folder / "originals" / "memo.md").is_file()
    assert "Both files are about" in (folder / "REVIEW.md").read_text()
    draft = notice["changes"][0]["draft"]
    assert "status: draft" in draft and "- memo.md" in draft and "- notice.txt" in draft

    edited = draft.replace("A utility bill.", "A utility bill or bank statement.")
    assert http.put(f"/api/proposals/{notice['name']}/drafts/reg-proof-of-address",
                    json={"content": edited}).status_code == 200

    r = http.post(f"/api/proposals/{notice['name']}/apply", json={"reviewer": "Zeyu"})
    assert r.status_code == 200, r.text
    assert r.json()["published"] == ["rego/registration/reg-proof-of-address.md"]
    published = (workspace.kb_dir / "rego/registration/reg-proof-of-address.md").read_text()
    assert "bank statement" in published and "reviewed_by: Zeyu" in published
    assert (workspace.inbox_dir / "_applied" / notice["name"]).is_dir()
    assert http.get("/api/proposals").json()["proposals"] == []
    assert http.get("/api/health").json()["collections"]["rego"] == 3  # KB reloaded


def test_separate_option_makes_one_proposal_per_item(workspace):
    http, queue = client_for(workspace)
    r = http.post("/api/import", files=FILES,
                  data={"text": "Subject: Address rule\n\nCall 0412 345 678.", "collection": "rego",
                        "separate": "true"})
    assert len(r.json()["jobs"]) == 3
    queue.wait()
    assert {j["name"] for j in http.get("/api/import/jobs").json()["jobs"]} == {
        "notice.txt", "memo.md", "address-rule.txt"}
    pasted = next(p for p in http.get("/api/proposals").json()["proposals"] if p["source"] == "address-rule.txt")
    assert "0412" not in (workspace.inbox_dir / pasted["name"] / "source.md").read_text()
    assert all("<reviewer_note>" not in r["messages"][0]["content"] for r in http.fake.requests)


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


def test_different_matters_in_one_batch_become_separate_reviews(workspace):
    def change(aid, title):
        return ProposedChange(action="new", article_id=aid, title=title, category="registration",
                              summary_zh=title, evidence=["x"], article_markdown="## Steps\n\n1. Do it.")
    two = BatchProposal(topics=[
        Topic(title_zh="注销流程", source_summary_zh="de-rego", source_files=["notice.txt"],
              changes=[change("reg-deregistration", "De-registration")]),
        Topic(title_zh="地址证明", source_summary_zh="address", source_files=["memo.md"],
              changes=[change("reg-proof-of-address", "Proof of address")]),
    ])
    http, queue = client_for(workspace, two)
    http.post("/api/import", files=FILES, data={"collection": "rego"})
    queue.wait()
    (job,) = http.get("/api/import/jobs").json()["jobs"]
    assert len(job["proposals"]) == 2 and job["changes"] == 2
    assert len(http.fake.requests) == 1  # still read together in one call

    props = {p["topic"]: p for p in http.get("/api/proposals").json()["proposals"]}
    assert set(props) == {"注销流程", "地址证明"}
    dereg = props["注销流程"]
    assert dereg["sources"] == ["notice.txt"] and dereg["name"].endswith("-reg-deregistration")
    folder = workspace.inbox_dir / dereg["name"]
    assert [p.name for p in (folder / "originals").iterdir()] == ["notice.txt"]
    assert "memo.md" not in (folder / "source.md").read_text()

    # Approving one matter leaves the other waiting for review.
    assert http.post(f"/api/proposals/{dereg['name']}/apply", json={"reviewer": "Zeyu"}).status_code == 200
    assert [p["topic"] for p in http.get("/api/proposals").json()["proposals"]] == ["地址证明"]
