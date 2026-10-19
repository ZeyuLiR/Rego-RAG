"""Wire settings, knowledge base, usage log and services together."""

from __future__ import annotations

from dataclasses import dataclass

from .collections import Collection, load_collections
from .config import Settings
from .kb import KnowledgeBase, load_kb
from .llm import LLM
from .qa import QAService
from .review import ReviewPolicy
from .usage import UsageLog
from .vectors import Embedder, FastEmbedder


@dataclass
class App:
    settings: Settings
    kb: KnowledgeBase
    collections: dict[str, Collection]
    policy: ReviewPolicy
    usage: UsageLog
    llm: LLM
    qa: QAService


def build_app(settings: Settings | None = None, client=None, embedder: Embedder | None = None) -> App:
    settings = settings or Settings.from_env()
    kb = load_kb(settings.kb_dir)
    usage = UsageLog(settings.usage_log, settings.monthly_budget_usd)
    llm = LLM(usage, client=client, backend=settings.llm_backend)
    qa = QAService(kb=kb, llm=llm, model=settings.qa_model, effort=settings.qa_effort,
                   full_context_token_limit=settings.full_context_token_limit,
                   gaps_log=settings.usage_log.parent / "gaps.jsonl",
                   retrieval=settings.retrieval,
                   # Loaded lazily: nothing is downloaded unless vector or hybrid mode is used.
                   embedder=embedder or FastEmbedder(settings.embed_model, settings.vector_dir / "models"),
                   vector_dir=settings.vector_dir)
    return App(
        settings=settings,
        kb=kb,
        collections=load_collections(settings.config_dir, settings.root),
        policy=ReviewPolicy.load(settings.config_dir / "reviewers.yaml"),
        usage=usage,
        llm=llm,
        qa=qa,
    )
