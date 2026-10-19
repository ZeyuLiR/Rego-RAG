# CLAUDE.md

Guidance for Claude Code working in this repository (cloud or local). The owner, Zeyu,
writes in Chinese; reply to them in Chinese. Code, comments and commit messages are English.

## What this is

A knowledge base for rego (vehicle registration) clerks at a company in NSW, Australia, plus a
Q&A assistant over it. Design doc: https://claude.ai/code/artifact/ab175223-ccc3-4271-8377-7bbcac76db1d

Key decisions (confirmed by the owner, 2026-10-19):
- `kb/` Markdown + YAML front matter is the single source of truth. Everything the model reads
  is derived from it. The model never writes to `kb/` directly; it writes proposals to `inbox/`
  and a human approves them (`regorag apply`).
- Source material is mostly English; clerks ask in Chinese. Keep articles in English; answers
  are in the question's language with English official terms kept.
- Cloud Claude API is allowed. Personal data is regex-redacted before sending (`ingest.redact`).
- One reviewer today (Zeyu); more later via `config/reviewers.yaml` only.
- Clerks use Teams and Outlook: a Teams bot is phase 3; import accepts `.eml`/`.msg`.
- ~10-20 source documents today, so Q&A sends the whole live collection with prompt caching and
  switches to BM25 retrieval above `REGORAG_FULL_CONTEXT_TOKEN_LIMIT`.
- A `sales` collection will come later: every feature is per collection (`config/collections.yaml`).

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q            # all tests use a fake client; no API calls, no cost
.venv/bin/regorag validate     # KB format check
.venv/bin/regorag serve        # web chat on :8000
```

Never run commands that call the real API (`ask`, `import`, `serve` + a question) without the
owner's go-ahead: they cost money. `regorag usage` shows spend so far.

## Code map

- `src/regorag/kb.py` — article parsing, validation, sections, `is_live`
- `src/regorag/retrieval.py` — `FullContextRetriever`, `BM25Retriever`, `build_retriever`
- `src/regorag/qa.py` — prompts and `QAService.ask`
- `src/regorag/llm.py` — the only place that calls Claude (budget check, `fallbacks="default"`, usage log)
- `src/regorag/usage.py` — usage log, price table, monthly budget
- `src/regorag/ingest.py` — read/redact/extract → `inbox/` proposal; `apply_proposal`
- `src/regorag/review.py` — reviewers and `publish`
- `src/regorag/api.py`, `static/chat.html`, `cli.py` — entry points

## Progress

Phase 1 (Q&A over imported material) — code done, not yet run against the real API:
- [x] KB format, template, glossary draft, collections and reviewers config
- [x] Q&A with citations, full-context/BM25 switch, gap logging
- [x] Import (eml/pdf/docx/msg/txt) → proposal → apply, PII redaction
- [x] Usage log + monthly budget cap, web chat, CLI, tests
- [ ] Owner adds `.env` with an API key and imports the existing ~10-20 documents
- [ ] Owner reviews and publishes the glossary and imported articles
- [ ] Build the 50-100 question eval set with real clerk questions (gate for phase 2)

Phase 2 (upload and review UI): upload page, proposals as GitHub PRs or a side-by-side review page.
Phase 3: feedback buttons, review-date reminders, Teams bot, permissions per role.
Phase 4 (optional): vector index behind `Retriever`, contradiction scan, MCP server.

Keep this Progress section current at the end of each work session so a new session (cloud or
local) can pick up from here.
