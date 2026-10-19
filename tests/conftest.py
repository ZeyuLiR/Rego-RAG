import datetime as dt
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from regorag.config import Settings

REPO = Path(__file__).resolve().parents[1]


def article(id, title, category="registration", status="published", body="", **extra):
    meta = {"id": id, "title": title, "category": category, "status": status, **extra}
    front = "\n".join(f"{k}: {v}" for k, v in meta.items())
    return f"---\n{front}\n---\n\n{body}\n"


class FakeClient:
    """Stands in for anthropic.Anthropic(); records requests, returns canned replies."""

    def __init__(self, replies=None, parsed=None):
        self.replies = list(replies or [])
        self.parsed = parsed
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create, parse=self._parse))

    def _response(self, kwargs, text="", parsed=None):
        self.requests.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200,
                                cache_creation_input_tokens=0, cache_read_input_tokens=5000)
        return SimpleNamespace(model=kwargs["model"], usage=usage, stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text=text)],
                               parsed_output=parsed)

    def _create(self, **kwargs):
        return self._response(kwargs, self.replies.pop(0) if self.replies else "")

    def _parse(self, **kwargs):
        return self._response(kwargs, parsed=self.parsed)


@pytest.fixture
def workspace(tmp_path):
    """A throwaway copy of config/ and kb/ plus two published test articles."""
    shutil.copytree(REPO / "config", tmp_path / "config")
    shutil.copytree(REPO / "kb", tmp_path / "kb")
    reg = tmp_path / "kb" / "rego" / "registration"
    reg.mkdir(parents=True)
    (reg / "interstate.md").write_text(article(
        "reg-interstate-transfer", "Interstate vehicle transfer", effective_from="2026-07-01",
        body="## When this applies\n\nVehicles registered in another state.\n\n"
             "## What you need\n\n- Blue Slip (AUVIS inspection)\n- Proof of identity\n\n"
             "## Fees\n\nPay the registration fee and motor vehicle duty."), encoding="utf-8")
    (reg / "renewal.md").write_text(article(
        "reg-renewal", "Registration renewal",
        body="## Steps\n\n1. Check the Green Slip (CTP) is current.\n2. Pink Slip if the car is over five years old."),
        encoding="utf-8")
    (reg / "future.md").write_text(article(
        "reg-future-rule", "Rule not yet in effect", effective_from="2099-01-01", body="Later."),
        encoding="utf-8")
    return Settings(root=tmp_path, kb_dir=tmp_path / "kb", config_dir=tmp_path / "config",
                    inbox_dir=tmp_path / "inbox", usage_log=tmp_path / "data" / "usage.jsonl",
                    vector_dir=tmp_path / "data" / "vectors", monthly_budget_usd=10.0)


class FakeEmbedder:
    """Hashed bag of words with a few Chinese -> English synonyms, standing in for a real
    multilingual model. Counts how many texts it embedded."""

    name = "fake-embedder"
    SYNONYMS = {"外州": "interstate", "材料": "need", "续期": "renewal", "绿单": "green"}

    def __init__(self):
        self.embedded = 0

    def embed(self, texts):
        import re
        import zlib

        self.embedded += len(texts)
        out = []
        for t in texts:
            for zh, en in self.SYNONYMS.items():
                t = t.replace(zh, f" {en} ")
            v = [0.0] * 64
            for w in re.findall(r"[a-z]+", t.lower()):
                v[zlib.crc32(w.encode()) % 64] += 1.0
            out.append(v)
        return out
