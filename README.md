# HuggingFace 模型下载器

基于 `hfd.sh + aria2` 的本地大模型高速下载工具，带可视化网页控制台，支持 hf-mirror 国内镜像 / 官方直连一键切换、并发调节、**下载限速**、断点续传、**失败自动重试（最多 4 次，指数退避）**、自动 sha256 校验、完成通知、**队列全部完成通知**、历史任务续传、任务队列自动接力、30 分钟速度曲线、**下载中磁盘实时预检（不足自动暂停）**、**模型库校验状态一览**、**主日志/模型日志切换**、开机自启与崩溃自愈。

> 当前版本 **v2.26 · 本系统由核桃制作**（整数大版本已确认；无特殊强调时仅递增小版本号）

## 启动方式

- **方式一（推荐，开机自启 + 崩溃自动重启）**：双击 `安装开机守护.command` 一次即可。它会把运行时部署到 `~/Library/Application Support/hf-model-downloader/` 并注册 macOS LaunchAgent，之后登录自动启动、进程崩溃 5 秒内自动拉起；配合「重启自动续传」实现断网/重启后无人值守续传。卸载双击 `卸载开机守护.command`。
  - 为什么要部署到 Application Support：`~/Desktop` 受 macOS 隐私（TCC）保护，launchd 后台进程无权读取会报 `Operation not permitted`；桌面目录保留为**源码**，改完代码重新双击安装脚本即同步。
- **方式二（临时手动）**：双击 `启动下载器.command`，自动启动并打开浏览器（若守护已在运行则直接打开页面）
- **方式三（终端）**：
  ```bash
  cd /path/to/hf-model-downloader
  python3 progress_server.py
  ```
  然后浏览器打开 http://localhost:8765 （https 同端口也可，自签名证书点继续即可）

## 使用流程

1. 在「① 新建下载」粘贴 HuggingFace 地址或 `组织/模型名`，gated/私有仓库在下方密码框填入 HF Token（read 权限，自动安全保存，不回显）
2. 点「分析」查看总大小、文件数、保存路径，可调整并发（分析成功后输入框自动清空，方便粘贴下一个链接；失败则保留便于修改）
3. 点「开始下载」；「③ 下载控制台」可随时切换镜像/直连、调并发、暂停/继续
4. 默认保存根目录在「④ 默认设置」里改，模型存到 `根目录/组织/模型名`，默认 `~/mlx-models`
5. 想一次排多个模型：分析后点「加入队列」（成功有顶部浮层提示，队列面板在「新建下载」正下方），当前任务完成并校验通过后会自动接力下一个（「下载队列」可移除/清空）
6. 当前任务区有最近 30 分钟速度曲线、当前/均值/峰值速度与目标盘剩余空间；页面切到后台时自动降频省资源

## 文件说明

| 文件 | 作用 |
|---|---|
| `progress_server.py` | 本地后端服务（端口 8765，http/https 双协议，路径自动跟随脚本位置） |
| `dashboard.html` | 可视化界面 |
| `hfd.sh` | HuggingFace 高速下载器（第三方，aria2/wget 后端） |
| `hf_download.sh` | 通用下载+校验脚本，由后端按任务调用 |
| `download_dsv4_vision.sh` | 首个 DeepSeek 任务的专用脚本（保留备查，通用任务用 hf_download.sh） |
| `安装/卸载开机守护.command` | 注册/移除 LaunchAgent（部署运行时到 Application Support） |
| `localhost.crt/key` | 本地 https 自签名证书 |
| `dl_config.json` | 持久化配置（默认路径、当前任务、历史） |
| `download.log` | 下载主日志 |

> 守护模式的实际运行目录是 `~/Library/Application Support/hf-model-downloader/`（含其独立的 dl_config.json / server.log / download.log）；模型数据仍在 `~/mlx-models`。

## 备注

- aria2 的 `-x`（单文件线程）/`-j`（同时下载分片数）上限均为 10
- 中断后重跑/点继续即自动断点续传；全部下完会自动与 HF 官方 sha256 比对，结果写 `<模型目录>/.hfd/verify_result.json` 并在页面显示，同时发 macOS 通知
- 「最近任务」可一键重新打开/断点续传；服务重启后，上次处于运行态且未完成的任务会自动续传（主动暂停的不会）
- 控制台按钮按运行状态自动启用/禁用；日志支持「只看问题」过滤与「清空显示」；控制口仅接受本机来源请求（校验 Host/Origin，防其他网页 CSRF）
- 依赖：`brew install aria2 wget`；token 存于 `~/.huggingface/token`（权限 600）
