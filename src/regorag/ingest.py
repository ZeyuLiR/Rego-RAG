"""Turn uploaded emails / PDFs / Word files into proposed knowledge base changes.

Files uploaded together are read together as one batch: they are often parts of one matter
(a process document, its letter template and a filled-in example), and a note pasted with
them can say how they relate. The model never edits the knowledge base. It writes one
proposal folder per batch under inbox/:

    inbox/<date>-<name>/
        REVIEW.md        what changes, why, and the evidence quotes (for the reviewer)
        proposal.json    the same, machine-readable
        source.md        the redacted text the model saw
        originals/       the uploaded files
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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .kb import KBError, KnowledgeBase, parse_article, safe_file_name, slugify
from .llm import LLM
from .review import ReviewPolicy, publish

# ---------------------------------------------------------------- reading files


def read_document(path: Path) -> str:
    """Plain text / Markdown for any supported file."""
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".json"}:
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
    evidence: list[str] = Field(description="Verbatim quotes from the sources supporting the change")
    source_files: list[str] = Field(default_factory=list,
                                    description="Names of the sources this change is based on")
    attach_files: list[str] = Field(
        default_factory=list,
        description="Names of uploaded files to store with this article so clerks can download "
                    "and use them as-is (blank templates, forms). Never a filled-in example.")
    article_markdown: str | None = Field(
        default=None,
        description="For new/update: the complete article body in Markdown (no front matter), "
                    "following the article template")


class Proposal(BaseModel):
    source_summary_zh: str
    changes: list[ProposedChange]


class Topic(Proposal):
    """One distinct matter in a batch; each topic becomes its own proposal to review."""
    title_zh: str = Field(description="A short Chinese name for this matter, shown to the reviewer")
    source_files: list[str] = Field(default_factory=list,
                                    description="Names of the sources this matter comes from")


class BatchProposal(BaseModel):
    topics: list[Topic]


EXTRACT_PROMPT = """\
You maintain a reviewed knowledge base. The <about> block describes what it is for and who uses it. \
New source material has arrived: one or more documents (emails, notices, PDFs, Word files, chat or \
social media conversations) uploaded together, sometimes with a note from the reviewer. Work out \
what, if anything, it changes in the knowledge base, and propose those changes for a human reviewer.

Rules:
- Read all the sources together before deciding anything, then group the batch into topics: \
one topic per distinct matter. Documents about the same matter, for example a process \
document, the letter template it uses and a filled-in example, belong to one topic and are \
combined into as few coherent articles as it needs, rather than one article per file. Sources \
that say different things about different matters go into separate topics, even when they \
arrived together: each topic is reviewed and approved separately. A source may belong to \
more than one topic. When nothing in the batch is relevant, return one topic with no changes.
- A <reviewer_note>, when present, is from the reviewer: it may say how the documents relate, \
what event they concern or what to focus on. Follow it. If it also states facts, you may use \
them, quoting the note as evidence.
- Conversations (chat logs, email threads, social media messages): use what the organisation's \
side stated or confirmed, and the questions that come up, not one-off opinions. Never record \
anything about the individual people in them.
- For templates and filled-in examples, describe what the form or letter is for, when it is \
used and what must be filled in. Never copy a customer's details from an example.
- When an uploaded file is a blank template or form that the knowledge base's users will need \
to use or send, list it in attach_files of the article it belongs to, so it is stored in the \
knowledge base and the assistant can hand it out. Mention it in the article. Never attach a \
filled-in example or anything containing a customer's details. If the reviewer note asks for a \
file to be added to an existing article, propose an "update" of that article that keeps its \
content and lists the file in attach_files.
- Only use what the sources say. Every change needs verbatim evidence quotes, and source_files \
naming the sources it came from.
- Prefer updating an existing article over creating a new one. Use "duplicate" when the source \
adds nothing new, and "conflict" when it contradicts an article in a way you cannot resolve \
(explain in summary_zh).
- For "new" and "update", write the complete article body in English, keeping the section \
headings and order of the article template. For an update, keep everything in the existing \
article that the source does not change.
- Keep prices, fees, dates and form numbers exactly as written. Never include personal details \
about customers.
- If the sources contain nothing relevant to this knowledge base, return no changes.
- Write title_zh, summary_zh and source_summary_zh in Chinese."""


@dataclass
class Source:
    name: str
    text: str
    original: Path | None = None  # the uploaded file, kept with the proposal


def propose(source_path: Path, kb: KnowledgeBase, collection: str, llm: LLM, model: str,
            template: str, categories: list[str], inbox_dir: Path,
            today: dt.date | None = None) -> list[Path]:
    """Read, redact and extract one file; write its proposal folders and return them."""
    return propose_batch([Source(source_path.name, read_document(source_path), source_path)], "",
                         kb, collection, llm, model, template, categories, inbox_dir, today)


def propose_text(source_name: str, raw_text: str, kb: KnowledgeBase, collection: str, llm: LLM,
                 model: str, template: str, categories: list[str], inbox_dir: Path,
                 today: dt.date | None = None) -> list[Path]:
    """Same as `propose`, for text that is already extracted (e.g. a pasted email)."""
    return propose_batch([Source(source_name, raw_text)], "", kb, collection, llm, model,
                         template, categories, inbox_dir, today)


def read_sources(paths: list[Path]) -> list[Source]:
    out = []
    for p in paths:
        try:
            out.append(Source(p.name, read_document(p), p))
        except Exception as exc:
            raise ValueError(f"{p.name} 读取失败：{exc}") from exc
    return out


def batch_name(sources: list[Source], note: str) -> str:
    """Folder and display name: the note's subject line if any, else the first file."""
    if note.strip() and re.search(r"(?im)^\s*(?:subject|主题)\s*[:：]", note):
        return Path(pasted_source_name(note)).stem
    stem = Path(sources[0].name).stem
    return stem if len(sources) == 1 else f"{stem}-and-{len(sources) - 1}-more"


