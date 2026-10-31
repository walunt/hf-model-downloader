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
  # 写阶段标记, 供看板区分"下载中/校验中/失败"
  mkdir -p "$DEST/.hfd" 2>/dev/null || true
  echo "{\"phase\":\"failed\",\"msg\":\"下载中断(code=$code)\"}" > "$DEST/.hfd/status.json" 2>/dev/null || true
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

# ---------- 下载(失败自动重试: 首次 + 最多 3 次重试, 指数退避 30/60/120s) 或 仅校验模式 ----------
mkdir -p "$DEST"

# ---------- 续传预检: 先校验已下载部分, 坏掉的删掉让 aria2c 重新下 ----------
# aria2c 续传只比对文件大小: 大小已满但内容损坏的文件会被判定"已完成"而跳过,
# 于是错误要拖到最后的整包校验才暴露(白跑几小时)。这里在下载前先扫一遍:
#   大小不足              → 未下完, 保留(aria2c 会续传, 删掉等于白下)
#   大小已满但 sha256 不符 → 损坏, 删除后重新下载
# 可用 HFD_PRECHECK=0 关闭(确认本地文件都完好时, 可跳过这段校验时间)
if [ "${HFD_PRECHECK:-1}" = "1" ] && [ -f "$DEST/.hfd/repo_metadata.json" ]; then
  echo "[预检] 校验已下载部分(仅比对已下满的文件), 损坏的会删除后重新下载 ..."
  python3 - "$DEST" <<'PYCHECK' || echo "[预检] 预检未正常完成, 忽略并继续下载"
import json, os, sys, hashlib, time
dest = sys.argv[1]
meta = os.path.join(dest, ".hfd", "repo_metadata.json")
try:
    with open(meta) as f: data = json.load(f)
except Exception as e:
    print("[预检] 元数据读取失败, 跳过:", e); sys.exit(0)
expect = {}
for sib in data.get("siblings", []):
    lfs = (sib.get("lfs") or {})
    name = sib.get("rfilename")
    if not name: continue
    size = sib.get("size") or lfs.get("size") or 0
    expect[name] = (lfs.get("sha256") or "", int(size or 0))
if not expect:
    print("[预检] 元数据无文件条目, 跳过"); sys.exit(0)
# 只处理"已下满"的文件: 大小不足的是正常断点, 交给 aria2c 续传, 不能删
full = []
for name, (sha, size) in expect.items():
    p = os.path.join(dest, name)
    if not os.path.exists(p): continue
    try: cur = os.path.getsize(p)
    except OSError: continue
    if size > 0 and cur != size: continue      # 未下满 → 跳过
    if not sha: continue                        # 无 sha256 可比对 → 跳过
    full.append((name, p, sha))
print("[预检] 已下满待比对 %d 个 / 共 %d 个文件" % (len(full), len(expect)))
removed = kept = 0
t0 = time.time()
for i, (name, p, sha) in enumerate(sorted(full), 1):
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(chunk)
    except OSError as e:
        print("[预检] 读取失败 %s: %s" % (name, e)); continue
    if h.hexdigest() == sha:
        kept += 1
    else:
        try:
            os.remove(p); removed += 1
            print("[预检] 损坏已删除(将重新下载): %s" % name)
        except OSError as e:
            print("[预检] 无法删除 %s: %s" % (name, e))
    if i % 10 == 0 or i == len(full):
        print("[预检] 进度 %d/%d (%.0fs)" % (i, len(full), time.time() - t0))
print("[预检] 完成: 完好 %d 个, 删除重下 %d 个, 耗时 %.0fs"
      % (kept, removed, time.time() - t0))
sys.exit(0)
PYCHECK
fi

if [ "${HFD_VERIFY_ONLY:-0}" = "1" ]; then
  echo "[校验] 仅校验模式(不下载), 直接比对已下载文件的 sha256 ..."
