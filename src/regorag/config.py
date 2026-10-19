"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the project has no extra dependency for it."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(frozen=True)
class Settings:
    root: Path = REPO_ROOT
    kb_dir: Path = field(default_factory=lambda: REPO_ROOT / "kb")
    config_dir: Path = field(default_factory=lambda: REPO_ROOT / "config")
    inbox_dir: Path = field(default_factory=lambda: REPO_ROOT / "inbox")
    usage_log: Path = field(default_factory=lambda: REPO_ROOT / "data" / "usage.jsonl")
    qa_model: str = "claude-opus-5-5"
    extract_model: str = "claude-opus-5-5"
    qa_effort: str = "medium"
    monthly_budget_usd: float = 50.0
    full_context_token_limit: int = 150_000
    llm_backend: str = "api"  # "api" or "claude-code"
    retrieval: str = "auto"  # auto | full | bm25 | vector | hybrid
    embed_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    vector_dir: Path = field(default_factory=lambda: REPO_ROOT / "data" / "vectors")
    # When set, the import and review pages require this token. Set it before the web
    # server is reachable by anyone other than reviewers.
    admin_token: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        _load_dotenv(REPO_ROOT / ".env")
        env = os.environ
        root = Path(env.get("REGORAG_ROOT", REPO_ROOT))
        return cls(
            root=root,
            kb_dir=Path(env.get("REGORAG_KB_DIR", root / "kb")),
            config_dir=Path(env.get("REGORAG_CONFIG_DIR", root / "config")),
            inbox_dir=Path(env.get("REGORAG_INBOX_DIR", root / "inbox")),
            usage_log=Path(env.get("REGORAG_USAGE_LOG", root / "data" / "usage.jsonl")),
            qa_model=env.get("REGORAG_QA_MODEL", cls.qa_model),
            extract_model=env.get("REGORAG_EXTRACT_MODEL", cls.extract_model),
            qa_effort=env.get("REGORAG_QA_EFFORT", cls.qa_effort),
            monthly_budget_usd=float(env.get("REGORAG_MONTHLY_BUDGET_USD", cls.monthly_budget_usd)),
            full_context_token_limit=int(
                env.get("REGORAG_FULL_CONTEXT_TOKEN_LIMIT", cls.full_context_token_limit)
            ),
            # Without an API key, default to Claude Code so the system works during setup.
            llm_backend=env.get("REGORAG_LLM_BACKEND")
            or ("api" if env.get("ANTHROPIC_API_KEY") else "claude-code"),
            admin_token=env.get("REGORAG_ADMIN_TOKEN", ""),
            retrieval=env.get("REGORAG_RETRIEVAL", cls.retrieval),
            embed_model=env.get("REGORAG_EMBED_MODEL", cls.embed_model),
            vector_dir=Path(env.get("REGORAG_VECTOR_DIR", root / "data" / "vectors")),
        )