def propose_batch(sources: list[Source], note: str, kb: KnowledgeBase, collection: str, llm: LLM,
                  model: str, template: str, categories: list[str], inbox_dir: Path,
                  today: dt.date | None = None, about: str = "") -> list[Path]:
    """Extract changes from several sources read together, plus an optional reviewer note.
    `about` is the collection's description from config/collections.yaml.

    The model groups the batch into topics (distinct matters); each topic is written as its
    own proposal folder, so different matters are reviewed and approved separately."""
    today = today or dt.date.today()
    note = redact(note.strip())
    texts = [(s, redact(s.text)) for s in sources]
    existing = "\n\n".join(a.render_for_model() for a in kb.collection(collection))
    content = (f"<reviewer_note>\n{note}\n</reviewer_note>\n\n" if note else "") + "\n\n".join(
        f"<source name=\"{s.name}\">\n{t}\n</source>" for s, t in texts)
    response = llm.parse(
        "extract",
        model=model,
        max_tokens=32000,
        output_format=BatchProposal,
        system=[
            {"type": "text", "text": EXTRACT_PROMPT},
            {"type": "text", "text": f"<about>\n{about.strip() or collection}\n</about>\n"
                                     f"<categories>{', '.join(categories)}</categories>\n"
                                     f"<article_template>\n{template}\n</article_template>\n"
                                     f"<knowledge_base collection=\"{collection}\">\n{existing}\n"
                                     f"</knowledge_base>",
             "cache_control": {"type": "ephemeral"}},
        ],
        messages=[{"role": "user", "content": content}],
    )
    names = [s.name for s in sources]
    topics = response.parsed_output.topics or [
        Topic(title_zh="没有可整理的内容", source_summary_zh="模型没有从这批资料中找到相关内容。",
              source_files=names, changes=[])]
    base = batch_name(sources, note)
    folders = []
    for i, topic in enumerate(topics, 1):
        used = [n for n in names if n in topic.source_files] or names
        picked = [(s, t) for s, t in texts if s.name in used]
        source_md = "\n\n".join(([f"# 说明\n\n{note}"] if note else [])
                                 + [f"# {s.name}\n\n{t}" for s, t in picked])
        name = base if len(topics) == 1 else \
            f"{base}-{topic.changes[0].article_id if topic.changes else i}"
        folder = write_proposal(topic, name, used, source_md, kb, collection, inbox_dir, today, note)
        originals = [s.original for s, _ in picked if s.original]
        if originals:
            (folder / "originals").mkdir()
            for path in originals:
                shutil.copyfile(path, folder / "originals" / path.name)
        folders.append(folder)
    return folders


def pasted_source_name(text: str, now: dt.datetime | None = None) -> str:
    """A readable name for pasted text: the email subject if there is one, else a timestamp."""
    match = re.search(r"(?im)^\s*(?:subject|主题)\s*[:：]\s*(.+)$", text)
    if match and slugify(match.group(1))[:60].strip("-"):
        return slugify(match.group(1))[:60].strip("-") + ".txt"
    now = now or dt.datetime.now()
    return f"pasted-{now:%Y%m%d-%H%M%S}.txt"