else
  MAX_ATTEMPTS=4
  RETRY_WAIT="${RETRY_WAIT:-30}"
  AUTH=(); TOKF=""
  # 安全: token 不走命令行(ps 可见), 改为写入 600 临时文件, 由 hfd.sh 读取
  if [ -n "$TOKEN" ]; then
    TOKF="$(mktemp "${TMPDIR:-/tmp}/hftok.XXXXXX")"
    chmod 600 "$TOKF"
    printf '%s' "$TOKEN" > "$TOKF"
    export HF_TOKEN_FILE="$TOKF"
    AUTH=(--hf_username "$USERNAME")   # 用户名非敏感, hfd.sh 的 gated 校验要求非空
  fi
  # 退出/中断时清掉临时 token 文件, 并抹掉 aria2c_urls.txt 里的 Authorization 头
  scrub_secrets() {
    [ -n "${TOKF:-}" ] && rm -f "$TOKF"
    local f="$DEST/.hfd/aria2c_urls.txt"
    if [ -f "$f" ] && grep -q 'Authorization: Bearer' "$f" 2>/dev/null; then
      grep -v 'Authorization: Bearer' "$f" > "$f.tmp" 2>/dev/null && mv "$f.tmp" "$f" || rm -f "$f.tmp"
      echo "[安全] 已清除 aria2c_urls.txt 中的鉴权头"
    fi
  }
  trap scrub_secrets EXIT
  scrub_secrets   # 先清一次上一次运行可能留下的残留
  export HFD_SPEED_LIMIT="${HFD_SPEED_LIMIT:-0}"
  attempt=1
  while :; do
    LIMIT_TXT=""
    if [ "${HFD_SPEED_LIMIT:-0}" != "0" ]; then LIMIT_TXT=" | 限速 ${HFD_SPEED_LIMIT}MB/s"; fi
    echo "[下载] $REPO (第 $attempt 次尝试)"
    echo "       -> $DEST"
    echo "       aria2 -x $X -j $J | 源: $HF_ENDPOINT${LIMIT_TXT}"
    if "$HFD" "$REPO" --local-dir "$DEST" --tool aria2c -x "$X" -j "$J" ${AUTH[@]+"${AUTH[@]}"}; then
      break
    fi
    if [ "$attempt" -ge "$MAX_ATTEMPTS" ]; then
      echo "!! 下载连续失败 ${MAX_ATTEMPTS} 次, 已放弃; 可稍后在看板点「继续」" >&2
      # 显式写失败标记(此处在 if 条件内, ERR trap 不会触发), 供看板区分"失败"与"中断"
      mkdir -p "$DEST/.hfd" 2>/dev/null || true
      echo "{\"phase\":\"failed\",\"msg\":\"下载连续失败 ${MAX_ATTEMPTS} 次\"}" > "$DEST/.hfd/status.json" 2>/dev/null || true
      exit 1
    fi
    wait=$((RETRY_WAIT * (1 << (attempt - 1))))  # 30/60/120
    echo "[重试] 等待 ${wait}s 后重试(第 $((attempt + 1))/${MAX_ATTEMPTS} 次)..." >&2
    sleep "$wait"
    attempt=$((attempt + 1))
  done
fi

# ---------- 通用 sha256 校验: 元数据里所有 LFS 文件 ----------
echo "[校验] 与 HF 官方 sha256 比对全部 LFS 文件 ..."
if python3 - "$DEST" <<'PY'
import json, sys, os, hashlib, time
dest = sys.argv[1]
meta = os.path.join(dest, ".hfd", "repo_metadata.json")
def st(**kw):
    """写阶段标记: 校验开始/进度/结束, 供看板显示'校验中 x/y'与失败状态"""
    p = os.path.join(dest, ".hfd", "status.json")
    try:
        with open(p) as f: d = json.load(f)
    except Exception: d = {}
    d.update(kw)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f: json.dump(d, f)
    except Exception: pass
with open(meta) as f:
    data = json.load(f)
expect = {}
for s in data.get("siblings", []):
    lfs = s.get("lfs") or {}
    if s.get("rfilename") and lfs.get("sha256"):
        expect[s["rfilename"]] = lfs["sha256"]
if not expect:
    print("[校验] 元数据无 LFS 条目, 跳过"); sys.exit(0)
total_n = len(expect)
st(phase="verifying", done=0, total=total_n, msg="")
ok = bad = miss = 0
for i, (name, sha) in enumerate(sorted(expect.items()), 1):
    p = os.path.join(dest, name)
    if not os.path.exists(p):
        print(f"MISSING  {name}"); miss += 1; continue
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() == sha: ok += 1
    else: print(f"CORRUPT  {name}"); bad += 1
    if i % 5 == 0 or i == total_n: st(done=i)  # 每 5 个文件更新一次校验进度
res = {"ok": ok, "total": total_n, "bad": bad, "miss": miss,
       "ts": time.strftime("%F %T")}
with open(os.path.join(dest, ".hfd", "verify_result.json"), "w") as f:
    json.dump(res, f, ensure_ascii=False, indent=2)
print(f"[校验] 通过 {ok}/{total_n}" + ("" if not (bad or miss) else f" | 损坏 {bad} 缺失 {miss}"))
if bad or miss:
    st(phase="failed", msg="sha256 校验未通过", done=total_n, total=total_n)
    sys.exit(1)
st(phase="done", msg="", done=total_n, total=total_n)
sys.exit(0)
PY
then
  echo "[完成] 模型已就绪: $DEST"
  notify "下载并校验完成" "$REPO 全部文件 sha256 一致，已就绪"
else
  notify "校验未通过" "$REPO 存在损坏或缺失，详见日志，可续传后重校验"
  exit 1
fi
