#!/usr/bin/env python3
"""通用 HuggingFace 模型下载器 v2.0: 本地看板 + 控制台 (http://localhost:8765, http/https 双协议)
v2.0: 多任务并发下载 — tasks 列表(最多 max_models 个同时下载) + 全局分片池(global_j)自动/手动分配
"""
import json, os, time, threading, socket, ssl, subprocess, re, signal, shutil, urllib.request, urllib.error
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJ = os.path.dirname(os.path.abspath(__file__))  # 项目目录跟随脚本自身位置, 整体迁移无需改代码
LOG  = os.path.join(PROJ, "download.log")
DL_SCRIPT = "hf_download.sh"
CFG_F = os.path.join(PROJ, "dl_config.json")
PORT = 8765
WINDOW_SEC, SAMPLE_SEC = 30, 5
RATE_HIST_MAX = 360  # 5s 一个点, 保留 30 分钟速率历史用于曲线图
MAX_CONN = 10        # hfd.sh 的 -x/-j 上限, 前后端共用此约束
MAX_MODELS_LIMIT = 5 # 最大同时下载模型数上限
MAX_MODELS_LIST = 200 # 模型库单次返回上限(防超大目录拖慢接口); 超限时接口返回 truncated, 由前端明确提示
CACHE_DIRS = ("_hf-cache",)  # hfd/HF Hub 缓存目录, 不是模型本体, 模型库需跳过
ENDPOINTS = {"mirror": "https://hf-mirror.com", "direct": "https://huggingface.co"}
# 仅作为无法追踪进程组时的兜底(兼容早期任务); 正常走 killpg 精确停止本任务进程组
# 模式用 [x] 技巧避免 pgrep -f 匹配到自身(pgrep 进程自己的命令行也含该串), 否则无真进程时误报"运行中"
KILL_PATS = ("[h]f_download.sh", "[d]ownload_dsv4_vision.sh", "[h]fd.sh", "[a]ria2c")

# ---------------- 配置 ----------------
_cfg_lock = threading.Lock()
_cfg = {"default_root": os.path.expanduser("~/mlx-models"),
        "username": "", "history": [], "queue": [], "speed_limit": 0,
        "tasks": {}, "max_models": 2, "global_j": 10, "alloc_mode": "auto"}
if os.path.exists(CFG_F):
    try: _cfg.update(json.load(open(CFG_F)))
    except Exception: pass
_cfg.setdefault("queue", [])
_cfg.setdefault("tasks", {})
# 迁移: v1.x 单任务 current → tasks[dest]
if _cfg.get("current"):
    c = _cfg["current"]
    if c and c.get("dest"): _cfg["tasks"][c["dest"]] = c
    _cfg["current"] = None
_cfg.setdefault("max_models", 2)
_cfg.setdefault("global_j", 10)
_cfg.setdefault("alloc_mode", "size")
_cfg.setdefault("x", 8)  # 每分片线程数(全局, 控制台统一设置, 应用到所有任务)
# 兼容 v2.0 旧值 "auto"(按剩余大小比例) → 新枚举名 "size"
if _cfg.get("alloc_mode") == "auto": _cfg["alloc_mode"] = "size"
# 队列任务补默认字段(旧任务可能缺)
for t in _cfg.get("queue", []):
    t.setdefault("added_at", time.strftime("%F %T"))

def save_cfg():
    """原子写: 先写临时文件再 replace, 避免写一半崩溃损坏配置"""
    try:
        tmp = CFG_F + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_F)
    except OSError:
        pass

def read_token():
    try: return open(os.path.expanduser("~/.huggingface/token")).read().strip()
    except OSError: return ""

def save_token(tok):
    tok = (tok or "").strip()
    if not tok: return False
    p = os.path.expanduser("~/.huggingface/token")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f: f.write(tok)
    os.chmod(p, 0o600)
    return True

def expand(p):
    return os.path.expanduser(os.path.expandvars(p or ""))

def disk_free(path=""):
    """返回目标路径所在卷的可用字节; 路径不存在时回退到默认根目录/用户主目录"""
    for cand in (path, _cfg.get("default_root"), os.path.expanduser("~")):
        cand = expand(cand) if cand else ""
        if not cand: continue
        try:
            d = cand
            while d and not os.path.isdir(d): d = os.path.dirname(d)
            return shutil.disk_usage(d or os.path.expanduser("~")).free
        except OSError: continue
    return 0

