#!/usr/bin/env bash
# 运行渲染层测试（84 项断言）
# 用法: bash tests/run.sh   或   node tests/render.test.js
set -euo pipefail
cd "$(dirname "$0")/.."
exec node tests/render.test.js
