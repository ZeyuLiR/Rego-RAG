"""HTTP API and the two web pages.

Run with `regorag serve` (or `uvicorn regorag.api:app`).
  /        chat page for clerks (a Teams bot can later call the same /api/ask)
  /import  drag-and-drop import and the review queue, for reviewers
"""

from __future__ import annotations

import hmac
import secrets
import subprocess
import tempfile
from importlib import resources
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .app import build_app
from .importer import ALLOWED_SUFFIXES, MAX_FILE_BYTES, ImportQueue
from .ingest import (apply_proposal, close_proposal, list_proposals, pasted_source_name, propose,
                     propose_text, proposal_folder, save_draft)
from .kb import KBError, load_kb
from .llm import RefusedError
from .qa import Turn
from .usage import BudgetExceeded


class AskRequest(BaseModel):
    question: str
    collection: str = "rego"
    history: list[dict] = []


class ApplyRequest(BaseModel):
    reviewer: str
    only: list[str] | None = None


class DraftRequest(BaseModel):
    content: str


def _page(name: str) -> str:
    return resources.files("regorag").joinpath(f"static/{name}").read_text(encoding="utf-8")


def _git_commit(root: Path, message: str) -> str | None:
    """Commit kb/ changes so Git history records who approved what. Returns the short hash."""
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)

    if git("rev-parse", "--is-inside-work-tree").returncode != 0:
        return None
    git("add", "kb")
    if git("commit", "-m", message).returncode != 0:
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
        return {"ok": True, "backend": a.settings.llm_backend,
                "collections": {c: len(a.kb.live(c)) for c in a.collections}}

    @api.post("/api/ask")
    def ask(req: AskRequest) -> dict:
        a = get()
        if req.collection not in a.collections:
            raise HTTPException(404, f"unknown collection {req.collection}")
        history = [Turn(t["role"], t["content"]) for t in req.history[-10:]
                   if t.get("role") in {"user", "assistant"} and t.get("content")]
        try:
            ans = a.qa.ask(req.question, req.collection, history)
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
        return {"answer": ans.text, "sources": links, "covered": ans.covered,
                "mode": ans.mode, "cost_usd": ans.cost_usd}

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
                           collection: str = Form(default="rego")) -> dict:
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
                        inbox_dir=a.settings.inbox_dir)

        jobs = [queue.submit(p.name, lambda p=p: propose(p, **args())) for p in staged]
        if text.strip():
            name = pasted_source_name(text)
            jobs.append(queue.submit(name, lambda: propose_text(name, text, **args())))
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

    api.state.import_queue = queue
    return api


app = create_app()
