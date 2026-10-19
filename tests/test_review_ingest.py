import datetime as dt
import json
from pathlib import Path

import pytest

from regorag.app import build_app
from regorag.ingest import Proposal, ProposedChange, apply_proposal, propose, redact
from regorag.kb import load_kb
from regorag.review import Reviewer, ReviewPolicy, publish
from conftest import FakeClient


def test_reviewer_rules():
    policy = ReviewPolicy([Reviewer("Zeyu", ("*",), senior=True), Reviewer("Amy", ("rego",))],
                          sensitive_categories={"fees"})
    assert policy.can_approve("zeyu", "sales", "fees")[0]
    assert policy.can_approve("Amy", "rego", "registration")[0]
    assert not policy.can_approve("Amy", "rego", "fees")[0]
    assert not policy.can_approve("Amy", "sales", "products")[0]
    assert not policy.can_approve("Bob", "rego", "faq")[0]


def test_publish_stamps_reviewer(workspace):
    path = workspace.kb_dir / "rego" / "glossary" / "glossary.md"
    publish(path, workspace.kb_dir, ReviewPolicy.load(workspace.config_dir / "reviewers.yaml"),
            "Zeyu", today=dt.date(2026, 10, 19))
    meta = load_kb(workspace.kb_dir).get("rego-glossary").meta
    assert meta.status == "published" and meta.reviewed_by == "Zeyu"
    assert meta.reviewed_at == dt.date(2026, 10, 19)
    assert "| Registration (rego) |" in path.read_text(encoding="utf-8")


def test_redact_removes_personal_data():
    text = "Call 0412 345 678 or email jo@example.com, VIN 1HGCM82633A004352, licence no 12345678."
    out = redact(text)
    assert "0412" not in out and "jo@example.com" not in out and "1HGCM82633A004352" not in out
    assert "12345678" not in out


def test_import_then_apply_round_trip(workspace, tmp_path):
    src = tmp_path / "notice.txt"
    src.write_text("From 1 July 2026 interstate vehicles also need proof of address. Contact jo@x.com")
    proposal = Proposal(source_summary_zh="外州转入新增地址证明要求。", changes=[
        ProposedChange(action="update", article_id="reg-interstate-transfer",
                       title="Interstate vehicle transfer", category="registration",
                       summary_zh="材料清单加上地址证明。", effective_from="2026-07-01",
                       evidence=["interstate vehicles also need proof of address"],
                       article_markdown="## What you need\n\n- Blue Slip\n- Proof of identity\n- Proof of address"),
        ProposedChange(action="new", article_id="reg-proof-of-address", title="Proof of address",
                       category="registration", summary_zh="新文章", evidence=["proof of address"],
                       article_markdown="## What you need\n\nA utility bill."),
    ])
    client = FakeClient(parsed=proposal)
    app = build_app(workspace, client=client)
    coll = app.collections["rego"]
    folder = propose(src, app.kb, "rego", app.llm, "claude-opus-5-5", coll.template(),
                     list(coll.categories), workspace.inbox_dir, today=dt.date(2026, 10, 19))

    assert "jo@x.com" not in (folder / "source.md").read_text()
    assert "jo@x.com" not in client.requests[0]["messages"][0]["content"]
    review = (folder / "REVIEW.md").read_text()
    assert "材料清单加上地址证明" in review and "> interstate vehicles also need proof of address" in review
    draft = (folder / "drafts" / "reg-interstate-transfer.md").read_text()
    assert "status: draft" in draft and "version: 2" in draft

    written = apply_proposal(folder, app.kb, workspace.kb_dir, app.policy, "Zeyu")
    assert len(written) == 2
    kb = load_kb(workspace.kb_dir)
    updated = kb.get("reg-interstate-transfer")
    assert updated.meta.status == "published" and updated.meta.version == 2
    assert "Proof of address" in updated.body
    assert kb.get("reg-proof-of-address").path == workspace.kb_dir / "rego" / "registration" / "reg-proof-of-address.md"


def test_apply_refuses_unknown_reviewer(workspace, tmp_path):
    folder = tmp_path / "p"
    (folder / "drafts").mkdir(parents=True)
    (folder / "proposal.json").write_text(json.dumps({"collection": "rego", "changes": [
        {"action": "new", "article_id": "x", "category": "faq"}]}))
    (folder / "drafts" / "x.md").write_text("---\nid: x\ntitle: X\ncategory: faq\n---\nbody")
    app = build_app(workspace, client=FakeClient())
    with pytest.raises(PermissionError):
        apply_proposal(folder, app.kb, workspace.kb_dir, app.policy, "Mallory")
