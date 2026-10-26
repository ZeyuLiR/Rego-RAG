"""HTTP API and the two web pages.

Run with `regorag serve` (or `uvicorn regorag.api:app`).
  /        chat page for clerks (a Teams bot can later call the same /api/ask)
  /import  drag-and-drop import, the review queue and answers that need checking, for reviewers
  /eval    the answer eval set: add questions, run it, read the last report
"""

from __future__ import annotations

import hmac
import secrets
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import asdict
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import evals
from .app import build_app
from .chats import ChatStore, open_chat_store
from .importer import ALLOWED_SUFFIXES, MAX_FILE_BYTES, ImportQueue
from .ingest import (Source, apply_proposal, close_proposal, list_proposals, pasted_source_name,
                     propose_batch, proposal_folder, read_sources, save_draft,
                     set_draft_attachments)
from .kb import KBError, load_kb
from .llm import RefusedError
from .qa import Turn
from .retrieval import MODES
from .usage import BudgetExceeded
from .vectors import VectorSearchUnavailable


class AskRequest(BaseModel):
    question: str
    collection: str = "rego"
    history: list[dict] = []
    retrieval: str | None = None  # override the default mode for this question
    user: str = ""  # the name typed on the chat page, for the answer log


class FeedbackRequest(BaseModel):
    answer_id: str
    helpful: bool
    comment: str = ""


class ResolveRequest(BaseModel):
    reviewer: str
    note: str = ""


class EvalItemRequest(BaseModel):
    id: str = ""
    question: str
    collection: str = "rego"
    expect: list[str] = []
    must_include: list[str] = []
    should_refuse: bool = False
    added_by: str = ""
    notes: str = ""


class EvalRunRequest(BaseModel):
    retrieval: str | None = None
    judge: bool = False


class ChatRequest(BaseModel):
    user: str
    title: str
    turns: list[dict]


class ApplyRequest(BaseModel):
    reviewer: str
    only: list[str] | None = None
    # article id -> uploaded file names to store with it (the reviewer's ticks on the page)
    attachments: dict[str, list[str]] | None = None


class DraftRequest(BaseModel):
    content: str


def _page(name: str) -> str:
    return resources.files("regorag").joinpath(f"static/{name}").read_text(encoding="utf-8")


