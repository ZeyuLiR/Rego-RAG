"""HTTP API and a minimal web chat page.

Run with `regorag serve` (or `uvicorn regorag.api:app`). A Teams bot can later call the
same /api/ask endpoint.
"""

from __future__ import annotations

from importlib import resources

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .app import build_app
from .llm import RefusedError
from .qa import Turn
from .usage import BudgetExceeded


class AskRequest(BaseModel):
    question: str
    collection: str = "rego"
    history: list[dict] = []


def create_app(regorag_app=None) -> FastAPI:
    state = {"app": regorag_app}

    def get():
        if state["app"] is None:
            state["app"] = build_app()
        return state["app"]

    api = FastAPI(title="Rego knowledge base")

    @api.get("/", response_class=HTMLResponse)
    def index() -> str:
        return resources.files("regorag").joinpath("static/chat.html").read_text(encoding="utf-8")

    @api.get("/api/health")
    def health() -> dict:
        a = get()
        return {"ok": True, "collections": {c: len(a.kb.live(c)) for c in a.collections}}

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

    return api


app = create_app()
