"""Build the test-mode chat page: the KB embedded in a page that asks Claude via claude.ai.

Used before an API key is set up. The page sends the same rules as qa.SYSTEM_PROMPT plus the
knowledge base to Claude on the viewer's own claude.ai account (the artifact `sample`
capability) and stores each question, answer and verdict in the artifact's database, which
becomes the eval set. Rebuild and republish whenever kb/ changes.
"""

from __future__ import annotations

import datetime as dt
import json
from importlib import resources
from pathlib import Path

from .kb import KnowledgeBase
from .qa import SYSTEM_PROMPT


def _embed(value) -> str:
    # JSON inside <script type="application/json">: only "</" can end the element early.
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def build_test_page(kb: KnowledgeBase, collection: str, out: Path,
                    today: dt.date | None = None) -> Path:
    today = today or dt.date.today()
    articles = [
        {"id": a.id, "title": a.meta.title, "live": a.is_live(today), "text": a.render_for_model()}
        for a in sorted(kb.collection(collection), key=lambda a: a.id)
        if a.meta.status != "archived"
    ]
    data = {"collection": collection, "version": today.isoformat() + f"·{len(articles)}",
            "articles": articles}
    template = resources.files("regorag").joinpath("static/test_chat_template.html").read_text(encoding="utf-8")
    html = template.replace("__KB_JSON__", _embed(data)).replace("__RULES_JSON__", _embed(SYSTEM_PROMPT))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out