def write_proposal(proposal: Proposal, name: str, source_names: list[str], source_text: str,
                   kb: KnowledgeBase, collection: str, inbox_dir: Path, today: dt.date,
                   note: str = "") -> Path:
    slug = slugify(name)[:80].strip("-") or "import"
    folder = inbox_dir / f"{today.isoformat()}-{slug}"
    n = 2
    while folder.exists():  # never overwrite an earlier proposal
        folder = inbox_dir / f"{today.isoformat()}-{slug}-{n}"
        n += 1
    title = " + ".join(source_names)
    topic = getattr(proposal, "title_zh", "")
    (folder / "drafts").mkdir(parents=True)
    (folder / "source.md").write_text(source_text, encoding="utf-8")
    (folder / "proposal.json").write_text(
        json.dumps({"collection": collection, "source": title, "sources": source_names,
                    "note": note, "topic": topic,
                    **proposal.model_dump(exclude={"title_zh", "source_files"})}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    lines = [f"# 待审核：{topic or title}", ""] + ([f"资料：{title}", ""] if topic else [])
    if note:
        lines += ["说明：", ""] + [f"> {line}" for line in note.splitlines()] + [""]
    lines += [proposal.source_summary_zh, ""]
    if not proposal.changes:
        lines.append("模型认为这批资料不需要修改知识库。")
    for c in proposal.changes:
        lines += [f"## [{c.action}] {c.title} (`{c.article_id}`)", "", c.summary_zh, ""]
        if c.effective_from:
            lines += [f"生效日期：{c.effective_from}", ""]
        if c.source_files:
            lines += [f"依据资料：{'、'.join(c.source_files)}", ""]
        if c.attach_files:
            lines += [f"存为附件（问答中可下载）：{'、'.join(c.attach_files)}", ""]
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
                "sources": sorted({*(existing.meta.sources if existing else []),
                                   *([f for f in c.source_files if f in source_names] or source_names)}),
                "version": (existing.meta.version + 1) if existing else 1,
                "owner": existing.meta.owner if existing else None,
                "attachments": _attachments(existing, c.attach_files, source_names) or None,
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


def _attachments(existing, attach_files: list[str], source_names: list[str]) -> list[dict]:
    """Existing attachments plus newly attached uploads, as front matter entries."""
    out = [a.model_dump(exclude_none=True) for a in (existing.meta.attachments if existing else [])]
    for name in attach_files:
        if name in source_names and not any(a["file"] == safe_file_name(name) for a in out):
            out.append({"file": safe_file_name(name), "title": Path(name).stem})
    return out


def _attachment_copies(folder: Path, article, kb_dir: Path) -> list[tuple[Path, Path]]:
    """Uploaded originals to copy into kb/<collection>/files/ for a draft's attachments."""
    files_dir = kb_dir / article.collection / "files"
    originals = {safe_file_name(p.name): p for p in (folder / "originals").glob("*")} \
        if (folder / "originals").is_dir() else {}
    copies = []
    for att in article.meta.attachments:
        if att.file in originals:
            copies.append((originals[att.file], files_dir / att.file))
        elif not (files_dir / att.file).is_file():
            raise KBError(f"{article.id}: 附件 {att.file} 不在上传的资料里，也不在知识库中")
    return copies


def apply_proposal(folder: Path, kb: KnowledgeBase, kb_dir: Path, policy: ReviewPolicy,
                   reviewer: str, only: list[str] | None = None) -> list[Path]:
    """Copy approved drafts into kb/ and publish them; archive retired articles."""
    data = json.loads((folder / "proposal.json").read_text(encoding="utf-8"))
    collection = data["collection"]
    changes = [c for c in data["changes"]
               if c["action"] in {"new", "update", "retire"} and (not only or c["article_id"] in only)]
    # Check everything first so a refusal or a broken draft never leaves a half-applied proposal.
    copies: dict[str, list[tuple[Path, Path]]] = {}
    for change in changes:
        ok, why = policy.can_approve(reviewer, collection, change["category"])
        if not ok:
            raise PermissionError(why)
        draft = folder / "drafts" / f"{change['article_id']}.md"
        if change["action"] != "retire" and draft.is_file():
            article = parse_article(draft, collection)
            if article is None or article.id != change["article_id"]:
                raise KBError(f"{draft.name}: front matter 缺失，或 id 与提案不一致")
            copies[article.id] = _attachment_copies(folder, article, kb_dir)
    written: list[Path] = []
    for change in changes:
        aid = change["article_id"]
        existing = kb.get(aid)
        if change["action"] in {"new", "update"}:
            draft = folder / "drafts" / f"{aid}.md"
            if not draft.is_file():
                continue
            target = existing.path if existing else kb_dir / collection / change["category"] / f"{aid}.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            for src, dest in copies.get(aid, []):
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
            shutil.copyfile(draft, target)
            written.append(publish(target, kb_dir, policy, reviewer))
        elif change["action"] == "retire" and existing:
            raw = existing.path.read_text(encoding="utf-8")
            existing.path.write_text(re.sub(r"(?m)^status:\s*\w+", "status: archived", raw, count=1),
                                     encoding="utf-8")
            written.append(existing.path)
    return written


# ---------------------------------------------------------------- the review queue

_FOLDER_NAME = re.compile(r"^[\w.-]+$")


def proposal_folder(inbox_dir: Path, name: str) -> Path:
    """Resolve a proposal folder by name, refusing anything outside inbox/."""
    folder = inbox_dir / name
    if not _FOLDER_NAME.match(name) or name.startswith((".", "_")) or not (folder / "proposal.json").is_file():
        raise FileNotFoundError(f"没有这个待审核提案：{name}")
    return folder


def _split_draft(draft: Path) -> tuple[dict, str]:
    raw = draft.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", raw, re.DOTALL)
    if not match:
        raise KBError(f"{draft.name}: front matter 缺失")
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def _originals(folder: Path) -> list[str]:
    d = folder / "originals"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def set_draft_attachments(folder: Path, article_id: str, originals: list[str]) -> None:
    """The reviewer's choice of which uploaded files to store with an article: replaces the
    attachments that come from this batch, keeps the ones the article already had."""
    draft = folder / "drafts" / f"{article_id}.md"
    if not _FOLDER_NAME.match(article_id) or not draft.is_file():
        raise FileNotFoundError(f"这个提案里没有草稿：{article_id}")
    uploaded = {safe_file_name(n): n for n in _originals(folder)}
    meta, body = _split_draft(draft)
    kept = [a for a in meta.get("attachments") or [] if a.get("file") not in uploaded]
    chosen = [{"file": safe_file_name(n), "title": Path(n).stem}
              for n in originals if safe_file_name(n) in uploaded]
    meta["attachments"] = kept + chosen
    if not meta["attachments"]:
        del meta["attachments"]
    front = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)
    draft.write_text(f"---\n{front}---\n{body}", encoding="utf-8")


def list_proposals(inbox_dir: Path) -> list[dict]:
    """Every proposal waiting for review, newest first, with its drafts' current text."""
    out = []
    if not inbox_dir.is_dir():
        return out
    for folder in sorted(inbox_dir.iterdir(), reverse=True):
        if folder.name.startswith((".", "_")) or not (folder / "proposal.json").is_file():
            continue
        data = json.loads((folder / "proposal.json").read_text(encoding="utf-8"))
        originals = _originals(folder)
        for change in data["changes"]:
            draft = folder / "drafts" / f"{change['article_id']}.md"
            change["draft"] = draft.read_text(encoding="utf-8") if draft.is_file() else None
            change["attached"] = []
            if change["draft"] is not None:
                try:
                    files = {a.get("file") for a in _split_draft(draft)[0].get("attachments") or []}
                except (KBError, yaml.YAMLError):
                    files = set()
                change["attached"] = [n for n in originals if safe_file_name(n) in files]
        out.append({"name": folder.name, "originals": originals, **data})
    return out


def save_draft(folder: Path, article_id: str, content: str) -> None:
    """Store the reviewer's edit of a draft (front matter included); `apply` uses it."""
    draft = folder / "drafts" / f"{article_id}.md"
    if not _FOLDER_NAME.match(article_id) or not draft.is_file():
        raise FileNotFoundError(f"这个提案里没有草稿：{article_id}")
    draft.write_text(content.rstrip() + "\n", encoding="utf-8")


def close_proposal(folder: Path, inbox_dir: Path, outcome: str) -> Path:
    """Move a handled proposal to inbox/_applied or inbox/_rejected (kept, not deleted)."""
    dest = inbox_dir / f"_{outcome}" / folder.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(dest))
    return dest
