"""Turn an uploaded email / PDF / Word file into proposed knowledge base changes.

The model never edits the knowledge base. It writes a proposal folder under inbox/:

    inbox/<date>-<source-name>/
        REVIEW.md        what changes, why, and the evidence quotes (for the reviewer)
        proposal.json    the same, machine-readable
        source.md        the redacted text the model saw
        drafts/<id>.md   the full proposed article for every new or updated article

The reviewer edits the drafts if needed, then `regorag apply <folder> --reviewer NAME`
copies them into kb/ and publishes them.
"""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import json
import re
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .kb import KnowledgeBase, slugify
from .llm import LLM
from .review import ReviewPolicy, publish

# ---------------------------------------------------------------- reading files


def read_document(path: Path) -> str:
    """Plain text / Markdown for any supported file."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".eml":
        return _read_eml(path)
    from markitdown import MarkItDown  # imported lazily: heavy, and not needed for Q&A

    return MarkItDown().convert(str(path)).text_content


def _read_eml(path: Path) -> str:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body else ""
    if body is not None and body.get_content_type() == "text/html":
        text = re.sub(r"<[^>]+>", " ", text)
    # Drop quoted earlier messages, which repeat old (possibly outdated) content.
    text = re.split(r"\n(?:-----Original Message-----|From: .+\nSent: |On .+ wrote:)", text)[0]
    parts = [f"Subject: {msg.get('subject', '')}", f"Date: {msg.get('date', '')}", "", text.strip()]
    for att in msg.iter_attachments():
        name = att.get_filename() or "attachment"
        parts.append(f"\n[Attachment: {name}]")
        if att.get_content_type().startswith("text/"):
            parts.append(att.get_content())
    return "\n".join(parts)


# ---------------------------------------------------------------- redaction

_REDACTIONS = [
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?61\s?|0)[2-478](?:[\s-]?\d){8}(?!\d)"), "[PHONE]"),
    (re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b"), "[VIN]"),
    (re.compile(r"(?i)\b(licen[cs]e|customer|client)\s*(?:no\.?|number|#)?\s*:?\s*\d{5,10}\b"),
     r"\1 [NUMBER]"),
]


def redact(text: str) -> str:
    """Remove obvious personal data before text leaves the machine.

    This catches common patterns only; reviewers still check source.md.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------- extraction


class ProposedChange(BaseModel):
    action: Literal["new", "update", "retire", "duplicate", "conflict"]
    article_id: str = Field(description="Existing article id for update/retire/duplicate/conflict; "
                                        "a new lowercase-dashed id for new")
    title: str
    category: str
    summary_zh: str = Field(description="One or two sentences in Chinese for the reviewer")
    effective_from: str | None = Field(default=None, description="YYYY-MM-DD if the source states it")
    evidence: list[str] = Field(description="Verbatim quotes from the source supporting the change")
    article_markdown: str | None = Field(
        default=None,
        description="For new/update: the complete article body in Markdown (no front matter), "
                    "following the article template")


class Proposal(BaseModel):
    source_summary_zh: str
    changes: list[ProposedChange]


EXTRACT_PROMPT = """\
You maintain a reviewed knowledge base for rego (vehicle registration) clerks in NSW, Australia. \
A new source document has arrived (an email, notice, PDF or Word file). Work out what, if \
anything, it changes in the knowledge base, and propose those changes for a human reviewer.

Rules:
- Only use what the source says. Every change needs verbatim evidence quotes from the source.
- Prefer updating an existing article over creating a new one. Use "duplicate" when the source \
adds nothing new, and "conflict" when it contradicts an article in a way you cannot resolve \
(explain in summary_zh).
- For "new" and "update", write the complete article body in English, keeping the template's \
section order: When this applies, What you need, Steps, Fees, Exceptions and notes, Sources. \
For an update, keep everything in the existing article that the source does not change.
- Keep fees, dates and form numbers exactly as written. Never include personal details about \
customers.
- If the source contains nothing relevant to rego work, return no changes.
- Write summary_zh and source_summary_zh in Chinese."""


