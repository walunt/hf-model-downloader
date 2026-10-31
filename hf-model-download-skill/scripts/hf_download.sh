#!/usr/bin/env bash
# ============================================================
# 通用 HuggingFace 模型下载器 (hfd.sh + aria2 多线程, 断点续传) v1.1
# 全部参数通过环境变量传入:
#   REPO        必填, 形如 org/name
#   DEST        必填, 本地保存目录
#   HFD_X       单文件连接数 1-10 (默认 10)
#   HFD_J       并行文件数 1-10 (默认 8)
#   HF_ENDPOINT 下载源 (默认 https://hf-mirror.com)
#   NEED_GB     磁盘预检阈值 GB (0 跳过)
#   HF_USERNAME gated 仓库需要的用户名(仅非空校验, 鉴权用 token)
#   HF_TOKEN    缺省时读 ~/.huggingface/token
# 完成/失败发 macOS 通知; 校验结果写 <DEST>/.hfd/verify_result.json
# ============================================================
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

: "${REPO:?缺少 REPO (org/name)}"
: "${DEST:?缺少 DEST}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HFD="$SCRIPT_DIR/hfd.sh"
X="${HFD_X:-10}"; J="${HFD_J:-8}"
export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
USERNAME="${HF_USERNAME:-hf_user}"
NEED_GB="${NEED_GB:-0}"

TOKEN="${HF_TOKEN:-}"
if [ -z "$TOKEN" ] && [ -f "$HOME/.huggingface/token" ]; then
  TOKEN="$(cat "$HOME/.huggingface/token")"
fi

# ---------- macOS 通知 ----------
notify(){ # $1=副标题 $2=正文
  command -v osascript >/dev/null 2>&1 || return 0
  osascript -e "display notification \"${2:-}\" with title \"HF 模型下载器\" subtitle \"${1:-}\"" >/dev/null 2>&1 || true
}
on_err(){
  local code=$?
  notify "下载中断/失败" "$REPO 异常退出(code=$code)，可在看板点「继续」断点续传"
}
trap on_err ERR

# ---------- 磁盘预检 ----------
if [ "${NEED_GB:-0}" -gt 0 ] 2>/dev/null; then
  mkdir -p "$DEST"
  AVAIL_GB=$(df -k "$DEST" | awk 'NR==2 {printf "%d", $4/1024/1024}')
  echo "[预检] $REPO | 目标盘可用 ${AVAIL_GB}GB (建议 ≥${NEED_GB}GB)"
  if [ "${AVAIL_GB:-0}" -lt "$NEED_GB" ]; then
    echo "!! 磁盘空间不足: 可用 ${AVAIL_GB}GB < 需要 ${NEED_GB}GB" >&2; exit 1
  fi
fi

# ---------- 依赖 ----------
for t in aria2c wget curl python3; do
  command -v "$t" >/dev/null 2>&1 || { echo "!! 缺少 $t — brew install aria2 wget" >&2; exit 1; }
done

# ---------- 下载 ----------
mkdir -p "$DEST"
echo "[下载] $REPO"
echo "       -> $DEST"
echo "       aria2 -x $X -j $J | 源: $HF_ENDPOINT"
AUTH=()
if [ -n "$TOKEN" ]; then AUTH=(--hf_username "$USERNAME" --hf_token "$TOKEN"); fi
"$HFD" "$REPO" --local-dir "$DEST" --tool aria2c -x "$X" -j "$J" ${AUTH[@]+"${AUTH[@]}"}

# ---------- 通用 sha256 校验: 元数据里所有 LFS 文件 ----------
echo "[校验] 与 HF 官方 sha256 比对全部 LFS 文件 ..."
if python3 - "$DEST" <<'PY'
import json, sys, os, hashlib, time
dest = sys.argv[1]
meta = os.path.join(dest, ".hfd", "repo_metadata.json")
with open(meta) as f:
    data = json.load(f)
expect = {s["rfilename"]: s["lfs"]["sha256"]
          for s in data.get("siblings", []) if s.get("lfs")}
if not expect:
    print("[校验] 元数据无 LFS 条目, 跳过"); sys.exit(0)
ok = bad = miss = 0
for name, sha in sorted(expect.items()):
    p = os.path.join(dest, name)
    if not os.path.exists(p):
        print(f"MISSING  {name}"); miss += 1; continue
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() == sha: ok += 1
    else: print(f"CORRUPT  {name}"); bad += 1
res = {"ok": ok, "total": len(expect), "bad": bad, "miss": miss,
       "ts": time.strftime("%F %T")}
with open(os.path.join(dest, ".hfd", "verify_result.json"), "w") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print(f"[校验] 通过 {ok}/{len(expect)}" + ("" if not (bad or miss) else f" | 损坏 {bad} 缺失 {miss}"))
sys.exit(1 if (bad or miss) else 0)
PY
then
  echo "[完成] 模型已就绪: $DEST"
  notify "下载并校验完成" "$REPO 全部文件 sha256 一致，已就绪"
else
  notify "校验未通过" "$REPO 存在损坏或缺失，详见日志，可续传后重校验"
  exit 1
fi
