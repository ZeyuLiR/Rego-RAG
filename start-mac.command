#!/bin/bash
# Double-click to start the knowledge base on macOS.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "[rego] 没找到 Python。请先安装：https://www.python.org/downloads/"
  read -r -p "按回车关闭"; exit 1
fi
python3 start.py
read -r -p "已停止。按回车关闭"
