#!/usr/bin/env python3
"""后端模块加载测试 —— 抓 py_compile 查不出来的错误。

背景：v3.49 实现 server.log 轮转时，给函数写了默认参数
`def f(max_bytes=LOG_MAX)`，而 LOG_MAX 在该函数定义处还没定义。
`python -m py_compile` 完全不报错（默认参数在**定义时**求值，属运行期行为），
只有真正加载模块才会 NameError —— 那样服务一启动就崩。

因此这里做两件事：
1. 截断掉文件末尾"启动服务"的代码，exec 加载整个模块定义（不真的起服务）
2. 断言关键名字都存在、且能正常调用

用法: python3 tests/test_backend_load.py   （或 bash tests/run.sh）
"""
import os
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
    """截断掉启动服务的代码后加载模块定义"""
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

print("=== B) 关键名字都存在 ===")
for name in ["LOG", "SLOG", "LOG_MAX", "LOG_KEEP", "rotate_log",
             "_rotate_server_log", "_safe_dest", "_safe_task_dest",
             "_log_exc", "handle_control", "list_models", "status_json"]:
    check("存在 " + name, name in ns)

print("=== C) 日志轮转函数可调用（默认参数不能引用未定义名字）===")
try:
    # 小文件 → 不该触发轮转，但必须能跑完不抛异常
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("hello\n")
        p = f.name
    r = ns["_rotate_server_log"](p, max_bytes=10 * 1024 * 1024, keep_bytes=1024)
    check("小文件不触发轮转", r is False, r)
    # 大文件 → 应触发轮转并保留尾部
    with open(p, "w") as f:
        f.write("X" * 200)
    r = ns["_rotate_server_log"](p, max_bytes=100, keep_bytes=50)
    check("大文件触发轮转", r is True, r)
    body = open(p, "rb").read()
    check("轮转后含标记", b"\xe5\xb7\xb2\xe8\xbd\xae\xe8\xbd\xac" in body or "轮转" in body.decode("utf-8", "replace"),
          body[:60])
    check("轮转后保留了尾部内容", body.endswith(b"X" * 50), body[-60:])
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

print("\n结果: %d 通过 / %d 失败" % (passed, failed))
sys.exit(1 if failed else 0)
