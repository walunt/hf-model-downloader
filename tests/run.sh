#!/usr/bin/env bash
# 跑全部测试：后端加载(Python) + 渲染层(Node)
# 用法: bash tests/run.sh
set -uo pipefail
cd "$(dirname "$0")/.."
rc=0

echo "──────── 后端加载测试 ────────"
python3 tests/test_backend_load.py || rc=1
echo
echo "──────── 渲染层测试 ────────"
node tests/render.test.js || rc=1
echo
if [ "$rc" -eq 0 ]; then echo "全部通过"; else echo "有失败项"; fi
exit "$rc"
