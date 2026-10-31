#!/usr/bin/env bash
# 双击启动 HF 模型下载器: 已运行则直接打开页面, 未运行则后台拉起
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if ! curl -s --max-time 2 http://127.0.0.1:8765/api/status >/dev/null 2>&1; then
  nohup python3 progress_server.py > server.log 2>&1 &
  sleep 3
fi
open "http://localhost:8765"
