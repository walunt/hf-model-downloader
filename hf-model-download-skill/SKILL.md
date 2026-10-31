---
name: hf-model-download
description: 在本机（尤其 macOS Apple Silicon 与国内网络）高速、可断点续传地下载 HuggingFace 大模型（MLX/GGUF/分片 safetensors，几十至数百 GB）。当用户需要从 huggingface.co 或 hf-mirror 拉取模型、用 hfd.sh + aria2 多线程加速、处理 gated 授权与 HF token、做 sha256 完整性校验、排查大文件下载的进度/速度/稀疏文件问题，或搭建可复用的本地模型下载流程时使用。
---

# HuggingFace 大模型高速下载

用 `hfd.sh`（HF 官方下载器封装）+ `aria2` 多线程，国内走 `hf-mirror.com` 镜像，断点续传，下完自动 sha256 校验。面向超大模型（单仓可达数百 GB），核心是**先核实、再下载、后校验**。

## 1. 软件需求

| 依赖 | 用途 | 安装 |
|---|---|---|
| Homebrew | macOS 包管理（Linux 用 apt/yum） | 缺失先装 |
| aria2 | 多线程/断点续传下载后端，必需 | `brew install aria2` |
| wget | hfd.sh 的备用后端 | `brew install wget` |
| curl | 调 HF API、取脚本 | 系统自带 |
| python3 | 分析元数据、sha256 校验 | 系统自带 |

非交互 shell（脚本/后台进程）常缺 Homebrew 的 PATH，命令前先：
`export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"`（Apple Silicon Homebrew 在 `/opt/homebrew/bin`）。

一键获取下载器（国内用镜像域名）：
```bash
curl -L -o hfd.sh https://hf-mirror.com/hfd/hfd.sh && chmod +x hfd.sh
```
本 skill 的 `scripts/` 已内置 `hfd.sh` 与通用脚本，可直接用。

## 2. 标准流程（9 步）

1. **环境检查**：`command -v aria2c wget curl python3`；确认可联网。
2. **装依赖**：`brew install aria2 wget`。
3. **取 hfd.sh**：见上，`chmod +x`。
4. **核实仓库**（关键，别凭名字猜）：解析用户给的 URL/`org/name`，调 HF API 拿到准确仓库 ID、大小、文件数、是否 gated（见 §3）。仓库名以 HF 实际返回的 `id` 为准。
5. **核磁盘**：预留 **≥2 倍模型体积**（分片下载+校验需要余量）：`df -h`。
6. **配 token**（仅 gated/私有）：见 §4。
7. **执行下载**：见 §5，后台运行，日志重定向到文件。
8. **sha256 校验**：见 §6，全部 LFS 文件与官方哈希逐一对账。
9. **挂载/使用**：如 MLX 用 `omlx serve --model-dir <根目录> --ssd-kv-cache true`；注意权重体积必须小于统一内存才能常驻（见 references/troubleshooting.md）。

## 3. 分析仓库（下载前必做）

```bash
# 公开仓库
curl -s "https://hf-mirror.com/api/models/<org>/<name>?blobs=true"
# gated/私有：加 -H "Authorization: Bearer $HF_TOKEN"
```
从返回 JSON 计算：`siblings[]` 数量=文件数；累加每项 `size`（无则 `lfs.size`）=总字节；`gated` 字段判断授权（false/auto/手动）；`private` 判断私有。
- 支持的输入形态：完整 URL（含 `/tree/main`、`/blob/...` 后缀，取前两段 org/name）、裸 `org/name`。
- 401=gated/私有需 token；404=仓库不存在；网络失败就换 `HF_ENDPOINT`（镜像↔直连）重试。
- 找量化版时以实际列举为准（如 MLX 多在 `mlx-community/*`），不要臆造仓库名。

## 4. Gated 授权与 Token

