import datetime as dt

import pytest

from regorag.kb import KBError, estimate_tokens, load_kb
from conftest import article


def test_loads_articles_and_skips_templates_and_landing_pages(workspace):
    kb = load_kb(workspace.kb_dir)
    ids = {a.id for a in kb.articles}
    assert {"reg-interstate-transfer", "reg-renewal", "rego-glossary"} <= ids
    assert "replace-with-lowercase-dashed-id" not in ids  # _template.md
    assert kb.get("reg-renewal").collection == "rego"


def test_live_excludes_drafts_and_future_rules(workspace):
    kb = load_kb(workspace.kb_dir)
    live = {a.id for a in kb.live("rego", today=dt.date(2026, 10, 19))}
    assert live == {"reg-interstate-transfer", "reg-renewal"}  # glossary is still a draft


def test_sections_keep_heading_path_and_anchor(workspace):
    art = load_kb(workspace.kb_dir).get("reg-interstate-transfer")
    secs = art.sections()
    assert [s.heading_path for s in secs] == [("When this applies",), ("What you need",), ("Fees",)]
    assert secs[1].anchor == "reg-interstate-transfer#what-you-need"
    assert "Blue Slip" in secs[1].text


def test_nested_headings(tmp_path):
    d = tmp_path / "kb" / "rego" / "x"
    d.mkdir(parents=True)
    (d / "a.md").write_text(article("a", "A", body="## One\n\ntext1\n\n### Sub\n\ntext2\n\n## Two\n\ntext3"))
    secs = load_kb(tmp_path / "kb").get("a").sections()
    assert [s.heading_path for s in secs] == [("One",), ("One", "Sub"), ("Two",)]


def test_duplicate_ids_and_bad_metadata_are_reported(tmp_path):
    d = tmp_path / "kb" / "rego" / "x"
    d.mkdir(parents=True)
    (d / "a.md").write_text(article("dup", "A"))
    (d / "b.md").write_text(article("dup", "B"))
    (d / "c.md").write_text(article("bad", "C", status="maybe"))
    with pytest.raises(KBError) as exc:
        load_kb(tmp_path / "kb")
    assert "duplicate id 'dup'" in str(exc.value)
    assert "c.md" in str(exc.value)


def test_estimate_tokens_counts_cjk():
    assert estimate_tokens("a" * 400) == 101
    assert estimate_tokens("注册") == 3