# ---------------- 仓库解析 / API 分析 ----------------
def parse_repo(text):
    t = (text or "").strip()
    if not t: return None
    if "://" in t:
        m = re.match(r"https?://[^/]+/(.*)", t)
        if not m: return None
        t = m.group(1)
    t = re.split(r"[?#]", t.strip("/"))[0]
    if t.endswith(".git"): t = t[:-4]          # 兼容 git clone 风格
    parts = [p for p in t.split("/") if p]
    if len(parts) < 2: return None
    repo = parts[0] + "/" + parts[1]
    # HF 仓库名仅 ASCII 字母数字与 ._- ; 显式 ASCII 字符类, 避免中文等通过后在 URL 编码报错
    return repo if re.match(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$", repo) else None

def hf_api(path, endpoint_key, timeout=25):
    req = urllib.request.Request(ENDPOINTS[endpoint_key] + path,
                                 headers={"Accept": "application/json", "User-Agent": "hf-model-downloader"})
    tok = read_token()
    if tok: req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

def analyze_repo(repo, endpoint_key):
    try:
        data = hf_api(f"/api/models/{repo}?blobs=true", endpoint_key)
    except urllib.error.HTTPError as e:
        if e.code == 401: return {"ok": False, "error": "该仓库为 gated/私有, 需要有效 HF token(已%s找到)" % ("" if read_token() else "未")}
        if e.code == 404: return {"ok": False, "error": "仓库不存在(404), 请检查地址"}
        return {"ok": False, "error": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"网络请求失败: {e}（可切换下载源再试）"}
    sib = data.get("siblings", [])
    total = sum((s.get("size") or (s.get("lfs") or {}).get("size") or 0) for s in sib)
    return {"ok": True, "repo": data.get("id", repo), "gated": data.get("gated", False),
            "private": bool(data.get("private")), "files": len(sib), "total": total,
            "pipeline": data.get("pipeline_tag"), "has_token": bool(read_token())}

# ---------------- 进程控制(精确到任务进程组) ----------------
def _run(cmd): subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def dl_running():
    """任一任务进程组存活即为运行中; 无记录时回退模式匹配(仅状态显示, 可能误报但不误杀)"""
    for t in _cfg.get("tasks", {}).values():
        if _pg_alive(t.get("pgid")): return True
    for pat in KILL_PATS:
        if subprocess.run(["pgrep", "-f", pat], stdout=subprocess.DEVNULL).returncode == 0:
            return True
    return False

def _pg_alive(pgid):
    if not pgid: return False
    try: os.killpg(pgid, 0); return True
    except Exception: return False

def _phase_of(t):
    """读取 <dest>/.hfd/status.json 的下载阶段; 读不到返回 None"""
    if not t: return None
    try:
        with open(os.path.join(t.get("dest", ""), ".hfd", "status.json")) as f:
            return json.load(f).get("phase")
    except Exception:
        return None

def _aria2_running(pgid):
    """任务进程组内是否有 aria2 进程 → 真正在下载(校验/完成阶段无 aria2)"""
    if not pgid: return False
    try:
        out = subprocess.check_output(["pgrep", "-g", str(pgid), "-f", "aria2"],
                                      stderr=subprocess.DEVNULL, timeout=3)
        return bool(out.strip())
    except Exception:
        return False

def stop_task(t=None):
    """按记录的进程组精确停止; 无记录或已退出时不动作, 绝不全局 pkill(避免误伤本机其他下载)"""
    pgid = (t or {}).get("pgid")
    if not _pg_alive(pgid): return
    try: os.killpg(pgid, signal.SIGTERM)
    except Exception: pass
    time.sleep(2.5)
    if _pg_alive(pgid):
        try: os.killpg(pgid, signal.SIGKILL)
        except Exception: pass

def launch(t):
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    env["REPO"] = t["repo"]; env["DEST"] = t["dest"]
    env["HFD_X"] = str(t["x"]); env["HFD_J"] = str(t["j"])
    env["HF_ENDPOINT"] = ENDPOINTS[t["endpoint"]]
    env["HF_USERNAME"] = _cfg.get("username") or "hf_user"
    env["HFD_SPEED_LIMIT"] = str(_cfg.get("speed_limit") or 0)  # 0=不限速, 由 hfd.sh 透传给 aria2c
    total = t.get("total") or 0
    env["NEED_GB"] = str(max(1, round(2 * total / 1e9))) if total else "0"
    logf = open(LOG, "ab")
    logf.write(("\n\n===== 启动下载 %s  %s  同时%s分片×每片%s线程  %s =====\n" % (
        time.strftime("%F %T"), t["repo"], t["j"], t["x"], t["endpoint"])).encode())
    logf.flush()
    p = subprocess.Popen(["bash", DL_SCRIPT], cwd=PROJ, env=env,
                         stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    logf.close()  # 子进程已持有副本, 父进程关闭避免句柄泄漏
    t["pid"] = p.pid
    t["pgid"] = p.pid  # start_new_session 使子进程成为新进程组组长, pid==pgid
    t["want_run"] = True
    save_cfg()  # 持久化 pgid: 否则服务重启后按磁盘旧 pgid 判断, 会误判进程状态、重复拉起或停不掉任务

def launch_verify(repo, dest):
    """仅校验: 独立进程(不入 tasks, 不影响下载任务显示), 只比对 sha256 不下载"""
    env = os.environ.copy()
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + env.get("PATH", "")
    env["REPO"] = repo; env["DEST"] = dest
    env["HFD_VERIFY_ONLY"] = "1"
    logf = open(LOG, "ab")
    logf.write(("\n\n===== 仅校验 %s  %s =====\n" % (time.strftime("%F %T"), repo)).encode())
    logf.flush()
    p = subprocess.Popen(["bash", DL_SCRIPT], cwd=PROJ, env=env,
                         stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    logf.close()
    return p.pid

def _clamp(v, lo, hi, d):
    try: return max(lo, min(hi, int(v)))
    except (TypeError, ValueError): return d

def _restart_tasks(tasks):
    """锁外: 对分片/限速变化的任务 stop→launch 生效(断点续传); 跳过校验/完成/失败阶段"""
    for t in tasks:
        if not t.get("want_run"): continue
        if not _pg_alive(t.get("pgid")):
            with _cfg_lock:
                if _cfg.get("tasks", {}).get(t["dest"]) is t:
                    launch(t)
            continue
        ph = _phase_of(t)
        if ph in ("verifying", "done", "failed"): continue
        stop_task(t); time.sleep(1)
        with _cfg_lock:
            if _cfg.get("tasks", {}).get(t["dest"]) is t and t.get("want_run"):
                launch(t)

# ---------------- 全局分片池分配 ----------------
def _remaining(t):
    """任务剩余待下载字节(用目录元数据与已下大小估算)"""
    try:
        tot, _, _ = metadata(t["dest"]); have = scan(t["dest"])[0]
        return max(0, (tot or t.get("total") or 0) - have)
    except Exception:
        return 0

def allocate_j():
    """按全局分片池与分配方案重算所有活动任务的 j; 返回 j 变化(需重启生效)的任务列表。
    调用方需持有 _cfg_lock。
    alloc_mode: size=按剩余大小比例(剩余大的占比高) / small=小模型优先(剩余小的先分满, 尽快完成释放名额) / manual=手动
    自动类方案每任务≥1; 手动模式尊重各任务已设 j, 只做 1..pool 钳制, 超池由前端限制。"""
    tasks = _cfg.get("tasks", {})
    pool = max(1, int(_cfg.get("global_j") or 10))
    active = [t for t in tasks.values() if t.get("want_run")]
    if not active: return []
    changed = []
    mode = _cfg.get("alloc_mode") or "size"
    if mode == "manual":
        for t in active:
            nj = max(1, min(pool, int(t.get("j") or 1)))
            if (t.get("j") or 1) != nj:
                t["j"] = nj; changed.append(t)
        if changed: save_cfg()
        return changed
    # 自动类方案
    n = len(active)
    if pool < n: pool = n  # 池子小于任务数时每人至少 1 位
    jmap = {id(t): 1 for t in active}
    left = pool - n
    if left > 0:
        rem = {id(t): _remaining(t) for t in active}
        total_rem = sum(rem.values())
        if total_rem <= 0:
            each = pool // n
            for t in active: jmap[id(t)] = each
            for i in range(pool - each * n): jmap[id(active[i % n])] += 1
        elif mode == "small":
            # 小模型优先: 权重 = 剩余量倒数, 剩余越少分得越多(尽快完成释放并发名额)
            inv = {id(t): (1.0 / rem[id(t)] if rem[id(t)] > 0 else 1e9) for t in active}
            tot_inv = sum(inv.values())
            order = sorted(active, key=lambda x: rem[id(x)])  # 剩余升序
            for t in order:
                share = max(0, int(left * inv[id(t)] / tot_inv))
                jmap[id(t)] += share; left -= share
                if left <= 0: break
            i = 0
            while left > 0:
                jmap[id(order[i % n])] += 1; left -= 1; i += 1
        else:  # size: 按剩余大小比例
            order = sorted(active, key=lambda x: -rem[id(x)])
            for t in order:
                share = max(0, int(left * rem[id(t)] / total_rem))
                jmap[id(t)] += share; left -= share
                if left <= 0: break
            i = 0
            while left > 0:
                jmap[id(order[i % n])] += 1; left -= 1; i += 1
    for t in active:
        nj = max(1, min(pool, jmap[id(t)]))
        if (t.get("j") or 1) != nj:
            t["j"] = nj; changed.append(t)
    if changed: save_cfg()
    return changed

def _fill_queue():
    """锁外调用: 并发有空位时从队列补位并启动(先锁内调整配置, 锁外 launch)"""
    launch_list = []
    with _cfg_lock:
        tasks = _cfg.get("tasks", {})
        q = _cfg.get("queue", [])
        maxm = max(1, int(_cfg.get("max_models") or 1))
        while q:
            active = [t for t in tasks.values() if t.get("want_run")]
            if len(active) >= maxm: break
            nxt = q.pop(0)
            t = {k: nxt.get(k) for k in ("repo", "dest", "endpoint", "total", "files_expected")}
            t.update(want_run=True, j=1, gated=None,
                     x=_clamp(_cfg.get("x"), 1, MAX_CONN, 8),  # 补位任务沿用控制台全局线程数
                     started_at=time.strftime("%F %T"), last_msg="队列自动开始")
            tasks[nxt["dest"]] = t
            launch_list.append(t)
        allocate_j()
        if launch_list: save_cfg()  # 无补位则配置未变, 不写盘(allocate_j 内部已按变化自行保存)
    for t in launch_list:
        launch(t)

def handle_control(data):
    action = str(data.get("action", ""))

    # analyze 含网络请求, 放在锁外执行, 避免阻塞其他请求
    if action == "analyze":
        repo = parse_repo(data.get("repo", ""))
        if not repo: return {"ok": False, "error": "无法解析仓库地址, 请填 org/name 或完整 HF 链接"}
        if data.get("token"): save_token(data["token"])
        ep = data.get("endpoint") if data.get("endpoint") in ENDPOINTS else "mirror"
        info = analyze_repo(repo, ep)
        if info.get("ok"):
            with _cfg_lock: root = _cfg["default_root"]
            info["dest"] = os.path.join(expand(root), repo)
            info["disk_free"] = disk_free(info["dest"])
        info["has_token"] = bool(read_token())
        return info

    # start_new: 创建任务; 并发有空位则启动, 否则入队等待
    if action == "start_new":
        repo = parse_repo(data.get("repo", ""))
        if not repo: return {"ok": False, "error": "无法解析仓库地址"}
        if data.get("token"): save_token(data["token"])
        # 每分片线程数由下载控制台全局统一设置(1-10), 新任务默认沿用
        x = _clamp(_cfg.get("x"), 1, MAX_CONN, 8)
        ep = data.get("endpoint") if data.get("endpoint") in ENDPOINTS else "mirror"
        info = analyze_repo(repo, ep)  # 尽力分析, 失败也允许下载
        stop_targets = []; launch_target = None; queued = False
        with _cfg_lock:
            dest = expand(data.get("dest", "").strip()) or os.path.join(expand(_cfg["default_root"]), repo)
            tasks = _cfg.get("tasks", {}); q = _cfg.get("queue", [])
            # 重复检测(同仓库或同保存目录, 覆盖任务与队列)
            dupq = next((i for i, t in enumerate(q) if t.get("repo") == repo or t.get("dest") == dest), None)
            dupt = next((t for t in tasks.values() if t.get("repo") == repo or t.get("dest") == dest), None)
            if (dupq is not None or dupt is not None) and not data.get("force"):
                qt = q[dupq] if dupq is not None else dupt
                return {"ok": False, "code": "queue_conflict",
                        "error": "该模型已在下载队列/任务中: " + qt.get("repo", ""),
                        "dup_repo": qt.get("repo", "")}
            if dupq is not None: q.pop(dupq)
            if dupt is not None:
                stop_targets.append(dupt); tasks.pop(dupt["dest"], None)
            task = {"repo": repo, "dest": dest, "x": x, "j": 1, "endpoint": ep,
                    "total": info.get("total", 0) if info.get("ok") else 0,
                    "files_expected": info.get("files", 0) if info.get("ok") else 0,
                    "gated": info.get("gated", False) if info.get("ok") else None,
                    "want_run": True, "started_at": time.strftime("%F %T"), "last_msg": "新建下载"}
            active = [t for t in tasks.values() if t.get("want_run")]
            maxm = max(1, int(_cfg.get("max_models") or 1))
            if len(active) >= maxm:
                # 并发已满 → 入队等待
                task["want_run"] = False; task["last_msg"] = "并发已满, 排队等待"
                q.append({k: task[k] for k in ("repo", "dest", "x", "endpoint", "total", "files_expected")}
                         | {"added_at": time.strftime("%F %T")})
                queued = True
            else:
                tasks[dest] = task
                reset_samples()
                allocate_j()
                launch_target = task
            _cfg["history"] = [h for h in _cfg.get("history", []) if h.get("repo") != repo][:19]
            _cfg["history"].insert(0, {"repo": repo, "dest": dest, "at": task["started_at"]})
            save_cfg()
        for t in stop_targets: stop_task(t)
        if launch_target: launch(launch_target)
        return {"ok": True, "task": (launch_target or task), "queued": queued, "dl_running": dl_running()}

    # resume_history: mode=resume(断点续传) / verify(仅校验) / redownload(删本地后全量重下)
    if action == "resume_history":
        repo = parse_repo(data.get("repo", "")); dest = expand((data.get("dest") or "").strip())
        if not repo or not dest: return {"ok": False, "error": "历史任务信息不完整"}
        if not os.path.isdir(dest): return {"ok": False, "error": "本地目录已不存在: " + dest}
        mode = data.get("mode") or "resume"
        # 仅校验: 独立进程, 不改 tasks, 不打断正在下载该目录的任务
        if mode == "verify":
            for t in _cfg.get("tasks", {}).values():
                if t.get("dest") == dest and _pg_alive(t.get("pgid")):
                    return {"ok": False, "error": "该模型正在下载中, 请稍后再校验"}
            pid = launch_verify(repo, dest)
            return {"ok": True, "verify_pid": pid}
        # 彻底重新下载: 先读元数据再删本地(锁外, 避免大目录删除阻塞其他请求), 然后全量重下
        if mode == "redownload":
            tot, _, cnt = metadata(dest)
            shutil.rmtree(dest, ignore_errors=True)
        else:
            tot, _, cnt = metadata(dest)
        stop_targets = []; launch_target = None; queued = False
        with _cfg_lock:
            tasks = _cfg.get("tasks", {}); q = _cfg.get("queue", [])
            old = tasks.get(dest)
            if old:
                stop_targets.append(old); tasks.pop(dest, None)
            _cfg["queue"] = [t for t in q if t.get("dest") != dest]  # 移除队列同目录
            ep = data.get("endpoint") if data.get("endpoint") in ENDPOINTS else "mirror"
            task = {"repo": repo, "dest": dest,
                    "x": _clamp(_cfg.get("x"), 1, MAX_CONN, 8), "j": 1, "endpoint": ep,
                    "total": tot, "files_expected": cnt,
                    "want_run": True, "started_at": time.strftime("%F %T"),
                    "last_msg": "彻底重新下载" if mode == "redownload" else "从历史任务续传"}
            active = [t for t in tasks.values() if t.get("want_run")]
            maxm = max(1, int(_cfg.get("max_models") or 1))
            if len(active) >= maxm:
                task["want_run"] = False; task["last_msg"] = "并发已满, 排队等待"
                _cfg["queue"].append({k: task[k] for k in ("repo", "dest", "x", "endpoint", "total", "files_expected")}
                                     | {"added_at": time.strftime("%F %T")})
                queued = True
            else:
                tasks[dest] = task
                reset_samples()
                allocate_j()
                launch_target = task
            # 重新下载也置顶更新最近任务(与 start_new 一致), 避免任务完成后从列表消失
            _cfg["history"] = [h for h in _cfg.get("history", []) if h.get("repo") != repo][:19]
            _cfg["history"].insert(0, {"repo": repo, "dest": dest, "at": task["started_at"]})
            save_cfg()
        for t in stop_targets: stop_task(t)
        if launch_target: launch(launch_target)
        return {"ok": True, "task": (launch_target or task), "queued": queued, "dl_running": dl_running()}

    # open_dir: 在访达中打开历史任务的本地目录(不涉及配置, 放锁外)
    if action == "open_dir":
        d = expand((data.get("dest") or "").strip())
        if not d or not os.path.isdir(d): return {"ok": False, "error": "目录不存在: " + (d or "(空)")}
        _run(["open", d])
        return {"ok": True, "dest": d}

    # delete_model: 删除本地模型目录。mode=trash(移入废纸篓,可恢复) / purge(彻底删除)
    # 只接受 org/name, 路径由后端在 default_root 下自行拼接校验; 下载中/排队中的拒绝删除
    if action == "delete_model":
        repo = (data.get("repo") or "").strip()
        mode = data.get("mode") if data.get("mode") in ("trash", "purge") else "trash"
        if repo in _deleting:
            return {"ok": False, "error": "该模型正在删除中, 请稍候"}
        with _cfg_lock:
            running = any(t.get("repo") == repo and _pg_alive(t.get("pgid"))
                          for t in _cfg.get("tasks", {}).values())
            queued = any(qt.get("repo") == repo for qt in _cfg.get("queue", []))
        if running: return {"ok": False, "error": "该模型正在下载中, 请先暂停后再删除"}
        if queued: return {"ok": False, "error": "该模型在下载队列中, 请先在队列里移除"}
        dest, err = _model_dir(repo)
        if not dest: return {"ok": False, "error": err}
        _deleting[repo] = {"dest": dest, "mode": mode, "started": time.strftime("%F %T")}
        _del_err.pop(repo, None)
        threading.Thread(target=_delete_worker, args=(dest, repo, mode), daemon=True).start()
        return {"ok": True, "repo": repo, "mode": mode, "dest": dest}

    # set_config: 全局设置(限速/并发模型数/分片池/分配模式), 变化即时生效(重启相关任务)
    if action == "set_config":
        if data.get("token"):
            if not save_token(data["token"]):  # 保存失败必须报错, 避免前端误报"已保存"
                return {"ok": False, "error": "token 保存失败(写入 ~/.huggingface/token 出错)"}
        restart_targets = []; need_fill = False
        with _cfg_lock:
            if data.get("default_root"):
                r = expand(data["default_root"].strip())
                if r: _cfg["default_root"] = r
            if data.get("username"): _cfg["username"] = data["username"].strip() or _cfg["username"]
            old_sp = _cfg.get("speed_limit", 0)
            if "speed_limit" in data:
                _cfg["speed_limit"] = _clamp(data.get("speed_limit"), 0, 10000, old_sp)
            speed_changed = _cfg["speed_limit"] != old_sp
            old_mm = _cfg.get("max_models", 2)
            if "max_models" in data:
                _cfg["max_models"] = _clamp(data["max_models"], 1, MAX_MODELS_LIMIT, old_mm)
            mm_changed = _cfg["max_models"] != old_mm
            old_gj = _cfg.get("global_j", 10)
            if "global_j" in data:
                _cfg["global_j"] = _clamp(data["global_j"], 1, MAX_CONN, old_gj)
            gj_changed = _cfg["global_j"] != old_gj
            old_x = _cfg.get("x", 8)
            if "x" in data:
                _cfg["x"] = _clamp(data["x"], 1, MAX_CONN, old_x)
            x_changed = _cfg["x"] != old_x
            am = data.get("alloc_mode")
            if am == "auto": am = "size"  # 兼容 v2.0 旧值
            if am in ("size", "small", "manual"):
                _cfg["alloc_mode"] = am
            # 限速变化 → 重启所有运行中任务
            if speed_changed:
                for t in _cfg.get("tasks", {}).values():
                    if t.get("want_run"):
                        t["last_msg"] = time.strftime("%T") + " 限速调整, 自动重启生效"
                        restart_targets.append(t)
            # 每片线程数变化 → 应用到所有任务并重启运行中任务
            if x_changed:
                for t in _cfg.get("tasks", {}).values():
                    t["x"] = _cfg["x"]
                    if t.get("want_run"):
                        t["last_msg"] = time.strftime("%T") + " 每片线程调整为 %d" % _cfg["x"]
                        if t not in restart_targets: restart_targets.append(t)
            # 分片池/模式变化 → 重新分配(自动模式)
            if gj_changed or "alloc_mode" in data or speed_changed:
                alloc = allocate_j()
                for t in alloc:
                    if t not in restart_targets: restart_targets.append(t)
            if mm_changed:
                need_fill = True
            save_cfg()
        _restart_tasks(restart_targets)
        if need_fill: _fill_queue()
        return {"ok": True, "cfg": public_cfg(), "restarted": bool(restart_targets)}

    with _cfg_lock:
        tasks = _cfg.get("tasks", {}); q = _cfg.get("queue", [])
        tail_ret = None; act = None; task = None
        if action == "enqueue":
            repo = parse_repo(data.get("repo", ""))
            if not repo: return {"ok": False, "error": "无法解析仓库地址"}
            if any(t.get("repo") == repo for t in tasks.values()) or any(t.get("repo") == repo for t in q):
                return {"ok": False, "error": "该模型正在下载或已在队列中"}
            dest = expand((data.get("dest") or "").strip()) or os.path.join(expand(_cfg["default_root"]), repo)
            if any(t.get("dest") == dest for t in tasks.values()) or any(t.get("dest") == dest for t in q):
                return {"ok": False, "error": "该保存目录已被下载任务或队列占用: " + dest}
            ep = data.get("endpoint") if data.get("endpoint") in ENDPOINTS else "mirror"
            q.append({"repo": repo, "dest": dest,
                      "x": _clamp(_cfg.get("x"), 1, MAX_CONN, 8),
                      "endpoint": ep, "total": int(data.get("total") or 0),
                      "files_expected": int(data.get("files") or 0),
                      "added_at": time.strftime("%F %T")})
            save_cfg()
            need_fill = len([t for t in tasks.values() if t.get("want_run")]) < max(1, int(_cfg.get("max_models") or 1))
            tail_ret = {"ok": True, "queue": list(q), "need_fill": need_fill}
        elif action in ("queue_remove", "queue_clear"):
            if action == "queue_clear": q.clear()
            else:
                idx = data.get("index")
                if isinstance(idx, int) and 0 <= idx < len(q): q.pop(idx)
            save_cfg()
            tail_ret = {"ok": True, "queue": q}
        else:
            # 任务操作: 按 dest 定位
            dest = expand((data.get("dest") or "").strip())
            task = tasks.get(dest)
            if action in ("stop", "start", "set_j") and not task:
                return {"ok": False, "error": "任务不存在"}
            if action == "stop":
                task["want_run"] = False
                task["last_msg"] = time.strftime("%T") + " 已暂停"
                save_cfg(); act = "stop"
            elif action == "start":
                active = [t for t in tasks.values() if t.get("want_run")]
                maxm = max(1, int(_cfg.get("max_models") or 1))
                if task.get("want_run"):
                    act = "none"
                elif len(active) >= maxm:
                    return {"ok": False, "error": "并发已满(最多 %d 个同时下载), 请先暂停其他任务" % maxm}
                else:
                    task["want_run"] = True
                    task["last_msg"] = time.strftime("%T") + " 已继续"
                    save_cfg(); act = "start"
            elif action == "set_j":
                if _cfg.get("alloc_mode") != "manual":
                    return {"ok": False, "error": "仅手动分配方案可设置分片数"}
                pool = max(1, int(_cfg.get("global_j") or 10))
                nj = _clamp(data.get("j"), 1, pool, task.get("j", 1))
                total_j = nj + sum((t.get("j") or 1) for t in tasks.values()
                                   if t.get("want_run") and t is not task)
                if total_j > pool:
                    return {"ok": False, "error": "分片总和超出全局池 %d, 请先减小其他任务" % pool}
                task["j"] = nj
                task["last_msg"] = time.strftime("%T") + " 分片调整为 %d" % nj
                save_cfg(); act = "set_j"
            else:
                return {"ok": False, "error": "unknown action"}
    # ---- 进程操作放锁外 ----
    if tail_ret is not None:
        if action == "enqueue" and tail_ret.get("need_fill"): _fill_queue()
        return tail_ret
    if act == "stop":
        stop_task(task)
        _fill_queue()  # 空位补队列
    elif act == "start":
        launch(task)
    elif act == "set_j":
        _restart_tasks([task])
    return {"ok": True, "task": task, "dl_running": dl_running()}

def public_cfg():
    return {"default_root": _cfg["default_root"], "username": _cfg.get("username", ""),
            "has_token": bool(read_token()), "speed_limit": _cfg.get("speed_limit", 0),
            "max_models": max(1, int(_cfg.get("max_models") or 1)),
            "global_j": max(1, int(_cfg.get("global_j") or 10)),
            "alloc_mode": _cfg.get("alloc_mode", "size"),
            "x": max(1, int(_cfg.get("x") or 8)),
            "max_models_limit": MAX_MODELS_LIMIT, "max_conn": MAX_CONN}

# ---------------- 磁盘扫描 ----------------
_meta_cache = {}  # dest -> (mtime, total, map, count), 按目录缓存避免多模型时反复读
def metadata(dest):
    if not dest: return 0, {}, 0
    mp = os.path.join(dest, ".hfd", "repo_metadata.json")
    try: mt = os.path.getmtime(mp)
    except OSError: return 0, {}, 0
    e = _meta_cache.get(dest)
    if not e or e[0] != mt:
        tot, m = 0, {}
        try:
            d = json.load(open(mp))
            for s in d.get("siblings", []):
                sz = s.get("size") or (s.get("lfs") or {}).get("size") or 0
                if s.get("rfilename"): m[s["rfilename"]] = sz; tot += sz
        except Exception:
            return 0, {}, 0
        _meta_cache[dest] = (mt, tot, m, len(m))
        if len(_meta_cache) > 60:  # 简单防膨胀
            for k in list(_meta_cache)[:10]: _meta_cache.pop(k, None)
    t = _meta_cache[dest]
    return t[1], t[2], t[3]

_scan_cache = {}  # dest -> (ts, result), 短 TTL 缓存避免状态轮询/采样/模型库多线程重复全目录扫描
SCAN_TTL = 1.5
def scan(dest):
    now = time.time()
    e = _scan_cache.get(dest)
    if e and now - e[0] < SCAN_TTL: return e[1]
    total, done, fcnt = 0, 0, 0
    active = []
    if dest and os.path.isdir(dest):
        _, emap, _ = metadata(dest)
        for root, dirs, files in os.walk(dest):
            dirs[:] = [d for d in dirs if d != ".hfd"]
            for fn in files:
                if fn.endswith(".aria2"): continue
                p = os.path.join(root, fn)
                try: st = os.stat(p)
                except OSError: continue
                sz = st.st_blocks * 512; fcnt += 1
                if not os.path.exists(p + ".aria2"): done += 1
                else: active.append((fn, sz, emap.get(fn, 0)))
                total += sz
    active.sort(key=lambda x: -x[1])
    res = (total, done, fcnt, active[:10])
    _scan_cache[dest] = (now, res)
    if len(_scan_cache) > 20:
        for k in list(_scan_cache)[:5]: _scan_cache.pop(k, None)
    return res

def verify_result(dest):
    """读取下载脚本写出的 sha256 校验结果(若有)"""
    p = os.path.join(dest or "", ".hfd", "verify_result.json")
    try:
        with open(p) as f: return json.load(f)
    except Exception: return None

def dl_phase(dest):
    """读取下载脚本写入的阶段标记: download/verifying/done/failed, 用于区分下载中/校验中/失败"""
    try:
        with open(os.path.join(dest or "", ".hfd", "status.json")) as f:
            d = json.load(f)
        return d.get("phase", "download"), d.get("done", 0), d.get("total", 0), d.get("msg", "")
    except Exception:
        return "download", 0, 0, ""

# ---------------- 速率采样(全局总速率 + 每任务实时速率) ----------------
_samples = deque(); _rate_hist = deque(maxlen=RATE_HIST_MAX)
_slock = threading.Lock()
_task_sample = {}   # dest -> (ts, bytes) 上一轮采样
_task_rates = {}    # dest -> 最新速率(B/s)
def reset_samples():
    with _slock: _samples.clear(); _rate_hist.clear(); _task_sample.clear(); _task_rates.clear()

def _sampler():
    while True:
        try:
            with _cfg_lock:
                act = [t for t in _cfg.get("tasks", {}).values()
                       if t.get("want_run") and _pg_alive(t.get("pgid"))]
            now = time.time(); total_b = 0; rates = {}
            for t in act:
                b = scan(t["dest"])[0]
                total_b += b
                e = _task_sample.get(t["dest"])
                if e and now - e[0] >= SAMPLE_SEC:
                    rates[t["dest"]] = max(0.0, (b - e[1]) / (now - e[0]))
                _task_sample[t["dest"]] = (now, b)
            with _slock:
                _samples.append((now, total_b))
                while _samples and now - _samples[0][0] > WINDOW_SEC + 2: _samples.popleft()
                rate = 0.0
                if len(_samples) >= 2:
                    (t0, b0), (t1, b1) = _samples[0], _samples[-1]
                    if t1 - t0 >= SAMPLE_SEC: rate = max(0.0, (b1 - b0) / (t1 - t0))
                _rate_hist.append((round(now, 1), round(rate, 1)))
                _task_rates.clear(); _task_rates.update(rates)
        except Exception:
            pass
        time.sleep(SAMPLE_SEC)
threading.Thread(target=_sampler, daemon=True).start()

def window_rate():
    with _slock:
        if len(_samples) >= 2:
            (t0, b0), (t1, b1) = _samples[0], _samples[-1]
            if t1 - t0 >= SAMPLE_SEC: return max(0.0, (b1 - b0) / (t1 - t0))
    return 0.0

def rate_hist_list():
    with _slock: return [[t, r] for t, r in _rate_hist]

def task_rates_snapshot():
    with _slock: return dict(_task_rates)

# ---------------- 任务队列自动接力 + 中断恢复 + 完成通知 ----------------
_task_state = {}  # dest -> {last_auto, auto_cnt, notified} 每任务独立节流计数
def _task_complete(t):
    """自然完成判断: 校验结果通过, 或(无校验结果时)大小达到总量"""
    try:
        tot, _, _ = metadata(t["dest"]); have = scan(t["dest"])[0]
        v = verify_result(t["dest"])
        good = bool(v) and not v.get("bad") and not v.get("miss") and v.get("ok") == v.get("total")
        return good or (tot and not v and have >= tot - 1024 * 1024)
    except Exception:
        return False

def _queue_watcher():
    """1) 检查每个运行任务: 完成(通知+释放空位)/失败(不重启)/中断(自动重启, 每任务独立节流)
       2) 并发有空位时从队列补位  3) 分片池重新分配"""
    first = True
    while True:
        time.sleep(8)
        try:
            launch_list = []
            dirty = False  # 仅在有实际状态变化时落盘, 避免无任务时也每 8s 重写配置
            with _cfg_lock:
                tasks = _cfg.get("tasks", {}); q = _cfg.get("queue", [])
                # 1) 逐任务检查
                for dest, t in list(tasks.items()):
                    if not t.get("want_run"): continue
                    if _pg_alive(t.get("pgid")): continue  # 进程还在
                    if _task_complete(t):
                        t["want_run"] = False
                        t["last_msg"] = time.strftime("%T") + " 已完成"
                        _task_state.pop(dest, None)
                        dirty = True
                        print("[队列] 任务完成:", t["repo"], flush=True)
                        _run(["osascript", "-e",
                              'display notification "下载完成: ' + t["repo"] + '" with title "HF 模型下载器" subtitle "已校验通过"'])
                    elif _phase_of(t) != "failed":
                        st = _task_state.setdefault(dest, {"last_auto": 0.0, "auto_cnt": 0, "notified": False})
                        now = time.time()
                        if now - st["last_auto"] >= 120 and st["auto_cnt"] < 3:
                            st["last_auto"] = now; st["auto_cnt"] += 1
                            t["last_msg"] = time.strftime("%T") + " 下载中断, 自动重启"
                            launch_list.append(t)
                            print("[自动恢复] 重启中断任务:", t["repo"], flush=True)
                        elif st["auto_cnt"] >= 3 and not st["notified"]:
                            st["notified"] = True
                            _run(["osascript", "-e",
                                  'display notification "下载任务多次中断, 已停止自动重启; 可在看板点「继续」" with title "HF 模型下载器" subtitle "需要人工介入"'])
                            print("[自动恢复] 已达 3 次上限, 停止自动重启:", t["repo"], flush=True)
                    else:
                        _task_state.pop(dest, None)
                # 2) 补位
                maxm = max(1, int(_cfg.get("max_models") or 1))
                while q:
                    active = [t for t in tasks.values() if t.get("want_run")]
                    if len(active) >= maxm: break
                    nxt = q.pop(0)
                    nt = {k: nxt.get(k) for k in ("repo", "dest", "x", "endpoint", "total", "files_expected")}
                    nt.update(want_run=True, j=1, gated=None,
                              started_at=time.strftime("%F %T"), last_msg="队列自动开始")
                    tasks[nxt["dest"]] = nt
                    launch_list.append(nt)
                    dirty = True
                    print("[队列] 补位开始:", nxt["repo"], flush=True)
                # 3) 分配分片(allocate_j 内部仅在有变化时写盘, 这里只补记队列/完成态变化)
                if allocate_j(): dirty = True
                if dirty: save_cfg()
            # 锁外: 补位/中断恢复的任务启动; 分片变化的任务重启生效
            for t in launch_list:
                launch(t)
            first = False
        except Exception:
            pass
threading.Thread(target=_queue_watcher, daemon=True).start()

# ---------------- 磁盘实时预检(所有运行任务总量) ----------------
def _disk_watcher():
    """下载中实时检查磁盘余量: 所有运行任务剩余可下载量 1.05 倍 + 2GB 缓冲, 不足则全部自动暂停并通知"""
    while True:
        time.sleep(10)
        try:
            with _cfg_lock:
                act = [t for t in _cfg.get("tasks", {}).values()
                       if t.get("want_run") and _pg_alive(t.get("pgid"))]
            if not act: continue
            need = 2 * 1e9
            for t in act:
                tot, _, _ = metadata(t["dest"]); have = scan(t["dest"])[0]
                total = tot or t.get("total") or 0
                if total: need += (total - have) * 1.05
            free = disk_free(act[0]["dest"])
            if free < need:
                with _cfg_lock:
                    for t in act:
                        t["want_run"] = False
                        t["last_msg"] = time.strftime("%T") + " 磁盘空间不足, 已自动暂停"
                    save_cfg()
                for t in act: stop_task(t)
                _run(["osascript", "-e",
                      'display notification "剩余可用 ' + ("%.1f" % (free / 1e9)) + 'GB 不足, 已自动暂停; 清理磁盘后可点「继续」" with title "HF 模型下载器" subtitle "磁盘空间不足"'])
                print("[磁盘预检] 空间不足, 已暂停 %d 个任务" % len(act), flush=True)
        except Exception:
            pass
threading.Thread(target=_disk_watcher, daemon=True).start()

# ---------------- 日志增量追踪 ----------------
_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b.|\x1b")
_PROG = re.compile(r"\d+\s*/\s*\d+\s*files\s*\|")
_logst = {"inode": None, "off": 0, "buf": "", "lines": deque(maxlen=40)}
_llock = threading.Lock()
def _update_log():
    with _llock:
        try: st = os.stat(LOG)
        except OSError: return
        if _logst["inode"] != st.st_ino or st.st_size < _logst["off"]:
            _logst.update(inode=st.st_ino, off=0, buf=""); _logst["lines"].clear()
        if st.st_size <= _logst["off"]: return
        try:
            with open(LOG, "rb") as f:
                f.seek(_logst["off"]); chunk = f.read()
                end = f.tell()
        except OSError: return
        _logst["off"] = end  # 以实际读到的位置为准, 避免竞态重复
        segs = re.split(r"[\r\n]+", _logst["buf"] + chunk.decode("utf-8", "replace"))
        _logst["buf"] = segs.pop()
        for s in segs:
            s = _ANSI.sub("", s).strip()
            if not s or _PROG.search(s): continue
            if not _logst["lines"] or _logst["lines"][-1] != s: _logst["lines"].append(s)

def read_log():
    _update_log()
    with _llock: return list(_logst["lines"])[-14:]

def tail_lines(path, n=14, max_bytes=512 * 1024):
    """读大文件尾部最多 n 行(只读末尾 max_bytes), 供"当前模型日志"使用"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2); size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        return [l for l in data.splitlines() if l.strip()][-n:]
    except OSError:
        return []

# ---------------- 模型库删除(移入废纸篓 / 彻底删除) ----------------
# 安全策略: 前端只传 org/name, 路径一律由后端在 default_root 下自行拼接并校验,
# 避免任意路径删除; 越界、软链、根目录本身、正在下载的一律拒绝。
_deleting = {}   # repo -> {"dest", "mode", "started"}  正在删除
_del_err = {}    # repo -> 最近一次删除失败原因

def _model_dir(repo):
    """把 org/name 解析为根目录下的真实子目录; 非法输入/越界/软链一律拒绝"""
    if not isinstance(repo, str) or not re.match(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$", repo):
        return None, "仓库名不合法(应为 org/name)"
    org, name = repo.split("/")
    root = os.path.realpath(expand(_cfg["default_root"]))
    dest = os.path.join(root, org, name)
    if os.path.islink(dest):
        return None, "目标是符号链接, 已拒绝删除"
    real = os.path.realpath(dest)
    if real == root or not real.startswith(root + os.sep):
        return None, "路径越界, 已拒绝删除"
    if os.path.basename(real) != name or os.path.basename(os.path.dirname(real)) != org:
        return None, "目录层级与仓库名不符, 已拒绝删除"
    if not os.path.isdir(real):
        return None, "目录不存在"
    return real, None

def _to_trash(dest):
    """同卷移入废纸篓: os.rename 是 O(1), 百 GB 也是秒级且可恢复; 跨卷返回 None"""
    trash = os.path.expanduser("~/.Trash")
    try: os.makedirs(trash, exist_ok=True)
    except OSError: return None
    name = os.path.basename(dest); target = os.path.join(trash, name); i = 1
    while os.path.exists(target):  # 重名时加序号, 绝不覆盖废纸篓里已有内容
        target = os.path.join(trash, "%s %d" % (name, i)); i += 1
    try:
        os.rename(dest, target)
        return target
    except OSError:
        return None  # 常见为跨卷(EXDEV), 交由调用方提示改用彻底删除

def _delete_worker(dest, repo, mode):
    """后台线程执行删除(大目录 rmtree 可能耗时, 不能阻塞 HTTP 请求)"""
    msg = ""
    try:
        if mode == "trash":
            tgt = _to_trash(dest)
            if tgt is None:
                msg = "无法移入废纸篓(可能跨卷), 请改用「彻底删除」"
            else:
                msg = ""  # 成功
        else:
            shutil.rmtree(dest)
    except Exception as e:
        msg = "删除失败: %s" % e
    finally:
        # 清掉指向该目录的任务条目与扫描缓存。注意同一目录可能有 /tmp 与 /private/tmp
        # 两种写法(macOS 上 /tmp 是软链), 因此按 realpath 比较而不是仅比对字符串。
        stale = []
        with _cfg_lock:
            tasks = _cfg.get("tasks", {})
            for k in list(tasks):
                try: same = os.path.realpath(k) == dest
                except OSError: same = False
                if k == dest or same: stale.append(k)
            for k in stale: tasks.pop(k, None)
            if stale: save_cfg()
        for k in stale + [dest]:
            _meta_cache.pop(k, None); _scan_cache.pop(k, None)
        if msg: _del_err[repo] = msg
        else: _del_err.pop(repo, None)
        _deleting.pop(repo, None)
        print("[删除] %s (%s) -> %s" % (repo, mode, msg or "完成"), flush=True)

# ---------------- 本地模型库 ----------------
def _models_resp(models, total, truncated):
    """统一 /api/models 返回结构; 附带"正在删除"与"删除失败原因", 供前端标注与提示"""
    return {"models": models, "total": total, "truncated": truncated, "limit": MAX_MODELS_LIST,
            "deleting": {r: v.get("mode", "trash") for r, v in _deleting.items()},
            "delete_errors": dict(_del_err)}

def list_models():
    """列出本地模型库: 按最近活动时间倒序(有元数据用其 mtime, 否则用目录 mtime)。
    不再静默截断——超过 MAX_MODELS_LIST 时返回 truncated/total, 由前端明确提示"未全部显示"。"""
    root = expand(_cfg["default_root"]); out = []
    if not os.path.isdir(root):
        return _models_resp([], 0, False)
    try: orgs = sorted(os.listdir(root))
    except OSError:
        return _models_resp([], 0, False)
    for org in orgs:
        op = os.path.join(root, org)
        if not os.path.isdir(op) or org.startswith("."): continue
        try: names = sorted(os.listdir(op))
        except OSError: continue
        for name in names:
            d = os.path.join(op, name)
            if not os.path.isdir(d) or name.startswith("."): continue
            if name in CACHE_DIRS: continue   # 缓存目录不计入模型库
            tot, _, _ = metadata(d)
            bytes_ = scan(d)[0]
            v = verify_result(d)
            if v and bytes_ == 0 and (v.get("total") or 0) > 0:
                # 校验记录只是历史快照: 目录里已无权重文件(被删除/移走)时不能继续显示"完整"
                state = "missing"
            elif v:
                state = "ok" if not v.get("bad") and not v.get("miss") and v.get("ok") == v.get("total") else "bad"
            else:
                # 无校验结果时读取下载脚本的阶段标记: 明确失败/校验中的模型不能误标"下载中"
                ph = dl_phase(d)[0]
                state = "failed" if ph == "failed" else ("verifying" if ph == "verifying" else "partial")
            dt = ""
            mp = os.path.join(d, ".hfd", "repo_metadata.json")
            try:
                # 活动时间取元数据 mtime(下载/续传都会刷新), 无元数据退回目录 mtime, 供倒序排列
                mtime = os.path.getmtime(mp) if os.path.exists(mp) else os.path.getmtime(d)
            except OSError:
                mtime = 0
            if os.path.exists(mp):
                dt = time.strftime("%m-%d %H:%M", time.localtime(mtime))
            out.append({"repo": f"{org}/{name}", "bytes": bytes_, "total": tot,
                        "pct": min(100.0, bytes_ / tot * 100) if tot else 0,
                        "state": state, "date": dt, "verify": v, "_mtime": mtime})
    out.sort(key=lambda m: -m["_mtime"])   # 最近有活动的排前面
    total = len(out)
    shown = out[:MAX_MODELS_LIST]
    for m in shown: m.pop("_mtime", None)
    return _models_resp(shown, total, total > MAX_MODELS_LIST)

def status_json():
    tasks = _cfg.get("tasks", {})
    rates = task_rates_snapshot()
    model_log = []
    first_run = next((t for t in tasks.values() if t.get("want_run")), None)
    if first_run:
        model_log = tail_lines(os.path.join(first_run["dest"], ".hfd", "download.log"))
    resp = {"cfg": public_cfg(), "tasks": [], "log": read_log(), "dl_running": dl_running(),
            "rate": window_rate(), "history": _cfg.get("history", [])[:10], "queue": _cfg.get("queue", []),
            "model_log": model_log,
            "disk_free": disk_free(_cfg.get("default_root")),
            "rate_hist": rate_hist_list(),
            "consts": {"max_conn": MAX_CONN, "max_models": max(1, int(_cfg.get("max_models") or 1)),
                       "global_j": max(1, int(_cfg.get("global_j") or 10)),
                       "alloc_mode": _cfg.get("alloc_mode", "auto"),
                       "max_models_limit": MAX_MODELS_LIMIT}}
    for dest, t in tasks.items():
        total_b, _, meta_cnt = metadata(dest)
        downloaded, done, fcnt, active = scan(dest)
        total = total_b or t.get("total") or 0
        expected = meta_cnt or t.get("files_expected") or fcnt
        rate = rates.get(dest, 0.0)
        phase, vdone, vtotal, phmsg = dl_phase(dest)
        resp["tasks"].append({
            "repo": t["repo"], "dest": dest, "endpoint": t["endpoint"],
            "x": t["x"], "j": t["j"], "gated": t.get("gated"),
            "want_run": bool(t.get("want_run")), "alive": _pg_alive(t.get("pgid")),
            "phase": phase, "phase_msg": phmsg,
            "verify_prog": [vdone, vtotal] if phase == "verifying" else None,
            "downloaded": downloaded, "total": total,
            "pct": min(100.0, downloaded / total * 100) if total else 0,
            "files_done": done, "files_expected": expected,
            "rate": rate,
            "eta_sec": (total - downloaded) / rate if rate > 0 and total else 0,
            "last_msg": t.get("last_msg", ""),
            "verify": verify_result(dest),
            "disk_free": disk_free(dest),
            "active": [{"name": n, "bytes": b, "size": sz,
                        "pct": min(100.0, b / sz * 100) if sz else 0} for n, b, sz in active],
        })
    return resp

# ---------------- HTTP ----------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, ctype="application/json; charset=utf-8", code=200):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # 浏览器主动断开连接, 正常现象, 静默
    def handle_error(self, request, client_address):
        # 静默浏览器断连类异常, 避免污染 server.log; 其他异常仍走默认打印
        if sys.exc_info()[0] in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        super().handle_error(request, client_address)
    def do_GET(self):
        if self.path == "/api/status": self._send(status_json())
        elif self.path == "/api/models": self._send(list_models())
        elif self.path == "/" or self.path.startswith("/index"):
            with open(os.path.join(PROJ, "dashboard.html"), "rb") as f: self._send(f.read(), "text/html; charset=utf-8")
        else:
            self._send({"ok": False, "error": "not found"}, code=404)
    def do_POST(self):
        if self.path != "/api/control": self._send({"ok": False, "error": "not found"}, code=404); return
        if not self._same_origin():
            self._send({"ok": False, "error": "forbidden: 仅允许本机页面调用"}, code=403); return
        try:
            n = int(self.headers.get("Content-Length", 0)); data = json.loads(self.rfile.read(n) or b"{}")
        except Exception: data = {}
        try: self._send(handle_control(data))
        except Exception as e: self._send({"ok": False, "error": str(e)})
    def _same_origin(self):
        """只接受来自本机页面的控制请求, 降低其他网页 CSRF 触发下载的可能"""
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        if host and host not in ("localhost", "127.0.0.1", "::1"): return False
        origin = self.headers.get("Origin") or self.headers.get("Referer")
        if origin:
            m = re.match(r"^https?://\[?([^\]/:]+)\]?", origin)
            if m and m.group(1).lower() not in ("localhost", "127.0.0.1"): return False
        return True
    def log_message(self, *a): pass

class Server(ThreadingHTTPServer):
    daemon_threads = True            # 请求线程随主进程退出, 避免悬挂连接拖住关闭
    allow_reuse_address = True
    def __init__(self, *a, certfile, keyfile, **kw):
        super().__init__(*a, **kw)
        self.ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ssl_ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    def get_request(self):
        sock, addr = self.socket.accept(); sock.settimeout(8)
        try:
            if sock.recv(1, socket.MSG_PEEK) == b"\x16":
                sock = self.ssl_ctx.wrap_socket(sock, server_side=True)
        except (ssl.SSLError, OSError):
            sock.close(); raise
        sock.settimeout(None); return sock, addr

def maybe_autoresume():
    """服务启动时, 恢复所有'期望运行'且未完成的下载任务(断点续传); 失败任务不自动续传"""
    for t in list(_cfg.get("tasks", {}).values()):
        if not t.get("want_run"): continue
        if _pg_alive(t.get("pgid")): continue
        if _phase_of(t) == "failed": continue  # 与队列 watcher 的 failed 处理保持一致
        tot, _, _ = metadata(t["dest"]); have = scan(t["dest"])[0]
        if tot and have >= tot - 1024 * 1024: continue  # 已完成
        if not os.path.isdir(t["dest"]): continue
        try:
            launch(t); print("已自动续传未完成任务:", t["repo"], flush=True)
        except Exception as e:
            print("自动续传失败:", e, flush=True)

if __name__ == "__main__":
    save_cfg()
    maybe_autoresume()
    srv = Server(("127.0.0.1", PORT), Handler,
                 certfile=os.path.join(PROJ, "localhost.crt"),
                 keyfile=os.path.join(PROJ, "localhost.key"))
    print(f"HF 模型下载器: http://localhost:{PORT} (https 同端口兼容)", flush=True)
    srv.serve_forever()
