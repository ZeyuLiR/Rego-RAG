"""One place where the app calls Claude: budget check, refusal fallback, usage logging."""

from __future__ import annotations

from typing import Any

import anthropic

from .usage import UsageLog

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class RefusedError(Exception):
    pass


class LLM:
    def __init__(self, usage: UsageLog, client: Any | None = None):
        self.usage = usage
        self._client = client

    @property
    def client(self):
        # Created lazily so tests and offline commands never need an API key.
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def create(self, purpose: str, **kwargs):
        """messages.create with server-side refusal fallback; logs usage."""
        self.usage.check_budget()
        response = self.client.beta.messages.create(
            betas=[FALLBACK_BETA], fallbacks="default", **kwargs
        )
        self._finish(purpose, response)
        return response

    def parse(self, purpose: str, **kwargs):
        """messages.parse (structured output into a Pydantic model); logs usage."""
        self.usage.check_budget()
        response = self.client.beta.messages.parse(
            betas=[FALLBACK_BETA], fallbacks="default", **kwargs
        )
        self._finish(purpose, response)
        return response

    def _finish(self, purpose: str, response) -> None:
        self.usage.record(purpose, response.model, response.usage)
        if response.stop_reason == "refusal":
            raise RefusedError("模型拒绝回答这个请求。")


def text_of(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()
