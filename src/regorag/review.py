"""Reviewers and publishing.

Reviewers live in config/reviewers.yaml so adding a person is a config change, not a
code change. Publishing an article stamps who approved it and when; the Git commit
that follows is the audit trail.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .kb import KBError, parse_article


@dataclass(frozen=True)
class Reviewer:
    name: str
    collections: tuple[str, ...] = ("*",)
    # Changes to sensitive categories (fees, legislation, ...) need a senior reviewer.
    senior: bool = False
    github: str | None = None

    def covers(self, collection: str) -> bool:
        return "*" in self.collections or collection in self.collections


@dataclass
class ReviewPolicy:
    reviewers: list[Reviewer]
    sensitive_categories: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, path: Path) -> "ReviewPolicy":
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        reviewers = [
            Reviewer(
                name=r["name"],
                collections=tuple(r.get("collections", ["*"])),
                senior=bool(r.get("senior", False)),
                github=r.get("github"),
            )
            for r in data.get("reviewers", [])
        ]
        return cls(reviewers, set(data.get("sensitive_categories", [])))

    def find(self, name: str) -> Reviewer | None:
        return next((r for r in self.reviewers if r.name.lower() == name.lower()), None)

    def can_approve(self, reviewer_name: str, collection: str, category: str) -> tuple[bool, str]:
        reviewer = self.find(reviewer_name)
        if reviewer is None:
            return False, f"'{reviewer_name}' 不在 config/reviewers.yaml 的审核人名单里"
        if not reviewer.covers(collection):
            return False, f"{reviewer.name} 没有 {collection} 知识库的审核权限"
        if category in self.sensitive_categories and not reviewer.senior:
            return False, f"{category} 属于敏感分类，需要 senior 审核人"
        return True, ""


def publish(path: Path, kb_dir: Path, policy: ReviewPolicy, reviewer_name: str,
            today: dt.date | None = None) -> Path:
    """Mark an article as published by `reviewer_name`. Returns the article path."""
    collection = path.resolve().relative_to(kb_dir.resolve()).parts[0]
    article = parse_article(path, collection)
    if article is None:
        raise KBError(f"{path} 不是知识库文章（缺少 front matter 或 id）")
    ok, why = policy.can_approve(reviewer_name, collection, article.meta.category)
    if not ok:
        raise PermissionError(why)

    raw = path.read_text(encoding="utf-8")
    _, front, body = raw.split("---", 2)
    data = yaml.safe_load(front) or {}
    data["status"] = "published"
    data["reviewed_by"] = policy.find(reviewer_name).name
    data["reviewed_at"] = (today or dt.date.today()).isoformat()
    front = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    path.write_text(f"---\n{front}---{body}", encoding="utf-8")
    return path