- 申请 read 权限 token：https://huggingface.co/settings/tokens ；账号需先在模型页同意条款（granted access）。
- 保存：`mkdir -p ~/.huggingface && printf '%s' "$TOKEN" > ~/.huggingface/token && chmod 600 ~/.huggingface/token`。
- **hfd.sh 对 gated 仓库要求 `--hf_username` 与 `--hf_token` 成对传入，缺一个就报 requires authentication**；实际请求只用 token，username 仅非空校验，可传 HF 用户名或任意非空值。
- 镜像站的 gating 同步比直连慢：直连已 200、镜像可能仍 401，等片刻或换直连。
- 永远不要在日志/回复里回显 token。

## 5. 下载命令与参数

```bash
export HF_ENDPOINT=https://hf-mirror.com     # 国内镜像；直连用 https://huggingface.co
./hfd.sh <org>/<name> \
  --local-dir "<目标目录，建议 根目录/org/name>" \
  --tool aria2c -x 10 -j 8 \
  --hf_username <user> --hf_token "$HF_TOKEN"   # 仅 gated 需要这两个
```
- `-x`：**单文件线程数**（每个分片开几条连接）；`-j`：**同时下载分片数**。**两者 hfd.sh 硬性上限都是 10**（写 16 会被拒）。宽带 <300M 用 `-x 8 -j 4`。
- **断点续传是自动的**：aria2 在目标目录写 `.aria2` 控制文件；中断后**原命令重跑**即续传，已完成文件自动跳过。
- 后台运行：`nohup bash hf_download.sh > download.log 2>&1 &`。
- 提速手段：调高 `-j/-x`（到 10）、镜像↔直连对比；每连接常被限速，连接数越多聚合越快。

### 一键脚本
`scripts/hf_download.sh` 已封装：PATH、磁盘预检（`NEED_GB`）、依赖检查、token 解析、gated 成对鉴权、hfd 下载、全校验。全部参数走环境变量：
```bash
REPO=org/name DEST=/path/to/model HFD_X=10 HFD_J=8 \
HF_ENDPOINT=https://hf-mirror.com HF_USERNAME=user bash scripts/hf_download.sh
```

## 6. 完整性校验

下载后 hfd 在 `<目标目录>/.hfd/repo_metadata.json` 留有每个文件的官方 `lfs.sha256`。逐文件哈希比对（`scripts/hf_download.sh` 已内置，覆盖所有 LFS 文件）：
```bash
shasum -a 256 <file>     # 与 metadata 中 lfs.sha256 对比
```
`MISSING`=缺文件（续传即可）、`CORRUPT`=损坏（删该分片重跑续传）。全部一致才算完成。

## 7. 进度与速度的正确口径（超大文件易错）

- aria2 多连接是**稀疏写入**：`ls`/`os.path.getsize` 的逻辑大小会**虚高**；真实落盘字节用 `du -sk` 或 Python `os.stat(f).st_blocks*512`（macOS 块 512B）。
- hfd.sh 在非 TTY（重定向到文件）下进度条恒显示 `0%`，是其显示缺陷，不代表没在下；以磁盘块增长为准。
- 速率应后台定时采样 + 滑动窗口计算，与前端轮询解耦；注意单位 B/s 与 MB/s 差 1e6 倍。
- 详细排障见 `references/troubleshooting.md`：稀疏文件、401/404、镜像同步、并发上限、内存与常驻、MLX 运行依赖等。

## 8. 交付与复用

- 模型与项目代码分离：模型统一放「模型根目录/org/name」（默认 `~/mlx-models`），下载器代码独立成项目。
- 路径不要写死：脚本用 `$(dirname "$0")`、服务用 `os.path.dirname(os.path.abspath(__file__))` 自定位，整体迁移免改。
- 本项目另带可视化控制台（progress_server.py + dashboard.html，端口 8765）：粘贴地址自动分析、页面调并发/切源/暂停续传、分片进度条，需要时可一并使用。
