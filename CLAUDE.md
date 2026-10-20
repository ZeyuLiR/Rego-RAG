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
- Until an API key is set up, the system calls Claude through the Claude Code CLI
  (`REGORAG_LLM_BACKEND=claude-code`, `src/regorag/claude_code.py`) on the owner's Claude plan.
  Same code path otherwise. Switch to `api` before clerks use it.
- Cloud Claude API is allowed. Personal data is regex-redacted before sending (`ingest.redact`).
- One reviewer today (Zeyu); more later via `config/reviewers.yaml` only.
- Clerks use Teams and Outlook: a Teams bot is phase 3; import accepts `.eml`/`.msg`.
- ~10-20 source documents today, so Q&A sends the whole live collection with prompt caching and
  switches to BM25 retrieval above `REGORAG_FULL_CONTEXT_TOKEN_LIMIT`.
- Retrieval mode is selectable (`REGORAG_RETRIEVAL` = auto|full|bm25|vector|hybrid, or per question
  from the chat page / `--retrieval`). Vector search is optional (`.[vector]` extra, local fastembed
  multilingual model, JSON cache in `data/vectors/`); default stays `auto`. The owner wants to
  compare with and without vectors before deciding.
- A `sales` collection will come later: every feature is per collection (`config/collections.yaml`).

## Commands

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q            # all tests use a fake client; no API calls, no cost
.venv/bin/regorag validate     # KB format check
.venv/bin/regorag serve        # web chat on :8000
```

Never run commands that call the real model (`ask`, `import`, `serve` + a question) without the
owner's go-ahead: they cost money or plan usage. `regorag usage` shows spend so far.

## Code map

- `src/regorag/kb.py` — article parsing, validation, sections, `is_live`
- `src/regorag/retrieval.py` — `FullContextRetriever`, `BM25Retriever`, `VectorRetriever`, `HybridRetriever`, `build_retriever`
- `src/regorag/vectors.py` — `FastEmbedder` (lazy, local), `VectorStore` (hash-keyed JSON cache)
- `src/regorag/qa.py` — prompts and `QAService.ask`
- `src/regorag/llm.py` — the only place that calls Claude (budget check, `fallbacks="default"`, usage log)
- `src/regorag/claude_code.py` — `claude -p` client with the SDK's `beta.messages.create/parse` shape
- `src/regorag/usage.py` — usage log, price table, monthly budget
- `src/regorag/ingest.py` — read/redact/extract → `inbox/` proposal; `apply_proposal`
- `src/regorag/review.py` — reviewers and `publish`
- `src/regorag/api.py`, `static/chat.html`, `static/import.html`, `cli.py` — entry points
- `src/regorag/importer.py` — background import job queue for the web page
- `src/regorag/chats.py` — server-side chat history (`data/chats.db`)

## Progress

Phase 1 (Q&A over imported material) — code done, not yet run against the real API:
- [x] KB format, template, glossary draft, collections and reviewers config
- [x] Q&A with citations, full-context/BM25 switch, gap logging
- [x] Import (eml/pdf/docx/msg/txt) → proposal → apply, PII redaction
- [x] Usage log + monthly budget cap, web chat, CLI, tests
- [x] Test mode without an API key: `regorag test-page` builds a chat page published as a
      claude.ai artifact (https://claude.ai/artifact/AUoFWFU8svZLYd2SCj7Gzz). It asks Claude on the
      viewer's claude.ai account and stores each question/answer/verdict in the artifact's
      `questions` collection (read it with ArtifactData). Rebuild and republish the same file
      whenever kb/ changes.
- [x] Claude Code backend: the real system runs without an API key (verified end to end)
- [x] Optional vector/hybrid retrieval with a per-question switch, `regorag compare`,
      `regorag eval-retrieval` (hit rate/MRR). Not yet run with the real embedding model
      (the cloud container cannot download it); tests use a fake embedder.
- [x] Web import page `/import`: drag multiple files or paste email text → background extraction
      (one job at a time) → review queue on the same page (edit drafts, approve selected, reject).
      Approve publishes, reloads the KB and git-commits kb/. `REGORAG_ADMIN_TOKEN` guards it.
- [x] One-step start: double-click `start-mac.command` / `start-windows.bat` (→ `start.py`: venv,
      install incl. `[vector]`, `.env`, `git pull --ff-only`, `serve --open` on a free port).
      Chinese tutorial: https://claude.ai/code/artifact/e4d2d04d-58c0-4d4d-a187-412feef23395
      (keep it in step with start-up or UI changes). The owner wants usage and startup as simple as possible.
- [x] Batch import: files and pasted text submitted together are extracted in one call
      (`ingest.propose_batch`); the pasted text is a `<reviewer_note>` on how they relate. One
      proposal folder per batch (`originals/`, per-change `source_files`). "分开整理" / `--separate`
      restores one proposal per file. The owner asked for related files to be considered together,
      and for different matters in one batch to be reviewed separately: the model returns
      `BatchProposal.topics`, and each topic becomes its own proposal folder / review card.
- [x] Windows: Claude Code via npm is a `claude.cmd` shim; `ClaudeCodeClient.command()` runs the
      bundled `claude.exe` (or node + cli.js). Output decoded as UTF-8. Owner runs Windows.
- [x] Attachments: blank templates/forms live in `kb/<collection>/files/`, listed in an article's
      `attachments` front matter. Extraction proposes `attach_files` (never filled-in examples);
      apply copies them from the proposal's `originals/`. Answers offer `[[file:name]]`, the chat
      shows download buttons, `/files/<collection>/<name>` serves only files of live articles.
      Answers also list the attachments of every cited article. The review card has per-article
      checkboxes for which uploaded files to attach (`ApplyRequest.attachments`).
- [x] Chat page: left sidebar with conversation history, stored on the server in
      `data/chats.db` (SQLite, `chats.py`) under the name the clerk types (no logins yet, so
      separated but not protected). Older browser-only history migrates on first use. Enter sends.
- [x] start.py update falls back to `git pull --rebase --autostash` because approvals commit kb/
      locally, and prints the current version so the owner can tell whether an update landed.
- [ ] Owner installs and logs in to Claude Code locally, runs `regorag import` on the existing
      ~10-20 documents (or sends them to Claude to run the same pipeline)
- [ ] Later: owner adds `.env` with an API key and sets `REGORAG_LLM_BACKEND=api`
- [ ] Owner reviews and publishes the glossary and imported articles
- [ ] Build the 50-100 question eval set with real clerk questions (gate for phase 2)

Phase 2 (upload and review UI): basic version done (`/import`); still to do: forward-to-mailbox
intake, side-by-side source/diff view.
Phase 3: feedback buttons, review-date reminders, Teams bot, permissions per role.
Phase 4 (optional): contradiction scan, MCP server, a vector database server behind `VectorStore` if the KB grows.

Keep this Progress section current at the end of each work session so a new session (cloud or
local) can pick up from here.
