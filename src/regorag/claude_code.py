"""Run the app's model calls through the Claude Code CLI instead of the API.

`claude -p` uses whatever account Claude Code is logged in with, so during setup the
system can run on the owner's Claude plan with no API key. It exposes the same
`beta.messages.create/parse` shape as the Anthropic SDK client, so the rest of the app
does not know which backend it is talking to. Switch with REGORAG_LLM_BACKEND.

Calls run with no tools, no MCP servers, no settings or CLAUDE.md files, and no saved
session, in an empty temporary directory: the model sees only the prompt we send.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace


class ClaudeCodeError(RuntimeError):
    pass


def plan_env() -> dict[str, str]:
    """The environment for `claude -p` without API credentials: with ANTHROPIC_API_KEY set,
    Claude Code bills the API instead of the Claude plan this backend is meant to use."""
    return {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}


def _system_text(system) -> str:
    if isinstance(system, str):
        return system
    return "\n\n".join(block["text"] for block in system or [])


def _prompt(messages: list[dict]) -> str:
    """Fold a multi-turn conversation into one prompt (the CLI takes a single message)."""
    *earlier, last = messages
    if not earlier:
        return last["content"]
    convo = "\n\n".join(f"<{m['role']}>\n{m['content']}\n</{m['role']}>" for m in earlier)
    return (f"<earlier_conversation>\n{convo}\n</earlier_conversation>\n\n"
            f"Answer the latest message:\n{last['content']}")


class ClaudeCodeClient:
    def __init__(self, executable: str = "claude", timeout_s: int = 600):
        self.executable = executable
        self.timeout_s = timeout_s
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create, parse=self.parse))

    def available(self) -> bool:
        return shutil.which(self.executable) is not None

    def command(self) -> list[str]:
        """The program to start. On Windows an npm install puts a `claude.cmd` shim on PATH,
        which CreateProcess cannot start and cmd.exe would mangle our arguments through, so
        run the program the shim points at instead."""
        exe = shutil.which(self.executable)
        if exe is None:
            raise ClaudeCodeError(
                "找不到 Claude Code（claude 命令）。请先安装并登录 Claude Code，"
                "或在 .env 里设置 REGORAG_LLM_BACKEND=api 并填写 ANTHROPIC_API_KEY。")
        if not exe.lower().endswith((".cmd", ".bat")):
            return [exe]
        pkg = Path(exe).parent / "node_modules" / "@anthropic-ai" / "claude-code"
        for native in (pkg / "bin" / "claude.exe", pkg / "bin" / "claude"):
            if native.is_file():
                return [str(native)]
        node = shutil.which("node")
        if (pkg / "cli.js").is_file() and node:
            return [node, str(pkg / "cli.js")]
        raise ClaudeCodeError(
            f"找到的 Claude Code 是 {exe}，无法直接调用。请在 PowerShell 运行 "
            "irm https://claude.ai/install.ps1 | iex 用官方方式安装后再试。")

    def create(self, *, model, system=None, messages, output_config=None, **_ignored):
        data = self._run(model, _system_text(system), _prompt(messages),
                         (output_config or {}).get("effort"))
        return self._response(model, data, data.get("result", ""))

    def parse(self, *, model, system=None, messages, output_format, output_config=None, **_ignored):
        schema = output_format.model_json_schema()
        data = self._run(model, _system_text(system), _prompt(messages),
                         (output_config or {}).get("effort"), schema)
        raw = data.get("structured_output")
        if raw is None:
            raw = json.loads(data.get("result") or "null")
        resp = self._response(model, data, json.dumps(raw, ensure_ascii=False))
        resp.parsed_output = output_format.model_validate(raw)
        return resp

    def _run(self, model: str, system: str, prompt: str, effort: str | None,
             schema: dict | None = None) -> dict:
        program = self.command()
        with tempfile.TemporaryDirectory(prefix="regorag-cc-") as tmp:
            system_file = Path(tmp) / "system.txt"
            system_file.write_text(system, encoding="utf-8")  # too long for argv once the KB grows
            cmd = [*program, "-p", "--output-format", "json", "--model", model,
                   "--system-prompt-file", str(system_file), "--tools", "",
                   "--strict-mcp-config", "--setting-sources", "", "--no-session-persistence"]
            if effort:
                cmd += ["--effort", effort]
            if schema:
                cmd += ["--json-schema", json.dumps(schema)]
            try:
                # Claude Code speaks UTF-8; Windows would otherwise decode with the local code page.
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", cwd=tmp,
                                      timeout=self.timeout_s, env=plan_env())
            except subprocess.TimeoutExpired as exc:
                raise ClaudeCodeError(f"Claude Code 超过 {self.timeout_s} 秒没有回应") from exc
            except OSError as exc:
                raise ClaudeCodeError(f"无法启动 Claude Code（{program[0]}）：{exc}") from exc
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            detail = (proc.stderr or proc.stdout).strip()[:500]
            raise ClaudeCodeError(f"Claude Code 调用失败：{detail}") from exc
        if data.get("is_error"):
            raise ClaudeCodeError(f"Claude Code 调用失败：{str(data.get('result'))[:500]}")
        return data

    @staticmethod
    def _response(model: str, data: dict, text: str) -> SimpleNamespace:
        u = data.get("usage") or {}
        usage = SimpleNamespace(
            input_tokens=u.get("input_tokens", 0),
            output_tokens=u.get("output_tokens", 0),
            cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
            cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
        )
        refused = data.get("stop_reason") == "refusal"
        return SimpleNamespace(model=model, usage=usage,
                               stop_reason="refusal" if refused else "end_turn",
                               content=[SimpleNamespace(type="text", text=text)],
                               parsed_output=None)
