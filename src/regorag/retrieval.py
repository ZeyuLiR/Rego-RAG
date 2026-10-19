"""Pick what the model reads for a question.

While a collection is small (the whole of it fits comfortably in the context window),
the model reads every live article: nothing can be missed and prompt caching makes
repeat questions cheap. Above `full_context_token_limit` we switch to keyword (BM25)
retrieval over article sections.

The mode can also be forced (REGORAG_RETRIEVAL, or per question) to compare approaches:
  auto    full context while it fits, else bm25 (the default)
  full    every live article
  bm25    keyword search over sections
  vector  embedding search over sections (see vectors.py; optional)
  hybrid  bm25 and vector rankings merged with reciprocal rank fusion
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .kb import Article, Section, estimate_tokens
from .vectors import Embedder, VectorSearchUnavailable, VectorStore, cosine

MODES = ("auto", "full", "bm25", "vector", "hybrid")

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from how i in is it of on or the to what when where which "
    "who why with do does can my your this that need".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Context:
    mode: str  # "full", "bm25", "vector" or "hybrid"
    text: str  # what goes into the prompt
    cite_ids: tuple[str, ...]  # article ids or section anchors the answer may cite
    scores: tuple[float, ...] = ()  # one per cite id for section search; empty for full


class Retriever(Protocol):
    mode: str

    def retrieve(self, query: str) -> Context: ...


class FullContextRetriever:
    mode = "full"

    def __init__(self, articles: list[Article]):
        self.articles = sorted(articles, key=lambda a: a.id)  # stable order keeps the cache warm
        self._text = "\n\n".join(a.render_for_model() for a in self.articles)

    def retrieve(self, query: str) -> Context:
        return Context(self.mode, self._text, tuple(a.id for a in self.articles))


class BM25Retriever:
    mode = "bm25"

    def __init__(self, articles: list[Article], k1: float = 1.5, b: float = 0.75, top_k: int = 8):
        self.articles = {a.id: a for a in articles}
        self.sections: list[Section] = [s for a in articles for s in a.sections()]
        self.k1, self.b, self.top_k = k1, b, top_k
        # Title and heading words are indexed with the section text so they can match too.
        self._docs = [tokenize(_section_text(self.articles, s)) for s in self.sections]
        self._tf = [Counter(d) for d in self._docs]
        n = len(self._docs) or 1
        self._avgdl = sum(len(d) for d in self._docs) / n
        df = Counter(t for d in self._docs for t in set(d))
        self._idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def scores(self, query: str) -> list[float]:
        q = tokenize(query)
        out = []
        for tf, doc in zip(self._tf, self._docs):
            dl = len(doc) or 1
            s = 0.0
            for t in q:
                f = tf.get(t)
                if f:
                    s += self._idf[t] * f * (self.k1 + 1) / (
                        f + self.k1 * (1 - self.b + self.b * dl / self._avgdl))
            out.append(s)
        return out

    def ranked(self, query: str) -> list[tuple[float, int]]:
        return [(sc, i) for sc, i in sorted(zip(self.scores(query), range(len(self.sections))),
                                            reverse=True) if sc > 0]

    def retrieve(self, query: str) -> Context:
        return _section_context(self.mode, self.articles, self.sections, self.ranked(query)[: self.top_k])


def _section_text(articles: dict[str, Article], s: Section) -> str:
    return " ".join((articles[s.article_id].meta.title, *s.heading_path, s.text))


def _section_context(mode: str, articles: dict[str, Article], sections: list[Section],
                     hits: list[tuple[float, int]]) -> Context:
    parts = []
    for _, i in hits:
        s = sections[i]
        title = articles[s.article_id].meta.title
        parts.append(f"<section id=\"{s.anchor}\" article=\"{title}\" path=\""
                     f"{' > '.join(s.heading_path)}\">\n{s.text}\n</section>")
    return Context(mode, "\n\n".join(parts), tuple(sections[i].anchor for _, i in hits),
                   tuple(round(sc, 4) for sc, _ in hits))


class VectorRetriever:
    mode = "vector"

    def __init__(self, articles: list[Article], embedder: Embedder, cache_path: Path | None = None,
                 top_k: int = 8, min_score: float = 0.2):
        self.articles = {a.id: a for a in articles}
        self.sections: list[Section] = [s for a in articles for s in a.sections()]
        self.store = VectorStore(cache_path, embedder)
        self.top_k, self.min_score = top_k, min_score
        self._vectors = self.store.vectors_for([_section_text(self.articles, s) for s in self.sections])

    def ranked(self, query: str) -> list[tuple[float, int]]:
        q = self.store.embed_query(query)
        scored = sorted(((cosine(q, v), i) for i, v in enumerate(self._vectors)), reverse=True)
        return [(sc, i) for sc, i in scored if sc >= self.min_score]

    def retrieve(self, query: str) -> Context:
        return _section_context(self.mode, self.articles, self.sections, self.ranked(query)[: self.top_k])


class HybridRetriever:
    """Keyword and vector rankings merged with reciprocal rank fusion (k=60)."""

    mode = "hybrid"

    def __init__(self, bm25: BM25Retriever, vector: VectorRetriever, top_k: int = 8, k: int = 60):
        self.bm25, self.vector, self.top_k, self.k = bm25, vector, top_k, k

    def ranked(self, query: str) -> list[tuple[float, int]]:
        fused: dict[int, float] = {}
        for ranking in (self.bm25.ranked(query), self.vector.ranked(query)):
            for rank, (_, i) in enumerate(ranking):
                fused[i] = fused.get(i, 0.0) + 1 / (self.k + rank + 1)
        return sorted(((sc, i) for i, sc in fused.items()), reverse=True)

    def retrieve(self, query: str) -> Context:
        return _section_context(self.mode, self.bm25.articles, self.bm25.sections,
                                self.ranked(query)[: self.top_k])


def build_retriever(articles: list[Article], full_context_token_limit: int, mode: str = "auto",
                    embedder: Embedder | None = None, cache_path: Path | None = None) -> Retriever:
    if mode not in MODES:
        raise ValueError(f"unknown retrieval mode {mode!r}; use one of {', '.join(MODES)}")
    if mode == "auto":
        total = sum(estimate_tokens(a.render_for_model()) for a in articles)
        mode = "full" if total <= full_context_token_limit else "bm25"
    if mode == "full":
        return FullContextRetriever(articles)
    if mode == "bm25":
        return BM25Retriever(articles)
    if embedder is None:
        raise VectorSearchUnavailable("没有配置向量模型。")
    vector = VectorRetriever(articles, embedder, cache_path)
    return vector if mode == "vector" else HybridRetriever(BM25Retriever(articles), vector)
