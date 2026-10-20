"""Answer a clerk's question from the knowledge base, with citations."""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .kb import KnowledgeBase
from .llm import LLM, text_of
from .retrieval import MODES, Context, Retriever, build_retriever
from .vectors import Embedder

SYSTEM_PROMPT = """\
You answer questions from rego (vehicle registration) clerks at a company in New South Wales, \
Australia. Answer only from the knowledge base provided below. It is the company's reviewed \
reference; your general knowledge of NSW rules may be out of date and must not be used.

How to answer:
- Clerks usually ask in Chinese. Reply in the language of the question. Keep English proper \
nouns, form names, fee names and official terms in English (e.g. Green Slip, Pink Slip, \
Service NSW), with a short Chinese gloss the first time if helpful.
- Lead with the direct answer, then the steps, documents or fees needed. Be concise.
- Copy fees, dates, form numbers and time limits exactly as written in the knowledge base. \
Mention the effective date when the source gives one.
- Cite the source after each claim as [[id]], using the article or section id shown in the \
knowledge base.
- Some articles list attachments (blank templates, forms) as `attachment: [[file:name]] title`. \
When the clerk asks for one, or needs one to do what the answer describes, offer it by writing \
its [[file:name]] marker exactly; the clerk gets a download link. Only offer attachments that \
are listed.
- If the knowledge base does not answer the question, or only partly answers it, say so \
plainly and do not fill the gap from general knowledge. Suggest the clerk check with a \
senior colleague or the official source.
- End with one line exactly in this form: SOURCES: id1, id2 (or SOURCES: NONE if the \
knowledge base did not cover the question)."""

REWRITE_PROMPT = """\
Rewrite the clerk's latest question as a short English keyword search query for a NSW vehicle \
registration knowledge base. Use the glossary to map Chinese or informal terms to the official \
English terms. Resolve references to earlier turns. Output only the query."""

_FILE = re.compile(r"\[\[file:([^\]\s]+)\]\]")
_SOURCES = re.compile(r"^\s*SOURCES:\s*(.*)$", re.MULTILINE | re.IGNORECASE)


@dataclass
class Answer:
    text: str
    sources: list[str]
    covered: bool
    mode: str
    search_query: str | None = None
    cost_usd: float = 0.0
    files: list[str] = field(default_factory=list)  # attachment file names offered in the answer


@dataclass
class Turn:
    role: str  # "user" | "assistant"
    content: str


@dataclass
class QAService:
    kb: KnowledgeBase
    llm: LLM
    model: str
    effort: str = "medium"
    full_context_token_limit: int = 150_000
    gaps_log: Path | None = None
    retrieval: str = "auto"  # default mode; see retrieval.MODES
    embedder: Embedder | None = None
    vector_dir: Path | None = None
    _retrievers: dict[tuple[str, str], Retriever] = field(default_factory=dict)

    def retriever(self, collection: str, mode: str | None = None) -> Retriever:
        mode = mode or self.retrieval
        if mode not in MODES:
            raise ValueError(f"unknown retrieval mode {mode!r}; use one of {', '.join(MODES)}")
        if (collection, mode) not in self._retrievers:
            self._retrievers[(collection, mode)] = build_retriever(
                self.kb.live(collection), self.full_context_token_limit, mode, self.embedder,
                self.vector_dir / f"{collection}.json" if self.vector_dir else None)
        return self._retrievers[(collection, mode)]

    def search(self, question: str, collection: str = "rego", mode: str | None = None,
               history: list[Turn] | None = None, query: str | None = None) -> tuple[Context, str | None]:
        """What the model would read for this question, without answering it.

        Section search first rewrites the question into an English query (one cheap model
        call) unless `query` is given."""
        retriever = self.retriever(collection, mode)
        if retriever.mode == "full":
            return retriever.retrieve(question), None
        query = query or self._rewrite(question, collection, history or [])
        return retriever.retrieve(query), query

    def glossary(self, collection: str) -> str:
        g = next((a for a in self.kb.live(collection) if a.meta.category == "glossary"), None)
        return g.body if g else ""

    def ask(self, question: str, collection: str = "rego",
            history: list[Turn] | None = None, mode: str | None = None) -> Answer:
        history = history or []
        calls_before = len(self.llm.usage.records())
        context, search_query = self.search(question, collection, mode, history)

        response = self.llm.create(
            "qa",
            model=self.model,
            max_tokens=16000,
            output_config={"effort": self.effort},
            system=self._system_blocks(context),
            messages=[{"role": t.role, "content": t.content} for t in history]
            + [{"role": "user", "content": question}],
        )
        answer = self._parse(text_of(response), context, search_query)
        answer.cost_usd = round(sum(r.cost_usd for r in self.llm.usage.records()[calls_before:]), 6)
        if not answer.covered:
            self._log_gap(question, collection)
        return answer

    def _system_blocks(self, context: Context) -> list[dict]:
        kb_text = context.text or "(no matching knowledge base content was found)"
        blocks = [
            {"type": "text", "text": SYSTEM_PROMPT},
            {"type": "text", "text": f"<knowledge_base mode=\"{context.mode}\">\n{kb_text}\n</knowledge_base>"},
        ]
        if context.mode == "full":
            # The whole KB is identical across questions until it is republished: cache it.
            blocks[-1]["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
        return blocks

    def _rewrite(self, question: str, collection: str, history: list[Turn]) -> str:
        convo = "\n".join(f"{t.role}: {t.content}" for t in history[-6:])
        response = self.llm.create(
            "rewrite",
            model=self.model,
            max_tokens=1000,
            output_config={"effort": "low"},
            system=REWRITE_PROMPT + "\n\n<glossary>\n" + self.glossary(collection) + "\n</glossary>",
            messages=[{"role": "user", "content": f"{convo}\nuser: {question}".strip()}],
        )
        return text_of(response) or question

    @staticmethod
    def _parse(text: str, context: Context, search_query: str | None) -> Answer:
        match = None
        for match in _SOURCES.finditer(text):
            pass
        sources: list[str] = []
        if match:
            raw = match.group(1).strip()
            text = (text[: match.start()] + text[match.end():]).strip()
            if raw.upper() != "NONE":
                valid = set(context.cite_ids)
                sources = [s.strip(" []") for s in raw.split(",") if s.strip(" []") in valid]
        allowed = set(context.files)
        files = list(dict.fromkeys(f for f in _FILE.findall(text) if f in allowed))
        return Answer(text=text, sources=sources, covered=bool(sources),
                      mode=context.mode, search_query=search_query, files=files)

    def _log_gap(self, question: str, collection: str) -> None:
        if not self.gaps_log:
            return
        self.gaps_log.parent.mkdir(parents=True, exist_ok=True)
        with self.gaps_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                                "collection": collection, "question": question},
                               ensure_ascii=False) + "\n")