def _git_commit(root: Path, message: str, paths: tuple[str, ...] = ("kb",)) -> str | None:
    """Commit kb/ (or other) changes so Git history records who changed what. Returns the short hash."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return None
    git("add", *paths)
    if git("commit", "-m", message, "--", *paths).returncode != 0:
        return None
    return git("rev-parse", "--short", "HEAD").stdout.strip() or None


def create_app(regorag_app=None) -> FastAPI:
    state = {"app": regorag_app}
    queue = ImportQueue()

    def get():
        if state["app"] is None:
            state["app"] = build_app()
        return state["app"]

    def reload_kb() -> None:
        a = get()
        a.kb = load_kb(a.settings.kb_dir)
        a.qa.kb = a.kb
        a.qa._retrievers.clear()

    def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
        token = get().settings.admin_token
        if token and not (x_admin_token and hmac.compare_digest(x_admin_token, token)):
            raise HTTPException(401, "需要管理口令（REGORAG_ADMIN_TOKEN）。")

    api = FastAPI(title="Rego knowledge base")
    admin = [Depends(require_admin)]

    # ------------------------------------------------------------ clerks

    @api.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _page("chat.html")

    @api.get("/api/health")
    def health() -> dict:
        a = get()
        return {"ok": True, "backend": a.settings.llm_backend, "retrieval": a.settings.retrieval,
                "collections": {c: len(a.kb.live(c)) for c in a.collections}}

    @api.post("/api/ask")
    def ask(req: AskRequest) -> dict:
        a = get()
        if req.collection not in a.collections:
            raise HTTPException(404, f"unknown collection {req.collection}")
        if req.retrieval and req.retrieval not in MODES:
            raise HTTPException(400, f"检索方式只能是 {', '.join(MODES)}")
        history = [Turn(t["role"], t["content"]) for t in req.history[-10:]
                   if t.get("role") in {"user", "assistant"} and t.get("content")]
        started = time.monotonic()
        try:
            ans = a.qa.ask(req.question, req.collection, history, req.retrieval)
        except VectorSearchUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except BudgetExceeded as exc:
            raise HTTPException(429, str(exc)) from exc
        except RefusedError as exc:
            raise HTTPException(422, str(exc)) from exc
        links = {}
        for src in ans.sources:
            art = a.kb.get(src.split("#")[0])
            if art:
                links[src] = {"title": art.meta.title,
                              "path": str(art.path.relative_to(a.settings.kb_dir))}
        # Files the answer offers, then the attachments of the articles it cites, so a
        # template is at hand whenever its article is used.
        names = list(ans.files)
        for src in ans.sources:
            art = a.kb.get(src.split("#")[0])
            names += [f.file for f in art.meta.attachments] if art else []
        files = {}
        for name in dict.fromkeys(names):
            att = a.kb.attachment(req.collection, name)
            if att:
                files[name] = {"title": att.label, "url": f"/files/{req.collection}/{name}"}
        # Every answer is logged; one with a figure missing from its sources goes straight on
        # the reviewers' list, so the article can be checked and corrected.
        answer_id = uuid.uuid4().hex[:12]
        versions = {}
        for src in ans.sources:
            art = a.kb.get(src.split("#")[0])
            if art:
                versions[art.id] = art.meta.version
        try:
            chats().log_answer({
                "id": answer_id, "user_name": req.user.strip()[:60], "collection": req.collection,
                "question": req.question, "answer": ans.text, "mode": ans.mode,
                "search_query": ans.search_query, "sources": ans.sources, "unverified": ans.unverified,
                "kb_versions": versions, "cost_usd": ans.cost_usd,
                "latency_ms": int((time.monotonic() - started) * 1000)})
        except Exception:  # the log must never cost the clerk their answer
            answer_id = None
        return {"answer": ans.text, "sources": links, "files": files, "covered": ans.covered,
                "mode": ans.mode, "search_query": ans.search_query, "cost_usd": ans.cost_usd,
                "unverified": ans.unverified, "answer_id": answer_id}

    @api.post("/api/feedback")
    def feedback(req: FeedbackRequest) -> dict:
        """A clerk's 有帮助 / 有错. "Wrong" puts the answer on the reviewers' list."""
        store = chats()
        if store.get_answer(req.answer_id) is None:
            raise HTTPException(404, "找不到这条回答")
        store.set_feedback(req.answer_id, req.helpful, req.comment.strip()[:2000])
        return {"ok": True}

    # ------------------------------------------------------------ chat history

    chat_state: dict = {}

    def chats() -> ChatStore:
        if "store" not in chat_state:
            settings = get().settings
            chat_state["store"] = open_chat_store(settings.chat_db, settings.root)
        return chat_state["store"]

    def clean(user: str, chat_id: str = "x") -> str:
        user = user.strip()
        if not user or len(user) > 60 or not chat_id or len(chat_id) > 64:
            raise HTTPException(400, "请先在左下角填写名字。")
        return user

    @api.get("/api/chats")
    def list_chats(user: str) -> dict:
        return {"chats": chats().list(clean(user))}

    @api.get("/api/chats/{chat_id}")
    def get_chat(chat_id: str, user: str) -> dict:
        chat = chats().get(clean(user, chat_id), chat_id)
        if chat is None:
            raise HTTPException(404, "没有这段对话")
        return chat

    @api.put("/api/chats/{chat_id}")
    def save_chat(chat_id: str, req: ChatRequest) -> dict:
        chats().save(clean(req.user, chat_id), chat_id, req.title[:80], req.turns)
        return {"ok": True}

    @api.delete("/api/chats/{chat_id}")
    def delete_chat(chat_id: str, user: str) -> dict:
        chats().delete(clean(user, chat_id), chat_id)
        return {"ok": True}

    @api.get("/files/{collection}/{name}")
    def attachment(collection: str, name: str) -> FileResponse:
        """Download an attachment. Only files listed by a live article are served."""
        a = get()
        att = a.kb.attachment(collection, name)
        path = a.settings.kb_dir / collection / "files" / name
        if att is None or not path.is_file():
            raise HTTPException(404, "没有这个文件")
        return FileResponse(path, filename=name)

    # ------------------------------------------------------------ answers to check

    def links_for(sources: list[str]) -> list[dict]:
        a = get()
        out = []
        for src in sources:
            art = a.kb.get(src.split("#")[0])
            out.append({"id": src, "title": art.meta.title if art else src,
                        "path": str(art.path.relative_to(a.settings.kb_dir)) if art else ""})
        return out

    @api.get("/api/review/items", dependencies=admin)
    def review_items() -> dict:
        """Answers with a figure not found in their sources, or marked wrong by a clerk."""
        items = chats().open_items()
        for it in items:
            it["source_links"] = links_for(it["sources"])
        return {"items": items}

    @api.post("/api/review/items/{answer_id}/resolve", dependencies=admin)
    def resolve_item(answer_id: str, req: ResolveRequest) -> dict:
        if not req.reviewer.strip():
            raise HTTPException(400, "请选择审核人")
        store = chats()
        if store.get_answer(answer_id) is None:
            raise HTTPException(404, "找不到这条回答")
        store.resolve(answer_id, req.reviewer.strip(), req.note.strip()[:2000])
        return {"ok": True}

    @api.get("/api/usage")
    def usage() -> dict:
        return get().usage.summary()

    # ------------------------------------------------------------ reviewers

    @api.get("/import", response_class=HTMLResponse)
    def import_page() -> str:
        return _page("import.html")

    @api.get("/api/admin/info", dependencies=admin)
    def admin_info() -> dict:
        a = get()
        return {"collections": {k: c.title for k, c in a.collections.items()},
                "reviewers": [r.name for r in a.policy.reviewers],
                "accept": sorted(ALLOWED_SUFFIXES), "max_mb": MAX_FILE_BYTES // (1024 * 1024),
                "backend": a.settings.llm_backend}

    @api.post("/api/import", dependencies=admin)
    async def start_import(files: list[UploadFile] = File(default=[]), text: str = Form(default=""),
                           collection: str = Form(default="rego"),
                           separate: bool = Form(default=False)) -> dict:
        """Files and pasted text sent together are read together as one batch, with the
        text as the reviewer's note on how they relate (or as the source when there are
        no files). `separate` restores one proposal per file."""
        a = get()
        if collection not in a.collections:
            raise HTTPException(404, f"没有这个知识库：{collection}")
        if not files and not text.strip():
            raise HTTPException(400, "请拖入文件或粘贴文字。")
        coll = a.collections[collection]
        uploads = Path(tempfile.mkdtemp(prefix="regorag-upload-"))
        staged: list[Path] = []
        for f in files:
            name = Path(f.filename or "upload").name
            if Path(name).suffix.lower() not in ALLOWED_SUFFIXES:
                raise HTTPException(400, f"{name}：不支持这种文件。可以上传 {', '.join(sorted(ALLOWED_SUFFIXES))}")
            data = await f.read()
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(400, f"{name}：文件超过 {MAX_FILE_BYTES // (1024 * 1024)} MB")
            path = uploads / secrets.token_hex(4) / name
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
            staged.append(path)

        def args():  # read the KB at run time so earlier approvals are taken into account
            return dict(kb=a.kb, collection=collection, llm=a.llm, model=a.settings.extract_model,
                        template=coll.template(), categories=list(coll.categories),
                        inbox_dir=a.settings.inbox_dir, about=coll.about)

        def batch(paths: list[Path], note: str = "", pasted: str = ""):
            def work():
                sources = read_sources(paths)
                if pasted:
                    sources.append(Source(pasted_source_name(pasted), pasted))
                return propose_batch(sources, note, **args())
            return work

        text = text.strip()
        if separate or not staged:
            jobs = [queue.submit(p.name, batch([p])) for p in staged]
            if text:
                jobs.append(queue.submit(pasted_source_name(text), batch([], pasted=text)))
        else:
            label = " + ".join([p.name for p in staged] + (["说明"] if text else []))
            jobs = [queue.submit(label, batch(staged, note=text))]
        return {"jobs": [j.id for j in jobs]}

    @api.get("/api/import/jobs", dependencies=admin)
    def import_jobs() -> dict:
        return {"jobs": queue.jobs()}

    @api.get("/api/proposals", dependencies=admin)
    def proposals() -> dict:
        return {"proposals": list_proposals(get().settings.inbox_dir)}

    def folder_or_404(name: str) -> Path:
        try:
            return proposal_folder(get().settings.inbox_dir, name)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.put("/api/proposals/{name}/drafts/{article_id}", dependencies=admin)
    def edit_draft(name: str, article_id: str, req: DraftRequest) -> dict:
        try:
            save_draft(folder_or_404(name), article_id, req.content)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True}

    @api.post("/api/proposals/{name}/apply", dependencies=admin)
    def apply(name: str, req: ApplyRequest) -> dict:
        a = get()
        folder = folder_or_404(name)
        try:
            for article_id, originals in (req.attachments or {}).items():
                set_draft_attachments(folder, article_id, originals)
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        try:
            written = apply_proposal(folder, a.kb, a.settings.kb_dir, a.policy, req.reviewer, req.only)
            reload_kb()
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except KBError as exc:
            raise HTTPException(400, f"草稿格式有误，没有发布：{exc}") from exc
        commit = _git_commit(a.settings.root, f"Publish {name} (reviewed by {req.reviewer})") if written else None
        close_proposal(folder, a.settings.inbox_dir, "applied")
        return {"published": [str(p.relative_to(a.settings.kb_dir)) for p in written], "commit": commit}

    @api.post("/api/proposals/{name}/reject", dependencies=admin)
    def reject(name: str) -> dict:
        a = get()
        close_proposal(folder_or_404(name), a.settings.inbox_dir, "rejected")
        return {"ok": True}

    # ------------------------------------------------------------ eval set

    eval_state: dict = {"running": None, "error": None}
    eval_lock = threading.Lock()

    @api.get("/eval", response_class=HTMLResponse)
    def eval_page() -> str:
        return _page("eval.html")

    @api.get("/api/eval/items", dependencies=admin)
    def eval_items() -> dict:
        a = get()
        return {"items": [asdict(i) for i in evals.load_items(a.settings.eval_file)],
                "articles": [{"id": art.id, "title": art.meta.title}
                             for c in a.collections for art in a.kb.live(c)]}

    @api.put("/api/eval/items", dependencies=admin)
    def save_eval_item(req: EvalItemRequest) -> dict:
        a = get()
        try:
            item = evals.upsert(a.settings.eval_file, req.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        _git_commit(a.settings.root, f"Eval set: save {item.id} ({item.added_by or 'unknown'})", ("eval",))
        return {"item": item.to_dict()}

    @api.delete("/api/eval/items/{item_id}", dependencies=admin)
    def delete_eval_item(item_id: str) -> dict:
        a = get()
        evals.remove(a.settings.eval_file, item_id)
        _git_commit(a.settings.root, f"Eval set: remove {item_id}", ("eval",))
        return {"ok": True}

    @api.post("/api/review/items/{answer_id}/to-eval", dependencies=admin)
    def item_to_eval(answer_id: str, req: EvalItemRequest) -> dict:
        """Turn a checked answer into an eval question (the reviewer fills in the right facts)."""
        if chats().get_answer(answer_id) is None:
            raise HTTPException(404, "找不到这条回答")
        return save_eval_item(req)

    @api.post("/api/eval/run", dependencies=admin)
    def run_eval(req: EvalRunRequest) -> dict:
        """Ask every eval question in the background (this costs usage) and save a report."""
        a = get()
        if req.retrieval and req.retrieval not in MODES:
            raise HTTPException(400, f"检索方式只能是 {', '.join(MODES)}")
        items = evals.load_items(a.settings.eval_file)
        if not items:
            raise HTTPException(400, "评测集还没有问题")
        with eval_lock:
            if eval_state["running"]:
                raise HTTPException(409, "评测正在运行，请等它结束")
            eval_state.update(running={"done": 0, "total": len(items), "question": ""}, error=None)

        def progress(n, total, item):
            eval_state["running"] = {"done": n - 1, "total": total, "question": item.question}

        def work():
            try:
                report = evals.run(a.qa, items, req.retrieval, req.judge, progress)
                evals.save_report(a.settings.eval_reports, report)
            except Exception as exc:
                eval_state["error"] = str(exc)
            finally:
                eval_state["running"] = None

        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "total": len(items)}

    @api.get("/api/eval/status", dependencies=admin)
    def eval_status() -> dict:
        return {"running": eval_state["running"], "error": eval_state["error"],
                "report": evals.latest_report(get().settings.eval_reports),
                "check_names": evals.CHECK_NAMES}

    api.state.import_queue = queue
    return api


app = create_app()
