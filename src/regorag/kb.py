"""Load the knowledge base: Markdown articles with a YAML front-matter header.

Layout: kb/<collection>/<category>/<article>.md. The Markdown is the single source
of truth; everything the AI reads (sections, chunks, indexes) is derived from it.
Files whose name starts with "_" (templates) and files with no `id` in their
front matter (landing pages such as index.md) are not articles.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

Status = Literal["draft", "published", "archived"]

_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ArticleMeta(BaseModel):
    id: str
    title: str
    category: str
    status: Status = "draft"
    tags: list[str] = Field(default_factory=list)
    state: str | None = None
    effective_from: dt.date | None = None
    review_by: dt.date | None = None
    owner: str | None = None
    sources: list[str] = Field(default_factory=list)
    version: int = 1
    reviewed_by: str | None = None
    reviewed_at: dt.date | None = None


@dataclass(frozen=True)
class Section:
    article_id: str
    anchor: str  # "article-id#heading-path", used in citations
    heading_path: tuple[str, ...]
    text: str


@dataclass(frozen=True)
class Article:
    meta: ArticleMeta
    collection: str
    path: Path
    body: str

    @property
    def id(self) -> str:
        return self.meta.id

    def is_live(self, today: dt.date | None = None) -> bool:
        """Published and already in effect."""
        today = today or dt.date.today()
        if self.meta.status != "published":
            return False
        return self.meta.effective_from is None or self.meta.effective_from <= today

    def is_overdue_for_review(self, today: dt.date | None = None) -> bool:
        today = today or dt.date.today()
        return self.meta.review_by is not None and self.meta.review_by < today

    def sections(self) -> list[Section]:
        """Split the body on headings; each section keeps its full heading path."""
        sections: list[Section] = []
        path: list[tuple[int, str]] = []  # (heading level, heading text)
        buf: list[str] = []

        def flush() -> None:
            text = "\n".join(buf).strip()
            if text:
                heading_path = tuple(h for _, h in path) or (self.meta.title,)
                anchor = f"{self.id}#{slugify(heading_path[-1])}"
                sections.append(Section(self.id, anchor, heading_path, text))
            buf.clear()

        in_code = False
        for line in self.body.splitlines():
            if line.lstrip().startswith("```"):
                in_code = not in_code
            match = None if in_code else _HEADING.match(line)
            if match:
                flush()
                level = len(match.group(1))
                while path and path[-1][0] >= level:
                    path.pop()
                path.append((level, match.group(2)))
            else:
                buf.append(line)
        flush()
        return sections

    def render_for_model(self) -> str:
        """The article as the model sees it: metadata the answer may need, then the body."""
        m = self.meta
        header = [f"<article id=\"{m.id}\" collection=\"{self.collection}\" category=\"{m.category}\">",
                  f"title: {m.title}"]
        if m.effective_from:
            header.append(f"effective_from: {m.effective_from.isoformat()}")
        if m.sources:
            header.append(f"sources: {', '.join(m.sources)}")
        return "\n".join(header) + "\n\n" + self.body.strip() + "\n</article>"


class KBError(Exception):
    pass


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    return re.sub(r"[\s_]+", "-", text).strip("-") or "section"


def parse_article(path: Path, collection: str) -> Article | None:
    """Parse one file. Returns None for non-articles; raises KBError for broken articles."""
    raw = path.read_text(encoding="utf-8")
    match = _FRONT_MATTER.match(raw)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise KBError(f"{path}: invalid YAML front matter: {exc}") from exc
    if not isinstance(data, dict) or "id" not in data:
        return None
    try:
        meta = ArticleMeta.model_validate(data)
    except ValidationError as exc:
        raise KBError(f"{path}: {exc}") from exc
    if not _ID.match(meta.id):
        raise KBError(f"{path}: id '{meta.id}' must be lowercase letters, digits and dashes")
    return Article(meta=meta, collection=collection, path=path, body=match.group(2))


@dataclass
class KnowledgeBase:
    articles: list[Article]

    def collection(self, name: str) -> list[Article]:
        return [a for a in self.articles if a.collection == name]

    def live(self, collection: str, today: dt.date | None = None) -> list[Article]:
        return [a for a in self.collection(collection) if a.is_live(today)]

    def get(self, article_id: str) -> Article | None:
        return next((a for a in self.articles if a.id == article_id), None)

    @property
    def collections(self) -> list[str]:
        return sorted({a.collection for a in self.articles})


def load_kb(kb_dir: Path) -> KnowledgeBase:
    """Load every article; raise KBError listing all problems (duplicate ids included)."""
    articles: list[Article] = []
    errors: list[str] = []
    for path in sorted(kb_dir.rglob("*.md")):
        rel = path.relative_to(kb_dir)
        if len(rel.parts) < 2 or path.name.startswith("_"):
            continue
        try:
            article = parse_article(path, collection=rel.parts[0])
        except KBError as exc:
            errors.append(str(exc))
            continue
        if article:
            articles.append(article)

    seen: dict[str, Path] = {}
    for article in articles:
        if article.id in seen:
            errors.append(f"duplicate id '{article.id}': {seen[article.id]} and {article.path}")
        seen[article.id] = article.path
    if errors:
        raise KBError("\n".join(errors))
    return KnowledgeBase(articles)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (no API call): ~4 chars per token for English, ~1 per CJK char."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    return cjk + (len(text) - cjk) // 4 + 1
