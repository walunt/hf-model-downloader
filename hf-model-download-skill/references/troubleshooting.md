# 大模型下载排障与细节参考

目录
1. 稀疏写入与真实进度
2. 授权类错误（401 / requires authentication）
3. 网络与镜像
4. aria2 / hfd.sh 参数与行为
5. 断点续传与损坏恢复
6. 速率统计与本地看板
7. MLX / Apple Silicon 运行约束
8. 命令速查

## 1. 稀疏写入与真实进度

- aria2 用多连接在文件不同偏移量写数据（pwrite），文件**逻辑大小**（最高写到的偏移）会迅速接近完整大小，但中间是空洞（hole）。
- `ls -l`、`stat -f%z`、Python `os.path.getsize` 给的是逻辑大小，会**严重高估**已下载量（实测 34GB 逻辑 / 5.9GB 真实）。
- 真实已落盘字节：
  - 命令：`du -sk <dir>`（KB），或 `du -h`
  - Python：`os.stat(path).st_blocks * 512`（macOS/BSD st_blocks 以 512B 计；Linux 通常也是 512B）
- 单文件进度 = 真实块字节 / 官方文件大小（取自 `.hfd/repo_metadata.json` 的 `size`）。
- 全部下完后空洞被填满，块大小≈逻辑大小，二者收敛。

## 2. 授权类错误

| 现象 | 原因 | 处理 |
|---|---|---|
| 所有文件 401 | gated/私有未带 token | 写 ~/.huggingface/token，hfd 传 --hf_token |
| `requires authentication` | hfd.sh 检查到 gated 但 username/token 没成对 | `--hf_username <非空> --hf_token <token>` 一起传 |
| 直连 200、镜像 401 | hf-mirror 的 gating 同步滞后 | 等几分钟，或临时 HF_ENDPOINT 改直连 |
| token 仍 401 | 没在模型页同意条款 / token 无 read 权限 / token 错 | HF 页面点 Agree，重建 read token |
| 私有仓库 | 必须是协作者且 token 有效 | 核对账号权限 |

hfd.sh 鉴权只真正使用 token；`--hf_username` 仅做非空校验。whoami 接口在镜像上可能不返回 name，直接用已知用户名或任意非空串即可。

## 3. 网络与镜像

- 国内默认 `export HF_ENDPOINT=https://hf-mirror.com`；API 也走 `https://hf-mirror.com/api/models/...`。
- 直连 `https://huggingface.co` 偶发 `HTTP 000`/超时，属网络抖动，重试或回镜像。
- 单连接常被限速（约几十~上百 KB/s），靠 `-x×-j` 的总连接数聚合带宽；镜像按 IP 限速时加连接收益变小，可切直连对比。
- 分析仓库（API）和实际下载可分别选源；切源不影响断点续传。

## 4. aria2 / hfd.sh 参数与行为

- `-x`（单文件连接数）、`-j`（并行文件数）**范围都被 hfd.sh 限制为 1–10**，超出直接报错。
- 默认 `--tool aria2c`；`--file-allocation=none`（稀疏，见 §1）；分片大小 `-k 1M`。
- hfd 先生成 `.hfd/aria2c_urls.txt` 与 `.hfd/repo_metadata.json`，再调 aria2。
- 非 TTY 时 hfd 自带的总进度条恒为 `[0%] 0/N files | 0B/s`，是显示缺陷；真实进度看磁盘块。
- 进程树：下载脚本 → hfd.sh → aria2c；停止时三者都要清：`pkill -f hf_download.sh; pkill -f hfd.sh; pkill -f aria2c`（先 TERM 等 2~3s 再 KILL 兜底）。注意只杀下载相关，别误杀进度服务器自身。

## 5. 断点续传与损坏恢复

- 续传依据是目标目录里的 `<file>.aria2` 控制文件与已写数据；**同命令重跑**即可，完成的文件跳过。
- 改并发、换镜像后重启下载不丢进度。
- 某分片校验 CORRUPT：删除该 `<file>` 与对应 `.aria2`，重跑让其重下。
- 磁盘要预留 ≥2 倍模型体积（下载块 + 完成后填充 + 校验读取）。

