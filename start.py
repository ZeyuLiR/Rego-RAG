"""One-step start: set up on first run, then open the knowledge base in the browser.

Double-click start-mac.command (macOS) or start-windows.bat (Windows), or run
`python3 start.py`. The first run creates .venv and installs everything (a few minutes);
later runs pull the latest version (in a git checkout) and only reinstall when
pyproject.toml changes.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
BIN = VENV / ("Scripts" if os.name == "nt" else "bin")
PY = BIN / ("python.exe" if os.name == "nt" else "python")
STAMP = VENV / ".installed"


def say(msg: str) -> None:
    print(f"[rego] {msg}", flush=True)


def update() -> None:
    """Fetch the latest version when this folder is a git checkout. Never blocks startup."""
    if not (ROOT / ".git").exists() or not shutil.which("git"):
        return
    try:
        r = subprocess.run(["git", "-C", str(ROOT), "pull", "--ff-only", "-q"],
                           capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return
    if r.returncode != 0:
        say("没能自动更新到最新版本（网络或本地有改动），继续用当前版本启动。")


def install() -> None:
    wanted = hashlib.sha256((ROOT / "pyproject.toml").read_bytes()).hexdigest()
    if not PY.exists():
        say("第一次启动：正在准备运行环境……")
        venv.create(VENV, with_pip=True)
    if STAMP.exists() and STAMP.read_text() == wanted:
        return
    say("正在安装组件（第一次需要几分钟）……")
    subprocess.run([str(PY), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=True)
    subprocess.run([str(PY), "-m", "pip", "install", "-q", "-e", f"{ROOT}[vector]"], check=True)
    STAMP.write_text(wanted)


def env_file() -> dict[str, str]:
    env = ROOT / ".env"
    if not env.exists():
        shutil.copy(ROOT / ".env.example", env)
        say("已创建设置文件 .env（默认不用 API key，用本机的 Claude Code）。")
    out = {}
    for line in env.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def main() -> int:
    if sys.version_info < (3, 11):
        say(f"需要 Python 3.11 或更新版本，现在是 {sys.version.split()[0]}。请先安装新版 Python：https://www.python.org/downloads/")
        return 1
    os.chdir(ROOT)
    update()
    install()
    settings = env_file()
    backend = settings.get("REGORAG_LLM_BACKEND") or ("api" if settings.get("ANTHROPIC_API_KEY") else "claude-code")
    if backend == "claude-code" and not shutil.which("claude"):
        say("提醒：没找到 Claude Code。网页能打开，但提问和导入会失败。安装方法见教程第 1 步。")
    say("正在启动，浏览器会自动打开。用完关掉这个窗口即可停止。")
    return subprocess.call([str(PY), "-m", "regorag.cli", "serve", "--open"])


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError:
        say("安装失败。请检查网络后再双击一次；还不行就把这个窗口的内容截图发给 Claude。")
        sys.exit(1)
