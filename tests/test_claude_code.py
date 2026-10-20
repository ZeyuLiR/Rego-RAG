import json
import os
import stat
from dataclasses import replace

import pytest

from regorag.app import build_app
from regorag.claude_code import ClaudeCodeClient, ClaudeCodeError
from regorag.ingest import Proposal

FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
system = open(args[args.index("--system-prompt-file") + 1], encoding="utf-8").read()
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"args": args, "prompt": prompt, "system": system, "cwd": os.getcwd()}) + "\n")
out = {"is_error": False, "stop_reason": "end_turn",
       "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}
if "--json-schema" in args:
    out["structured_output"] = {"source_summary_zh": "无变化", "changes": []}
    out["result"] = json.dumps(out["structured_output"])
else:
    out["result"] = "答案 [[reg-renewal]]\nSOURCES: reg-renewal"
print(json.dumps(out))
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    exe = tmp_path / "claude"
    exe.write_text(FAKE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    return ClaudeCodeClient(executable=str(exe)), log


def calls(log):
    return [json.loads(l) for l in log.read_text().splitlines()]


def test_qa_through_claude_code(workspace, fake_claude):
    client, log = fake_claude
    app = build_app(replace(workspace, llm_backend="claude-code"), client=client)
    ans = app.qa.ask("续期要什么？")
    assert ans.covered and ans.sources == ["reg-renewal"]
    c = calls(log)[0]
    assert c["prompt"] == "续期要什么？"
    assert "Answer only from the knowledge base" in c["system"] and "reg-renewal" in c["system"]
    assert c["args"][c["args"].index("--tools") + 1] == ""
    assert "--no-session-persistence" in c["args"] and "--strict-mcp-config" in c["args"]
    assert "--betas" not in c["args"]
    rec = app.usage.records()[-1]
    assert rec.backend == "claude-code"
    assert app.usage.summary()["claude_code_calls"] == 1 and app.usage.month_total() == 0


def test_history_is_folded_into_one_prompt(workspace, fake_claude):
    from regorag.qa import Turn

    client, log = fake_claude
    app = build_app(replace(workspace, llm_backend="claude-code"), client=client)
    app.qa.ask("那摩托车呢？", history=[Turn("user", "续期要什么？"), Turn("assistant", "要 Green Slip。")])
    prompt = calls(log)[0]["prompt"]
    assert "<user>\n续期要什么？\n</user>" in prompt and prompt.endswith("那摩托车呢？")


def test_structured_extraction_through_claude_code(fake_claude):
    client, log = fake_claude
    resp = client.parse(model="claude-opus-5-5", system="s", messages=[{"role": "user", "content": "doc"}],
                        output_format=Proposal)
    assert resp.parsed_output == Proposal(source_summary_zh="无变化", changes=[])
    assert "--json-schema" in calls(log)[0]["args"]


def test_missing_cli_gives_clear_error():
    client = ClaudeCodeClient(executable="definitely-not-installed-claude")
    with pytest.raises(ClaudeCodeError, match="REGORAG_LLM_BACKEND=api"):
        client.create(model="m", messages=[{"role": "user", "content": "q"}])


def test_backend_defaults_from_api_key(monkeypatch):
    from regorag.config import Settings

    monkeypatch.delenv("REGORAG_LLM_BACKEND", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert Settings.from_env().llm_backend == "claude-code"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert Settings.from_env().llm_backend == "api"


def _npm_shim(tmp_path, *files):
    tmp_path.mkdir(parents=True, exist_ok=True)
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\n")
    shim.chmod(0o755)
    pkg = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code"
    for f in files:
        (pkg / f).parent.mkdir(parents=True, exist_ok=True)
        (pkg / f).write_text("")
    return ClaudeCodeClient(executable=str(shim)), pkg


def test_windows_npm_shim_runs_the_real_program(tmp_path, monkeypatch):
    # npm on Windows puts claude.cmd on PATH; starting it directly fails with WinError 2.
    client, pkg = _npm_shim(tmp_path, "bin/claude.exe")
    assert client.command() == [str(pkg / "bin" / "claude.exe")]

    client, pkg = _npm_shim(tmp_path / "old", "cli.js")
    monkeypatch.setattr("regorag.claude_code.shutil.which",
                        lambda name: "/usr/bin/node" if name == "node" else str(tmp_path / "old" / "claude.cmd"))
    assert client.command() == ["/usr/bin/node", str(pkg / "cli.js")]


def test_unusable_shim_gives_clear_error(tmp_path):
    client, _ = _npm_shim(tmp_path)
    with pytest.raises(ClaudeCodeError, match="install.ps1"):
        client.command()