## 6. 速率统计与本地看板（经验）

- 不要在「每次前端请求」时按距上次请求的增量算速率——轮询间隔抖动会导致数字乱跳甚至假尖峰。
- 正确做法：**后台守护线程固定 5s 采样真实字节，用 20–30s 滑动窗口算速率**，与请求时序解耦；ETA 用同一速率。
- 单位：后端统一 B/s；前端显示 MB/s 务必 `/1e6`（曾因漏除出现「1,438,518 MB/s」假象，实际 1.44MB/s）。
- 本地网页看板：HTTP 服务读磁盘出 JSON；浏览器可能把 `localhost` 自动升级成 https 导致 ERR_SSL_PROTOCOL_ERROR——显式输 `http://`，或让服务做 http/https 同端口（首字节 0x16=TLS 则包 SSL，自签名证书）。

## 7. MLX / Apple Silicon 运行约束

- 统一内存即“显存”：**权重体积必须小于物理统一内存才能完整加载/常驻（pin to memory）**。例：146GB 权重在 128GB 机器上无法 pin，会失败或严重换页；需要更大内存机型或更小量化（如 2bit/4bit）。
- 选量化版先看体积：mxfp8/mxfp4 接近原精度体积，2bit/4bit 显著更小；以 HF API 实际 size 为准。
- 部分新版 MLX 模型需要新版 `mlx`/`mlx-vlm`（普通 pip 可能只装到旧 stub）与 `jinja2`，按模型卡要求装预发布版。
- 名字带 Vision 不等于真含视觉塔：转换可能丢弃 ViT（模型卡会注明 “Text only”）；以模型卡/张量清单为准。
- 挂载示例：`omlx serve --model-dir ~/mlx-models --ssd-kv-cache true`，CLI 一般在 `~/.omlx/bin/omlx`。

## 8. 做成 launchd 开机自启的关键坑

- `~/Desktop`、`~/Documents`、`~/Downloads` 受 macOS TCC 保护：**终端里手动跑正常的脚本，注册成 LaunchAgent 后会 `Operation not permitted` 直接退出（常见 last exit code 78 EX_CONFIG），且 stderr 可能为空**。
- 解法二选一：① 运行时放到非保护目录（推荐 `~/Library/Application Support/<app>/`），桌面只留源码，安装脚本负责 cp 同步；② 在「系统设置→隐私与安全性→完全磁盘访问」里给运行时二进制授权（系统 Python 升级后可能失效，较脆）。
- LaunchAgent 用 `/usr/bin/python3`（Command Line Tools，纯标准库即可），不要依赖 AI 工具自带的临时运行时路径。
- `KeepAlive=true` + `ThrottleInterval=5` 实现崩溃秒级自愈；改完用 `launchctl bootstrap/kickstart gui/$(id -u) <plist>`，排查用 `launchctl print gui/$(id -u)/<label>` 看 `state / pid / last exit code`。
- 守护进程与下载进程解耦：只重启/替换服务进程不会动到已在跑的 aria2，续传安全。

## 9. 命令速查

```bash
# 依赖
brew install aria2 wget
# 取下载器
curl -L -o hfd.sh https://hf-mirror.com/hfd/hfd.sh && chmod +x hfd.sh
# 分析（gated 加 -H "Authorization: Bearer $TOKEN"）
curl -s "https://hf-mirror.com/api/models/<org>/<name>?blobs=true" | python3 -m json.tool
# token
mkdir -p ~/.huggingface && printf '%s' "$TOKEN" > ~/.huggingface/token && chmod 600 ~/.huggingface/token
# 真实进度 / 磁盘
du -sh "<目标目录>"; df -h ~
# 下载（gated 末尾加 --hf_username u --hf_token "$TOKEN"）
HF_ENDPOINT=https://hf-mirror.com ./hfd.sh <org>/<name> --local-dir "<dir>" --tool aria2c -x 10 -j 8
# 单文件校验
shasum -a 256 <file>
# 停止下载进程树
pkill -f hfd.sh; pkill -f aria2c
```
