#!/usr/bin/env bash
# ============================================================
# DeepSeek-V4-Flash-Vision-Exp (MLX) 一键下载 + 校验
#   仓库: orcarouter/DeepSeek-V4-Flash-Vision-Uncensored-MLX  (≈156GB, gated)
#   通道: hf-mirror 镜像 + hfd.sh + aria2 多线程, 断点续传
#   校验: 全部 .safetensors 与 HF 官方 sha256 逐文件比对
#
# 用法:
#   export HF_TOKEN=<你的 read token>
#   ./download_dsv4_vision.sh                          # 默认 -x 10 -j 8
#   HFD_X=8 HFD_J=4 ./download_dsv4_vision.sh          # 宽带 <300M 时
#   HFD_DEST=/Volumes/... ./download_dsv4_vision.sh    # 自定义下载目录
#
# 注意: hfd.sh 的 -x/-j 上限为 10; 断点续传自动生效, 中断后重跑同命令即可
# ============================================================
set -euo pipefail

REPO="orcarouter/DeepSeek-V4-Flash-Vision-Uncensored-MLX"
DEST="${HFD_DEST:-$HOME/mlx-models/orcarouter/DeepSeek-V4-Flash-Vision-Uncensored-MLX}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HFD="$SCRIPT_DIR/hfd.sh"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"

X="${HFD_X:-10}"; J="${HFD_J:-8}"   # aria2 每文件线程 / 并行文件数 (hfd.sh 限 1-10)

# ---------- Token 解析: 环境变量 > ~/.huggingface/token ----------
TOKEN="${HF_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$HOME/.huggingface/token" ]; then
  TOKEN="$(cat "$HOME/.huggingface/token")"
fi
if [ -z "$TOKEN" ]; then
  echo "!! 缺少 HF token: export HF_TOKEN=xxx 或写入 ~/.huggingface/token 后重跑" >&2
  echo "   获取: https://huggingface.co/settings/tokens (read 权限即可)" >&2
  exit 1
fi

# ---------- Username 解析: hfd.sh 对 gated 仓库要求 username+token 成对传入 ----------
# 注意: hfd.sh 的请求实际只用 token 鉴权, username 仅用于非空校验; 可覆盖为 HF_USERNAME
USERNAME="${HF_USERNAME:-hf_user}"

# ---------- 磁盘预检: 预留 ≥2 倍模型体积 (156GB × 2 = 312GB) ----------
NEED_GB=312
AVAIL_GB=$(df -k "$HOME" | awk 'NR==2 {printf "%d", $4/1024/1024}')
echo "[预检] $REPO ≈156GB | 磁盘可用 ${AVAIL_GB}GB (需 ≥${NEED_GB}GB)"
if [ "$AVAIL_GB" -lt "$NEED_GB" ]; then
  echo "!! 磁盘空间不足, 请清理或改 HFD_DEST 指向其他卷" >&2
  exit 1
fi

# ---------- 依赖检查 ----------
for t in aria2c wget curl python3; do
  command -v "$t" >/dev/null 2>&1 || { echo "!! 缺少 $t — 请先安装: brew install aria2 wget" >&2; exit 1; }
done

# ---------- 下载 (断点续传: 中断后原命令重跑即可) ----------
echo "[下载] $REPO"
echo "       -> $DEST"
echo "       aria2 -x $X -j $J | 镜像: $HF_ENDPOINT"
mkdir -p "$(dirname "$DEST")"
"$HFD" "$REPO" --local-dir "$DEST" --tool aria2c -x "$X" -j "$J" --hf_username "$USERNAME" --hf_token "$TOKEN"

# ---------- 校验: 与 HF 元数据 sha256 逐文件比对 ----------
echo "[校验] 与 HF 官方 sha256 比对全部 .safetensors ..."
python3 - "$DEST" <<'PY'
import json, sys, os, hashlib
dest = sys.argv[1]
meta = os.path.join(dest, ".hfd", "repo_metadata.json")
with open(meta) as f:
    data = json.load(f)
expect = {s["rfilename"]: s["lfs"]["sha256"]
          for s in data.get("siblings", [])
          if s.get("rfilename", "").endswith(".safetensors") and s.get("lfs")}
if not expect:
    print("!! 元数据中未找到 .safetensors 条目, 校验中止"); sys.exit(1)
ok = bad = 0
for name, sha in sorted(expect.items()):
    p = os.path.join(dest, name)
    if not os.path.exists(p):
        print(f"MISSING  {name}"); bad += 1; continue
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() == sha:
        ok += 1
    else:
        print(f"CORRUPT  {name}"); bad += 1
print(f"[校验] 通过 {ok}/{len(expect)}" + ("" if bad else " — 全部一致 ✓"))
sys.exit(1 if bad else 0)
PY

echo "[完成] 模型已就绪: $DEST"
echo "       oMLX 挂载: omlx serve --model-dir $(dirname "$DEST") --ssd-kv-cache true"
