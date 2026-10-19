import datetime as dt

from regorag.kb import load_kb
from regorag.retrieval import BM25Retriever, FullContextRetriever, build_retriever


def live(workspace):
    return load_kb(workspace.kb_dir).live("rego", today=dt.date(2026, 10, 19))


def test_small_kb_uses_full_context(workspace):
    r = build_retriever(live(workspace), full_context_token_limit=150_000)
    assert isinstance(r, FullContextRetriever)
    ctx = r.retrieve("anything")
    assert set(ctx.cite_ids) == {"reg-interstate-transfer", "reg-renewal"}
    assert "<article id=\"reg-renewal\"" in ctx.text


def test_large_kb_switches_to_bm25(workspace):
    assert isinstance(build_retriever(live(workspace), full_context_token_limit=10), BM25Retriever)


def test_bm25_ranks_matching_section_first(workspace):
    ctx = BM25Retriever(live(workspace)).retrieve("blue slip interstate")
    assert ctx.mode == "bm25"
    assert ctx.cite_ids[0] == "reg-interstate-transfer#what-you-need"


def test_bm25_returns_nothing_for_unrelated_query(workspace):
    assert BM25Retriever(live(workspace)).retrieve("boat licence").cite_ids == ()
