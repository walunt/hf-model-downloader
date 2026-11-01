// 渲染层测试: 用最小 DOM 桩加载 dashboard.html 的 <script>, 直接调渲染/加载函数断言输出
// 运行: node tests/render.test.js   (或 bash tests/run.sh)
// 覆盖: A-H 渲染断言 / I 组 loadLib 真实执行 / J 组 refresh 真实执行 / K 组 续传预检真实执行
const fs = require("fs");
const nodePath = require("path");
const os = require("os");
// 路径基于脚本自身位置计算: 仓库整体迁移/换机 clone 后无需改这里
const PROJ = nodePath.resolve(__dirname, "..");
const html = fs.readFileSync(nodePath.join(PROJ, "dashboard.html"), "utf8");
const script = html.match(/<script>([\s\S]*)<\/script>/)[1];

function mkEl() {
  const el = {
    style: {}, dataset: {}, className: "", textContent: "", innerHTML: "", value: "",
    children: [], checked: false, disabled: false,
    appendChild() {}, addEventListener() {}, removeEventListener() {},
    add() {}, remove() {},   // <select>.add(option) 用
    options: [],
    setAttribute() {}, getAttribute() { return null; }, removeAttribute() {},
    querySelectorAll() { return []; }, querySelector() { return null; },
    getContext() { return new Proxy({}, { get: () => () => {} }); },
    getBoundingClientRect() { return { width: 0, height: 0, top: 0, left: 0 }; },
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
  return el;
}
const els = {};
global.document = {
  getElementById(id) { return els[id] || (els[id] = mkEl()); },
  querySelectorAll() { return []; },
  querySelector() { return null; },
  createElement() { return mkEl(); },
  addEventListener() {},
  documentElement: mkEl(),
  body: mkEl(),
  visibilityState: "visible",
  title: "",
};
global.window = global;
global.localStorage = { _d: {}, getItem(k) { return this._d[k] ?? null; }, setItem(k, v) { this._d[k] = String(v); }, removeItem(k) { delete this._d[k]; } };
global.fetch = () => new Promise(() => {});
global.setInterval = () => 0; global.setTimeout = () => 0; global.clearTimeout = () => {}; global.clearInterval = () => {};
global.requestAnimationFrame = () => 0;
global.navigator = { userAgent: "node" };
global.location = { protocol: "http:", host: "127.0.0.1:8765" };
global.confirm = () => true; global.alert = () => {};
global.devicePixelRatio = 1;
// 浏览器内置的 Option 构造函数（buildConsoleOptions 用到），Node 没有需补
global.Option = class Option { constructor(text, value) { this.text = text; this.value = value; } };
global.addEventListener = () => {};

function loadScript() { (0, eval)(script); }
try { loadScript(); } catch (e) { console.log("✗ 脚本执行失败:", e.message); process.exit(1); }

let pass = 0, fail = 0;
function check(name, cond, detail) {
  if (cond) { pass++; console.log("  ✓ " + name); }
  else { fail++; console.log("  ✗ " + name + (detail ? "  → " + detail : "")); }
}

const GB = 1024 ** 3;
function task(over) {
  return Object.assign({
    repo: "mlx-community/Qwen3-Coder-Next-4bit", dest: "/Users/<你>/mlx-models/a/b",
    endpoint: "mirror", x: 8, j: 5, want_run: true, alive: true, pct: 42.5,
    downloaded: 12 * GB, total: 28 * GB, files_done: 3, files_expected: 8,
    rate: 18.3e6, eta_sec: 3900, phase: "download", active: [],
    last_msg: "02:41:33 下载中", verify: null,
  }, over || {});
}
const cfg = { alloc_mode: "size", global_j: 10, max_models: 2 };

console.log("=== A) 已完成任务 → 折叠紧凑卡片 ===");
const done = task({ repo: "Youssofal/Qwen3.8-27B", dest: "/m/done", want_run: false, alive: false,
                    pct: 100, downloaded: 28 * GB, phase: "done", last_msg: "02:41:33 已完成",
                    verify: { ok: 9, total: 9, bad: 0, miss: 0 } });
const hc = taskCardHtml({ cfg }, done, 0);
check("使用 compact 卡片", hc.includes("class='task compact'"), hc.slice(0, 60));
check("含「展开」按钮", hc.includes(">展开<"));
check("进度条用绿色 bar-done", hc.includes("bar bar-done"));
check("不再出现暂停/继续按钮", !hc.includes(">暂停<") && !hc.includes(">继续<"));
check("只保留删除按钮", hc.includes("删除任务"));
check("状态文案只出现一次「已完成」", (hc.match(/已完成/g) || []).length === 1,
      (hc.match(/已完成/g) || []).length + " 次");
check("显示完成时间而非重复状态", hc.includes("完成于 02:41:33"));
check("汇总行含体积与校验", /\d+\.\d+ \/ \d+\.\d+ GB/.test(hc) && hc.includes("校验 9/9"));

console.log("=== B) 下载中任务 → 完整卡片 + 单行统计 ===");
const run = task({});
const hr = taskCardHtml({ cfg }, run, 1);
check("不是 compact", !hr.includes("task compact"));
check("用单行统计 srow 替代四宫格", hr.includes("class='srow'") && !hr.includes("class='cell'"));
check("进度条为默认蓝(无 bar-done)", hr.includes("class='bar'") && !hr.includes("bar-done"));
check("保留暂停/继续/删除", hr.includes(">暂停<") && hr.includes(">继续<") && hr.includes("删除任务"));
check("无「收起」按钮(未完成)", !hr.includes(">收起<"));
check("四项统计齐全", ["已下载", "速度", "预计剩余", "每片线程"].every(k => hr.includes(k)));

console.log("=== C) 进度条分色 ===");
check("已完成 → bar-done", barCls(done) === " bar-done", barCls(done));
check("下载中 → 无额外类", barCls(run) === "", JSON.stringify(barCls(run)));
check("已暂停 → bar-idle", barCls(task({ want_run: false, alive: false })) === " bar-idle");
check("校验中 → bar-verify", barCls(task({ phase: "verifying", verify_prog: [3, 9] })) === " bar-verify");
check("失败 → bar-bad", barCls(task({ phase: "failed", phase_msg: "下载连续失败 4 次" })) === " bar-bad");

console.log("=== D) 列表视图 ===");
const tasks = [done, run, task({ repo: "x/paused", dest: "/m/p", want_run: false, alive: false, pct: 0, downloaded: 0 })];
const list = taskListHtml({ cfg }, tasks);
check("表格 9 列 colgroup", (list.match(/<col[ >]/g) || []).length === 9, (list.match(/<col[ >]/g) || []).length + " 个");
check("已完成行进度条分绿", list.includes("pbar bar-done"));
check("已暂停行进度条变灰", list.includes("pbar bar-idle"));
check("已完成行只有删除按钮", (list.match(/>删除<\/button>/g) || []).length === 3);

console.log("=== E) 失效历史 ===");
renderHist([
  { repo: "a/gone", dest: "/m/gone", at: "2026-10-31 20:00", exists: false },
  { repo: "a/ok", dest: "/m/ok", at: "2026-10-31 21:00", exists: true },
]);
const hist = els["hist"] ? els["hist"].innerHTML : "";
check("失效行加 dead 类", hist.includes("hist-row dead"));
check("失效行显示「目录已删除」标记", hist.includes("目录已删除"));
check("失效行的重新校验被禁用", /verifyHist\(0\)' disabled/.test(hist));
check("失效行的打开目录被禁用", /openLocalDir\(0\)' disabled/.test(hist));
check("失效行仍可重新下载", /resumeHist\(0\)'>重新下载/.test(hist));
check("正常行未被置灰", !/hist-row dead'><div class='hr'><b>a\/ok/.test(hist));
check("标题显示失效条数", (els["histDeadTag"].textContent || "").includes("1 条失效"), els["histDeadTag"].textContent);
check("清理按钮已显示", els["btnCleanHist"].style.display === "");

console.log("=== F) 展开状态记忆(localStorage 持久化) ===");
// let 声明不会泄漏到外层, 直接重载脚本并预置 localStorage 来模拟"上次已展开"
global.localStorage.setItem("hfTaskExpand", JSON.stringify([done.dest]));
loadScript();
const hx = taskCardHtml({ cfg }, done, 0);
check("已展开 → 完整卡片", !hx.includes("task compact") && hx.includes("class='srow'"));
check("已展开 → 出现「收起」", hx.includes(">收起<"));
global.localStorage.removeItem("hfTaskExpand");
loadScript();
const hy = taskCardHtml({ cfg }, done, 0);
check("未展开 → 回到 compact", hy.includes("task compact"));

console.log("=== G) 目录已被删除的任务 ===");
const gone = task({ dest: "/m/del", exists: false, pct: 0, downloaded: 0, total: 0,
                    want_run: false, alive: false, last_msg: "02:35:48 已完成", verify: null });
check("徽标为「目录不存在」", taskBadge(gone, true).txt.includes("目录不存在"), taskBadge(gone, true).txt);
check("不显示为已暂停/已完成", taskBadge(gone, true).cls === "bad");
check("进度条为红色", barCls(gone) === " bar-bad", barCls(gone));
check("只保留删除按钮", (opsHtml(gone, 0) || "").includes("删除任务") && !(opsHtml(gone, 0)).includes("继续"));
const hg = taskCardHtml({ cfg }, gone, 2);
check("卡片给出删除提示", hg.includes("本地目录已被删除"));
check("不再显示『未知 GB』", !hg.includes("未知"));
check("不显示统计行", !hg.includes("class='srow'"));
check("不显示百分比大字", !hg.includes("class='pct'"));
check("不显示分片信息", !hg.includes("分片 "));
check("正常任务仍显示 exists 为真时的统计", taskCardHtml({ cfg }, task({ exists: true }), 0).includes("class='srow'"));

console.log("=== H) 主题变量完整性（防止变量自我引用） ===");
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
const selfRef = css.match(/--([\w-]+):\s*var\(--\1\)/g) || [];
check("没有自引用变量", selfRef.length === 0, selfRef.join(" , "));
check("深色 --bg 是字面色值", /--bg:\s*#/.test(css));
check("存在浅色主题块", /html\[data-theme="light"\]/.test(css));
check("浅色块定义了 --card", /html\[data-theme="light"\][\s\S]{0,400}--card:/.test(css));

// === I) 模型库真实加载路径（防止重构时删掉变量定义，如 v3.41 的 trunc/errBox 事故）===
(async () => {
console.log("=== I) loadLib 真实执行（mock fetch） ===");
const GB2 = 1024 ** 3;
const payload = {
  models: [
    { repo: "mlx-community/Qwen2.5-7B-4bit", bytes: 4.2 * GB2, total: 4.2 * GB2, pct: 100,
      state: "ok", date: "2026-10-30", verify: { ok: 9, total: 9, bad: 0, miss: 0 }, local_only: false },
    { repo: "ocr-models/local-one", bytes: 1.1 * GB2, total: 0, pct: 0,
      state: "missing", date: "2026-10-20", verify: null, local_only: true },
    // 状态必须严格区分: 在任务里才是"下载中", 不在任务里又没下完 = "不完整"
    { repo: "t/in-task", bytes: 1 * GB2, total: 4 * GB2, pct: 25,
      state: "downloading", date: "2026-10-29", verify: null, local_only: false,
      dest: "/m/in-task" },
    { repo: "t/in-queue", bytes: 0, total: 4 * GB2, pct: 0,
      state: "queued", date: "2026-10-29", verify: null, local_only: false, dest: "/m/in-queue" },
    { repo: "t/interrupted", bytes: 1 * GB2, total: 4 * GB2, pct: 25,
      state: "partial", date: "2026-10-29", verify: null, local_only: false, dest: "/m/interrupted" },
    { repo: "t/finished-unverified", bytes: 3 * GB2, total: 3 * GB2, pct: 100,
      state: "partial", date: "2026-10-29", verify: null, local_only: false,
      dest: "/m/finished-unverified" },
    { repo: "t/selfmade", bytes: 2 * GB2, total: 0, pct: 0,
      state: "partial", date: "2026-10-28", verify: null, local_only: true, dest: "/m/selfmade" },
  ],
  total: 7, truncated: false, limit: 200,
  deleting: {}, delete_errors: {}, backfill: { running: 0, total: 0, errors: {} },
};
global.fetch = async () => ({ status: 200, json: async () => payload });
try {
  await loadLib();
} catch (e) {
  check("loadLib 不抛异常", false, e.message);
}
// 注意: loadLib 的 catch 写的是 textContent(不是 innerHTML), 两处都要查
const libHtml = els["lib"].innerHTML + " " + (els["lib"].textContent || "");
check("不出现『读取失败』", !libHtml.includes("读取失败"), libHtml.slice(0, 160));
check("没有抛未定义变量", !/Can't find variable/.test(libHtml), libHtml.slice(0, 160));
els["lib"].textContent = "";   // 清掉, 避免影响后续断言
check("渲染出模型名", libHtml.includes("Qwen2.5-7B-4bit"), libHtml.slice(0, 120));
check("渲染出本地目录模型", libHtml.includes("ocr-models/local-one"));
check("本地目录显示『上游无此仓库』", libHtml.includes("上游无此仓库"));
check("有删除按钮", libHtml.includes("openDelModal"));
// 状态兜底文案: 100% 无校验记录不能显示"下载中"
// 按 lib-item 整行取(按钮在模型名之后, 只取前半段会漏)
const rowOf = (name) => {
  const parts = libHtml.split("<div class='lib-item'>");
  return parts.find(x => x.includes(name)) || "";
};
check("在任务列表里 → 「下载中」", rowOf("t/in-task").includes("下载中"), rowOf("t/in-task").slice(-120));
check("在队列里 → 「等待下载」", rowOf("t/in-queue").includes("等待下载"), rowOf("t/in-queue").slice(-120));
check("不在任务里且未下完 → 「不完整」", rowOf("t/interrupted").includes("不完整"), rowOf("t/interrupted").slice(-120));
check("中断的模型不再误标「下载中」", !rowOf("t/interrupted").includes("下载中"));
check("100% 未校验 → 「待校验」", rowOf("t/finished-unverified").includes("待校验"),
      rowOf("t/finished-unverified").slice(-120));
check("本地自建目录 → 「本地目录」", rowOf("t/selfmade").includes("本地目录"), rowOf("t/selfmade").slice(-120));
check("模型名可点击打开目录", libHtml.includes("openModelDir"), "缺少 onclick");
// 按钮文案要区分: 已下完只差校验 → 重新校验; 没下完 → 继续下载
check("待校验行显示「重新校验」", rowOf("t/finished-unverified").includes("重新校验"),
      rowOf("t/finished-unverified").slice(-150));
check("待校验行用 verifyModel", rowOf("t/finished-unverified").includes("verifyModel"));
check("待校验行不再显示「继续下载」", !rowOf("t/finished-unverified").includes("继续下载"),
      rowOf("t/finished-unverified").slice(-150));
check("未下完行显示「继续下载」", rowOf("t/interrupted").includes("继续下载"),
      rowOf("t/interrupted").slice(-150));
check("未下完行不再显示「重新校验」", !rowOf("t/interrupted").includes("重新校验"));
check("在任务里的行不给这两个按钮", !rowOf("t/in-task").includes("继续下载") &&
      !rowOf("t/in-task").includes("重新校验"), rowOf("t/in-task").slice(-150));
check("模型名带目录 title", libHtml.includes("点击在访达中打开"));

// 截断提示 + 删除失败提示（这两段正是被误删过的）
payload.truncated = true; payload.total = 500;
payload.delete_errors = { "a/b": "权限不足" };
global.fetch = async () => ({ status: 200, json: async () => payload });
await loadLib();
const h2 = els["lib"].innerHTML;
check("超上限时显示截断提示", h2.includes("仅显示最近有活动的"), h2.slice(0, 160));
check("删除失败时显示错误提示", h2.includes("删除未完成") && h2.includes("权限不足"), h2.slice(0, 160));

// 空模型库
global.fetch = async () => ({ status: 200, json: async () => ({ models: [], total: 0 }) });
await loadLib();
check("空模型库显示提示", els["lib"].innerHTML.includes("还没有模型"), els["lib"].innerHTML.slice(0, 80));

console.log("=== J) refresh 主渲染路径真实执行 ===");
const statusPayload = {
  cfg: { default_root: "/Users/<你>/mlx-models", username: "", speed_limit: 0, has_token: false,
         lan: false, lan_urls: [], global_j: 10, max_models: 2, x: 10, alloc_mode: "size" },
  consts: { max_conn: 64, max_models_limit: 8 },
  tasks: [
    { repo: "mlx-community/A", dest: "/m/a", endpoint: "mirror", x: 8, j: 5, want_run: true, alive: true,
      pct: 42.5, downloaded: 12e9, total: 28e9, files_done: 3, files_expected: 8, rate: 18e6,
      eta_sec: 3900, phase: "download", active: [], last_msg: "下载中", verify: null, exists: true },
    { repo: "mlx-community/B", dest: "/m/b", endpoint: "mirror", x: 8, j: 2, want_run: false, alive: false,
      pct: 100, downloaded: 5e9, total: 5e9, files_done: 4, files_expected: 4, rate: 0,
      eta_sec: null, phase: "done", active: [], last_msg: "01:20:33 已完成",
      verify: { ok: 4, total: 4, bad: 0, miss: 0 }, exists: true },
  ],
  queue: [{ repo: "t/q0", dest: "/m/q0", x: 8, endpoint: "mirror", total: 1000, files_expected: 1, added_at: "2026-11-01 00:00:00" }],
  history: [{ repo: "t/old", dest: "/m/old", at: "2026-10-30 10:00:00", exists: true }],
  log: [], model_log: [], rate_hist: [], rate: 0, disk_free: 500e9, dl_running: true,
};
global.fetch = async () => ({ status: 200, json: async () => statusPayload });
try { await refresh(); } catch (e) { check("refresh 不抛异常", false, e.message); }
const logTxt = (document.getElementById("log").textContent || "");
check("refresh 不出现『无法连接』", !logTxt.includes("无法连接服务器"), logTxt.slice(0, 120));
const taskHtml = (document.getElementById("tasks").innerHTML || "");
check("渲染出下载中任务", taskHtml.includes("mlx-community/A"), taskHtml.slice(0, 120));
check("已完成任务为紧凑卡", taskHtml.includes("task compact"));
check("队列已渲染", (document.getElementById("queue").innerHTML||"").includes("t/q0"), (document.getElementById("queue").innerHTML||"").slice(0, 100));
check("历史已渲染", (document.getElementById("hist").innerHTML||"").includes("t/old"));
check("任务计数正确", document.getElementById("taskCount").textContent === "2 个", document.getElementById("taskCount").textContent);

// 列表视图也要能跑通
setTaskView("list");
check("列表视图渲染出表格", (document.getElementById("tasks").innerHTML||"").includes("taskTable"), (document.getElementById("tasks").innerHTML||"").slice(0, 80));
setTaskView("card");

// === L) 体验项 12/14/15 ===
console.log("=== L) 空壳隐藏 / 批量重新校验 / 暂停与等待的区分 ===");
// I 组最后测过"空模型库"，先把正常数据装回来
global.fetch = async () => ({ status: 200, json: async () => payload });
await loadLib();
// 15) 已暂停=中性灰(idle)，等待启动=琥珀(stop)，两者不能同色同文案
const paused = task({ want_run: false, alive: false, pct: 30, verify: null });
const waiting = task({ want_run: true, alive: false, pct: 30, verify: null });
check("已暂停 → cls=idle(灰)", taskBadge(paused, true).cls === "idle", taskBadge(paused, true).cls);
check("已暂停文案含「已暂停」", taskBadge(paused, true).txt.includes("已暂停"));
check("等待启动 → cls=stop(琥珀)", taskBadge(waiting, true).cls === "stop", taskBadge(waiting, true).cls);
check("等待启动文案明确(不是含糊的「等待中」)",
      taskBadge(waiting, true).txt.includes("等待启动"), taskBadge(waiting, true).txt);
check("两者徽标类不同(能一眼分开)",
      taskBadge(paused, true).cls !== taskBadge(waiting, true).cls);

// 12) 隐藏空壳: 勾选后 bytes=0 的条目应被过滤
const beforeHide = document.getElementById("lib").innerHTML;
check("默认能看见 bytes=0 的空壳(t/in-queue)", beforeHide.includes("t/in-queue"));
document.getElementById("hideEmpty").checked = true;
renderLibRows();
const afterHide = document.getElementById("lib").innerHTML;
check("勾选后空壳被隐藏", !afterHide.includes("t/in-queue"));
check("给出了隐藏提示", afterHide.includes("已隐藏") && afterHide.includes("空壳"), afterHide.slice(0, 160));
check("非空的模型仍在", afterHide.includes("t/interrupted"));
document.getElementById("hideEmpty").checked = false;
renderLibRows();

// 14) 批量重新校验: 按钮存在且绑定了 batch_verify
// 注: selLib 是 let 声明, 不会泄漏到 global(与 expandSet 同理), 故不模拟点击,
// 改为验证按钮渲染 + 源码里确实用 batch_verify 且带 items
check("批量栏有「重新校验」按钮", html.includes('id="btnLibVerify"'));
check("绑定了 batch_verify 动作", html.includes('action:"batch_verify"') && html.includes("items:"));
check("批量校验会带上 dest", /repo:m\.repo,\s*dest:m\.dest/.test(html) || html.includes("dest:m.dest"));
check("提示文案说明是串行执行", html.includes("串行"));

// === K) 下载脚本的续传预检（提取真实代码执行，防止改坏）===
console.log("=== K) hf_download.sh 续传预检 ===");
try {
  const cp = require("child_process");
  const fsx = require("fs");
  const SH = nodePath.join(PROJ, "hf_download.sh");
  const sh = fsx.readFileSync(SH, "utf8");
  const marker = "<<'PYCHECK'";
  const mi = sh.indexOf(marker);
  check("脚本含预检代码块", mi > 0);
  const start = sh.indexOf("\n", mi) + 1;
  const end = sh.indexOf("\nPYCHECK", start);
  check("预检代码块完整", end > start, `start=${start} end=${end}`);
  const tmpDir = fsx.mkdtempSync(nodePath.join(os.tmpdir(), "hf-precheck-"));
  const pyFile = nodePath.join(tmpDir, "precheck.py");
  fsx.writeFileSync(pyFile, sh.slice(start, end + 1));

  const root = nodePath.join(tmpDir, "case");
  fsx.rmSync(root, { recursive: true, force: true });
  fsx.mkdirSync(root + "/.hfd", { recursive: true });
  const crypto = require("crypto");
  const sha = (b) => crypto.createHash("sha256").update(b).digest("hex");
  const good = Buffer.concat(Array(100).fill(Buffer.from("good")));
  const bad = Buffer.concat(Array(100).fill(Buffer.from("BAD!")));
  const part = Buffer.concat(Array(10).fill(Buffer.from("part")));
  fsx.writeFileSync(root + "/good.bin", good);
  fsx.writeFileSync(root + "/bad.bin", bad);      // 大小满但内容错
  fsx.writeFileSync(root + "/partial.bin", part); // 未下满
  fsx.writeFileSync(root + "/.hfd/repo_metadata.json", JSON.stringify({ siblings: [
    { rfilename: "good.bin", size: good.length, lfs: { sha256: sha(good), size: good.length } },
    { rfilename: "bad.bin", size: bad.length, lfs: { sha256: sha(good), size: bad.length } },
    { rfilename: "partial.bin", size: 400, lfs: { sha256: sha(Buffer.alloc(400)), size: 400 } },
    { rfilename: "notyet.bin", size: 400, lfs: { sha256: sha(Buffer.alloc(400)), size: 400 } },
  ]}));
  const out = cp.execFileSync("python3", [pyFile, root], { encoding: "utf8" });
  const ex = (n) => fsx.existsSync(root + "/" + n);
  check("完好的文件保留", ex("good.bin"));
  check("大小满但内容错 → 删除重下", !ex("bad.bin"), out);
  check("未下满的文件保留(交给 aria2c 续传)", ex("partial.bin"));
  check("预检输出含统计", /完好 \d+ 个, 删除重下 \d+ 个/.test(out), out.slice(-120));
  fsx.rmSync(tmpDir, { recursive: true, force: true });
} catch (e) {
  check("预检测试可执行", false, e.message);
}

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
})();
