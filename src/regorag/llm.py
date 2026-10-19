"""One place where the app calls Claude: budget check, refusal fallback, usage logging."""

from __future__ import annotations

from typing import Any

import anthropic

from .usage import UsageLog

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class RefusedError(Exception):
    pass


class LLM:
    """backend "api": the Anthropic API (needs ANTHROPIC_API_KEY, billed per token).
    backend "claude-code": the Claude Code CLI on this machine (runs on the Claude plan it is
    logged in with; for setup and testing before an API key exists)."""

    def __init__(self, usage: UsageLog, client: Any | None = None, backend: str = "api"):
        self.usage = usage
        self.backend = backend
        self._client = client

    @property
    def client(self):
        # Created lazily so tests and offline commands never need credentials.
        if self._client is None:
            if self.backend == "claude-code":
                from .claude_code import ClaudeCodeClient

                self._client = ClaudeCodeClient()
            else:
                self._client = anthropic.Anthropic()
        return self._client

    def _extra(self) -> dict:
        # Server-side refusal fallback is an API feature; the CLI handles its own.
        return {"betas": [FALLBACK_BETA], "fallbacks": "default"} if self.backend == "api" else {}

    def create(self, purpose: str, **kwargs):
        """messages.create; logs usage."""
        self.usage.check_budget()
        response = self.client.beta.messages.create(**self._extra(), **kwargs)
        self._finish(purpose, response)
        return response

    def parse(self, purpose: str, **kwargs):
        """messages.parse (structured output into a Pydantic model); logs usage."""
        self.usage.check_budget()
        response = self.client.beta.messages.parse(**self._extra(), **kwargs)
        self._finish(purpose, response)
        return response

    def _finish(self, purpose: str, response) -> None:
        self.usage.record(purpose, response.model, response.usage, backend=self.backend)
        if response.stop_reason == "refusal":
            raise RefusedError("模型拒绝回答这个请求。")


def text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()
