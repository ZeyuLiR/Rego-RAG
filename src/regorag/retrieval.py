"""Pick what the model reads for a question.

While a collection is small (the whole of it fits comfortably in the context window),
the model reads every live article: nothing can be missed and prompt caching makes
repeat questions cheap. Above `full_context_token_limit` we switch to keyword (BM25)
retrieval over article sections. A vector index can later be added behind the same
`Retriever` interface without touching the Q&A code.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Protocol

from .kb import Article, Section, estimate_tokens

_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from how i in is it of on or the to what when where which "
    "who why with do does can my your this that need".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP]


@dataclass(frozen=True)
class Context:
    mode: str  # "full" or "bm25"
    text: str  # what goes into the prompt
    cite_ids: tuple[str, ...]  # article ids or section anchors the answer may cite


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
        self._docs = [
            tokenize(" ".join((self.articles[s.article_id].meta.title, *s.heading_path, s.text)))
            for s in self.sections
        ]
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

    def retrieve(self, query: str) -> Context:
        ranked = sorted(zip(self.scores(query), range(len(self.sections))), reverse=True)
        hits = [self.sections[i] for score, i in ranked[: self.top_k] if score > 0]
        parts = []
        for s in hits:
            title = self.articles[s.article_id].meta.title
            parts.append(f"<section id=\"{s.anchor}\" article=\"{title}\" path=\""
                         f"{' > '.join(s.heading_path)}\">\n{s.text}\n</section>")
        return Context(self.mode, "\n\n".join(parts), tuple(s.anchor for s in hits))


def build_retriever(articles: list[Article], full_context_token_limit: int) -> Retriever:
    total = sum(estimate_tokens(a.render_for_model()) for a in articles)
    if total <= full_context_token_limit:
        return FullContextRetriever(articles)
    return BM25Retriever(articles)
