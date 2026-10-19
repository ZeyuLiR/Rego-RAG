"""Vector (embedding) search over article sections. Optional: off unless REGORAG_RETRIEVAL
is `vector` or `hybrid`, or a question asks for it.

The embedding model runs locally (fastembed, ONNX on CPU, no API cost). The default model is
multilingual, so a Chinese question can match English text by meaning even without the
English rewrite. Vectors are cached in data/vectors/<collection>.json, keyed by a hash of
each section's text, so only new or changed sections are embedded again after publishing.
At this size (tens of articles, a few hundred sections) a JSON file and a linear scan are
the vector database; a server such as LanceDB or pgvector can go behind `VectorStore` later
without touching retrieval or Q&A.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Protocol

DEFAULT_EMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class VectorSearchUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """Local embedding model. The model files (~220 MB) download on first use."""

    def __init__(self, model: str = DEFAULT_EMBED_MODEL, cache_dir: Path | None = None):
        self.name = model
        self.cache_dir = cache_dir
        self._model = None

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self._model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as exc:
                raise VectorSearchUnavailable(
                    "向量检索需要先安装：.venv/bin/pip install -e \".[vector]\"") from exc
            try:
                self._model = TextEmbedding(self.name, cache_dir=str(self.cache_dir) if self.cache_dir else None)
            except Exception as exc:  # download or model load failure
                raise VectorSearchUnavailable(f"无法加载向量模型 {self.name}：{exc}") from exc
        return [list(map(float, v)) for v in self._model.embed(texts)]


def _norm(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class VectorStore:
    """Normalised vectors for one collection, persisted as JSON."""

    def __init__(self, path: Path | None, embedder: Embedder):
        self.path = path
        self.embedder = embedder

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def _load(self) -> dict[str, list[float]]:
        if not self.path or not self.path.is_file():
            return {}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data["vectors"] if data.get("model") == self.embedder.name else {}

    def vectors_for(self, texts: list[str]) -> list[list[float]]:
        """Return a vector per text, embedding only the ones not cached yet."""
        cached = self._load()
        keys = [self.key(t) for t in texts]
        missing = [(k, t) for k, t in dict(zip(keys, texts)).items() if k not in cached]
        if missing:
            new = self.embedder.embed([t for _, t in missing])
            cached.update({k: _norm(v) for (k, _), v in zip(missing, new)})
        keep = {k: cached[k] for k in keys}  # drop sections that no longer exist
        if self.path and (missing or len(keep) != len(cached)):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"model": self.embedder.name, "vectors": keep}),
                                 encoding="utf-8")
        return [keep[k] for k in keys]

    def embed_query(self, text: str) -> list[float]:
        return _norm(self.embedder.embed([text])[0])


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