def propose(source_path: Path, kb: KnowledgeBase, collection: str, llm: LLM, model: str,
            template: str, categories: list[str], inbox_dir: Path,
            today: dt.date | None = None) -> Path:
    """Read, redact and extract one file; write the proposal folder and return it."""
    today = today or dt.date.today()
    text = redact(read_document(source_path))
    existing = "\n\n".join(a.render_for_model() for a in kb.collection(collection))
    response = llm.parse(
        "extract",
        model=model,
        max_tokens=32000,
        output_format=Proposal,
        system=[
            {"type": "text", "text": EXTRACT_PROMPT},
            {"type": "text", "text": f"<categories>{', '.join(categories)}</categories>\n"
                                     f"<article_template>\n{template}\n</article_template>\n"
                                     f"<knowledge_base collection=\"{collection}\">\n{existing}\n"
                                     f"</knowledge_base>",
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content":
                   f"<source name=\"{source_path.name}\">\n{text}\n</source>"}],
    )
    proposal: Proposal = response.parsed_output
    return write_proposal(proposal, source_path, text, kb, collection, inbox_dir, today)


def write_proposal(proposal: Proposal, source_path: Path, source_text: str, kb: KnowledgeBase,
                   collection: str, inbox_dir: Path, today: dt.date) -> Path:
    folder = inbox_dir / f"{today.isoformat()}-{slugify(source_path.stem)}"
    n = 2
    while folder.exists():
        folder = inbox_dir / f"{today.isoformat()}-{slugify(source_path.stem)}-{n}"
        n += 1
    (folder / "drafts").mkdir(parents=True)
    (folder / "source.md").write_text(source_text, encoding="utf-8")
    (folder / "proposal.json").write_text(
        json.dumps({"collection": collection, "source": source_path.name,
                    **proposal.model_dump()}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    lines = [f"# 待审核：{source_path.name}", "", proposal.source_summary_zh, ""]
    if not proposal.changes:
        lines.append("模型认为这份文件不需要修改知识库。")
    for c in proposal.changes:
        lines += [f"## [{c.action}] {c.title} (`{c.article_id}`)", "", c.summary_zh, ""]
        if c.effective_from:
            lines += [f"生效日期：{c.effective_from}", ""]
        lines += ["原文依据：", ""] + [f"> {q}" for q in c.evidence] + [""]
        if c.action in {"new", "update"} and c.article_markdown:
            existing = kb.get(c.article_id)
            meta = {
                "id": c.article_id,
                "title": c.title,
                "category": c.category,
                "status": "draft",
                "state": "NSW" if collection == "rego" else None,
                "effective_from": c.effective_from,
                "sources": sorted({*(existing.meta.sources if existing else []), source_path.name}),
                "version": (existing.meta.version + 1) if existing else 1,
                "owner": existing.meta.owner if existing else None,
            }
            meta = {k: v for k, v in meta.items() if v is not None}
            front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
            (folder / "drafts" / f"{c.article_id}.md").write_text(
                f"---\n{front}---\n\n{c.article_markdown.strip()}\n", encoding="utf-8")
            lines += [f"草稿：`drafts/{c.article_id}.md`", ""]
    lines += ["---", "",
              "通过：先按需修改 drafts/ 里的草稿，然后运行",
              f"`regorag apply {folder.relative_to(inbox_dir.parent)} --reviewer <你的名字>`",
              "驳回：直接删除这个文件夹。"]
    (folder / "REVIEW.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def apply_proposal(folder: Path, kb: KnowledgeBase, kb_dir: Path, policy: ReviewPolicy,
                   reviewer: str, only: list[str] | None = None) -> list[Path]:
    """Copy approved drafts into kb/ and publish them; archive retired articles."""
    data = json.loads((folder / "proposal.json").read_text(encoding="utf-8"))
    collection = data["collection"]
    written: list[Path] = []
    for change in data["changes"]:
        aid = change["article_id"]
        if only and aid not in only:
            continue
        if change["action"] not in {"new", "update", "retire"}:
            continue
        existing = kb.get(aid)
        ok, why = policy.can_approve(reviewer, collection, change["category"])
        if not ok:
            raise PermissionError(why)
        if change["action"] in {"new", "update"}:
            draft = folder / "drafts" / f"{aid}.md"
            if not draft.is_file():
                continue
            target = existing.path if existing else kb_dir / collection / change["category"] / f"{aid}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(draft, target)
            written.append(publish(target, kb_dir, policy, reviewer))
        elif change["action"] == "retire" and existing:
            raw = existing.path.read_text(encoding="utf-8")
            existing.path.write_text(re.sub(r"(?m)^status:\s*\w+", "status: archived", raw, count=1),
                                     encoding="utf-8")
            written.append(existing.path)
    return written
