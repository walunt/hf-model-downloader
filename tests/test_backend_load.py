#!/usr/bin/env python3
"""后端模块加载测试 —— 抓 py_compile 查不出来的错误。

背景：v3.49 实现 server.log 轮转时，给函数写了默认参数
`def f(max_bytes=LOG_MAX)`，而 LOG_MAX 在该函数定义处还没定义。
`python -m py_compile` 完全不报错（默认参数在**定义时**求值，属运行期行为），
只有真正加载模块才会 NameError —— 那样服务一启动就崩。

因此这里做两件事：
1. 截断掉文件末尾"启动服务"的代码，exec 加载整个模块定义（不真的起服务）
2. 断言关键名字存在、关键逻辑可调用、常量关系成立

用法: python3 tests/test_backend_load.py   （或 bash tests/run.sh）
"""
import os
import re
import sys
import glob
import shutil
import tempfile

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(PROJ, "progress_server.py")

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print("  ✓ " + name)
    else:
        failed += 1
        print("  ✗ " + name + ("  → " + str(detail)[:120] if detail else ""))


def load_module(proj=None, name="probe"):
    """截断掉末尾启动服务的代码后，加载模块定义"""
    src = open(SRC, encoding="utf-8").read()
    head = src.split("srv = Server(")[0]
    if proj:
        head = head.replace('PROJ = os.path.dirname(os.path.abspath(__file__))',
                            'PROJ = "%s"' % proj)
    ns = {"__name__": name, "__file__": os.path.abspath(SRC)}
    exec(compile(head, SRC, "exec"), ns)
    return ns


print("=== A) 模块能完整加载（抓 NameError / 未定义名字）===")
try:
    ns = load_module()
    check("模块加载无异常", True)
except Exception as e:
    check("模块加载无异常", False, "%s: %s" % (type(e).__name__, e))
    print("\n结果: %d 通过 / %d 失败" % (passed, failed))
    sys.exit(1)

src_txt = open(SRC, encoding="utf-8").read()

print("=== B) 关键名字都存在 ===")
for name in ["LOG", "SLOG", "LOG_MAX", "LOG_KEEP", "CACHE_MAX",
             "MAX_MODELS_LIST", "rotate_log", "_rotate_server_log",
             "_safe_dest", "_safe_task_dest", "_log_exc", "_pg_alive",
             "handle_control", "list_models", "status_json"]:
    check("存在 " + name, name in ns)

print("=== C) 日志轮转函数可调用（默认参数不能引用未定义名字）===")
try:
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("hello\n")
        p = f.name
    r = ns["_rotate_server_log"](p, max_bytes=10 * 1024 * 1024, keep_bytes=1024)
    check("小文件不触发轮转", r is False, r)
    with open(p, "w") as f:
        f.write("X" * 200)
    r = ns["_rotate_server_log"](p, max_bytes=100, keep_bytes=50)
    check("大文件触发轮转", r is True, r)
    body = open(p, "rb").read()
    check("轮转后含标记", "轮转" in body.decode("utf-8", "replace"), body[:60])
    check("轮转后保留尾部内容", body.endswith(b"X" * 50), body[-60:])
    os.unlink(p)
except Exception as e:
    check("轮转函数可调用", False, "%s: %s" % (type(e).__name__, e))

print("=== D) 配置损坏时先备份，不静默丢弃 ===")
tmpd = tempfile.mkdtemp(prefix="cfgload-")
try:
    ns2 = load_module(proj=tmpd, name="probe2")
    cfg = ns2["CFG_F"]
    open(cfg, "w").write('{"default_root": "/tmp", "tasks": {  <<< 半截JSON')
    ns3 = load_module(proj=tmpd, name="probe3")     # 重新加载 → 触发载入分支
    baks = glob.glob(cfg + ".corrupt-*")
    check("生成了 .corrupt- 备份", len(baks) == 1, baks)
    if baks:
        check("备份内容完整", open(baks[0]).read().startswith('{"default_root"'))
finally:
    shutil.rmtree(tmpd, ignore_errors=True)

print("=== E) pgid 校验：进程组存在但不是下载进程 → 判为不存活 ===")
alive = ns["_pg_alive"]
check("pgid=None → False", alive(None) is False)
check("不存在 pgid → False", alive(999999) is False)
pg = os.getpgrp()
try:
    os.killpg(pg, 0)
    pg_exists = True
except Exception:
    pg_exists = False
if pg_exists:
    check("无关进程组不算下载进程存活", alive(pg) is False,
          "误判为存活会导致误杀无关进程")

print("=== F) 性能：缓存上限 >= 模型列表上限 ===")
check("CACHE_MAX >= MAX_MODELS_LIST",
      ns["CACHE_MAX"] >= ns["MAX_MODELS_LIST"],
      "CACHE_MAX=%s MAX_MODELS_LIST=%s" % (ns["CACHE_MAX"], ns["MAX_MODELS_LIST"]))
check("CACHE_MAX >= 200（模型一多缓存才不会失效）", ns["CACHE_MAX"] >= 200, ns["CACHE_MAX"])
check("scan 缓存不再硬编码 20", "len(_scan_cache) > 20" not in src_txt)
check("meta 缓存不再硬编码 60", "len(_meta_cache) > 60" not in src_txt)

print("=== G) 性能：dl_running 结果缓存（避免每次轮询都 fork pgrep）===")


class _R(object):
    returncode = 1      # pgrep 未命中 → 不在下载


calls = []
real_run = ns["subprocess"].run
try:
    ns["subprocess"].run = lambda *a, **k: (
        calls.append(a[0][0] if a and a[0] else "?"), _R())[1]
    ns["_dlrun_cache"][0] = 0.0        # 清缓存，强制重算
    r1 = ns["dl_running"]()
    n1 = len(calls)
    r2 = ns["dl_running"]()            # 2s 缓存内 → 不应再 fork
    r3 = ns["dl_running"]()
    n2 = len(calls)
    check("无下载时返回 False", r1 is False and r2 is False and r3 is False, (r1, r2, r3))
    check("3 次调用只 fork 了一次 pgrep", n1 >= 1 and n2 == n1,
          "首次 %d 次，累计 %d 次" % (n1, n2))
finally:
    ns["subprocess"].run = real_run

print("=== H) 并发安全：_cfg 容器遍历必须取快照 ===")
bad = []
for ln in src_txt.split("\n"):
    st = ln.strip()
    if re.search(r'for .+ in _cfg\.get\("(tasks|queue)".*\)(\.values\(\)|\.items\(\))?:', st) \
            and "list(" not in st:
        bad.append(st[:70])
    if st.startswith("for dest, t in tasks.items():") and "list(" not in st:
        bad.append(st[:70])
check("没有裸遍历 _cfg 容器的 for 循环", not bad, bad)

print("\n结果: %d 通过 / %d 失败" % (passed, failed))
sys.exit(1 if failed else 0)
